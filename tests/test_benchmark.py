"""Tests for the benchmark infrastructure: metric definitions, ambiguous-edge
exclusion, coverage, compare gates, report rendering, and determinism."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from codecontextfabric.benchmark.compare import evaluate_gates, load_release_gates
from codecontextfabric.benchmark.groundtruth import GroundTruth, evaluate
from codecontextfabric.benchmark.metrics import (
    QuerySample,
    cross_file_dependent_coverage,
    edge_counts,
    graph_signature,
    summarize_query_samples,
)
from codecontextfabric.benchmark.report import render_report
from codecontextfabric.benchmark.runner import run_benchmark
from codecontextfabric.graph.store import (
    EdgeRow,
    GraphStore,
    NodeRow,
    UnresolvedReferenceRow,
)
from codecontextfabric.indexer import Indexer

REPO_ROOT = Path(__file__).resolve().parents[1]
GT_DEMO = REPO_ROOT / "benchmarks" / "fixtures" / "gt_demo"
GROUND_TRUTH_YAML = REPO_ROOT / "benchmarks" / "ground_truth.yaml"
SOTA_TARGETS = REPO_ROOT / "benchmarks" / "sota-targets.yaml"


@pytest.fixture()
def gt_demo_repo(tmp_path: Path) -> Path:
    dest = tmp_path / "gt_demo"
    shutil.copytree(GT_DEMO, dest)
    return dest


@pytest.fixture()
def built_gt_demo(gt_demo_repo: Path):
    db_path = gt_demo_repo / ".repoweaver" / "graph.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with GraphStore(db_path) as store:
        Indexer(gt_demo_repo, store).build()
    return gt_demo_repo


def _open(repo: Path) -> GraphStore:
    return GraphStore(repo / ".repoweaver" / "graph.db")


# ---------------------------------------------------------------------------
# Metric definitions
# ---------------------------------------------------------------------------


class TestEdgeCounts:
    def test_gt_demo_has_eight_resolved_two_ambiguous(self, built_gt_demo):
        with _open(built_gt_demo) as store:
            total, resolved, ambiguous = edge_counts(store)
        assert (total, resolved, ambiguous) == (10, 8, 2)

    def test_ambiguous_edge_excluded_from_resolved_even_at_boundary_confidence(self):
        """An ambiguous reference (2 equally-valid candidates) is stored in
        unresolved_reference, never in `edge` — so it must contribute 0 to
        `resolved` and exactly len(candidates) to `ambiguous`, proving the
        candidate set (not just a confidence threshold) drives the count."""
        store = GraphStore(":memory:").open()
        try:
            store.replace_file_nodes(
                "A.java",
                [
                    NodeRow(
                        id="n1",
                        kind="class",
                        qualified_name="A",
                        simple_name="A",
                        file="A.java",
                        span_start=1,
                        span_end=1,
                    ),
                    NodeRow(
                        id="n2",
                        kind="class",
                        qualified_name="B",
                        simple_name="B",
                        file="B.java",
                        span_start=1,
                        span_end=1,
                    ),
                    NodeRow(
                        id="n3",
                        kind="class",
                        qualified_name="C",
                        simple_name="C",
                        file="C.java",
                        span_start=1,
                        span_end=1,
                    ),
                ],
            )
            store.replace_file_unresolved(
                "A.java",
                [
                    UnresolvedReferenceRow(
                        from_id="n1",
                        type="EXTENDS",
                        target_name="B",
                        candidates=["n2", "n3"],
                        reason="ambiguous_supertype",
                        file="A.java",
                        line=1,
                    )
                ],
            )
            store.commit()
            total, resolved, ambiguous = edge_counts(store)
            assert (total, resolved, ambiguous) == (2, 0, 2)
        finally:
            store.close()

    def test_unique_high_confidence_edge_counts_as_resolved(self):
        store = GraphStore(":memory:").open()
        try:
            store.replace_file_nodes(
                "A.java",
                [
                    NodeRow(
                        id="n1",
                        kind="class",
                        qualified_name="A",
                        simple_name="A",
                        file="A.java",
                        span_start=1,
                        span_end=1,
                    ),
                    NodeRow(
                        id="n2",
                        kind="class",
                        qualified_name="B",
                        simple_name="B",
                        file="B.java",
                        span_start=1,
                        span_end=1,
                    ),
                ],
            )
            store.replace_file_edges(
                "A.java",
                [
                    EdgeRow(
                        from_id="n1",
                        to_id="n2",
                        type="EXTENDS",
                        provenance="test",
                        confidence=1.0,
                        file="A.java",
                        line=1,
                    )
                ],
                "test-1",
            )
            store.commit()
            assert edge_counts(store) == (1, 1, 0)
        finally:
            store.close()


class TestCrossFileDependentCoverage:
    def test_gt_demo_coverage_is_three_of_seven(self, built_gt_demo):
        with _open(built_gt_demo) as store:
            total, covered = cross_file_dependent_coverage(store)
        assert (total, covered) == (7, 3)

    def test_scope_prefix_limits_both_coverage_denominator_and_sources(
        self, built_gt_demo
    ):
        with _open(built_gt_demo) as store:
            total, covered = cross_file_dependent_coverage(
                store, ["com/example/gt/GammaWorker.java"]
            )
        # A single-file scope cannot be covered by an edge whose source must
        # also be inside that same scope and in a different file.
        assert (total, covered) == (1, 0)

    def test_ambiguous_incoming_edge_does_not_count_as_covering(self, built_gt_demo):
        """AlphaWorker/BetaWorker in gt_demo receive only *ambiguous* incoming
        edges (the deliberate close() collision) — they must not count
        toward coverage even though they do receive a cross-file edge."""
        with _open(built_gt_demo) as store:
            row = store.conn.execute(
                "SELECT id FROM node WHERE qualified_name = 'com.example.gt.AlphaWorker#close()'"
            ).fetchone()
            covered = store.conn.execute(
                """
                SELECT COUNT(*) FROM edge
                JOIN node AS from_node ON from_node.id = edge.from_id
                WHERE edge.to_id = ? AND edge.confidence >= 0.5 AND edge.ambiguous_candidates = '[]'
                """,
                (row["id"],),
            ).fetchone()[0]
            assert covered == 0

    def test_same_file_edge_does_not_count_as_cross_file(self):
        store = GraphStore(":memory:").open()
        try:
            store.replace_file_nodes(
                "A.java",
                [
                    NodeRow(
                        id="n1",
                        kind="class",
                        qualified_name="A",
                        simple_name="A",
                        file="A.java",
                        span_start=1,
                        span_end=1,
                    ),
                    NodeRow(
                        id="n2",
                        kind="method",
                        qualified_name="A#m",
                        simple_name="m",
                        file="A.java",
                        span_start=1,
                        span_end=1,
                    ),
                ],
            )
            store.replace_file_edges(
                "A.java",
                [
                    EdgeRow(
                        from_id="n1",
                        to_id="n2",
                        type="CALLS",
                        provenance="test",
                        confidence=1.0,
                        file="A.java",
                        line=1,
                    )
                ],
                "test-1",
            )
            store.commit()
            total, covered = cross_file_dependent_coverage(store)
            assert (total, covered) == (1, 0)
        finally:
            store.close()


class TestGraphSignature:
    def test_deterministic_across_rebuilds(self, gt_demo_repo):
        db1 = gt_demo_repo / ".repoweaver" / "graph1.db"
        db2 = gt_demo_repo / ".repoweaver" / "graph2.db"
        db1.parent.mkdir(parents=True, exist_ok=True)
        with GraphStore(db1) as store1:
            Indexer(gt_demo_repo, store1).build()
            hash1 = graph_signature(store1)
        with GraphStore(db2) as store2:
            Indexer(gt_demo_repo, store2).build()
            hash2 = graph_signature(store2)
        assert hash1 == hash2

    def test_changes_when_source_changes(self, built_gt_demo):
        with _open(built_gt_demo) as store:
            before = graph_signature(store)

        (built_gt_demo / "com/example/gt/Router.java").write_text(
            "package com.example.gt;\npublic class Router { public void shutdown() { } }\n"
        )
        with GraphStore(built_gt_demo / ".repoweaver" / "graph.db") as store:
            Indexer(built_gt_demo, store).build()
            store.commit()
            after = graph_signature(store)
        assert before != after


# ---------------------------------------------------------------------------
# Ground truth correctness
# ---------------------------------------------------------------------------


class TestGroundTruth:
    def test_perfect_score_on_reference_fixture(self, built_gt_demo):
        gt = GroundTruth.load(GROUND_TRUTH_YAML)
        with _open(built_gt_demo) as store:
            result = evaluate(gt, store)
        assert result["node_recall"] == 1.0
        assert result["edge_precision"] == 1.0
        assert result["edge_recall"] == 1.0
        assert result["expected_ambiguous_correctly_flagged_rate"] == 1.0
        assert result["query_topk_recall"] == 1.0
        assert result["query_mrr"] == 1.0

    def test_missing_node_drops_recall(self, gt_demo_repo):
        (gt_demo_repo / "com/example/gt/Router.java").unlink()
        db_path = gt_demo_repo / ".repoweaver" / "graph.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with GraphStore(db_path) as store:
            Indexer(gt_demo_repo, store).build()
            store.commit()
            gt = GroundTruth.load(GROUND_TRUTH_YAML)
            result = evaluate(gt, store)
        assert result["node_recall"] < 1.0
        assert any("Router" in m for m in result["nodes_missing"])


# ---------------------------------------------------------------------------
# compare() gates
# ---------------------------------------------------------------------------


class TestCompareGates:
    def test_pass_when_all_measured_gates_clear(self):
        gates = {"ambiguous_edge_rate": {"op": "<=", "value": 0.10, "description": ""}}
        result = evaluate_gates({"ambiguous_edge_rate": 0.05}, gates)
        assert result.status == "PASS"
        assert result.gates[0].status == "PASS"

    def test_fail_when_a_gate_misses(self):
        gates = {"ambiguous_edge_rate": {"op": "<=", "value": 0.10, "description": ""}}
        result = evaluate_gates({"ambiguous_edge_rate": 0.89}, gates)
        assert result.status == "FAIL"
        assert result.gates[0].status == "FAIL"
        assert result.gates[0].gap == pytest.approx(0.79)

    def test_skip_when_metric_is_null_not_counted_as_pass(self):
        gates = {"ambiguous_edge_rate": {"op": "<=", "value": 0.10, "description": ""}}
        result = evaluate_gates({"ambiguous_edge_rate": None}, gates)
        assert result.gates[0].status == "SKIP"
        assert result.status == "FAIL"  # missing required evidence blocks release

    def test_nested_correctness_field_lookup(self):
        gates = {"fixture_node_recall": {"op": ">=", "value": 0.98, "description": ""}}
        result = evaluate_gates({"correctness": {"node_recall": 0.99}}, gates)
        assert result.gates[0].status == "PASS"

    def test_real_sota_targets_file_loads_and_flags_v0_1_gson_gap(self):
        import json

        candidate = json.loads(
            (REPO_ROOT / "benchmarks" / "baselines" / "v0.1.0-gson.json").read_text()
        )
        gates = load_release_gates(SOTA_TARGETS)
        result = evaluate_gates(candidate, gates)
        assert result.status == "FAIL"
        by_key = {g.key: g.status for g in result.gates}
        assert by_key["ambiguous_edge_rate"] == "FAIL"
        assert by_key["cross_file_dependent_coverage"] == "FAIL"
        # The reproducible baseline also carries fixture correctness and
        # deterministic rebuild evidence, so those independent gates pass.
        assert by_key["fixture_node_recall"] == "PASS"
        assert by_key["deterministic_rebuild"] == "PASS"


# ---------------------------------------------------------------------------
# report()
# ---------------------------------------------------------------------------


class TestReport:
    def test_render_includes_metrics_and_gates(self):
        candidate = {
            "name": "demo",
            "repo": "/tmp/demo",
            "adapter": "ccf",
            "status": "MEASURED",
            "nodes": 10,
            "ambiguous_edge_rate": 0.05,
        }
        markdown = render_report(candidate, targets_path=str(SOTA_TARGETS))
        assert "# Benchmark report — demo" in markdown
        assert "| Nodes | 10 |" in markdown
        assert "Release gates" in markdown

    def test_render_without_targets_skips_gates_section(self):
        markdown = render_report({"name": "demo", "repo": "x", "adapter": "ccf"})
        assert "Release gates" not in markdown


# ---------------------------------------------------------------------------
# End-to-end run_benchmark()
# ---------------------------------------------------------------------------


class TestRunBenchmark:
    def test_run_benchmark_end_to_end(self):
        result = run_benchmark(
            repo=GT_DEMO,
            name="gt_demo",
            adapter="ccf",
            ground_truth=GROUND_TRUTH_YAML,
        )
        assert result["status"] == "MEASURED"
        assert result["nodes"] == 16
        assert result["edges_ambiguous"] == 2
        assert result["correctness"]["node_recall"] == 1.0
        assert result["deterministic_rebuild"] is True

    def test_skip_adapter_reports_skip_not_fail(self):
        result = run_benchmark(repo=GT_DEMO, name="gt_demo", adapter="codegraph")
        assert result["status"] == "SKIP"
        assert "reason" in result


# ---------------------------------------------------------------------------
# summarize_query_samples() / _percentile() boundary cases
# ---------------------------------------------------------------------------


class TestSummarizeQuerySamples:
    def test_zero_samples_yields_all_none(self):
        summary = summarize_query_samples([])
        assert summary == {
            "query_latency_ms_p50": None,
            "query_latency_ms_p95": None,
            "context_tokens_p50": None,
            "context_tokens_p95": None,
        }

    def test_single_sample_yields_that_samples_values(self):
        sample = QuerySample(query="q", latency_ms=12.5, context_tokens=40)
        summary = summarize_query_samples([sample])
        assert summary == {
            "query_latency_ms_p50": 12.5,
            "query_latency_ms_p95": 12.5,
            "context_tokens_p50": 40.0,
            "context_tokens_p95": 40.0,
        }

    def test_multiple_samples_matches_expected_percentiles(self):
        samples = [
            QuerySample(query=f"q{i}", latency_ms=float(i), context_tokens=i)
            for i in range(1, 21)
        ]
        summary = summarize_query_samples(samples)
        assert summary["query_latency_ms_p50"] == pytest.approx(10.5)
        assert summary["query_latency_ms_p95"] == pytest.approx(19.05)
