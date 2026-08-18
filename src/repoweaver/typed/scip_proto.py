"""Dependency-free protobuf wire-format decoder for SCIP (scip.proto) indexes.

Only the messages RepoWeaver's typed overlay needs are decoded: Index,
Metadata, Document, Occurrence, SymbolInformation. Field numbers below mirror
the public scip.proto (https://github.com/sourcegraph/scip) wire layout —
this is a hand-rolled subset decoder, not a general protobuf library: it
understands varint / length-delimited wire types and both packed and
unpacked repeated scalars, which is all proto3 ever emits for these messages.

Unknown field numbers are skipped (forward-compatible with newer scip.proto
fields we don't consume) rather than raising.
"""

from __future__ import annotations

from dataclasses import dataclass, field

_WIRE_VARINT = 0
_WIRE_64BIT = 1
_WIRE_LENGTH_DELIMITED = 2
_WIRE_32BIT = 5


def read_varint(data: bytes, pos: int) -> tuple[int, int]:
    """Decode one base-128 varint starting at `pos`. Returns (value, next_pos)."""
    result = 0
    shift = 0
    while True:
        if pos >= len(data):
            raise ValueError("truncated varint")
        b = data[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, pos
        shift += 7


def _varint_to_signed32(value: int) -> int:
    """Two's-complement fold-back for varint-encoded int32 (proto3 `int32` is
    NOT zigzag-encoded — negative values are sign-extended to 64 bits by the
    writer, so a varint that decodes to a value >= 2**63 represents a
    negative int32). SCIP never emits negative range/role values in practice,
    but this keeps the decoder correct rather than merely lucky."""
    if value >= 1 << 63:
        value -= 1 << 64
    if value > 0x7FFFFFFF or value < -0x80000000:
        value &= 0xFFFFFFFF
        if value >= 0x80000000:
            value -= 0x100000000
    return value


def iter_fields(data: bytes):
    """Yield (field_number, wire_type, raw_value) for each top-level field in
    one embedded message's bytes. raw_value is an int for varint/32/64-bit
    wire types, or a bytes slice for length-delimited fields."""
    pos = 0
    n = len(data)
    while pos < n:
        tag, pos = read_varint(data, pos)
        field_no = tag >> 3
        wire_type = tag & 0x7
        if wire_type == _WIRE_VARINT:
            value, pos = read_varint(data, pos)
            yield field_no, wire_type, value
        elif wire_type == _WIRE_LENGTH_DELIMITED:
            length, pos = read_varint(data, pos)
            value = data[pos : pos + length]
            if len(value) != length:
                raise ValueError(f"truncated length-delimited field {field_no}")
            pos += length
            yield field_no, wire_type, value
        elif wire_type == _WIRE_64BIT:
            value = data[pos : pos + 8]
            pos += 8
            yield field_no, wire_type, value
        elif wire_type == _WIRE_32BIT:
            value = data[pos : pos + 4]
            pos += 4
            yield field_no, wire_type, value
        else:
            raise ValueError(f"unsupported wire type {wire_type} for field {field_no}")


def _packed_varints(raw: bytes) -> list[int]:
    """Decode a length-delimited byte string as a run of packed varints —
    proto3's default encoding for `repeated int32`/`repeated bool` fields."""
    out = []
    pos = 0
    while pos < len(raw):
        value, pos = read_varint(raw, pos)
        out.append(_varint_to_signed32(value))
    return out


class _RawMessage:
    """Field-number -> list of raw (wire_type, value) entries, with typed
    accessors that fold together packed and unpacked repeated encodings."""

    def __init__(self, data: bytes) -> None:
        self.entries: dict[int, list[tuple[int, object]]] = {}
        for field_no, wire_type, value in iter_fields(data):
            self.entries.setdefault(field_no, []).append((wire_type, value))

    def get_string(self, field_no: int, default: str = "") -> str:
        for wire_type, value in self.entries.get(field_no, []):
            if wire_type == _WIRE_LENGTH_DELIMITED:
                return value.decode("utf-8", errors="replace")
        return default

    def get_repeated_strings(self, field_no: int) -> list[str]:
        return [
            value.decode("utf-8", errors="replace")
            for wire_type, value in self.entries.get(field_no, [])
            if wire_type == _WIRE_LENGTH_DELIMITED
        ]

    def get_varint(self, field_no: int, default: int = 0) -> int:
        for wire_type, value in self.entries.get(field_no, []):
            if wire_type == _WIRE_VARINT:
                return _varint_to_signed32(value)
        return default

    def get_repeated_int32(self, field_no: int) -> list[int]:
        """Handles both packed (single length-delimited entry containing many
        varints) and unpacked (one varint-wire-type entry per element)
        encodings — a proto3 writer only ever produces one or the other for
        a given field, but never mixes them within one message instance."""
        out: list[int] = []
        for wire_type, value in self.entries.get(field_no, []):
            if wire_type == _WIRE_LENGTH_DELIMITED:
                out.extend(_packed_varints(value))
            elif wire_type == _WIRE_VARINT:
                out.append(_varint_to_signed32(value))
        return out

    def get_repeated_messages(self, field_no: int) -> list[bytes]:
        return [
            value
            for wire_type, value in self.entries.get(field_no, [])
            if wire_type == _WIRE_LENGTH_DELIMITED
        ]

    def get_message(self, field_no: int) -> bytes | None:
        msgs = self.get_repeated_messages(field_no)
        return msgs[0] if msgs else None


# SymbolRole bitmask (scip.proto `SymbolRole` enum) — only the bit RepoWeaver
# needs to tell a definition occurrence from a reference occurrence.
ROLE_DEFINITION = 0x1


@dataclass
class Occurrence:
    range: list[
        int
    ]  # [startLine, startChar, endChar] or [startLine, startChar, endLine, endChar], 0-based
    symbol: str
    symbol_roles: int
    enclosing_range: list[int] = field(default_factory=list)

    @property
    def is_definition(self) -> bool:
        return bool(self.symbol_roles & ROLE_DEFINITION)

    def normalized_range(self) -> tuple[int, int, int, int]:
        """(start_line, start_col, end_line, end_col), all 0-based."""
        return normalize_range(self.range)

    def normalized_enclosing_range(self) -> tuple[int, int, int, int]:
        """Enclosing range if present, else falls back to `range` itself."""
        return normalize_range(self.enclosing_range or self.range)


@dataclass
class SymbolInformation:
    symbol: str
    display_name: str = ""
    kind: int = 0


@dataclass
class Document:
    relative_path: str
    occurrences: list[Occurrence] = field(default_factory=list)
    symbols: list[SymbolInformation] = field(default_factory=list)
    language: str = ""


@dataclass
class Metadata:
    project_root: str = ""
    tool_name: str = ""


@dataclass
class Index:
    metadata: Metadata
    documents: list[Document] = field(default_factory=list)
    external_symbols: list[SymbolInformation] = field(default_factory=list)


def normalize_range(range_: list[int]) -> tuple[int, int, int, int]:
    """SCIP ranges are 3 elements `[startLine, startChar, endChar]` (same
    line) or 4 elements `[startLine, startChar, endLine, endChar]`."""
    if len(range_) == 3:
        start_line, start_char, end_char = range_
        return start_line, start_char, start_line, end_char
    if len(range_) == 4:
        return range_[0], range_[1], range_[2], range_[3]
    raise ValueError(
        f"SCIP range must have 3 or 4 elements, got {len(range_)}: {range_}"
    )


def _decode_occurrence(raw: bytes) -> Occurrence:
    msg = _RawMessage(raw)
    return Occurrence(
        range=msg.get_repeated_int32(1),
        symbol=msg.get_string(2),
        symbol_roles=msg.get_varint(3),
        enclosing_range=msg.get_repeated_int32(7),
    )


def _decode_symbol_information(raw: bytes) -> SymbolInformation:
    msg = _RawMessage(raw)
    return SymbolInformation(
        symbol=msg.get_string(1),
        display_name=msg.get_string(6),
        kind=msg.get_varint(5),
    )


def _decode_document(raw: bytes) -> Document:
    msg = _RawMessage(raw)
    return Document(
        relative_path=msg.get_string(1),
        occurrences=[_decode_occurrence(o) for o in msg.get_repeated_messages(2)],
        symbols=[_decode_symbol_information(s) for s in msg.get_repeated_messages(3)],
        language=msg.get_string(4),
    )


def _decode_metadata(raw: bytes) -> Metadata:
    msg = _RawMessage(raw)
    tool_info_raw = msg.get_message(2)
    tool_name = _RawMessage(tool_info_raw).get_string(1) if tool_info_raw else ""
    return Metadata(project_root=msg.get_string(3), tool_name=tool_name)


def decode_index(data: bytes) -> Index:
    """Decode a full `scip.Index` message from its serialized bytes."""
    msg = _RawMessage(data)
    metadata_raw = msg.get_message(1)
    metadata = _decode_metadata(metadata_raw) if metadata_raw else Metadata()
    return Index(
        metadata=metadata,
        documents=[_decode_document(d) for d in msg.get_repeated_messages(2)],
        external_symbols=[
            _decode_symbol_information(s) for s in msg.get_repeated_messages(3)
        ],
    )
