from datetime import datetime, timezone
import unittest

from app.models import BoardPayload, ChannelConfig
from app.services.decoder import (
    DecoderService,
    decode_bits,
    decode_channel_state,
    get_bit,
    get_diag_bit,
    get_in_bit,
    get_out_bit,
)


def build_decoder() -> DecoderService:
    signal_map = [
        ChannelConfig.model_validate(
            {
                "channelKey": "QL6C6",
                "channelIndex": 6,
                "signalId": "1s212b",
                "purpose": "left hook extend",
                "board": "B31/U15",
                "module": "QL6C",
                "photoIndex": 10,
                "sourceTopic": "puma_board",
            }
        ),
        ChannelConfig.model_validate(
            {
                "channelKey": "QL6D0",
                "channelIndex": 0,
                "signalId": "1s250b",
                "purpose": "right grip open",
                "board": "B31/U16",
                "module": "QL6D",
                "photoIndex": 8,
                "sourceTopic": "puma_board_u16",
            }
        ),
    ]
    return DecoderService(signal_map=signal_map, event_texts={})


class BoardPayloadValidationTest(unittest.TestCase):
    def test_accepts_optional_other_field(self) -> None:
        payload = BoardPayload.model_validate({"in": 12, "inversed": 34, "out": 56, "other": 78})
        self.assertEqual(payload.in_, 12)
        self.assertEqual(payload.inversed, 34)
        self.assertEqual(payload.out, 56)
        self.assertEqual(payload.other, 78)


class BitHelpersTest(unittest.TestCase):
    def test_decode_bits_lsb_order(self) -> None:
        self.assertEqual(decode_bits(5, size=8), [1, 0, 1, 0, 0, 0, 0, 0])

    def test_get_bit(self) -> None:
        self.assertEqual(get_bit(0b10000000, 7), 1)
        self.assertEqual(get_bit(0b10000000, 6), 0)

    def test_in_and_diag_mapping_reversed(self) -> None:
        self.assertEqual(get_in_bit("6", 0b10000000), 1)  # bit7 -> 6
        self.assertEqual(get_in_bit("D", 0b00000001), 1)  # bit0 -> D
        self.assertEqual(get_diag_bit("6", 0b10000000), 1)  # bit7 -> 6
        self.assertEqual(get_diag_bit("D", 0b00000001), 1)  # bit0 -> D

    def test_out_mapping_direct(self) -> None:
        self.assertEqual(get_out_bit("6", 0b00000001), 1)  # bit0 -> 6
        self.assertEqual(get_out_bit("7", 0b00000010), 1)  # bit1 -> 7
        self.assertEqual(get_out_bit("D", 0b10000000), 1)  # bit7 -> D


class TruthTableTest(unittest.TestCase):
    def test_truth_table(self) -> None:
        cases = [
            ((0, 0, 1), ("normal", None, "normal", False, False)),
            ((1, 1, 1), ("normal", None, "normal", True, False)),
            ((0, 1, 0), ("fault", "break", "break", True, True)),
            ((1, 1, 0), ("fault", "break", "break", True, True)),
            ((1, 0, 0), ("fault", "short", "short", False, True)),
            ((1, 0, 1), ("unknown", "unknown", "unknown", False, False)),
            ((0, 0, 0), ("unknown", "unknown", "unknown", False, False)),
            ((0, 1, 1), ("unknown", "unknown", "unknown", False, False)),
        ]

        for inputs, expected in cases:
            with self.subTest(inputs=inputs):
                state = decode_channel_state(*inputs)
                self.assertEqual(state.status, expected[0])
                self.assertEqual(state.fault_type, expected[1])
                self.assertEqual(state.status_code, expected[2])
                self.assertEqual(state.yellow_led, expected[3])
                self.assertEqual(state.red_led, expected[4])


class QL6CMappingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.decoder = build_decoder()
        self.now = datetime.now(timezone.utc)

    def _decode(self, *, in_value: int, inversed_value: int, out_value: int) -> dict[str, object]:
        payload = BoardPayload.model_validate(
            {
                "in": in_value,
                "inversed": inversed_value,
                "out": out_value,
                "other": 0,
            }
        )
        decoded = self.decoder.decode_board_payload(payload=payload, topic="puma_board", updated_at=self.now)
        return {item.logicalChannel: item for item in decoded}

    def test_only_ql6c_working_channels_6_to_d(self) -> None:
        payload = BoardPayload.model_validate({"in": 0, "inversed": 0, "out": 0, "other": 0})
        decoded = self.decoder.decode_board_payload(payload=payload, topic="puma_board", updated_at=self.now)
        logical = [item.logicalChannel for item in decoded]
        self.assertEqual(logical, ["6", "7", "8", "9", "A", "B", "C", "D"])
        self.assertEqual([item.channelIndex for item in decoded], [6, 7, 8, 9, 10, 11, 12, 13])

    def test_in_bit_mapping_reversed(self) -> None:
        mapping = ["6", "7", "8", "9", "A", "B", "C", "D"]
        for bit_index, channel in zip([7, 6, 5, 4, 3, 2, 1, 0], mapping):
            with self.subTest(bit_index=bit_index, channel=channel):
                by_channel = self._decode(in_value=(1 << bit_index), inversed_value=0, out_value=0)
                self.assertEqual(by_channel[channel].inBit, 1)
                self.assertEqual(sum(item.inBit for item in by_channel.values()), 1)

    def test_diag_bit_mapping_reversed(self) -> None:
        mapping = ["6", "7", "8", "9", "A", "B", "C", "D"]
        for bit_index, channel in zip([7, 6, 5, 4, 3, 2, 1, 0], mapping):
            with self.subTest(bit_index=bit_index, channel=channel):
                by_channel = self._decode(in_value=0, inversed_value=(1 << bit_index), out_value=0)
                self.assertEqual(by_channel[channel].diagBit, 1)
                self.assertEqual(sum(item.diagBit for item in by_channel.values()), 1)

    def test_out_bit_mapping_direct(self) -> None:
        mapping = ["6", "7", "8", "9", "A", "B", "C", "D"]
        for bit_index, channel in enumerate(mapping):
            with self.subTest(bit_index=bit_index, channel=channel):
                by_channel = self._decode(in_value=0, inversed_value=0, out_value=(1 << bit_index))
                self.assertEqual(by_channel[channel].outBit, 1)
                self.assertEqual(sum(item.outBit for item in by_channel.values()), 1)

    def test_extended_fields_and_status_logic(self) -> None:
        # channel 6: (1,1,1) => normal
        # channel 8: (0,1,0) => fault/break (OUT bit index for channel 8 is 2 in direct mapping)
        by_channel = self._decode(
            in_value=(1 << 7),
            inversed_value=(1 << 7),
            out_value=(1 << 0) | (1 << 2),
        )

        channel_6 = by_channel["6"]
        self.assertEqual(channel_6.status, "normal")
        self.assertEqual(channel_6.faultType, None)
        self.assertIsNone(channel_6.faultText)
        self.assertEqual(channel_6.stateTuple, [1, 1, 1])
        self.assertEqual(channel_6.yellow_led, True)
        self.assertEqual(channel_6.red_led, False)

        channel_8 = by_channel["8"]
        self.assertEqual(channel_8.status, "fault")
        self.assertEqual(channel_8.faultType, "break")
        self.assertEqual(channel_8.faultText, "Обрыв")
        self.assertEqual(channel_8.stateTuple, [0, 1, 0])
        self.assertEqual(channel_8.yellow_led, True)
        self.assertEqual(channel_8.red_led, True)

        channel_7 = by_channel["7"]
        self.assertEqual(channel_7.status, "unknown")
        self.assertEqual(channel_7.faultType, "unknown")
        self.assertIsNone(channel_7.faultText)


if __name__ == "__main__":
    unittest.main()
