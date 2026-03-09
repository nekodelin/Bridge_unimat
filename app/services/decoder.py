import logging
from dataclasses import dataclass
from datetime import datetime

from app.models import BoardPayload, ChannelConfig, EventTextConfig
from app.schemas import ChannelState
from app.utils import extract_bit_from_bytes, normalize_channel_index

FAULT_STATUSES = {"open_circuit", "short_circuit"}
NORMAL_STATUSES = {"normal", "active"}
DEBUG_STRATEGIES = ("strategyA", "strategyB")
DEBUG_BIT_ORDERS = ("normal", "reversed")
DEBUG_SUMMARY_STATUSES = ("normal", "short_circuit", "open_circuit", "unknown")

# Field mapping can be changed in one place.
BIT_SOURCE_MAP = {
    "input": "in_",
    "output": "out",
    "diagnostic": "inversed",
}

FAULT_ACTION_TEXT = (
    "Проверить фишку гидрораспределителя и выполнить визуальный осмотр электропроводки цепи"
)
UNKNOWN_CAUSE_TEXT = "Комбинация сигналов не описана в таблице истинности"
UNKNOWN_ACTION_TEXT = "Проверить корректность входных данных и схему подключения"

logger = logging.getLogger("unimat.decoder")


@dataclass(slots=True)
class ChannelDecodeResult:
    status: str
    description: str
    state_label: str
    is_fault: bool
    severity: str


def decode_bits(value: int, size: int = 16) -> list[int]:
    if size <= 0:
        return []
    normalized = int(value)
    return [(normalized >> bit_index) & 1 for bit_index in range(size)]


def decode_channel(input_bit: int, output_bit: int, diagnostic_bit: int) -> ChannelDecodeResult:
    key = (int(input_bit), int(output_bit), int(diagnostic_bit))
    if key == (0, 0, 1):
        return ChannelDecodeResult(
            status="normal",
            description="Ключ выключен",
            state_label="Норма",
            is_fault=False,
            severity="info",
        )
    if key == (1, 1, 1):
        return ChannelDecodeResult(
            status="normal",
            description="Ключ включен",
            state_label="Норма",
            is_fault=False,
            severity="info",
        )
    if key in {(0, 1, 0), (1, 1, 0)}:
        return ChannelDecodeResult(
            status="open_circuit",
            description="Обрыв цепи",
            state_label="Обрыв",
            is_fault=True,
            severity="error",
        )
    if key == (1, 0, 0):
        return ChannelDecodeResult(
            status="short_circuit",
            description="Короткое замыкание / авария / термозащита",
            state_label="КЗ",
            is_fault=True,
            severity="error",
        )
    return ChannelDecodeResult(
        status="unknown",
        description="Неизвестная комбинация сигналов",
        state_label="Неизвестно",
        is_fault=False,
        severity="warning",
    )


class DecoderService:
    def __init__(
        self,
        signal_map: list[ChannelConfig],
        event_texts: dict[str, EventTextConfig],
    ) -> None:
        self.signal_map = signal_map
        self.event_texts = event_texts
        self.max_channel_index = max((item.channelIndex for item in signal_map), default=0)
        self.bit_size = max(self.max_channel_index + 1, 16)
        self.channels_by_topic_and_index: dict[str, dict[int, ChannelConfig]] = {}
        self._out_of_range_warnings: set[tuple[str, int]] = set()
        for channel_cfg in signal_map:
            topic_map = self.channels_by_topic_and_index.setdefault(channel_cfg.sourceTopic, {})
            topic_map.setdefault(channel_cfg.channelIndex, channel_cfg)

    def default_inactive_channels(self) -> list[ChannelState]:
        channels: list[ChannelState] = []
        for channel_cfg in self.signal_map:
            purpose = self._resolve_purpose(channel_cfg)
            channels.append(
                ChannelState(
                    channelKey=channel_cfg.channelKey,
                    channelIndex=channel_cfg.channelIndex,
                    signalId=channel_cfg.signalId,
                    title=purpose,
                    purpose=purpose,
                    photoIndex=channel_cfg.photoIndex,
                    board=channel_cfg.board,
                    unit=self._resolve_unit(channel_cfg.board),
                    module=channel_cfg.module,
                    topic=channel_cfg.sourceTopic,
                    input=0,
                    output=0,
                    diagnostic=0,
                    status="inactive",
                    stateLabel="Неактивно",
                    message="Нет данных",
                    cause=None,
                    action=None,
                    severity="info",
                    isFault=False,
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
        input_values = [int(getattr(payload, BIT_SOURCE_MAP["input"]))]
        output_values = [int(getattr(payload, BIT_SOURCE_MAP["output"]))]
        diagnostic_values = [int(getattr(payload, BIT_SOURCE_MAP["diagnostic"]))]

        decoded_channels: list[ChannelState] = []
        for channel_cfg in self.signal_map:
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

            channel_state = decode_channel(input_bit, output_bit, diagnostic_bit)
            cause, action = self._resolve_cause_action(channel_state.status, channel_cfg.signalId)
            purpose = self._resolve_purpose(channel_cfg)

            decoded_channels.append(
                ChannelState(
                    channelKey=channel_cfg.channelKey,
                    topic=topic,
                    board=channel_cfg.board,
                    unit=self._resolve_unit(channel_cfg.board),
                    module=channel_cfg.module,
                    channelIndex=channel_cfg.channelIndex,
                    signalId=channel_cfg.signalId,
                    title=purpose,
                    purpose=purpose,
                    photoIndex=channel_cfg.photoIndex,
                    input=input_bit,
                    output=output_bit,
                    diagnostic=diagnostic_bit,
                    status=channel_state.status,  # type: ignore[arg-type]
                    stateLabel=channel_state.state_label,
                    message=channel_state.description,
                    cause=cause,
                    action=action,
                    severity=channel_state.severity,  # type: ignore[arg-type]
                    isFault=channel_state.is_fault,
                    updatedAt=updated_at,
                )
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

        raw_block = {
            "in": {"decimal": raw_in, "bin": self._format_binary(raw_in, size)},
            "inversed": {"decimal": raw_inversed, "bin": self._format_binary(raw_inversed, size)},
            "out": {"decimal": raw_out, "bin": self._format_binary(raw_out, size)},
            "other": payload.other,
        }

        in_bits_by_order = {
            "normal": self._decode_bits_for_order(raw_in, size=size, order="normal"),
            "reversed": self._decode_bits_for_order(raw_in, size=size, order="reversed"),
        }
        inversed_bits_by_order = {
            "normal": self._decode_bits_for_order(raw_inversed, size=size, order="normal"),
            "reversed": self._decode_bits_for_order(raw_inversed, size=size, order="reversed"),
        }
        out_bits_by_order = {
            "normal": self._decode_bits_for_order(raw_out, size=size, order="normal"),
            "reversed": self._decode_bits_for_order(raw_out, size=size, order="reversed"),
        }

        interpretations: dict[str, dict[str, dict]] = {}
        for order in DEBUG_BIT_ORDERS:
            interpretations[order] = {}
            for strategy in DEBUG_STRATEGIES:
                interpretations[order][strategy] = self._build_strategy_debug(
                    input_bits=in_bits_by_order[order],
                    inversed_bits=inversed_bits_by_order[order],
                    output_bits=out_bits_by_order[order],
                    strategy=strategy,
                    topic=topic,
                )

        default_view = interpretations["normal"]["strategyA"]
        return {
            "timestamp": timestamp,
            "source": source,
            "sourceTopic": topic,
            "raw": raw_block,
            "bits": default_view["bits"],
            "summary": default_view["summary"],
            "interpretations": interpretations,
        }

    def _build_strategy_debug(
        self,
        input_bits: list[int],
        inversed_bits: list[int],
        output_bits: list[int],
        strategy: str,
        topic: str,
    ) -> dict:
        bits: list[dict] = []
        summary = {status: 0 for status in DEBUG_SUMMARY_STATUSES}
        mapped_bits = 0
        topic_map = self.channels_by_topic_and_index.get(topic, {})

        for bit_index in range(len(input_bits)):
            input_bit = input_bits[bit_index]
            inversed_bit = inversed_bits[bit_index]
            output_bit = output_bits[bit_index]
            diagnostic_bit = inversed_bit if strategy == "strategyA" else 1 - inversed_bit
            channel_state = decode_channel(input_bit, output_bit, diagnostic_bit)
            channel_cfg = topic_map.get(bit_index)

            decoded_status = channel_state.status
            if channel_cfg is None:
                decoded_status = "unmapped"
            else:
                mapped_bits += 1
                if channel_state.status in summary:
                    summary[channel_state.status] += 1

            bits.append(
                {
                    "bitIndex": bit_index,
                    "inputBit": input_bit,
                    "inversedBit": inversed_bit,
                    "outputBit": output_bit,
                    "diagnosticBit": diagnostic_bit,
                    "channelKey": channel_cfg.channelKey if channel_cfg else None,
                    "signalId": channel_cfg.signalId if channel_cfg else None,
                    "decodedStatus": decoded_status,
                }
            )

        return {
            "bits": bits,
            "summary": {
                **summary,
                "mappedBits": mapped_bits,
                "totalBits": len(bits),
            },
        }

    def _decode_bits_for_order(self, value: int, size: int, order: str) -> list[int]:
        bits = decode_bits(value, size=size)
        if order == "reversed":
            return list(reversed(bits))
        return bits

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
            warn_key = (topic, normalized_index)
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

    def _resolve_cause_action(self, status: str, signal_id: str) -> tuple[str | None, str | None]:
        if status == "open_circuit":
            return f"Обрыв цепи гидрораспределителя {signal_id}", FAULT_ACTION_TEXT
        if status == "short_circuit":
            return f"Короткое замыкание в цепи гидрораспределителя {signal_id}", FAULT_ACTION_TEXT
        if status == "unknown":
            return UNKNOWN_CAUSE_TEXT, UNKNOWN_ACTION_TEXT
        return None, None

    def _resolve_purpose(self, channel_cfg: ChannelConfig) -> str:
        event_text = self.event_texts.get(channel_cfg.signalId)
        if event_text and event_text.purpose:
            return event_text.purpose
        return channel_cfg.purpose

    @staticmethod
    def _resolve_unit(board_value: str) -> str | None:
        if "/" not in board_value:
            return None
        parts = [chunk.strip() for chunk in board_value.split("/") if chunk.strip()]
        if len(parts) < 2:
            return None
        return parts[-1]

    def _resolve_bit_size(self, payload: BoardPayload) -> int:
        max_payload_value = max(int(payload.in_), int(payload.inversed), int(payload.out), 0)
        dynamic_size = max_payload_value.bit_length()
        return max(self.bit_size, dynamic_size, 1)

    @staticmethod
    def _format_binary(value: int, size: int) -> str:
        if value < 0:
            return f"-0b{format(abs(value), f'0{size}b')}"
        return f"0b{format(value, f'0{size}b')}"
