from dataclasses import dataclass
from datetime import datetime

from app.models import BoardPayload, ChannelConfig, EventTextConfig
from app.schemas import ChannelState

FAULT_STATUSES = {"breakage", "short_circuit"}
NORMAL_STATUSES = {"normal", "active"}
DEBUG_STRATEGIES = ("strategyA", "strategyB")
DEBUG_BIT_ORDERS = ("normal", "reversed")
DEBUG_SUMMARY_STATUSES = ("normal", "short_circuit", "breakage", "unknown")

# Field mapping can be changed in one place.
BIT_SOURCE_MAP = {
    "input_inversed": "inversed",
    "output": "out",
    "diagnostic": "in_",
}


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
    if key == (0, 1, 0):
        return ChannelDecodeResult(
            status="breakage",
            description="Обрыв",
            state_label="Обрыв",
            is_fault=True,
            severity="error",
        )
    if key == (1, 0, 0):
        return ChannelDecodeResult(
            status="short_circuit",
            description="Короткое замыкание",
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
        for channel_cfg in signal_map:
            topic_map = self.channels_by_topic_and_index.setdefault(channel_cfg.sourceTopic, {})
            topic_map.setdefault(channel_cfg.channelIndex, channel_cfg)

    def default_inactive_channels(self) -> list[ChannelState]:
        channels: list[ChannelState] = []
        for channel_cfg in self.signal_map:
            event_text = self.event_texts.get(channel_cfg.signalId)
            title = event_text.eventTitle if event_text else channel_cfg.purpose
            purpose = event_text.purpose if event_text else channel_cfg.purpose
            action = event_text.action if event_text else None
            channels.append(
                ChannelState(
                    channelKey=channel_cfg.channelKey,
                    channelIndex=channel_cfg.channelIndex,
                    signalId=channel_cfg.signalId,
                    title=title,
                    purpose=purpose,
                    photoIndex=channel_cfg.photoIndex,
                    board=channel_cfg.board,
                    module=channel_cfg.module,
                    input=0,
                    output=0,
                    diagnostic=0,
                    status="inactive",
                    stateLabel="Неактивно",
                    message="Нет данных",
                    cause=None,
                    action=action,
                    severity="info",
                    isFault=False,
                )
            )
        return channels

    def decode_board_payload(self, payload: BoardPayload, topic: str) -> list[ChannelState]:
        inversed_bits = decode_bits(int(getattr(payload, BIT_SOURCE_MAP["input_inversed"])), self.bit_size)
        output_bits = decode_bits(int(getattr(payload, BIT_SOURCE_MAP["output"])), self.bit_size)
        diagnostic_bits = decode_bits(int(getattr(payload, BIT_SOURCE_MAP["diagnostic"])), self.bit_size)

        decoded_channels: list[ChannelState] = []
        for channel_cfg in self.signal_map:
            if channel_cfg.sourceTopic != topic:
                continue

            index = channel_cfg.channelIndex
            inversed_bit = inversed_bits[index] if index < len(inversed_bits) else 0
            input_bit = 0 if inversed_bit == 1 else 1
            output_bit = output_bits[index] if index < len(output_bits) else 0
            diagnostic_bit = diagnostic_bits[index] if index < len(diagnostic_bits) else 0

            channel_state = decode_channel(input_bit, output_bit, diagnostic_bit)
            event_text = self.event_texts.get(channel_cfg.signalId)

            title = event_text.eventTitle if event_text else channel_cfg.purpose
            purpose = event_text.purpose if event_text else channel_cfg.purpose
            action = event_text.action if event_text else None
            cause = None
            if channel_state.status == "breakage" and event_text:
                cause = event_text.breakageCause
            elif channel_state.status == "short_circuit" and event_text:
                cause = event_text.shortCause

            message = title if channel_state.is_fault else channel_state.description

            decoded_channels.append(
                ChannelState(
                    channelKey=channel_cfg.channelKey,
                    channelIndex=channel_cfg.channelIndex,
                    signalId=channel_cfg.signalId,
                    title=title,
                    purpose=purpose,
                    photoIndex=channel_cfg.photoIndex,
                    board=channel_cfg.board,
                    module=channel_cfg.module,
                    input=input_bit,
                    output=output_bit,
                    diagnostic=diagnostic_bit,
                    status=channel_state.status,  # type: ignore[arg-type]
                    stateLabel=channel_state.state_label,
                    message=message,
                    cause=cause,
                    action=action,
                    severity=channel_state.severity,  # type: ignore[arg-type]
                    isFault=channel_state.is_fault,
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

    def _resolve_bit_size(self, payload: BoardPayload) -> int:
        max_payload_value = max(int(payload.in_), int(payload.inversed), int(payload.out), 0)
        dynamic_size = max_payload_value.bit_length()
        return max(self.bit_size, dynamic_size, 1)

    @staticmethod
    def _format_binary(value: int, size: int) -> str:
        if value < 0:
            return f"-0b{format(abs(value), f'0{size}b')}"
        return f"0b{format(value, f'0{size}b')}"
