"""Adversarial overload-resolution tests.

Covers same-owner/same-arity overload disambiguation via argument-type
hints: String-vs-Class / Reader-vs-Type, int-vs-long (widening + phase-1
exact-match), null-argument non-disambiguation, and argument-type-hint
inference sourced from identifiers, object-creation expressions, class
literals, and cast expressions.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from repoweaver.graph.store import GraphStore
from repoweaver.indexer import Indexer

FIXTURE = Path(__file__).parent / "fixtures" / "overloads"
PKG = "com.example.overloads"


@pytest.fixture()
def store() -> GraphStore:
    s = GraphStore(":memory:")
    s.open()
    Indexer(FIXTURE, s).build()
    yield s
    s.close()


def _id(store: GraphStore, qualified_name: str) -> str:
    matches = store.find_by_qualified_name(qualified_name)
    assert matches, f"no node for {qualified_name}"
    return matches[0]["id"]


def _resolved_targets(store: GraphStore, from_qname: str) -> set[str]:
    from_id = _id(store, from_qname)
    return {
        n["qualified_name"]
        for n, edge_type, _ in store.neighbors(from_id, direction="out")
        if edge_type == "CALLS"
    }


def _unresolved(store: GraphStore, from_qname: str, target_name: str) -> dict | None:
    from_id = _id(store, from_qname)
    row = store.conn.execute(
        "SELECT * FROM unresolved_reference WHERE from_id = ? AND target_name = ?",
        (from_id, target_name),
    ).fetchone()
    return dict(row) if row else None


CALLER_RUN = f"{PKG}.Caller#run(Codec,String,Object)"


def test_string_class_vs_reader_type_resolves_exact_match(store: GraphStore) -> None:
    """fromJson(json, Foo.class) is an exact (String, Class) match; the
    (Reader, Type) overload scores zero on both arguments and is rejected."""
    targets = _resolved_targets(store, CALLER_RUN)
    assert f"{PKG}.Codec#fromJson(String,Class)" in targets
    assert f"{PKG}.Codec#fromJson(Reader,Type)" not in targets


def test_null_argument_does_not_disambiguate_reference_overloads(
    store: GraphStore,
) -> None:
    """write(null) can't be told apart from write(String) vs
    write(StringBuilder) using null alone — must stay ambiguous, not guess."""
    targets = _resolved_targets(store, CALLER_RUN)
    assert f"{PKG}.Codec#write(String)" not in targets
    assert f"{PKG}.Codec#write(StringBuilder)" not in targets

    unresolved = _unresolved(store, CALLER_RUN, "write")
    assert unresolved is not None
    candidate_ids = set(json.loads(unresolved["candidates"]))
    assert _id(store, f"{PKG}.Codec#write(String)") in candidate_ids
    assert _id(store, f"{PKG}.Codec#write(StringBuilder)") in candidate_ids


def test_cast_expression_argument_disambiguates(store: GraphStore) -> None:
    """tag((String) something) infers the cast's type and picks tag(String)
    over tag(Object) via subtype/exact scoring."""
    targets = _resolved_targets(store, CALLER_RUN)
    assert f"{PKG}.Codec#tag(String)" in targets
    assert f"{PKG}.Codec#tag(Object)" not in targets


def test_object_creation_argument_disambiguates(store: GraphStore) -> None:
    """accept(new Foo()) infers the created type and picks accept(Foo) over
    accept(Bar)."""
    targets = _resolved_targets(store, CALLER_RUN)
    assert f"{PKG}.Codec#accept(Foo)" in targets
    assert f"{PKG}.Codec#accept(Bar)" not in targets


def _evidence_lines(store: GraphStore, from_qname: str, to_qname: str) -> set[int]:
    from_id = _id(store, from_qname)
    to_id = _id(store, to_qname)
    edge = store.edge_between(from_id, to_id)
    assert edge is not None, f"no CALLS edge {from_qname} -> {to_qname}"
    return {e["line"] for e in store.evidence_for_edge(edge["id"])}


def test_int_literal_prefers_exact_over_widening_constructor(
    store: GraphStore,
) -> None:
    """new Box(5) (line 9) is an exact match for Box(int); Box(long) only
    scores via widening, so phase-1 exact-match wins even though the margin
    is 1 — the line-9 call must land on Box(int), not Box(long)."""
    lines = _evidence_lines(store, CALLER_RUN, f"{PKG}.Box#<init>(int)")
    assert 9 in lines
    long_lines = _evidence_lines(store, CALLER_RUN, f"{PKG}.Box#<init>(long)")
    assert 9 not in long_lines


def test_long_literal_eliminates_int_constructor(store: GraphStore) -> None:
    """new Box(5L) (line 10) can never narrow to Box(int) — a long literal
    eliminates the int overload outright, leaving Box(long) as the sole
    survivor for that call site."""
    lines = _evidence_lines(store, CALLER_RUN, f"{PKG}.Box#<init>(long)")
    assert 10 in lines
    int_lines = _evidence_lines(store, CALLER_RUN, f"{PKG}.Box#<init>(int)")
    assert 10 not in int_lines
