import logging
from dataclasses import dataclass
from datetime import datetime

from app.models import BoardPayload, ChannelConfig, EventTextConfig
from app.schemas import ChannelState
from app.utils import extract_bit_from_bytes, normalize_channel_index, unpack_bits

FAULT_STATUSES = {"open_circuit", "short_circuit", "fault_break", "fault_short"}
NORMAL_STATUSES = {"normal", "active", "normal_on", "normal_off"}

QL6C_MODULE = "QL6C"
QL6C_BOARD = "B31"
QL6C_UNIT = "U15"
DEFAULT_QL6C_TOPIC = "puma_board"
BIT_GROUP_WIDTH = 8
CHANNEL_MAP = {
    0: "6",
    1: "7",
    2: "8",
    3: "9",
    4: "A",
    5: "B",
    6: "C",
    7: "D",
}

LEGACY_BIT_SOURCE_MAP = {
    "input": "in_",
    "output": "out",
    "diagnostic": "inversed",
}

FAULT_ACTION_TEXT = "Проверить фишку гидрораспределителя и выполнить визуальный осмотр электропроводки цепи"
UNKNOWN_CAUSE_TEXT = "Комбинация сигналов не описана в таблице истинности"
UNKNOWN_ACTION_TEXT = "Проверить корректность входных данных и схему подключения"

QL6C_LOGICAL_CHANNELS = tuple(CHANNEL_MAP[index] for index in range(BIT_GROUP_WIDTH))
QL6C_CHANNEL_META: dict[str, tuple[str, str]] = {
    "6": ("1s212b", "крюк слева выдвинуть"),
    "7": ("1s212a", "крюк слева задвинуть"),
    "8": ("1s213b", "крюк справа выдвинуть"),
    "9": ("1s213a", "крюк справа задвинуть"),
    "A": ("1s247b", "левый роликовый захват снаружи открыть"),
    "B": ("1s247a", "левый роликовый захват снаружи закрыть"),
    "C": ("1s248b", "левый роликовый захват внутри открыть"),
    "D": ("1s248a", "левый роликовый захват внутри закрыть"),
}

logger = logging.getLogger("unimat.decoder")


@dataclass(frozen=True, slots=True)
class DecodedState:
    status: str
    status_code: str
    label: str
    state_label: str
    description: str
    fault: bool
    severity: str
    fault_type: str | None
    yellow_led: bool
    red_led: bool

    @property
    def is_fault(self) -> bool:
        return self.fault

    @property
    def status_label(self) -> str:
        return self.label


@dataclass(frozen=True, slots=True)
class ChannelDescriptor:
    board: str
    unit: str | None
    module: str
    logical_channel: str
    raw_channel: str
    ui_channel_index: int
    signal_id: str
    title: str
    purpose: str
    source_topic: str
    in_bit: int
    out_bit: int
    diagnostic_bit: int
    photo_index: int | None = None

    @property
    def channel_key(self) -> str:
        return f"{self.module}{self.ui_channel_index}"


@dataclass(frozen=True, slots=True)
class EventTextResult:
    message: str
    reason: str | None
    cause: str | None
    action: str | None


def decode_bits(value: int, size: int = 16) -> list[int]:
    if size <= 0:
        return []
    decoded = unpack_bits(value, width=size)
    return [decoded[index] for index in range(1, size + 1)]


def decode_triplet(input_bit: int, output_bit: int, diagnostic_bit: int) -> DecodedState:
    key = (int(input_bit), int(output_bit), int(diagnostic_bit))
    if key == (0, 0, 1):
        return DecodedState(
            status="normal_off",
            status_code="normal",
            label="Норма",
            state_label="Ключ выключен",
            description="Ключ выключен",
            fault=False,
            severity="info",
            fault_type=None,
            yellow_led=False,
            red_led=False,
        )
    if key == (1, 1, 1):
        return DecodedState(
            status="normal_on",
            status_code="normal",
            label="Норма",
            state_label="Ключ включен",
            description="Ключ включен",
            fault=False,
            severity="info",
            fault_type=None,
            yellow_led=True,
            red_led=False,
        )
    if key in {(0, 1, 0), (1, 1, 0)}:
        return DecodedState(
            status="fault_break",
            status_code="break",
            label="Обрыв",
            state_label="Обрыв",
            description="Обрыв",
            fault=True,
            severity="error",
            fault_type="break",
            yellow_led=True,
            red_led=True,
        )
    if key == (1, 0, 0):
        return DecodedState(
            status="fault_short",
            status_code="short",
            label="КЗ",
            state_label="Короткое замыкание",
            description="Короткое замыкание",
            fault=True,
            severity="error",
            fault_type="short",
            yellow_led=False,
            red_led=True,
        )
    return DecodedState(
        status="unknown",
        status_code="unknown",
        label="Неизвестно",
        state_label="Неизвестная комбинация сигналов",
        description="Неизвестная комбинация сигналов",
        fault=False,
        severity="warning",
        fault_type=None,
        yellow_led=False,
        red_led=False,
    )


def decode_channel(input_bit: int, output_bit: int, diagnostic_bit: int) -> DecodedState:
    # Backward-compatible alias used by tests and external imports.
    return decode_triplet(input_bit, output_bit, diagnostic_bit)


class DecoderService:
    def __init__(
        self,
        signal_map: list[ChannelConfig],
        event_texts: dict[str, EventTextConfig],
    ) -> None:
        self.signal_map = signal_map
        self.event_texts = event_texts
        self._out_of_range_warnings: set[tuple[str, int, str]] = set()
        self._unknown_triplet_warnings: set[tuple[str, str]] = set()

        self.ql6c_topic, self.ql6c_enabled = self._resolve_ql6c_topic(signal_map)
        self.ql6c_descriptors = (
            self._build_ql6c_descriptors(signal_map, source_topic=self.ql6c_topic)
            if self.ql6c_enabled
            else []
        )

        replaced_pairs = {(self.ql6c_topic, QL6C_MODULE)} if self.ql6c_enabled else set()
        self.legacy_signal_map = [
            channel_cfg
            for channel_cfg in signal_map
            if (channel_cfg.sourceTopic, channel_cfg.module) not in replaced_pairs
        ]

        self.max_channel_index = max(
            (
                channel_cfg.channelIndex
                for channel_cfg in self.legacy_signal_map
            ),
            default=0,
        )
        self.bit_size = max(self.max_channel_index + 1, BIT_GROUP_WIDTH)
        self.channels_by_topic_and_index: dict[str, dict[int, ChannelConfig]] = {}
        for channel_cfg in self.legacy_signal_map:
            topic_map = self.channels_by_topic_and_index.setdefault(channel_cfg.sourceTopic, {})
            topic_map.setdefault(channel_cfg.channelIndex, channel_cfg)

    def default_inactive_channels(self) -> list[ChannelState]:
        channels: list[ChannelState] = []

        for descriptor in self.ql6c_descriptors:
            channels.append(
                ChannelState(
                    channelKey=descriptor.channel_key,
                    channelIndex=descriptor.ui_channel_index,
                    signalId=descriptor.signal_id,
                    title=descriptor.title,
                    purpose=descriptor.purpose,
                    photoIndex=descriptor.photo_index,
                    board=descriptor.board,
                    unit=descriptor.unit,
                    module=descriptor.module,
                    logicalChannel=descriptor.logical_channel,
                    rawChannel=descriptor.raw_channel,
                    topic=descriptor.source_topic,
                    input=0,
                    output=0,
                    diagnostic=0,
                    status="inactive",
                    statusCode=None,
                    statusLabel="Неактивно",
                    stateLabel="Неактивно",
                    label="Неактивно",
                    faultType=None,
                    yellow_led=False,
                    red_led=False,
                    message="Нет данных",
                    reason=None,
                    cause=None,
                    action=None,
                    severity="info",
                    fault=False,
                    isFault=False,
                    raw={"in": 0, "out": 0, "dg": 0},
                    rawBits={"in": 0, "out": 0, "dg": 0},
                    updatedAt=None,
                )
            )

        for channel_cfg in self.legacy_signal_map:
            purpose = self._resolve_purpose(channel_cfg.signalId, channel_cfg.purpose)
            _, unit = self._split_board_and_unit(channel_cfg.board)
            channels.append(
                ChannelState(
                    channelKey=channel_cfg.channelKey,
                    channelIndex=channel_cfg.channelIndex,
                    signalId=channel_cfg.signalId,
                    title=purpose,
                    purpose=purpose,
                    photoIndex=channel_cfg.photoIndex,
                    board=channel_cfg.board,
                    unit=unit,
                    module=channel_cfg.module,
                    logicalChannel=str(channel_cfg.channelIndex),
                    rawChannel=str(channel_cfg.channelIndex),
                    topic=channel_cfg.sourceTopic,
                    input=0,
                    output=0,
                    diagnostic=0,
                    status="inactive",
                    statusCode=None,
                    statusLabel="Неактивно",
                    stateLabel="Неактивно",
                    label="Неактивно",
                    faultType=None,
                    yellow_led=False,
                    red_led=False,
                    message="Нет данных",
                    reason=None,
                    cause=None,
                    action=None,
                    severity="info",
                    fault=False,
                    isFault=False,
                    raw={"in": 0, "out": 0, "dg": 0},
                    rawBits={"in": 0, "out": 0, "dg": 0},
                    updatedAt=None,
                )
            )
        return channels

    def decode_board_payload(
        self,
        payload: BoardPayload,
        topic: str,
        updated_at: datetime | None = None,
    ) -> list[ChannelState]:
        logger.debug("Decoding board payload topic=%s raw=%s", topic, payload.to_raw_dict())

        decoded_channels: list[ChannelState] = []
        decoded_channels.extend(
            self._decode_ql6c_payload(payload=payload, topic=topic, updated_at=updated_at)
        )
        decoded_channels.extend(
            self._decode_legacy_payload(payload=payload, topic=topic, updated_at=updated_at)
        )

        if not decoded_channels:
            logger.warning(
                "No mapped channels for topic=%s. payload=%s",
                topic,
                payload.to_raw_dict(),
            )
            return []

        logger.info(
            "Decoded channels topic=%s total=%s faults=%s unknown=%s",
            topic,
            len(decoded_channels),
            sum(1 for item in decoded_channels if item.isFault),
            sum(1 for item in decoded_channels if item.status == "unknown"),
        )
        return decoded_channels

    def build_debug_report(
        self,
        payload: BoardPayload,
        topic: str,
        timestamp: datetime | None,
        source: str,
    ) -> dict:
        size = self._resolve_bit_size(payload)
        raw_in = int(payload.in_)
        raw_inversed = int(payload.inversed)
        raw_out = int(payload.out)
        raw_other = int(payload.other) if payload.other is not None else 0

        decoded_channels = self.decode_board_payload(payload=payload, topic=topic, updated_at=timestamp)
        summary = {
            "totalChannels": len(decoded_channels),
            "faultCount": sum(1 for item in decoded_channels if item.isFault),
            "unknownCount": sum(1 for item in decoded_channels if item.status == "unknown"),
            "normalCount": sum(1 for item in decoded_channels if item.status in NORMAL_STATUSES),
        }

        channel_bits = [
            {
                "channelKey": item.channelKey,
                "channelIndex": item.channelIndex,
                "logicalChannel": item.logicalChannel,
                "inputBit": item.input,
                "outputBit": item.output,
                "diagnosticBit": item.diagnostic,
                "status": item.status,
                "statusCode": item.statusCode,
                "yellow_led": item.yellow_led,
                "red_led": item.red_led,
            }
            for item in decoded_channels
        ]

        return {
            "timestamp": timestamp,
            "source": source,
            "sourceTopic": topic,
            "raw": {
                "in": {"decimal": raw_in, "bin": self._format_binary(raw_in, size)},
                "inversed": {"decimal": raw_inversed, "bin": self._format_binary(raw_inversed, size)},
                "out": {"decimal": raw_out, "bin": self._format_binary(raw_out, size)},
                "other": {"decimal": raw_other, "bin": self._format_binary(raw_other, size)},
            },
            "bits": {
                "in": unpack_bits(raw_in, width=BIT_GROUP_WIDTH),
                "inversed": unpack_bits(raw_inversed, width=BIT_GROUP_WIDTH),
                "out": unpack_bits(raw_out, width=BIT_GROUP_WIDTH),
                "other": unpack_bits(raw_other, width=BIT_GROUP_WIDTH),
            },
            "summary": summary,
            "interpretations": {
                "decodedChannels": channel_bits,
            },
        }

    def _decode_ql6c_payload(
        self,
        *,
        payload: BoardPayload,
        topic: str,
        updated_at: datetime | None,
    ) -> list[ChannelState]:
        if topic != self.ql6c_topic:
            return []
        if not self.ql6c_enabled:
            return []

        in_bits = unpack_bits(int(payload.in_), width=BIT_GROUP_WIDTH)
        inversed_bits = unpack_bits(int(payload.inversed), width=BIT_GROUP_WIDTH)
        out_bits = unpack_bits(int(payload.out), width=BIT_GROUP_WIDTH)
        other_bits = unpack_bits(int(payload.other) if payload.other is not None else 0, width=BIT_GROUP_WIDTH)

        logger.debug(
            "Unpacked QL6C bits topic=%s in=%s inversed(dg)=%s out=%s other(raw)=%s",
            topic,
            in_bits,
            inversed_bits,
            out_bits,
            other_bits,
        )

        channels: list[ChannelState] = []
        for descriptor in self.ql6c_descriptors:
            input_bit = in_bits.get(descriptor.in_bit, 0)
            output_bit = out_bits.get(descriptor.out_bit, 0)
            diagnostic_bit = inversed_bits.get(descriptor.diagnostic_bit, 0)

            decoded_state = decode_triplet(input_bit, output_bit, diagnostic_bit)
            text = self._format_event_text(
                signal_id=descriptor.signal_id,
                fallback_purpose=descriptor.purpose,
                decoded_state=decoded_state,
            )

            if decoded_state.status == "unknown":
                unknown_key = (topic, descriptor.channel_key)
                if unknown_key not in self._unknown_triplet_warnings:
                    self._unknown_triplet_warnings.add(unknown_key)
                    logger.warning(
                        "Unknown triplet topic=%s module=%s channel=%s in=%s out=%s dg=%s",
                        topic,
                        descriptor.module,
                        descriptor.logical_channel,
                        input_bit,
                        output_bit,
                        diagnostic_bit,
                    )

            logger.debug(
                "QL6C channel topic=%s key=%s logical=%s map(in:%s->%s out:%s->%s dg(inversed):%s->%s) in=%s out=%s dg=%s status=%s",
                topic,
                descriptor.channel_key,
                descriptor.logical_channel,
                descriptor.in_bit,
                input_bit,
                descriptor.out_bit,
                output_bit,
                descriptor.diagnostic_bit,
                diagnostic_bit,
                input_bit,
                output_bit,
                diagnostic_bit,
                decoded_state.status,
            )

            channels.append(
                ChannelState(
                    channelKey=descriptor.channel_key,
                    channelIndex=descriptor.ui_channel_index,
                    signalId=descriptor.signal_id,
                    title=descriptor.title,
                    purpose=descriptor.purpose,
                    photoIndex=descriptor.photo_index,
                    board=descriptor.board,
                    unit=descriptor.unit,
                    module=descriptor.module,
                    logicalChannel=descriptor.logical_channel,
                    rawChannel=descriptor.raw_channel,
                    topic=topic,
                    input=input_bit,
                    output=output_bit,
                    diagnostic=diagnostic_bit,
                    status=decoded_state.status,  # type: ignore[arg-type]
                    statusCode=decoded_state.status_code,
                    statusLabel=decoded_state.status_label,
                    stateLabel=decoded_state.state_label,
                    label=decoded_state.label,
                    faultType=decoded_state.fault_type,  # type: ignore[arg-type]
                    yellow_led=decoded_state.yellow_led,
                    red_led=decoded_state.red_led,
                    message=text.message,
                    reason=text.reason,
                    cause=text.cause,
                    action=text.action,
                    severity=decoded_state.severity,  # type: ignore[arg-type]
                    fault=decoded_state.fault,
                    isFault=decoded_state.fault,
                    raw={"in": input_bit, "out": output_bit, "dg": diagnostic_bit},
                    rawBits={"in": input_bit, "out": output_bit, "dg": diagnostic_bit},
                    updatedAt=updated_at,
                )
            )
        return channels

    def _decode_legacy_payload(
        self,
        *,
        payload: BoardPayload,
        topic: str,
        updated_at: datetime | None,
    ) -> list[ChannelState]:
        input_values = [int(getattr(payload, LEGACY_BIT_SOURCE_MAP["input"]))]
        output_values = [int(getattr(payload, LEGACY_BIT_SOURCE_MAP["output"]))]
        diagnostic_values = [int(getattr(payload, LEGACY_BIT_SOURCE_MAP["diagnostic"]))]

        decoded_channels: list[ChannelState] = []
        for channel_cfg in self.legacy_signal_map:
            if channel_cfg.sourceTopic != topic:
                continue

            index = channel_cfg.channelIndex
            input_bit = self._extract_channel_bit(
                values=input_values,
                channel_index=index,
                topic=topic,
                field_name="in",
            )
            output_bit = self._extract_channel_bit(
                values=output_values,
                channel_index=index,
                topic=topic,
                field_name="out",
            )
            diagnostic_bit = self._extract_channel_bit(
                values=diagnostic_values,
                channel_index=index,
                topic=topic,
                field_name="inversed",
            )

            decoded_state = decode_triplet(input_bit, output_bit, diagnostic_bit)
            legacy_state = self._to_legacy_state(decoded_state)
            cause, action = self._resolve_cause_action(legacy_state.status, channel_cfg.signalId)
            purpose = self._resolve_purpose(channel_cfg.signalId, channel_cfg.purpose)
            _, unit = self._split_board_and_unit(channel_cfg.board)

            decoded_channels.append(
                ChannelState(
                    channelKey=channel_cfg.channelKey,
                    topic=topic,
                    board=channel_cfg.board,
                    unit=unit,
                    module=channel_cfg.module,
                    channelIndex=channel_cfg.channelIndex,
                    logicalChannel=str(channel_cfg.channelIndex),
                    rawChannel=str(channel_cfg.channelIndex),
                    signalId=channel_cfg.signalId,
                    title=purpose,
                    purpose=purpose,
                    photoIndex=channel_cfg.photoIndex,
                    input=input_bit,
                    output=output_bit,
                    diagnostic=diagnostic_bit,
                    status=legacy_state.status,  # type: ignore[arg-type]
                    statusCode=decoded_state.status_code,
                    statusLabel=legacy_state.status_label,
                    stateLabel=legacy_state.state_label,
                    label=legacy_state.label,
                    faultType=decoded_state.fault_type,  # type: ignore[arg-type]
                    yellow_led=decoded_state.yellow_led,
                    red_led=decoded_state.red_led,
                    message=legacy_state.description,
                    reason=cause,
                    cause=cause,
                    action=action,
                    severity=legacy_state.severity,  # type: ignore[arg-type]
                    fault=legacy_state.fault,
                    isFault=legacy_state.fault,
                    raw={"in": input_bit, "out": output_bit, "dg": diagnostic_bit},
                    rawBits={"in": input_bit, "out": output_bit, "dg": diagnostic_bit},
                    updatedAt=updated_at,
                )
            )

        return decoded_channels

    def _extract_channel_bit(
        self,
        *,
        values: list[int],
        channel_index: int | str,
        topic: str,
        field_name: str,
    ) -> int:
        normalized_index = normalize_channel_index(channel_index)
        byte_index = normalized_index // 8
        if byte_index >= len(values):
            warn_key = (topic, normalized_index, field_name)
            if warn_key not in self._out_of_range_warnings:
                self._out_of_range_warnings.add(warn_key)
                logger.warning(
                    "Channel bit out of payload range topic=%s field=%s channelIndex=%s -> using 0",
                    topic,
                    field_name,
                    normalized_index,
                )
            return 0
        return extract_bit_from_bytes(values, normalized_index)

    def _format_event_text(
        self,
        *,
        signal_id: str,
        fallback_purpose: str,
        decoded_state: DecodedState,
    ) -> EventTextResult:
        event_text = self.event_texts.get(signal_id)
        purpose = self._resolve_purpose(signal_id, fallback_purpose)
        event_title = event_text.eventTitle if event_text else None
        action = event_text.action if event_text and event_text.action else None

        if decoded_state.status == "normal_on":
            return EventTextResult(
                message=purpose,
                reason=None,
                cause=None,
                action=None,
            )

        if decoded_state.status == "normal_off":
            return EventTextResult(
                message=purpose,
                reason=None,
                cause=None,
                action=None,
            )

        if decoded_state.status == "fault_break":
            reason = "Обрыв цепи"
            cause = event_text.breakageCause if event_text else reason
            return EventTextResult(
                message=decoded_state.status_label,
                reason=reason,
                cause=cause,
                action=action or FAULT_ACTION_TEXT,
            )

        if decoded_state.status == "fault_short":
            reason = "Короткое замыкание"
            cause = event_text.shortCause if event_text else reason
            return EventTextResult(
                message=decoded_state.status_label,
                reason=reason,
                cause=cause,
                action=action or FAULT_ACTION_TEXT,
            )

        return EventTextResult(
            message=event_title or decoded_state.state_label,
            reason=decoded_state.description,
            cause=UNKNOWN_CAUSE_TEXT,
            action=UNKNOWN_ACTION_TEXT,
        )

    def _resolve_cause_action(self, status: str, signal_id: str) -> tuple[str | None, str | None]:
        if status == "open_circuit":
            return f"Обрыв цепи гидрораспределителя {signal_id}", FAULT_ACTION_TEXT
        if status == "short_circuit":
            return f"Короткое замыкание в цепи гидрораспределителя {signal_id}", FAULT_ACTION_TEXT
        if status == "unknown":
            return UNKNOWN_CAUSE_TEXT, UNKNOWN_ACTION_TEXT
        return None, None

    def _resolve_purpose(self, signal_id: str, fallback_purpose: str) -> str:
        event_text = self.event_texts.get(signal_id)
        if event_text and event_text.purpose:
            return event_text.purpose
        return fallback_purpose

    @staticmethod
    def _split_board_and_unit(board_value: str) -> tuple[str, str | None]:
        if "/" not in board_value:
            return board_value, None
        parts = [chunk.strip() for chunk in board_value.split("/") if chunk.strip()]
        if not parts:
            return board_value, None
        if len(parts) == 1:
            return parts[0], None
        return parts[0], parts[-1]

    def _resolve_bit_size(self, payload: BoardPayload) -> int:
        max_payload_value = max(
            int(payload.in_),
            int(payload.inversed),
            int(payload.out),
            int(payload.other) if payload.other is not None else 0,
            0,
        )
        dynamic_size = max_payload_value.bit_length()
        return max(self.bit_size, dynamic_size, 1)

    @staticmethod
    def _format_binary(value: int, size: int) -> str:
        if value < 0:
            return f"-0b{format(abs(value), f'0{size}b')}"
        return f"0b{format(value, f'0{size}b')}"

    @staticmethod
    def _resolve_ql6c_topic(signal_map: list[ChannelConfig]) -> tuple[str, bool]:
        for channel_cfg in signal_map:
            board = channel_cfg.board.upper()
            if channel_cfg.module != QL6C_MODULE:
                continue
            if "B31" in board and "U15" in board:
                return channel_cfg.sourceTopic, True
        return DEFAULT_QL6C_TOPIC, False

    @staticmethod
    def _build_ql6c_descriptors(
        signal_map: list[ChannelConfig],
        *,
        source_topic: str,
    ) -> list[ChannelDescriptor]:
        photo_index_by_signal: dict[str, int | None] = {}
        for channel_cfg in signal_map:
            if channel_cfg.sourceTopic == source_topic and channel_cfg.module == QL6C_MODULE:
                photo_index_by_signal[channel_cfg.signalId] = channel_cfg.photoIndex

        descriptors: list[ChannelDescriptor] = []
        for ui_index, logical_channel in enumerate(QL6C_LOGICAL_CHANNELS):
            signal_id, title = QL6C_CHANNEL_META[logical_channel]
            descriptors.append(
                ChannelDescriptor(
                    board=QL6C_BOARD,
                    unit=QL6C_UNIT,
                    module=QL6C_MODULE,
                    logical_channel=logical_channel,
                    raw_channel=logical_channel,
                    ui_channel_index=ui_index,
                    signal_id=signal_id,
                    title=title,
                    purpose=title,
                    source_topic=source_topic,
                    in_bit=ui_index + 1,
                    out_bit=ui_index + 1,
                    diagnostic_bit=ui_index + 1,
                    photo_index=photo_index_by_signal.get(signal_id),
                )
            )
        return descriptors

    @staticmethod
    def _to_legacy_state(decoded_state: DecodedState) -> DecodedState:
        if decoded_state.status == "normal_on":
            return DecodedState(
                status="normal",
                status_code=decoded_state.status_code,
                label="Норма",
                state_label="Норма",
                description="Ключ включен",
                fault=False,
                severity="info",
                fault_type=None,
                yellow_led=decoded_state.yellow_led,
                red_led=decoded_state.red_led,
            )
        if decoded_state.status == "normal_off":
            return DecodedState(
                status="normal",
                status_code=decoded_state.status_code,
                label="Норма",
                state_label="Норма",
                description="Ключ выключен",
                fault=False,
                severity="info",
                fault_type=None,
                yellow_led=decoded_state.yellow_led,
                red_led=decoded_state.red_led,
            )
        if decoded_state.status == "fault_break":
            return DecodedState(
                status="open_circuit",
                status_code=decoded_state.status_code,
                label="Обрыв",
                state_label="Обрыв",
                description="Обрыв цепи",
                fault=True,
                severity="error",
                fault_type="break",
                yellow_led=decoded_state.yellow_led,
                red_led=decoded_state.red_led,
            )
        if decoded_state.status == "fault_short":
            return DecodedState(
                status="short_circuit",
                status_code=decoded_state.status_code,
                label="КЗ",
                state_label="КЗ",
                description="Короткое замыкание / авария / термозащита",
                fault=True,
                severity="error",
                fault_type="short",
                yellow_led=decoded_state.yellow_led,
                red_led=decoded_state.red_led,
            )
        return DecodedState(
            status="unknown",
            status_code=decoded_state.status_code,
            label="Неизвестно",
            state_label="Неизвестно",
            description="Неизвестная комбинация сигналов",
            fault=False,
            severity="warning",
            fault_type=None,
            yellow_led=decoded_state.yellow_led,
            red_led=decoded_state.red_led,
        )
