"""v0.3.1 follow-up fixes: seed prioritization for bare type-name queries and
zlib compression of `file_refs_cache` payloads."""

from __future__ import annotations

import json
import sqlite3
import zlib
from pathlib import Path

import pytest

from codecontextfabric.explore import explore
from codecontextfabric.graph.store import GraphStore
from codecontextfabric.indexer import Indexer, _parsed_file_to_json

FIXTURE = Path(__file__).parent / "fixtures" / "javademo"


@pytest.fixture()
def built_demo(tmp_path: Path) -> Path:
    repo = tmp_path / "javademo"
    repo.mkdir()
    for src in sorted((FIXTURE).rglob("*.java")):
        dest = repo / src.relative_to(FIXTURE)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(src.read_text())
    db_path = repo / ".repoweaver" / "graph.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with GraphStore(db_path) as store:
        Indexer(repo, store).build()
    return repo


def test_bare_type_name_query_leads_with_the_class_slice(built_demo: Path) -> None:
    """A user typing a bare class name wants the class first, not its
    same-named constructor or unrelated methods that merely mention it."""
    response = explore("EnglishGreeter", task="locate", repo=str(built_demo))
    assert "candidates" not in response
    assert response["slices"], "expected at least one slice"
    top = response["slices"][0]
    assert top["qualified_name"] == "com.example.demo.EnglishGreeter"
    assert top["span_end"] - top["span_start"] >= 3  # the class body, not a stub


def test_candidate_callers_flow_through_interface_declaration(built_demo: Path) -> None:
    """T2 hardening: an implementation method's callers arrive via the
    interface declaration (Java static call sites target the declared
    type), so the panorama must mark them `via` the interface."""
    response = explore("greet", task="impact", repo=str(built_demo))
    assert "candidates" in response
    impl_cand = next(
        c
        for c in response["candidates"]
        if c["qualified_name"] == "com.example.demo.EnglishGreeter#greet(String)"
    )
    callers = impl_cand["callers"]
    assert any("via" in cl and "Greeter" in cl["via"] for cl in callers), (
        f"expected interface-via caller, got {callers}"
    )


def test_type_prioritization_keeps_method_queries_unharmed(built_demo: Path) -> None:
    """Genuine same-family ambiguity (three distinct `greet` methods) still
    returns candidates, and a unique-name symbol query (Formatter) leads
    with the type node as before."""
    ambiguous = explore("greet", task="locate", repo=str(built_demo))
    assert "candidates" in ambiguous
    names = {c["qualified_name"] for c in ambiguous["candidates"]}
    assert "com.example.demo.Greeter#greet(String)" in names

    unique = explore("Formatter", task="locate", repo=str(built_demo))
    assert "candidates" not in unique
    assert unique["slices"][0]["qualified_name"] == "com.example.demo.Formatter"


def test_file_refs_cache_payload_is_zlib_compressed(built_demo: Path) -> None:
    db_path = built_demo / ".repoweaver" / "graph.db"
    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT file, payload FROM file_refs_cache").fetchall()
    assert rows, "expected cache rows after build"
    for _file, payload in rows:
        assert isinstance(payload, bytes), "payload should be stored as a BLOB"
        assert payload[:1] == b"\x78", "payload should be zlib-compressed"
        decompressed = zlib.decompress(payload).decode("utf-8")
        assert "source" not in json.loads(decompressed)


def test_roundtrip_through_store_matches_direct_serialization(
    built_demo: Path,
) -> None:
    store = GraphStore(":memory:").open()
    try:
        from codecontextfabric.parser.java import JavaParser

        pf = JavaParser(built_demo).parse_file(built_demo / "com/example/demo/App.java")
        payload = _parsed_file_to_json(pf)
        store.set_file_refs_cache("com/example/demo/App.java", "hash1", payload)
        got = store.get_file_refs_cache("com/example/demo/App.java")
        assert got is not None and got[0] == "hash1"
        assert got[1] == payload
    finally:
        store.close()


def test_legacy_plain_text_payload_still_read(tmp_path: Path) -> None:
    """Rows written by pre-compression versions must degrade to a cache miss
    (or parse), never raise out of a build."""
    store = GraphStore(":memory:").open()
    try:
        store.conn.execute(
            "INSERT INTO file_refs_cache (file, content_hash, payload)"
            " VALUES ('A.java', 'h1', ?)",
            ('{"file": "A.java", "not": "a real payload"}',),
        )
        got = store.get_file_refs_cache("A.java")
        assert got is not None and "not" in got[1]
        # corrupt zlib-looking row degrades to empty string -> indexer re-parses
        store.conn.execute(
            "UPDATE file_refs_cache SET payload = ? WHERE file = 'A.java'",
            (b"\x78\x00garbage",),
        )
        got = store.get_file_refs_cache("A.java")
        assert got is not None and got[1] == ""
    finally:
        store.close()
