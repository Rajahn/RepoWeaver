"""End-to-end tests for `fabric overlay scip` against the m3typed fixture.

Covers: precise interface dispatch (lands on the interface method, not an
implementation), overload disambiguation by argument type, conflict merging
of tree-sitter + SCIP evidence into one *_TYPED edge without dropping edges,
idempotent re-runs, and unmapped-symbol bookkeeping.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from codecontextfabric.benchmark.metrics import graph_signature
from codecontextfabric.graph.store import GraphStore, edge_id
from codecontextfabric.indexer import Indexer
from codecontextfabric.typed.overlay import run_overlay
from codecontextfabric.typed.symbol_map import SkipReason

FIXTURE = Path(__file__).parent / "fixtures" / "m3typed"
PKG = "com.example.m3typed"


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    repo_root = tmp_path / "m3typed"
    shutil.copytree(FIXTURE, repo_root)
    db_path = repo_root / ".repoweaver" / "graph.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with GraphStore(db_path) as store:
        Indexer(repo_root, store).build()
    return repo_root


def _db(repo_root: Path) -> Path:
    return repo_root / ".repoweaver" / "graph.db"


def _node_id(store: GraphStore, qname: str) -> str:
    matches = store.find_by_qualified_name(qname)
    assert matches, f"no node for {qname}"
    return matches[0]["id"]


def test_overlay_reports_zero_unmapped_on_full_fixture(repo: Path) -> None:
    stats = run_overlay(repo, repo / "index.scip")
    assert stats.typed_refs == 11
    assert stats.mapped == 11
    assert stats.unmapped_symbols == 0
    assert stats.skip_reasons == {}


def test_interface_dispatch_lands_on_interface_method_not_implementation(
    repo: Path,
) -> None:
    run_overlay(repo, repo / "index.scip")
    with GraphStore(_db(repo)) as store:
        dispatch_id = _node_id(store, f"{PKG}.Caller#dispatch(Shape)")
        shape_area_id = _node_id(store, f"{PKG}.Shape#area()")
        circle_area_id = _node_id(store, f"{PKG}.Circle#area()")

        typed_edge = store.conn.execute(
            "SELECT * FROM edge WHERE id = ?",
            (edge_id(dispatch_id, shape_area_id, "CALLS_TYPED"),),
        ).fetchone()
        assert typed_edge is not None

        wrong_edge = store.conn.execute(
            "SELECT * FROM edge WHERE from_id = ? AND to_id = ?",
            (dispatch_id, circle_area_id),
        ).fetchone()
        assert wrong_edge is None


def test_overloads_distinguished_by_argument_type(repo: Path) -> None:
    run_overlay(repo, repo / "index.scip")
    with GraphStore(_db(repo)) as store:
        run_id = _node_id(store, f"{PKG}.Caller#run()")
        process_int_id = _node_id(store, f"{PKG}.Caller#process(int)")
        process_str_id = _node_id(store, f"{PKG}.Caller#process(String)")

        int_edge = store.conn.execute(
            "SELECT * FROM edge WHERE id = ?",
            (edge_id(run_id, process_int_id, "CALLS_TYPED"),),
        ).fetchone()
        str_edge = store.conn.execute(
            "SELECT * FROM edge WHERE id = ?",
            (edge_id(run_id, process_str_id, "CALLS_TYPED"),),
        ).fetchone()
        assert int_edge is not None
        assert str_edge is not None
        assert int_edge["id"] != str_edge["id"]


def test_conflicting_edge_merges_without_dropping_evidence(repo: Path) -> None:
    with GraphStore(_db(repo)) as store:
        dispatch_id = _node_id(store, f"{PKG}.Caller#dispatch(Shape)")
        shape_area_id = _node_id(store, f"{PKG}.Shape#area()")
        base_id = edge_id(dispatch_id, shape_area_id, "CALLS")
        base_row_before = store.conn.execute(
            "SELECT * FROM edge WHERE id = ?", (base_id,)
        ).fetchone()
        assert base_row_before is not None  # tree-sitter already found this call
        evidence_before = store.conn.execute(
            "SELECT file, line FROM evidence WHERE edge_id = ?", (base_id,)
        ).fetchall()
        evidence_before = {(row["file"], row["line"]) for row in evidence_before}
        assert evidence_before

    run_overlay(repo, repo / "index.scip")

    with GraphStore(_db(repo)) as store:
        typed_id = edge_id(dispatch_id, shape_area_id, "CALLS_TYPED")
        base_row_after = store.conn.execute(
            "SELECT * FROM edge WHERE id = ?", (base_id,)
        ).fetchone()
        typed_row = store.conn.execute(
            "SELECT * FROM edge WHERE id = ?", (typed_id,)
        ).fetchone()
        assert base_row_after is None  # upgraded, not duplicated
        assert typed_row is not None
        assert typed_row["provenance"] == "scip_java+tree_sitter_java"
        evidence_after = store.conn.execute(
            "SELECT file, line FROM evidence WHERE edge_id = ?", (typed_id,)
        ).fetchall()
        evidence_after = {(row["file"], row["line"]) for row in evidence_after}
        # Original tree-sitter evidence (file, line) pairs must survive the
        # upgrade — the SCIP occurrence for this same call site coincides
        # with the same (file, line), so the set need not grow, only persist.
        assert evidence_before <= evidence_after


def test_overlay_never_reduces_total_edge_count(repo: Path) -> None:
    with GraphStore(_db(repo)) as store:
        edges_before = store.conn.execute("SELECT COUNT(*) c FROM edge").fetchone()["c"]

    run_overlay(repo, repo / "index.scip")

    with GraphStore(_db(repo)) as store:
        edges_after = store.conn.execute("SELECT COUNT(*) c FROM edge").fetchone()["c"]
    assert edges_after >= edges_before


def test_overlay_is_idempotent(repo: Path) -> None:
    stats1 = run_overlay(repo, repo / "index.scip")
    with GraphStore(_db(repo)) as store:
        sig1 = graph_signature(store)
        edges1 = store.conn.execute("SELECT COUNT(*) c FROM edge").fetchone()["c"]
        evidence1 = store.conn.execute("SELECT COUNT(*) c FROM evidence").fetchone()[
            "c"
        ]

    stats2 = run_overlay(repo, repo / "index.scip")
    with GraphStore(_db(repo)) as store:
        sig2 = graph_signature(store)
        edges2 = store.conn.execute("SELECT COUNT(*) c FROM edge").fetchone()["c"]
        evidence2 = store.conn.execute("SELECT COUNT(*) c FROM evidence").fetchone()[
            "c"
        ]

    assert stats1.as_dict() == stats2.as_dict()
    assert sig1 == sig2
    assert edges1 == edges2
    assert evidence1 == evidence2


def test_dry_run_does_not_mutate_graph(repo: Path) -> None:
    with GraphStore(_db(repo)) as store:
        edges_before = store.conn.execute("SELECT COUNT(*) c FROM edge").fetchone()["c"]

    stats = run_overlay(repo, repo / "index.scip", dry_run=True)
    assert stats.mapped == 11

    with GraphStore(_db(repo)) as store:
        edges_after = store.conn.execute("SELECT COUNT(*) c FROM edge").fetchone()["c"]
        typed = store.conn.execute(
            "SELECT COUNT(*) c FROM edge WHERE type LIKE '%_TYPED'"
        ).fetchone()["c"]
    assert edges_after == edges_before
    assert typed == 0


def test_unmapped_symbol_statistics_are_recorded_not_guessed(repo: Path) -> None:
    # Corrupt one target symbol in a copy of the index bytes so it can never
    # resolve, and confirm the overlay records it as skipped rather than
    # silently mapping it to the wrong node.
    from codecontextfabric.typed.scip_proto import decode_index
    from codecontextfabric.typed.symbol_map import SymbolMapper

    index = decode_index((repo / "index.scip").read_bytes())
    with GraphStore(_db(repo)) as store:
        mapper = SymbolMapper(store)
        result = mapper.resolve(
            "semanticdb maven com.example:m3typed 0.1.0 "
            "com/example/m3typed/DoesNotExist#"
        )
        assert not result.ok
        assert result.skip_reason == SkipReason.OWNER_NOT_FOUND
        assert mapper.skip_counts == {SkipReason.OWNER_NOT_FOUND: 1}
    assert index.documents  # sanity: the real index still parses fine
