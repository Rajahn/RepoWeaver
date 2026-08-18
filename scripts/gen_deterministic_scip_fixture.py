#!/usr/bin/env python3
"""Deterministic .scip fixture generator for tests/fixtures/m3typed.

Used as a fallback by scripts/build_scip_fixture.sh when a real scip-java
binary isn't available (e.g. no network access to GitHub Releases / Maven
Central in this environment). Hand-encodes protobuf wire bytes matching the
scip.proto field numbers documented in src/repoweaver/typed/scip_proto.py,
so it exercises the exact same wire format a real scip-java run would
produce for this fixture's five source files — see
docs/adr/0003-typed-overlay.md for the field-number mapping and the
line/column offsets below, which were hand-verified against the fixture
source under tests/fixtures/m3typed/com/example/m3typed/.

No third-party dependencies; pure varint/length-delimited protobuf encoding.
"""

from __future__ import annotations

from pathlib import Path

ROLE_DEFINITION = 0x1
PKG = "com.example.m3typed"
COORD = ("maven", "com.example:m3typed", "0.1.0")


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


def symbol(descriptor_suffix: str) -> str:
    manager, name, version = COORD
    return f"semanticdb {manager} {name} {version} {descriptor_suffix}"


def _occurrence(
    range_: list[int], sym: str, is_definition: bool, enclosing: list[int] | None = None
) -> bytes:
    payload = _packed_int32_field(1, range_)
    payload += _string_field(2, sym)
    if is_definition:
        payload += _tag(3, 0) + _varint(ROLE_DEFINITION)
    if enclosing:
        payload += _packed_int32_field(7, enclosing)
    return payload


def _document(relative_path: str, occurrences: list[bytes]) -> bytes:
    payload = _string_field(1, relative_path)
    for occ in occurrences:
        payload += _message_field(2, occ)
    payload += _string_field(4, "java")
    return payload


def build_index() -> bytes:
    pkg = f"{PKG.replace('.', '/')}/"

    shape_occs = [
        _occurrence([2, 18, 23], symbol(f"{pkg}Shape#"), True, [2, 0, 4, 1]),
        _occurrence([3, 11, 15], symbol(f"{pkg}Shape#area()."), True),
    ]

    circle_occs = [
        _occurrence([2, 13, 19], symbol(f"{pkg}Circle#"), True, [2, 0, 13, 1]),
        _occurrence([2, 31, 36], symbol(f"{pkg}Shape#"), False),
        _occurrence([5, 11, 17], symbol(f"{pkg}Circle#<init>(D)."), True, [5, 0, 7, 5]),
        _occurrence([10, 18, 22], symbol(f"{pkg}Circle#area()."), True, [10, 0, 12, 5]),
    ]

    square_occs = [
        _occurrence([2, 13, 19], symbol(f"{pkg}Square#"), True, [2, 0, 13, 1]),
        _occurrence([2, 31, 36], symbol(f"{pkg}Shape#"), False),
        _occurrence([5, 11, 17], symbol(f"{pkg}Square#<init>(D)."), True, [5, 0, 7, 5]),
        _occurrence([10, 18, 22], symbol(f"{pkg}Square#area()."), True, [10, 0, 12, 5]),
    ]

    box_occs = [
        _occurrence([2, 13, 16], symbol(f"{pkg}Box#"), True, [2, 0, 12, 1]),
        _occurrence(
            [5, 11, 14],
            symbol(f"{pkg}Box#<init>(Ljava/lang/Object;)."),
            True,
            [5, 0, 7, 5],
        ),
        _occurrence(
            [9, 13, 21],
            symbol(f"{pkg}Box#identity(Ljava/lang/Object;)."),
            True,
            [9, 0, 11, 5],
        ),
    ]

    dispatch_sym = symbol(f"{pkg}Caller#dispatch(Lcom/example/m3typed/Shape;).")
    process_int_sym = symbol(f"{pkg}Caller#process(I).")
    process_str_sym = symbol(f"{pkg}Caller#process(Ljava/lang/String;).")
    caller_occs = [
        _occurrence([2, 13, 19], symbol(f"{pkg}Caller#"), True, [2, 0, 25, 1]),
        _occurrence([3, 9, 17], dispatch_sym, True, [3, 0, 5, 5]),
        _occurrence([4, 14, 18], symbol(f"{pkg}Shape#area()."), False),
        _occurrence([7, 9, 16], process_int_sym, True, [7, 0, 9, 5]),
        _occurrence([11, 9, 16], process_str_sym, True, [11, 0, 13, 5]),
        _occurrence([15, 9, 12], symbol(f"{pkg}Caller#run()."), True, [15, 0, 24, 5]),
        _occurrence([16, 29, 35], symbol(f"{pkg}Circle#<init>(D)."), False),
        _occurrence([17, 28, 34], symbol(f"{pkg}Square#<init>(D)."), False),
        _occurrence([18, 8, 16], dispatch_sym, False),
        _occurrence([19, 8, 16], dispatch_sym, False),
        _occurrence([20, 8, 15], process_int_sym, False),
        _occurrence([21, 8, 15], process_str_sym, False),
        _occurrence(
            [22, 30, 33], symbol(f"{pkg}Box#<init>(Ljava/lang/Object;)."), False
        ),
        _occurrence(
            [23, 12, 20], symbol(f"{pkg}Box#identity(Ljava/lang/Object;)."), False
        ),
    ]

    documents = [
        _document("com/example/m3typed/Shape.java", shape_occs),
        _document("com/example/m3typed/Circle.java", circle_occs),
        _document("com/example/m3typed/Square.java", square_occs),
        _document("com/example/m3typed/Box.java", box_occs),
        _document("com/example/m3typed/Caller.java", caller_occs),
    ]

    tool_info = _string_field(1, "gen_deterministic_scip_fixture")
    metadata = _string_field(3, ".") + _message_field(2, tool_info)

    payload = _message_field(1, metadata)
    for doc in documents:
        payload += _message_field(2, doc)
    return payload


def main() -> None:
    out_path = (
        Path(__file__).resolve().parents[1]
        / "tests"
        / "fixtures"
        / "m3typed"
        / "index.scip"
    )
    out_path.write_bytes(build_index())
    print(f"wrote {out_path} ({out_path.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
