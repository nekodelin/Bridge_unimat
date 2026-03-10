import logging
from dataclasses import dataclass
from datetime import datetime

from app.models import BoardPayload, ChannelConfig, EventTextConfig
from app.schemas import ChannelState
from app.utils import extract_bit_from_bytes, normalize_channel_index, unpack_bits

FAULT_STATUSES = {"fault"}
NORMAL_STATUSES = {"normal"}

QL6C_MODULE = "QL6C"
QL6C_BOARD = "B31"
QL6C_UNIT = "U15"
DEFAULT_QL6C_TOPIC = "puma_board"
BIT_GROUP_WIDTH = 8

QL6C_CHANNEL_SEQUENCE = ("6", "7", "8", "9", "A", "B", "C", "D")
QL6C_CHANNEL_INDEX_BY_LOGICAL = {
    "6": 6,
    "7": 7,
    "8": 8,
    "9": 9,
    "A": 10,
    "B": 11,
    "C": 12,
    "D": 13,
}
QL6C_IN_BIT_INDEX_BY_CHANNEL = {
    channel: (BIT_GROUP_WIDTH - 1 - index)
    for index, channel in enumerate(QL6C_CHANNEL_SEQUENCE)
}
QL6C_DIAG_BIT_INDEX_BY_CHANNEL = QL6C_IN_BIT_INDEX_BY_CHANNEL.copy()
QL6C_OUT_BIT_INDEX_BY_CHANNEL = {channel: index for index, channel in enumerate(QL6C_CHANNEL_SEQUENCE)}

LEGACY_BIT_SOURCE_MAP = {
    "input": "in_",
    "output": "out",
    "diagnostic": "inversed",
}

FAULT_ACTION_TEXT = (
    "РџСЂРѕРІРµСЂРёС‚СЊ С„РёС€РєСѓ РіРёРґСЂРѕСЂР°СЃРїСЂРµРґРµР»РёС‚РµР»СЏ "
    "Рё РІС‹РїРѕР»РЅРёС‚СЊ РІРёР·СѓР°Р»СЊРЅС‹Р№ РѕСЃРјРѕС‚СЂ СЌР»РµРєС‚СЂРѕРїСЂРѕРІРѕРґРєРё С†РµРїРё"
)
UNKNOWN_CAUSE_TEXT = "РљРѕРјР±РёРЅР°С†РёСЏ СЃРёРіРЅР°Р»РѕРІ РЅРµ РѕРїРёСЃР°РЅР° РІ С‚Р°Р±Р»РёС†Рµ РёСЃС‚РёРЅРЅРѕСЃС‚Рё"
UNKNOWN_ACTION_TEXT = "РџСЂРѕРІРµСЂРёС‚СЊ РєРѕСЂСЂРµРєС‚РЅРѕСЃС‚СЊ РІС…РѕРґРЅС‹С… РґР°РЅРЅС‹С… Рё СЃС…РµРјСѓ РїРѕРґРєР»СЋС‡РµРЅРёСЏ"

QL6C_CHANNEL_META: dict[str, tuple[str, str]] = {
    "6": ("1s212b", "РєСЂСЋРє СЃР»РµРІР° РІС‹РґРІРёРЅСѓС‚СЊ"),
    "7": ("1s212a", "РєСЂСЋРє СЃР»РµРІР° Р·Р°РґРІРёРЅСѓС‚СЊ"),
    "8": ("1s213b", "РєСЂСЋРє СЃРїСЂР°РІР° РІС‹РґРІРёРЅСѓС‚СЊ"),
    "9": ("1s213a", "РєСЂСЋРє СЃРїСЂР°РІР° Р·Р°РґРІРёРЅСѓС‚СЊ"),
    "A": ("1s247b", "Р»РµРІС‹Р№ СЂРѕР»РёРєРѕРІС‹Р№ Р·Р°С…РІР°С‚ СЃРЅР°СЂСѓР¶Рё РѕС‚РєСЂС‹С‚СЊ"),
    "B": ("1s247a", "Р»РµРІС‹Р№ СЂРѕР»РёРєРѕРІС‹Р№ Р·Р°С…РІР°С‚ СЃРЅР°СЂСѓР¶Рё Р·Р°РєСЂС‹С‚СЊ"),
    "C": ("1s248b", "Р»РµРІС‹Р№ СЂРѕР»РёРєРѕРІС‹Р№ Р·Р°С…РІР°С‚ РІРЅСѓС‚СЂРё РѕС‚РєСЂС‹С‚СЊ"),
    "D": ("1s248a", "Р»РµРІС‹Р№ СЂРѕР»РёРєРѕРІС‹Р№ Р·Р°С…РІР°С‚ РІРЅСѓС‚СЂРё Р·Р°РєСЂС‹С‚СЊ"),
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
    channel_key: str
    channel_index: int
    board: str
    unit: str | None
    module: str
    logical_channel: str
    raw_channel: str
    signal_id: str
    title: str
    purpose: str
    source_topic: str
    photo_index: int | None = None


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


def get_bit(value: int, bit_index: int) -> int:
    if bit_index < 0:
        raise ValueError("bit_index must be >= 0")
    return (int(value) >> int(bit_index)) & 1


def get_in_bit(channel: str, raw_in: int) -> int:
    normalized_channel = _normalize_ql6c_channel(channel)
    return get_bit(raw_in, QL6C_IN_BIT_INDEX_BY_CHANNEL[normalized_channel])


def get_out_bit(channel: str, raw_out: int) -> int:
    normalized_channel = _normalize_ql6c_channel(channel)
    return get_bit(raw_out, QL6C_OUT_BIT_INDEX_BY_CHANNEL[normalized_channel])


def get_diag_bit(channel: str, raw_inversed: int) -> int:
    normalized_channel = _normalize_ql6c_channel(channel)
    return get_bit(raw_inversed, QL6C_DIAG_BIT_INDEX_BY_CHANNEL[normalized_channel])


def decode_channel_state(in_bit: int, out_bit: int, dg_bit: int) -> DecodedState:
    key = (int(in_bit), int(out_bit), int(dg_bit))

    if key in {(0, 0, 1), (1, 1, 1)}:
        return DecodedState(
            status="normal",
            status_code="normal",
            label="Норма",
            state_label="Норма",
            description="Норма",
            fault=False,
            severity="info",
            fault_type=None,
            yellow_led=bool(in_bit),
            red_led=False,
        )

    if key in {(0, 1, 0), (1, 1, 0)}:
        return DecodedState(
            status="fault",
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
            status="fault",
            status_code="short",
            label="КЗ",
            state_label="КЗ",
            description="КЗ",
            fault=True,
            severity="error",
            fault_type="short",
            yellow_led=False,
            red_led=True,
        )

    return DecodedState(
        status="unknown",
        status_code="unknown",
        label="Нет данных",
        state_label="Нет данных",
        description="Нет данных",
        fault=False,
        severity="warning",
        fault_type="unknown",
        yellow_led=False,
        red_led=False,
    )


def decode_triplet(input_bit: int, output_bit: int, diagnostic_bit: int) -> DecodedState:
    # Backward-compatible alias used by tests and external imports.
    return decode_channel_state(input_bit, output_bit, diagnostic_bit)


def decode_channel(input_bit: int, output_bit: int, diagnostic_bit: int) -> DecodedState:
    # Backward-compatible alias used by tests and external imports.
    return decode_channel_state(input_bit, output_bit, diagnostic_bit)


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
            (channel_cfg.channelIndex for channel_cfg in self.legacy_signal_map),
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
                    channelIndex=descriptor.channel_index,
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
                    statusLabel="РќРµР°РєС‚РёРІРЅРѕ",
                    stateLabel="РќРµР°РєС‚РёРІРЅРѕ",
                    stateText="РќРµР°РєС‚РёРІРЅРѕ",
                    label="РќРµР°РєС‚РёРІРЅРѕ",
                    faultType=None,
                    inBit=0,
                    outBit=0,
                    diagBit=0,
                    stateTuple=[0, 0, 0],
                    yellow_led=False,
                    red_led=False,
                    message="РќРµС‚ РґР°РЅРЅС‹С…",
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
                    statusLabel="РќРµР°РєС‚РёРІРЅРѕ",
                    stateLabel="РќРµР°РєС‚РёРІРЅРѕ",
                    stateText="РќРµР°РєС‚РёРІРЅРѕ",
                    label="РќРµР°РєС‚РёРІРЅРѕ",
                    faultType=None,
                    inBit=0,
                    outBit=0,
                    diagBit=0,
                    stateTuple=[0, 0, 0],
                    yellow_led=False,
                    red_led=False,
                    message="РќРµС‚ РґР°РЅРЅС‹С…",
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
            sum(1 for item in decoded_channels if item.status == "fault"),
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
            "faultCount": sum(1 for item in decoded_channels if item.status == "fault"),
            "unknownCount": sum(1 for item in decoded_channels if item.status == "unknown"),
            "normalCount": sum(1 for item in decoded_channels if item.status == "normal"),
        }

        channel_bits = [
            {
                "channelKey": item.channelKey,
                "channelIndex": item.channelIndex,
                "logicalChannel": item.logicalChannel,
                "inputBit": item.input,
                "outputBit": item.output,
                "diagnosticBit": item.diagnostic,
                "stateTuple": item.stateTuple,
                "status": item.status,
                "statusCode": item.statusCode,
                "faultType": item.faultType,
                "stateText": item.stateText,
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

        raw_in = int(payload.in_)
        raw_inversed = int(payload.inversed)
        raw_out = int(payload.out)
        raw_other = int(payload.other) if payload.other is not None else 0

        logger.debug(
            "QL6C raw topic=%s in=%s inversed(dg)=%s out=%s other(raw)=%s",
            topic,
            decode_bits(raw_in, size=BIT_GROUP_WIDTH),
            decode_bits(raw_inversed, size=BIT_GROUP_WIDTH),
            decode_bits(raw_out, size=BIT_GROUP_WIDTH),
            decode_bits(raw_other, size=BIT_GROUP_WIDTH),
        )

        channels: list[ChannelState] = []
        for descriptor in self.ql6c_descriptors:
            input_bit = get_in_bit(descriptor.logical_channel, raw_in)
            output_bit = get_out_bit(descriptor.logical_channel, raw_out)
            diagnostic_bit = get_diag_bit(descriptor.logical_channel, raw_inversed)
            logger.info(
                "channel decode channelIndex=%s in=%s out=%s dg=%s",
                descriptor.channel_index,
                input_bit,
                output_bit,
                diagnostic_bit,
            )

            decoded_state = decode_channel_state(input_bit, output_bit, diagnostic_bit)
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
                "channel=%s tuple=(%s,%s,%s) => %s",
                descriptor.logical_channel,
                input_bit,
                output_bit,
                diagnostic_bit,
                decoded_state.status_code,
            )

            channels.append(
                ChannelState(
                    channelKey=descriptor.channel_key,
                    channelIndex=descriptor.channel_index,
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
                    stateText=decoded_state.state_label,
                    label=decoded_state.label,
                    faultType=decoded_state.fault_type,  # type: ignore[arg-type]
                    inBit=input_bit,
                    outBit=output_bit,
                    diagBit=diagnostic_bit,
                    stateTuple=[input_bit, output_bit, diagnostic_bit],
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

            decoded_state = decode_channel_state(input_bit, output_bit, diagnostic_bit)
            cause, action = self._resolve_cause_action(decoded_state.fault_type, channel_cfg.signalId)
            purpose = self._resolve_purpose(channel_cfg.signalId, channel_cfg.purpose)
            _, unit = self._split_board_and_unit(channel_cfg.board)

            logger.debug(
                "channel=%s tuple=(%s,%s,%s) => %s",
                channel_cfg.channelIndex,
                input_bit,
                output_bit,
                diagnostic_bit,
                decoded_state.status_code,
            )

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
                    status=decoded_state.status,  # type: ignore[arg-type]
                    statusCode=decoded_state.status_code,
                    statusLabel=decoded_state.status_label,
                    stateLabel=decoded_state.state_label,
                    stateText=decoded_state.state_label,
                    label=decoded_state.label,
                    faultType=decoded_state.fault_type,  # type: ignore[arg-type]
                    inBit=input_bit,
                    outBit=output_bit,
                    diagBit=diagnostic_bit,
                    stateTuple=[input_bit, output_bit, diagnostic_bit],
                    yellow_led=decoded_state.yellow_led,
                    red_led=decoded_state.red_led,
                    message=decoded_state.description,
                    reason=cause,
                    cause=cause,
                    action=action,
                    severity=decoded_state.severity,  # type: ignore[arg-type]
                    fault=decoded_state.fault,
                    isFault=decoded_state.fault,
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
        action = event_text.action if event_text and event_text.action else None

        if decoded_state.status == "normal":
            return EventTextResult(
                message=purpose,
                reason=None,
                cause=None,
                action=None,
            )

        if decoded_state.status == "fault" and decoded_state.fault_type == "break":
            reason = "РћР±СЂС‹РІ С†РµРїРё"
            cause = event_text.breakageCause if event_text else reason
            return EventTextResult(
                message=decoded_state.state_label,
                reason=reason,
                cause=cause,
                action=action or FAULT_ACTION_TEXT,
            )

        if decoded_state.status == "fault" and decoded_state.fault_type == "short":
            reason = "РљРѕСЂРѕС‚РєРѕРµ Р·Р°РјС‹РєР°РЅРёРµ"
            cause = event_text.shortCause if event_text else reason
            return EventTextResult(
                message=decoded_state.state_label,
                reason=reason,
                cause=cause,
                action=action or FAULT_ACTION_TEXT,
            )

        return EventTextResult(
            message=decoded_state.state_label,
            reason=decoded_state.description,
            cause=UNKNOWN_CAUSE_TEXT,
            action=UNKNOWN_ACTION_TEXT,
        )

    def _resolve_cause_action(
        self,
        fault_type: str | None,
        signal_id: str,
    ) -> tuple[str | None, str | None]:
        if fault_type == "break":
            return f"РћР±СЂС‹РІ С†РµРїРё РіРёРґСЂРѕСЂР°СЃРїСЂРµРґРµР»РёС‚РµР»СЏ {signal_id}", FAULT_ACTION_TEXT
        if fault_type == "short":
            return (
                f"РљРѕСЂРѕС‚РєРѕРµ Р·Р°РјС‹РєР°РЅРёРµ РІ С†РµРїРё РіРёРґСЂРѕСЂР°СЃРїСЂРµРґРµР»РёС‚РµР»СЏ {signal_id}",
                FAULT_ACTION_TEXT,
            )
        if fault_type == "unknown":
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
        ql6c_channels = [
            channel_cfg
            for channel_cfg in signal_map
            if channel_cfg.sourceTopic == source_topic
            and channel_cfg.module == QL6C_MODULE
            and "B31" in channel_cfg.board.upper()
            and "U15" in channel_cfg.board.upper()
            and channel_cfg.channelIndex in QL6C_CHANNEL_INDEX_BY_LOGICAL.values()
        ]
        by_channel_index = {channel_cfg.channelIndex: channel_cfg for channel_cfg in ql6c_channels}

        descriptors: list[ChannelDescriptor] = []
        for logical_channel in QL6C_CHANNEL_SEQUENCE:
            channel_index = QL6C_CHANNEL_INDEX_BY_LOGICAL[logical_channel]
            cfg = by_channel_index.get(channel_index)

            default_signal_id, default_title = QL6C_CHANNEL_META[logical_channel]
            signal_id = cfg.signalId if cfg else default_signal_id
            title = cfg.purpose if cfg else default_title
            purpose = cfg.purpose if cfg else default_title
            channel_key = cfg.channelKey if cfg else f"{QL6C_MODULE}{logical_channel}"
            board_value = cfg.board if cfg else f"{QL6C_BOARD}/{QL6C_UNIT}"
            board, unit = DecoderService._split_board_and_unit(board_value)
            photo_index = cfg.photoIndex if cfg else None

            descriptors.append(
                ChannelDescriptor(
                    channel_key=channel_key,
                    channel_index=channel_index,
                    board=board,
                    unit=unit,
                    module=QL6C_MODULE,
                    logical_channel=logical_channel,
                    raw_channel=logical_channel,
                    signal_id=signal_id,
                    title=title,
                    purpose=purpose,
                    source_topic=source_topic,
                    photo_index=photo_index,
                )
            )

        return descriptors


def _normalize_ql6c_channel(channel: str) -> str:
    normalized = channel.strip().upper()
    if normalized not in QL6C_CHANNEL_SEQUENCE:
        raise ValueError(f"Unsupported QL6C channel '{channel}'")
    return normalized
