from datetime import datetime, timezone
import unittest

from app.models import BoardPayload, ChannelConfig
from app.services.decoder import DecoderService, decode_channel
from app.utils import extract_bit, extract_bit_from_bytes, normalize_channel_index


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


class DecoderTruthTableTest(unittest.TestCase):
    def test_truth_table(self) -> None:
        cases = [
            ((0, 0, 1), ("normal", "Норма", "Ключ выключен", False, "info")),
            ((1, 1, 1), ("normal", "Норма", "Ключ включен", False, "info")),
            ((0, 1, 0), ("open_circuit", "Обрыв", "Обрыв цепи", True, "error")),
            ((1, 1, 0), ("open_circuit", "Обрыв", "Обрыв цепи", True, "error")),
            (
                (1, 0, 0),
                (
                    "short_circuit",
                    "КЗ",
                    "Короткое замыкание / авария / термозащита",
                    True,
                    "error",
                ),
            ),
            (
                (0, 0, 0),
                ("unknown", "Неизвестно", "Неизвестная комбинация сигналов", False, "warning"),
            ),
        ]

        for inputs, expected in cases:
            with self.subTest(inputs=inputs):
                result = decode_channel(*inputs)
                self.assertEqual(result.status, expected[0])
                self.assertEqual(result.state_label, expected[1])
                self.assertEqual(result.description, expected[2])
                self.assertEqual(result.is_fault, expected[3])
                self.assertEqual(result.severity, expected[4])


class BitExtractionTest(unittest.TestCase):
    def test_extract_bit_single_byte(self) -> None:
        self.assertEqual(extract_bit(0b10110010, 0), 0)
        self.assertEqual(extract_bit(0b10110010, 1), 1)
        self.assertEqual(extract_bit(0b10110010, 7), 1)
        self.assertEqual(extract_bit(0b10110010, 8), 0)

    def test_extract_bit_with_hex_channel_index(self) -> None:
        self.assertEqual(normalize_channel_index("A"), 10)
        self.assertEqual(extract_bit_from_bytes([0b00000000, 0b00000100], "A"), 1)
        self.assertEqual(extract_bit_from_bytes([0b11111111], "F"), 0)


class DecodedChannelsBuildTest(unittest.TestCase):
    def test_builds_decoded_channels_for_known_signals(self) -> None:
        signal_map = [
            ChannelConfig.model_validate(
                {
                    "channelKey": "QL6C0",
                    "channelIndex": 0,
                    "signalId": "1s201a",
                    "purpose": "подъем ПРУ слева",
                    "board": "B31/U15",
                    "module": "QL6C",
                    "sourceTopic": "puma_board",
                }
            ),
            ChannelConfig.model_validate(
                {
                    "channelKey": "QL6C1",
                    "channelIndex": 1,
                    "signalId": "1s201b",
                    "purpose": "опускание ПРУ слева",
                    "board": "B31/U15",
                    "module": "QL6C",
                    "sourceTopic": "puma_board",
                }
            ),
            ChannelConfig.model_validate(
                {
                    "channelKey": "QL6C2",
                    "channelIndex": 2,
                    "signalId": "1s202a",
                    "purpose": "подъем ПРУ справа",
                    "board": "B31/U15",
                    "module": "QL6C",
                    "sourceTopic": "puma_board",
                }
            ),
        ]
        decoder = DecoderService(signal_map=signal_map, event_texts={})
        payload = BoardPayload.model_validate({"in": 0b00000110, "inversed": 0b00000001, "out": 0b00000010, "other": 99})
        now = datetime.now(timezone.utc)

        decoded = decoder.decode_board_payload(payload=payload, topic="puma_board", updated_at=now)

        self.assertEqual(len(decoded), 3)
        by_key = {item.channelKey: item for item in decoded}

        self.assertEqual(by_key["QL6C0"].status, "normal")
        self.assertEqual(by_key["QL6C0"].message, "Ключ выключен")
        self.assertIsNone(by_key["QL6C0"].cause)
        self.assertIsNone(by_key["QL6C0"].action)

        self.assertEqual(by_key["QL6C1"].status, "open_circuit")
        self.assertEqual(by_key["QL6C1"].cause, "Обрыв цепи гидрораспределителя 1s201b")
        self.assertEqual(
            by_key["QL6C1"].action,
            "Проверить фишку гидрораспределителя и выполнить визуальный осмотр электропроводки цепи",
        )

        self.assertEqual(by_key["QL6C2"].status, "short_circuit")
        self.assertEqual(by_key["QL6C2"].unit, "U15")
        self.assertEqual(by_key["QL6C2"].topic, "puma_board")
        self.assertEqual(by_key["QL6C2"].updatedAt, now)


if __name__ == "__main__":
    unittest.main()
