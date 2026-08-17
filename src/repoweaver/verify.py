"""`fabric verify` — milestone verification suite.

M1 checks that the whole closed loop actually works end-to-end against the
bundled Java fixture: parsing, edge resolution, retrieval, freshness, the
explore() contract shape, and token-budget trimming. It is deliberately not a
unit test — it's a black-box smoke test of the built CLI/library, meant to
catch "works in pytest, broken for real users" gaps.
"""

from __future__ import annotations

import shutil
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from repoweaver.explore import BLIND_SPOTS, explore
from repoweaver.graph.store import GraphStore
from repoweaver.indexer import Indexer

_FIXTURE = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "javademo"


@dataclass
class VerifyResult:
    passed: bool
    report_lines: list[str] = field(default_factory=list)


class _Check:
    def __init__(self, report: list[str]) -> None:
        self.report = report
        self.failures = 0

    def check(self, name: str, condition: bool, detail: str = "") -> None:
        if condition:
            self.report.append(f"  [PASS] {name}")
        else:
            self.failures += 1
            suffix = f" — {detail}" if detail else ""
            self.report.append(f"  [FAIL] {name}{suffix}")


@contextmanager
def _built_fixture_repo():
    with tempfile.TemporaryDirectory(prefix="repoweaver-verify-") as tmp:
        repo_root = Path(tmp) / "javademo"
        shutil.copytree(_FIXTURE, repo_root)
        db_path = repo_root / ".repoweaver" / "graph.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with GraphStore(db_path) as store:
            Indexer(repo_root, store).build()
        yield repo_root


def run_verification(level: str, repo: Path) -> VerifyResult:
    if level == "benchmark":
        return _run_benchmark_verification()
    if level != "m1":
        return VerifyResult(
            False,
            [f"Level '{level}' is not implemented. Supported: 'm1', 'benchmark'."],
        )

    report: list[str] = [f"RepoWeaver verify --level m1  (fixture: {_FIXTURE})"]
    c = _Check(report)

    if not _FIXTURE.exists():
        c.check("fixture present", False, str(_FIXTURE))
        report.append("FAIL — cannot continue without fixture")
        return VerifyResult(False, report)

    with _built_fixture_repo() as repo_root:
        db_path = repo_root / ".repoweaver" / "graph.db"

        with GraphStore(db_path) as store:
            report.append("-- parsing & node extraction --")
            classes = [
                node
                for node in store.find_by_simple_name("EnglishGreeter")
                if node["kind"] == "class"
            ]
            c.check("class EnglishGreeter indexed", len(classes) == 1)
            greeter_iface = store.find_by_qualified_name("com.example.demo.Greeter")
            c.check(
                "interface Greeter indexed",
                len(greeter_iface) == 1 and greeter_iface[0]["kind"] == "interface",
            )
            level_enum = store.find_by_qualified_name("com.example.demo.Level")
            c.check(
                "enum Level indexed",
                len(level_enum) == 1 and level_enum[0]["kind"] == "enum",
            )
            methods = store.find_by_simple_name("greet")
            c.check("method greet() indexed (>=2 overrides)", len(methods) >= 2)
            all_ctors = store.conn.execute(
                "SELECT COUNT(*) FROM node WHERE kind = 'constructor'"
            ).fetchone()[0]
            c.check("at least one constructor indexed", all_ctors >= 1, str(all_ctors))
            fields = store.conn.execute(
                "SELECT COUNT(*) FROM node WHERE kind = 'field'"
            ).fetchone()[0]
            c.check("at least one field indexed", fields >= 1, str(fields))

            report.append("-- edge resolution --")
            stats = store.stats()
            edge_types = stats["edge_types"]
            c.check(
                "IMPLEMENTS edges resolved",
                edge_types.get("IMPLEMENTS", 0) >= 2,
                str(edge_types),
            )
            c.check(
                "EXTENDS edges resolved",
                edge_types.get("EXTENDS", 0) >= 1,
                str(edge_types),
            )
            c.check(
                "CALLS edges resolved", edge_types.get("CALLS", 0) >= 3, str(edge_types)
            )
            c.check(
                "IMPORTS edges resolved",
                edge_types.get("IMPORTS", 0) >= 1,
                str(edge_types),
            )

            english_greeter = store.find_by_qualified_name(
                "com.example.demo.EnglishGreeter"
            )[0]
            implements_edges = [
                e
                for e in store.neighbors(english_greeter["id"], "out", 0.0)
                if e[1] == "IMPLEMENTS"
            ]
            c.check(
                "EnglishGreeter --IMPLEMENTS--> Greeter",
                any(
                    e[0]["qualified_name"] == "com.example.demo.Greeter"
                    for e in implements_edges
                ),
            )

        report.append("-- retrieval (FTS + PPR) --")
        response = explore(query="Greeter", task="understand", repo=str(repo_root))
        c.check(
            "explore() returns slices for 'Greeter'",
            len(response.get("slices", [])) > 0,
        )
        hit_files = {s["file"] for s in response.get("slices", [])}
        c.check(
            "retrieval finds Greeter.java",
            any("Greeter.java" in f for f in hit_files),
            str(hit_files),
        )

        report.append("-- freshness --")
        response_fresh = explore(query="Greeter", task="locate", repo=str(repo_root))
        c.check(
            "freshness reports 'ok' right after build",
            response_fresh["stats"]["freshness"] == "ok",
        )
        (repo_root / "com/example/demo/Greeter.java").write_text(
            (repo_root / "com/example/demo/Greeter.java").read_text() + "\n// touched\n"
        )
        response_stale = explore(query="Greeter", task="locate", repo=str(repo_root))
        c.check(
            "freshness reports 'stale' after edit",
            response_stale["stats"]["freshness"] == "stale",
        )

        report.append("-- explore() contract shape --")
        for key in ("query", "task", "repo", "slices", "stats", "blind_spots"):
            c.check(f"response has key '{key}'", key in response)
        c.check(
            "blind_spots matches frozen contract string",
            response.get("blind_spots") == BLIND_SPOTS,
        )
        for key in (
            "nodes_visited",
            "edges_traversed",
            "tokens_estimated",
            "freshness",
        ):
            c.check(f"stats has key '{key}'", key in response.get("stats", {}))

        report.append("-- impact / debug tasks --")
        impact = explore(query="format", task="impact", repo=str(repo_root))
        c.check("impact task returns blast_radius key", "blast_radius" in impact)
        debug = explore(query="App -> Formatter", task="debug", repo=str(repo_root))
        c.check("debug task returns call_path key", "call_path" in debug)

        report.append("-- token budget --")
        tight = explore(
            query="Greeter", task="understand", repo=str(repo_root), max_tokens=1
        )
        est = tight["stats"]["tokens_estimated"]
        c.check(
            "token budget trims response to the requested budget",
            est <= 1 and len(tight["slices"]) <= 1,
            str(est),
        )
        wide = explore(
            query="Greeter", task="understand", repo=str(repo_root), max_tokens=100000
        )
        c.check(
            "wider budget yields at least as many slices",
            len(wide["slices"]) >= len(tight["slices"]),
        )

    passed = c.failures == 0
    report.append("")
    report.append("PASS" if passed else f"FAIL ({c.failures} check(s) failed)")
    return VerifyResult(passed, report)


_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_GROUND_TRUTH = _PROJECT_ROOT / "benchmarks" / "ground_truth.yaml"
_GT_FIXTURE = _PROJECT_ROOT / "benchmarks" / "fixtures" / "gt_demo"
_SOTA_TARGETS = _PROJECT_ROOT / "benchmarks" / "sota-targets.yaml"


def _run_benchmark_verification() -> VerifyResult:
    """`fabric verify --level benchmark` — a CI-safe check of the benchmark
    infrastructure itself, run only against the bundled gt_demo fixture (no
    cloning of gson/okhttp/etc). It asserts two different things:

    1. The metric *definitions* are stable on a fixture whose expected graph
       shape is known exactly (regression protection for metrics.py).
    2. The tool's own correctness (node recall, edge precision/recall,
       ambiguity flagging, query top-k/MRR) is 1.0 on that fixture.

    It deliberately does NOT require ambiguous_edge_rate/coverage to clear
    production release gates here — gt_demo is a small adversarial fixture
    built to contain a genuine ambiguity, so those two gates are *expected*
    to fail on it (see benchmarks/sota-targets.yaml and
    docs/benchmark-methodology.md). Real-repo-scale gate compliance is
    tracked in benchmarks/baselines/, not gated in CI.
    """
    from repoweaver.benchmark.compare import evaluate_gates, load_release_gates
    from repoweaver.benchmark.runner import run_benchmark

    report: list[str] = ["RepoWeaver verify --level benchmark"]
    c = _Check(report)

    if not _GROUND_TRUTH.exists() or not _GT_FIXTURE.exists():
        c.check("ground truth fixture present", False, str(_GROUND_TRUTH))
        report.append("FAIL — cannot continue without fixture")
        return VerifyResult(False, report)

    result = run_benchmark(
        repo=_GT_FIXTURE,
        name="gt_demo",
        adapter="repoweaver",
        ground_truth=_GROUND_TRUTH,
    )

    report.append("-- metric definitions (regression, exact values on gt_demo) --")
    c.check("run status is MEASURED", result.get("status") == "MEASURED", str(result))
    c.check("nodes == 16", result.get("nodes") == 16, str(result.get("nodes")))
    c.check(
        "edges_total == 9",
        result.get("edges_total") == 9,
        str(result.get("edges_total")),
    )
    c.check(
        "edges_resolved == 7 (confidence>=0.5, non-ambiguous)",
        result.get("edges_resolved") == 7,
        str(result.get("edges_resolved")),
    )
    c.check(
        "edges_ambiguous == 2 (the deliberate close() collision)",
        result.get("edges_ambiguous") == 2,
        str(result.get("edges_ambiguous")),
    )
    c.check(
        "ambiguous_edge_rate == 2/9",
        result.get("ambiguous_edge_rate") == _approx(2 / 9),
        str(result.get("ambiguous_edge_rate")),
    )
    c.check(
        "cross_file_dependent_coverage == 3/7",
        result.get("cross_file_dependent_coverage") == _approx(3 / 7),
        str(result.get("cross_file_dependent_coverage")),
    )
    c.check(
        "deterministic_rebuild is True",
        result.get("deterministic_rebuild") is True,
        str(result.get("deterministic_rebuild")),
    )

    report.append("-- correctness (ground truth) --")
    correctness = result.get("correctness") or {}
    for key in (
        "node_recall",
        "edge_precision",
        "edge_recall",
        "query_topk_recall",
        "query_mrr",
    ):
        c.check(f"{key} == 1.0", correctness.get(key) == 1.0, str(correctness.get(key)))
    c.check(
        "expected ambiguous pair correctly flagged",
        correctness.get("expected_ambiguous_correctly_flagged_rate") == 1.0,
        str(correctness.get("expected_ambiguous_correctly_flagged_rate")),
    )

    report.append("-- compare() gate mechanism --")
    gates = load_release_gates(_SOTA_TARGETS)
    comparison = evaluate_gates(result, gates)
    gate_status = {g.key: g.status for g in comparison.gates}
    c.check(
        "fixture-correctness gates PASS",
        all(
            gate_status.get(k) == "PASS"
            for k in (
                "fixture_node_recall",
                "fixture_edge_precision",
                "fixture_edge_recall",
                "deterministic_rebuild",
            )
        ),
        str(gate_status),
    )
    c.check(
        "ambiguity/coverage gates correctly reported FAIL on this adversarial fixture",
        gate_status.get("ambiguous_edge_rate") == "FAIL"
        and gate_status.get("cross_file_dependent_coverage") == "FAIL",
        str(gate_status),
    )
    c.check(
        "overall comparison status is FAIL (by design)", comparison.status == "FAIL"
    )

    passed = c.failures == 0
    report.append("")
    report.append("PASS" if passed else f"FAIL ({c.failures} check(s) failed)")
    return VerifyResult(passed, report)


def _approx(target: float, tol: float = 1e-9) -> object:
    """Tiny float-equality helper so this module doesn't need a pytest import."""

    class _Approx:
        def __eq__(self, other: object) -> bool:
            return isinstance(other, (int, float)) and abs(other - target) < tol

    return _Approx()
