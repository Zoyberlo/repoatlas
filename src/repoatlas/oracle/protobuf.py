"""A minimal, dependency-free protobuf wire-format reader and writer.

SCIP indexes ship as binary protobuf. Depending on ``protobuf`` plus a
``protoc`` build step would put a compiler in the way of running the test
suite, so RepoAtlas decodes the wire format directly. The wire format is
tiny and frozen, unlike the SCIP schema itself, which is why field numbers
live in :mod:`repoatlas.oracle.scip` and only the encoding lives here.

Reference: https://protobuf.dev/programming-guides/encoding/

The writer exists so tests can build fixtures byte-for-byte instead of
committing binaries whose provenance a reader cannot check.
"""

from __future__ import annotations

import enum
import struct
from collections.abc import Iterator

__all__ = [
    "ProtobufError",
    "WireType",
    "as_bytes",
    "as_int",
    "as_str",
    "decode_message",
    "encode_field",
    "encode_message",
    "encode_varint",
    "packed_varints",
    "read_varint",
]


class ProtobufError(ValueError):
    """Raised when a byte stream is not valid protobuf."""


class WireType(enum.IntEnum):
    VARINT = 0
    FIXED64 = 1
    LENGTH_DELIMITED = 2
    START_GROUP = 3  # deprecated, rejected
    END_GROUP = 4  # deprecated, rejected
    FIXED32 = 5


# A decoded message maps a field number to every value seen for it, in order.
# Repeated fields therefore need no schema knowledge to decode.
Fields = dict[int, list[object]]

_MAX_VARINT_BYTES = 10  # 64-bit varints never exceed ten groups of seven bits


def read_varint(data: bytes, pos: int) -> tuple[int, int]:
    """Read one base-128 varint, returning ``(value, next_position)``."""
    result = 0
    shift = 0
    start = pos
    while True:
        if pos >= len(data):
            raise ProtobufError(f"truncated varint starting at offset {start}")
        if pos - start >= _MAX_VARINT_BYTES:
            raise ProtobufError(f"varint at offset {start} exceeds 64 bits")
        byte = data[pos]
        result |= (byte & 0x7F) << shift
        pos += 1
        if not byte & 0x80:
            return result, pos
        shift += 7


def decode_message(data: bytes) -> Fields:
    """Decode one protobuf message into ``{field_number: [values]}``.

    Varints and fixed-width fields decode to ``int``; length-delimited
    fields stay as ``bytes`` because only the schema knows whether they hold
    a string, a nested message or a packed array.
    """
    fields: Fields = {}
    pos = 0
    size = len(data)
    while pos < size:
        key, pos = read_varint(data, pos)
        field_number = key >> 3
        try:
            wire_type = WireType(key & 0x07)
        except ValueError:
            raise ProtobufError(f"unknown wire type {key & 0x07} at offset {pos}") from None
        if field_number == 0:
            raise ProtobufError(f"field number 0 is not valid, at offset {pos}")

        value: object
        if wire_type is WireType.VARINT:
            value, pos = read_varint(data, pos)
        elif wire_type is WireType.FIXED64:
            if pos + 8 > size:
                raise ProtobufError(f"truncated fixed64 at offset {pos}")
            value = struct.unpack_from("<Q", data, pos)[0]
            pos += 8
        elif wire_type is WireType.FIXED32:
            if pos + 4 > size:
                raise ProtobufError(f"truncated fixed32 at offset {pos}")
            value = struct.unpack_from("<I", data, pos)[0]
            pos += 4
        elif wire_type is WireType.LENGTH_DELIMITED:
            length, pos = read_varint(data, pos)
            end = pos + length
            if end > size:
                raise ProtobufError(
                    f"length-delimited field {field_number} claims {length} bytes "
                    f"but only {size - pos} remain"
                )
            value = data[pos:end]
            pos = end
        else:
            raise ProtobufError(
                f"group wire type {wire_type.name} is not supported (field {field_number})"
            )
        fields.setdefault(field_number, []).append(value)
    return fields


def packed_varints(payload: bytes) -> list[int]:
    """Decode a packed repeated varint field."""
    values: list[int] = []
    pos = 0
    while pos < len(payload):
        value, pos = read_varint(payload, pos)
        values.append(value)
    return values


def _zigzag_decode(value: int) -> int:
    return (value >> 1) ^ -(value & 1)


def as_int(fields: Fields, number: int, default: int = 0, *, signed: bool = False) -> int:
    """Read a scalar integer field, tolerating a missing value."""
    values = fields.get(number)
    if not values:
        return default
    raw = values[-1]
    if not isinstance(raw, int):
        raise ProtobufError(f"field {number} is not an integer: {type(raw).__name__}")
    return _zigzag_decode(raw) if signed else raw


def as_bytes(fields: Fields, number: int, default: bytes = b"") -> bytes:
    values = fields.get(number)
    if not values:
        return default
    raw = values[-1]
    if not isinstance(raw, bytes):
        raise ProtobufError(f"field {number} is not length-delimited")
    return raw


def as_str(fields: Fields, number: int, default: str = "") -> str:
    values = fields.get(number)
    if not values:
        return default
    raw = values[-1]
    if not isinstance(raw, bytes):
        raise ProtobufError(f"field {number} is not length-delimited")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProtobufError(f"field {number} is not valid UTF-8") from exc


def submessages(fields: Fields, number: int) -> Iterator[Fields]:
    """Decode every occurrence of a repeated nested message field."""
    for raw in fields.get(number, []):
        if not isinstance(raw, bytes):
            raise ProtobufError(f"field {number} is not length-delimited")
        yield decode_message(raw)


def strings(fields: Fields, number: int) -> list[str]:
    """Decode every occurrence of a repeated string field."""
    out: list[str] = []
    for raw in fields.get(number, []):
        if not isinstance(raw, bytes):
            raise ProtobufError(f"field {number} is not length-delimited")
        out.append(raw.decode("utf-8"))
    return out


def int_array(fields: Fields, number: int) -> list[int]:
    """Decode a repeated int32 field written either packed or unpacked."""
    out: list[int] = []
    for raw in fields.get(number, []):
        if isinstance(raw, int):
            out.append(raw)
        elif isinstance(raw, bytes):
            out.extend(packed_varints(raw))
        else:
            raise ProtobufError(f"field {number} holds {type(raw).__name__}")
    return out


# --- writing, used to build test fixtures --------------------------------


def encode_varint(value: int) -> bytes:
    if value < 0:
        raise ValueError("negative values need zigzag or fixed encoding")
    out = bytearray()
    while True:
        chunk = value & 0x7F
        value >>= 7
        if value:
            out.append(chunk | 0x80)
        else:
            out.append(chunk)
            return bytes(out)


def encode_field(number: int, value: bytes | int | str) -> bytes:
    """Encode one field, picking the wire type from the Python type."""
    if number <= 0:
        raise ValueError("field numbers start at 1")
    if isinstance(value, bool):
        raise TypeError("pass bools as int to make the wire type explicit")
    if isinstance(value, int):
        return encode_varint(number << 3 | WireType.VARINT) + encode_varint(value)
    payload = value.encode("utf-8") if isinstance(value, str) else value
    return (
        encode_varint(number << 3 | WireType.LENGTH_DELIMITED)
        + encode_varint(len(payload))
        + payload
    )


def encode_packed(number: int, values: list[int]) -> bytes:
    payload = b"".join(encode_varint(v) for v in values)
    return (
        encode_varint(number << 3 | WireType.LENGTH_DELIMITED)
        + encode_varint(len(payload))
        + payload
    )


def encode_message(parts: list[tuple[int, bytes | int | str]]) -> bytes:
    """Encode a whole message from ``(field_number, value)`` pairs."""
    return b"".join(encode_field(number, value) for number, value in parts)
