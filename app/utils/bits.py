from collections.abc import Sequence


def unpack_bits(value: int, width: int = 8) -> dict[int, int]:
    """Unpack an integer bitmask into a 1-based bit dictionary.

    Rule:
    - `bit1` is the least-significant bit (LSB).
    - `bitN` is `(value >> (N-1)) & 1`.
    """
    normalized_width = int(width)
    if normalized_width <= 0:
        return {}

    normalized_value = int(value)
    if normalized_value < 0:
        normalized_value &= (1 << normalized_width) - 1

    return {
        bit_index + 1: (normalized_value >> bit_index) & 1
        for bit_index in range(normalized_width)
    }


def normalize_channel_index(channel_index: int | str) -> int:
    if isinstance(channel_index, int):
        return channel_index

    token = str(channel_index).strip()
    if not token:
        raise ValueError("channel index is empty")

    lowered = token.lower()
    if lowered.startswith("0x"):
        return int(lowered, 16)
    if len(lowered) == 1 and lowered in "abcdef":
        return int(lowered, 16)
    if any(char in "abcdef" for char in lowered):
        return int(lowered, 16)
    return int(lowered, 10)


def extract_bit(
    byte_value: int,
    channel_index: int | str,
    *,
    byte_offset: int = 0,
    bits_per_byte: int = 8,
) -> int:
    index = normalize_channel_index(channel_index) - int(byte_offset)
    if index < 0 or index >= bits_per_byte:
        return 0
    normalized = int(byte_value) & ((1 << bits_per_byte) - 1)
    return (normalized >> index) & 1


def extract_bit_from_bytes(
    byte_values: Sequence[int],
    channel_index: int | str,
    *,
    bits_per_byte: int = 8,
) -> int:
    index = normalize_channel_index(channel_index)
    if index < 0:
        return 0
    byte_index = index // bits_per_byte
    if byte_index < 0 or byte_index >= len(byte_values):
        return 0
    bit_index = index % bits_per_byte
    return extract_bit(byte_values[byte_index], bit_index, bits_per_byte=bits_per_byte)
