from datetime import datetime, timezone
import unittest

from app.models import BoardPayload, ChannelConfig
from app.services.decoder import DecoderService, decode_bits, decode_triplet


def build_decoder() -> DecoderService:
    # Keep config intentionally minimal: DecoderService builds canonical QL6C map itself.
    signal_map = [
        ChannelConfig.model_validate(
            {
                "channelKey": "QL6C6",
                "channelIndex": 6,
                "signalId": "1s212b",
                "purpose": "крюк слева выдвинуть",
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
                "purpose": "правый роликовый захват снаружи открыть",
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
        payload = BoardPayload.model_validate(
            {"in": 12, "inversed": 34, "out": 56, "other": 78}
        )
        self.assertEqual(payload.in_, 12)
        self.assertEqual(payload.inversed, 34)
        self.assertEqual(payload.out, 56)
        self.assertEqual(payload.other, 78)
        self.assertEqual(
            payload.to_raw_dict(),
            {"in": 12, "inversed": 34, "out": 56, "other": 78},
        )


class BitOrderTest(unittest.TestCase):
    def test_unpack_in_5_uses_lsb_as_bit0(self) -> None:
        bits = decode_bits(5, size=8)
        # bit0..bit7 for decimal 5 (0b00000101)
        self.assertEqual(bits, [1, 0, 1, 0, 0, 0, 0, 0])
        self.assertEqual(bits[0], 1)
        self.assertEqual(bits[1], 0)
        self.assertEqual(bits[2], 1)


class QL6CMappingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.decoder = build_decoder()
        self.now = datetime.now(timezone.utc)
        self.logical_order_direct = ["6", "7", "8", "9", "A", "B", "C", "D"]
        self.logical_order_out_reversed = ["D", "C", "B", "A", "9", "8", "7", "6"]

    def test_in_mapping_direct_bit0_to_6_bit7_to_d(self) -> None:
        for bit_index, logical_channel in enumerate(self.logical_order_direct):
            payload = BoardPayload.model_validate(
                {
                    "in": 1 << bit_index,
                    "inversed": 255,
                    "out": 1 << (7 - bit_index),
                    "other": 0,
                }
            )
            decoded = self.decoder.decode_board_payload(payload=payload, topic="puma_board", updated_at=self.now)
            by_logical = {item.logicalChannel: item for item in decoded}
            self.assertEqual(by_logical[logical_channel].input, 1)
            self.assertEqual(sum(item.input for item in decoded), 1)

    def test_dg_mapping_direct_from_inversed_bit0_to_6_bit7_to_d(self) -> None:
        for bit_index, logical_channel in enumerate(self.logical_order_direct):
            payload = BoardPayload.model_validate(
                {
                    "in": 255,
                    "inversed": 1 << bit_index,
                    "out": 255,
                    # Deliberately opposite to ensure QL6C DG is NOT read from `other`.
                    "other": 255 ^ (1 << bit_index),
                }
            )
            decoded = self.decoder.decode_board_payload(payload=payload, topic="puma_board", updated_at=self.now)
            by_logical = {item.logicalChannel: item for item in decoded}
            self.assertEqual(by_logical[logical_channel].diagnostic, 1)
            self.assertEqual(sum(item.diagnostic for item in decoded), 1)

    def test_out_mapping_reversed_bit0_to_d_bit7_to_6(self) -> None:
        for bit_index, logical_channel in enumerate(self.logical_order_out_reversed):
            payload = BoardPayload.model_validate(
                {
                    "in": 255,
                    "inversed": 0,
                    "out": 1 << bit_index,
                    "other": 0,
                }
            )
            decoded = self.decoder.decode_board_payload(payload=payload, topic="puma_board", updated_at=self.now)
            by_logical = {item.logicalChannel: item for item in decoded}
            self.assertEqual(by_logical[logical_channel].output, 1)
            self.assertEqual(sum(item.output for item in decoded), 1)


class DecoderTruthTableTest(unittest.TestCase):
    def test_truth_table(self) -> None:
        cases = [
            ((0, 0, 1), ("normal_off", "Норма", None, "Ключ выключен", False)),
            ((1, 1, 1), ("normal_on", "Норма", None, "Ключ включен", False)),
            ((0, 1, 0), ("fault_break", "Обрыв", "break", "Обрыв", True)),
            ((1, 1, 0), ("fault_break", "Обрыв", "break", "Обрыв", True)),
            ((1, 0, 0), ("fault_short", "КЗ", "short", "Короткое замыкание", True)),
            ((0, 0, 0), ("unknown", "Неизвестно", None, "Неизвестная комбинация сигналов", False)),
        ]

        for inputs, expected in cases:
            with self.subTest(inputs=inputs):
                result = decode_triplet(*inputs)
                self.assertEqual(result.status, expected[0])
                self.assertEqual(result.status_label, expected[1])
                self.assertEqual(result.fault_type, expected[2])
                self.assertEqual(result.state_label, expected[3])
                self.assertEqual(result.fault, expected[4])


class DecodePayloadE2ETest(unittest.TestCase):
    def setUp(self) -> None:
        self.decoder = build_decoder()
        self.now = datetime.now(timezone.utc)

    def _decode(self, *, in_value: int, inversed_value: int, out_value: int, other_value: int) -> dict[str, object]:
        payload = BoardPayload.model_validate(
            {
                "in": in_value,
                "inversed": inversed_value,
                "out": out_value,
                "other": other_value,
            }
        )
        decoded = self.decoder.decode_board_payload(payload=payload, topic="puma_board", updated_at=self.now)
        by_logical = {item.logicalChannel: item for item in decoded}
        return {"decoded": decoded, "by_logical": by_logical}

    def test_full_decode_example_in5_inversed251_out160(self) -> None:
        result = self._decode(in_value=5, inversed_value=251, out_value=160, other_value=0)
        decoded = result["decoded"]
        by_logical = result["by_logical"]

        assert isinstance(decoded, list)
        assert isinstance(by_logical, dict)
        self.assertEqual(len(decoded), 8)

        expected_keys = [f"QL6C{index}" for index in range(8)]
        self.assertEqual([item.channelKey for item in decoded], expected_keys)
        self.assertEqual([item.channelIndex for item in decoded], list(range(8)))
        self.assertEqual([item.logicalChannel for item in decoded], ["6", "7", "8", "9", "A", "B", "C", "D"])

        for item in decoded:
            self.assertEqual(item.board, "B31")
            self.assertEqual(item.unit, "U15")
            self.assertEqual(item.module, "QL6C")
            self.assertEqual(item.updatedAt, self.now)

        expected_status_by_channel = {
            "6": "normal_on",
            "7": "normal_off",
            "8": "fault_break",
            "9": "normal_off",
            "A": "normal_off",
            "B": "normal_off",
            "C": "normal_off",
            "D": "normal_off",
        }
        self.assertEqual(
            {channel: by_logical[channel].status for channel in expected_status_by_channel},
            expected_status_by_channel,
        )

        self.assertEqual(by_logical["6"].statusLabel, "Норма")
        self.assertEqual(by_logical["6"].stateLabel, "Ключ включен")
        self.assertEqual(by_logical["6"].faultType, None)
        self.assertEqual(by_logical["6"].rawBits, {"in": 1, "out": 1, "dg": 1})
        self.assertEqual(by_logical["6"].message, "крюк слева выдвинуть")

        self.assertEqual(by_logical["8"].statusLabel, "Обрыв")
        self.assertEqual(by_logical["8"].faultType, "break")
        self.assertEqual(by_logical["8"].rawBits, {"in": 1, "out": 1, "dg": 0})
        self.assertEqual(by_logical["8"].message, "Обрыв")

    def test_full_decode_example_in5_inversed251_out128(self) -> None:
        result = self._decode(in_value=5, inversed_value=251, out_value=128, other_value=255)
        decoded = result["decoded"]
        by_logical = result["by_logical"]

        assert isinstance(decoded, list)
        assert isinstance(by_logical, dict)
        self.assertEqual(len(decoded), 8)

        expected_status_by_channel = {
            "6": "normal_on",
            "7": "normal_off",
            "8": "fault_short",
            "9": "normal_off",
            "A": "normal_off",
            "B": "normal_off",
            "C": "normal_off",
            "D": "normal_off",
        }
        self.assertEqual(
            {channel: by_logical[channel].status for channel in expected_status_by_channel},
            expected_status_by_channel,
        )

        self.assertEqual(by_logical["6"].statusLabel, "Норма")
        self.assertEqual(by_logical["6"].rawBits, {"in": 1, "out": 1, "dg": 1})
        self.assertEqual(by_logical["6"].message, "крюк слева выдвинуть")

        self.assertEqual(by_logical["8"].statusLabel, "КЗ")
        self.assertEqual(by_logical["8"].stateLabel, "Короткое замыкание")
        self.assertEqual(by_logical["8"].faultType, "short")
        self.assertEqual(by_logical["8"].rawBits, {"in": 1, "out": 0, "dg": 0})
        self.assertEqual(by_logical["8"].message, "КЗ")


if __name__ == "__main__":
    unittest.main()
