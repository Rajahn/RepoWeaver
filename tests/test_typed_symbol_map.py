"""Tests for SCIP symbol -> RepoWeaver qualified_name mapping."""

from __future__ import annotations

from pathlib import Path

import pytest

from repoweaver.graph.store import GraphStore
from repoweaver.indexer import Indexer
from repoweaver.typed.symbol_map import (
    SkipReason,
    SymbolMapper,
    decode_jvm_param_types,
    parse_symbol,
)

FIXTURE = Path(__file__).parent / "fixtures" / "m3typed"
PKG = "com/example/m3typed"


@pytest.fixture()
def store() -> GraphStore:
    s = GraphStore(":memory:")
    s.open()
    Indexer(FIXTURE, s).build()
    yield s
    s.close()


def _sym(descriptor: str) -> str:
    return f"semanticdb maven com.example:m3typed 0.1.0 {descriptor}"


def test_parse_symbol_type_descriptor() -> None:
    parsed = parse_symbol(_sym(f"{PKG}/Shape#"))
    assert parsed is not None
    assert [d.suffix for d in parsed.descriptors] == [
        "namespace",
        "namespace",
        "namespace",
        "type",
    ]
    assert parsed.descriptors[-1].name == "Shape"


def test_parse_symbol_method_descriptor_with_disambiguator() -> None:
    parsed = parse_symbol(_sym(f"{PKG}/Caller#process(I)."))
    assert parsed is not None
    method = parsed.descriptors[-1]
    assert method.suffix == "method"
    assert method.name == "process"
    assert method.disambiguator == "I"


def test_parse_symbol_local_scheme() -> None:
    parsed = parse_symbol("local 5")
    assert parsed is not None
    assert parsed.is_local


def test_parse_symbol_malformed_returns_none() -> None:
    assert parse_symbol("not-a-scip-symbol") is None


def test_decode_jvm_param_types_primitive_and_reference() -> None:
    assert decode_jvm_param_types("ILjava/lang/String;") == ["int", "String"]


def test_decode_jvm_param_types_empty() -> None:
    assert decode_jvm_param_types("") == []


def test_decode_jvm_param_types_unrecognized_returns_none() -> None:
    assert decode_jvm_param_types("Qgarbage") is None


def test_resolve_type_symbol(store: GraphStore) -> None:
    mapper = SymbolMapper(store)
    result = mapper.resolve(_sym(f"{PKG}/Shape#"))
    assert result.ok
    assert result.node["qualified_name"] == "com.example.m3typed.Shape"


def test_resolve_field_term_symbol(store: GraphStore) -> None:
    mapper = SymbolMapper(store)
    result = mapper.resolve(_sym(f"{PKG}/Circle#radius."))
    assert result.ok
    assert result.node["qualified_name"] == "com.example.m3typed.Circle#radius"


def test_resolve_single_candidate_method_bypasses_disambiguator(
    store: GraphStore,
) -> None:
    mapper = SymbolMapper(store)
    result = mapper.resolve(_sym(f"{PKG}/Shape#area()."))
    assert result.ok
    assert result.node["qualified_name"] == "com.example.m3typed.Shape#area()"


def test_resolve_overload_by_int_disambiguator(store: GraphStore) -> None:
    mapper = SymbolMapper(store)
    result = mapper.resolve(_sym(f"{PKG}/Caller#process(I)."))
    assert result.ok
    assert result.node["qualified_name"] == "com.example.m3typed.Caller#process(int)"


def test_resolve_overload_by_string_disambiguator(store: GraphStore) -> None:
    mapper = SymbolMapper(store)
    result = mapper.resolve(_sym(f"{PKG}/Caller#process(Ljava/lang/String;)."))
    assert result.ok
    assert result.node["qualified_name"] == "com.example.m3typed.Caller#process(String)"


def test_resolve_constructor_translates_init_to_class_name(store: GraphStore) -> None:
    mapper = SymbolMapper(store)
    result = mapper.resolve(_sym(f"{PKG}/Circle#<init>(D)."))
    assert result.ok
    assert result.node["kind"] == "constructor"
    assert result.node["qualified_name"] == "com.example.m3typed.Circle#<init>(double)"


def test_resolve_unknown_owner_is_skipped_not_guessed(store: GraphStore) -> None:
    mapper = SymbolMapper(store)
    result = mapper.resolve(_sym("com/example/m3typed/NoSuchType#"))
    assert not result.ok
    assert result.skip_reason == SkipReason.OWNER_NOT_FOUND
    assert mapper.skip_counts[SkipReason.OWNER_NOT_FOUND] == 1


def test_resolve_unknown_member_is_skipped_not_guessed(store: GraphStore) -> None:
    mapper = SymbolMapper(store)
    result = mapper.resolve(_sym(f"{PKG}/Shape#noSuchMethod()."))
    assert not result.ok
    assert result.skip_reason == SkipReason.MEMBER_NOT_FOUND


def test_resolve_local_symbol_is_skipped(store: GraphStore) -> None:
    mapper = SymbolMapper(store)
    result = mapper.resolve("local 3")
    assert not result.ok
    assert result.skip_reason == SkipReason.LOCAL_SYMBOL


def test_resolve_malformed_symbol_is_skipped(store: GraphStore) -> None:
    mapper = SymbolMapper(store)
    result = mapper.resolve("garbage")
    assert not result.ok
    assert result.skip_reason == SkipReason.MALFORMED_SYMBOL


def test_ambiguous_overload_with_undecodable_disambiguator_is_skipped(
    store: GraphStore,
) -> None:
    mapper = SymbolMapper(store)
    result = mapper.resolve(_sym(f"{PKG}/Caller#process(Qgarbage)."))
    assert not result.ok
    assert result.skip_reason == SkipReason.AMBIGUOUS_OVERLOAD
