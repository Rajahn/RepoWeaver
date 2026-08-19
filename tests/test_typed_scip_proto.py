"""Byte-level tests for the hand-rolled SCIP protobuf decoder.

Each test hand-encodes the wire bytes for one scip.proto message shape
(varint, length-delimited, packed repeated int32, negative int32 fold-back)
so the decoder is verified against the wire format directly rather than only
through the higher-level fixture pipeline.
"""

from __future__ import annotations

from codecontextfabric.typed.scip_proto import (
    ROLE_DEFINITION,
    decode_index,
    normalize_range,
    read_varint,
)


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        b = value & 0x7F
        value >>= 7
        if value:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _tag(field_no: int, wire_type: int) -> bytes:
    return _varint((field_no << 3) | wire_type)


def _string_field(field_no: int, value: str) -> bytes:
    encoded = value.encode("utf-8")
    return _tag(field_no, 2) + _varint(len(encoded)) + encoded


def _message_field(field_no: int, payload: bytes) -> bytes:
    return _tag(field_no, 2) + _varint(len(payload)) + payload


def _packed_int32_field(field_no: int, values: list[int]) -> bytes:
    payload = b"".join(_varint(v) for v in values)
    return _tag(field_no, 2) + _varint(len(payload)) + payload


def test_read_varint_multi_byte() -> None:
    # 300 = 0b100101100 -> low 7 bits 0101100 with continuation, then 0b10
    assert read_varint(bytes([0xAC, 0x02]), 0) == (300, 2)


def test_read_varint_single_byte() -> None:
    assert read_varint(bytes([0x7F]), 0) == (127, 1)


def test_normalize_range_three_element_same_line() -> None:
    assert normalize_range([3, 11, 15]) == (3, 11, 3, 15)


def test_normalize_range_four_element_multiline() -> None:
    assert normalize_range([2, 0, 13, 1]) == (2, 0, 13, 1)


def test_packed_repeated_range_decodes_in_order() -> None:
    occ_payload = _packed_int32_field(1, [10, 4, 10, 9]) + _string_field(2, "sym#")
    doc_payload = _string_field(1, "A.java") + _message_field(2, occ_payload)
    index_payload = _message_field(2, doc_payload)

    index = decode_index(index_payload)
    assert len(index.documents) == 1
    doc = index.documents[0]
    assert doc.relative_path == "A.java"
    assert len(doc.occurrences) == 1
    occ = doc.occurrences[0]
    assert occ.range == [10, 4, 10, 9]
    assert occ.symbol == "sym#"
    assert not occ.is_definition


def test_symbol_role_definition_bit_set() -> None:
    occ_payload = (
        _packed_int32_field(1, [1, 0, 5])
        + _string_field(2, "sym#")
        + _tag(3, 0)
        + _varint(ROLE_DEFINITION)
    )
    doc_payload = _string_field(1, "A.java") + _message_field(2, occ_payload)
    index = decode_index(_message_field(2, doc_payload))
    occ = index.documents[0].occurrences[0]
    assert occ.is_definition
    assert occ.symbol_roles == ROLE_DEFINITION


def test_symbol_role_reference_bit_unset() -> None:
    occ_payload = (
        _packed_int32_field(1, [1, 0, 5])
        + _string_field(2, "sym#")
        + _tag(3, 0)
        + _varint(0)
    )
    doc_payload = _string_field(1, "A.java") + _message_field(2, occ_payload)
    index = decode_index(_message_field(2, doc_payload))
    assert not index.documents[0].occurrences[0].is_definition


def test_enclosing_range_field_seven() -> None:
    occ_payload = (
        _packed_int32_field(1, [4, 8, 16])
        + _string_field(2, "owner#method().")
        + _tag(3, 0)
        + _varint(ROLE_DEFINITION)
        + _packed_int32_field(7, [4, 0, 6, 1])
    )
    doc_payload = _string_field(1, "A.java") + _message_field(2, occ_payload)
    index = decode_index(_message_field(2, doc_payload))
    occ = index.documents[0].occurrences[0]
    assert occ.enclosing_range == [4, 0, 6, 1]
    assert occ.normalized_enclosing_range() == (4, 0, 6, 1)


def test_unpacked_repeated_int32_also_decodes() -> None:
    # A conformant writer may emit `repeated int32` unpacked (one varint-wire
    # entry per element) — the decoder must fold both encodings together.
    unpacked_range = b"".join(_tag(1, 0) + _varint(v) for v in [7, 2, 9])
    occ_payload = unpacked_range + _string_field(2, "sym#")
    doc_payload = _string_field(1, "A.java") + _message_field(2, occ_payload)
    index = decode_index(_message_field(2, doc_payload))
    assert index.documents[0].occurrences[0].range == [7, 2, 9]


def test_metadata_tool_name_and_project_root() -> None:
    tool_info = _string_field(1, "gen_deterministic_scip_fixture")
    metadata_payload = _string_field(3, ".") + _message_field(2, tool_info)
    index_payload = _message_field(1, metadata_payload)
    index = decode_index(index_payload)
    assert index.metadata.tool_name == "gen_deterministic_scip_fixture"
    assert index.metadata.project_root == "."


def test_unknown_field_number_is_skipped_not_fatal() -> None:
    doc_payload = _string_field(1, "A.java") + _string_field(99, "unknown-future-field")
    index = decode_index(_message_field(2, doc_payload))
    assert index.documents[0].relative_path == "A.java"
