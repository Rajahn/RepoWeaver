"""`fabric verify` — milestone verification suite.

M1 checks that the whole closed loop actually works end-to-end against the
bundled Java fixture: parsing, edge resolution, retrieval, freshness, the
explore() contract shape, and token-budget trimming. It is deliberately not a
unit test — it's a black-box smoke test of the built CLI/library, meant to
catch "works in pytest, broken for real users" gaps.

Each level (m1/m2/m3/benchmark) shares one skeleton: a `_Check` accumulates
[PASS]/[FAIL] lines into a report, `_fixture_missing` short-circuits when a
required fixture isn't present, and `_finish` appends the trailing PASS/FAIL
summary line. Level-specific logic is split into small per-section check
functions so no single function grows into its own god-module.
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
_OVERLOADS_FIXTURE = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "overloads"


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


def _finish(report: list[str], c: _Check) -> VerifyResult:
    passed = c.failures == 0
    report.append("")
    report.append("PASS" if passed else f"FAIL ({c.failures} check(s) failed)")
    return VerifyResult(passed, report)


def _fixture_missing(
    report: list[str], c: _Check, check_name: str, path: Path
) -> VerifyResult:
    c.check(check_name, False, str(path))
    report.append("FAIL — cannot continue without fixture")
    return VerifyResult(False, report)


@contextmanager
def _built_fixture_repo(fixture: Path = _FIXTURE, name: str = "javademo"):
    with tempfile.TemporaryDirectory(prefix="repoweaver-verify-") as tmp:
        repo_root = Path(tmp) / name
        shutil.copytree(fixture, repo_root)
        db_path = repo_root / ".repoweaver" / "graph.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with GraphStore(db_path) as store:
            Indexer(repo_root, store).build()
        yield repo_root


def run_verification(level: str, repo: Path) -> VerifyResult:
    runners = {
        "m1": _run_m1_verification,
        "m2": _run_m2_verification,
        "m3": _run_m3_verification,
        "query": _run_query_verification,
        "benchmark": _run_benchmark_verification,
    }
    runner = runners.get(level)
    if runner is None:
        return VerifyResult(
            False,
            [
                (
                    f"Level '{level}' is not implemented. "
                    "Supported: 'm1', 'm2', 'm3', 'query', 'benchmark'."
                )
            ],
        )
    return runner()


# ----------------------------------------------------------------------
# m1 — closed-loop smoke test
# ----------------------------------------------------------------------


def _m1_parsing_checks(store: GraphStore, c: _Check) -> None:
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


def _m1_edge_checks(store: GraphStore, c: _Check) -> None:
    stats = store.stats()
    edge_types = stats["edge_types"]
    c.check(
        "IMPLEMENTS edges resolved",
        edge_types.get("IMPLEMENTS", 0) >= 2,
        str(edge_types),
    )
    c.check(
        "EXTENDS edges resolved", edge_types.get("EXTENDS", 0) >= 1, str(edge_types)
    )
    c.check("CALLS edges resolved", edge_types.get("CALLS", 0) >= 3, str(edge_types))
    c.check(
        "IMPORTS edges resolved", edge_types.get("IMPORTS", 0) >= 1, str(edge_types)
    )

    english_greeter = store.find_by_qualified_name("com.example.demo.EnglishGreeter")[0]
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


def _m1_retrieval_checks(repo_root: Path, c: _Check) -> dict:
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
    return response


def _m1_freshness_checks(repo_root: Path, c: _Check) -> None:
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


def _m1_contract_checks(response: dict, c: _Check) -> None:
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


def _m1_task_checks(repo_root: Path, c: _Check) -> None:
    impact = explore(query="format", task="impact", repo=str(repo_root))
    c.check("impact task returns blast_radius key", "blast_radius" in impact)
    debug = explore(query="App -> Formatter", task="debug", repo=str(repo_root))
    c.check("debug task returns call_path key", "call_path" in debug)


def _m1_budget_checks(repo_root: Path, c: _Check) -> None:
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


def _run_m1_verification() -> VerifyResult:
    report: list[str] = [f"RepoWeaver verify --level m1  (fixture: {_FIXTURE})"]
    c = _Check(report)

    if not _FIXTURE.exists():
        return _fixture_missing(report, c, "fixture present", _FIXTURE)

    with _built_fixture_repo() as repo_root:
        db_path = repo_root / ".repoweaver" / "graph.db"

        with GraphStore(db_path) as store:
            report.append("-- parsing & node extraction --")
            _m1_parsing_checks(store, c)
            report.append("-- edge resolution --")
            _m1_edge_checks(store, c)

        report.append("-- retrieval (FTS + PPR) --")
        response = _m1_retrieval_checks(repo_root, c)

        report.append("-- freshness --")
        _m1_freshness_checks(repo_root, c)

        report.append("-- explore() contract shape --")
        _m1_contract_checks(response, c)

        report.append("-- impact / debug tasks --")
        _m1_task_checks(repo_root, c)

        report.append("-- token budget --")
        _m1_budget_checks(repo_root, c)

    return _finish(report, c)


# ----------------------------------------------------------------------
# query — v0.4.0 query-facade guarantees (T1-T4, ADR-0004)
# ----------------------------------------------------------------------


def _query_qualified_syntax_checks(repo_root: Path, c: _Check) -> None:
    for query in ("Formatter#format", "Formatter.format", "format(String)"):
        result = explore(query=query, task="impact", repo=str(repo_root))
        c.check(
            f"qualified syntax '{query}' resolves directly (no candidates)",
            "candidates" not in result,
            str(result.get("candidates")),
        )
        c.check(
            f"qualified syntax '{query}' seeds the exact node",
            bool(result.get("slices"))
            and result["slices"][0]["qualified_name"]
            == "com.example.demo.Formatter#format(String)",
            str(result.get("slices")),
        )


def _query_overload_signature_checks(overloads_root: Path, c: _Check) -> None:
    result = explore(
        query="Codec#fromJson(String,Class)", task="understand", repo=str(overloads_root)
    )
    c.check(
        "signature disambiguates a same-name overload",
        "candidates" not in result
        and bool(result.get("slices"))
        and result["slices"][0]["qualified_name"]
        == "com.example.overloads.Codec#fromJson(String,Class)",
        str(result),
    )

    panorama = explore(
        query="Codec#fromJson", task="understand", repo=str(overloads_root)
    )
    c.check(
        "owner-only qualified query with 3 overloads falls to a panorama",
        len(panorama.get("candidates") or []) == 3,
        str(panorama.get("candidates")),
    )
    for candidate in panorama.get("candidates") or []:
        c.check(
            f"candidate {candidate.get('qualified_name')} carries callers+blast_summary+context",
            "callers" in candidate
            and "blast_summary" in candidate
            and "file" in candidate
            and "span_start" in candidate
            and "signature" in candidate,
            str(candidate),
        )


def _query_ambiguity_panorama_checks(repo_root: Path, c: _Check) -> None:
    result = explore(query="greet", task="understand", repo=str(repo_root))
    candidates = result.get("candidates") or []
    c.check("bare-name ambiguity ('greet') surfaces >=2 candidates", len(candidates) >= 2)
    c.check(
        "every candidate carries callers (<=5) and a blast_summary",
        all(
            len(cand.get("callers") or []) <= 5 and isinstance(cand.get("blast_summary"), dict)
            for cand in candidates
        ),
        str(candidates),
    )


def _query_cluster_rerank_checks(c: _Check) -> None:
    with tempfile.TemporaryDirectory(prefix="repoweaver-verify-query-") as tmp:
        repo_root = Path(tmp) / "cluster"
        pkg = repo_root / "com" / "example" / "duty"
        pkg.mkdir(parents=True)
        (pkg / "DutyJudgementService.java").write_text(
            "package com.example.duty;\n"
            "public class DutyJudgementService {\n"
            "    public void decide() {}\n"
            "    public void judgement() {}\n"
            "    public void rule() {}\n"
            "}\n"
        )
        (pkg / "StockOutRuleStrategyTest.java").write_text(
            "package com.example.duty;\n"
            "public class StockOutRuleStrategyTest {\n"
            "    public void decideReasonBBBRuleJudgementDuty() {}\n"
            "}\n"
        )
        db_path = repo_root / ".repoweaver" / "graph.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with GraphStore(db_path) as store:
            Indexer(repo_root, store).build()

        result = explore(
            query="duty judgement decide rule", task="locate", repo=str(repo_root)
        )
        top = (result.get("slices") or [{}])[0].get("qualified_name", "")
        c.check(
            "multi-word query ranks the service-class cluster above a loud Test method",
            "DutyJudgementService" in top and "StockOutRuleStrategyTest" not in top,
            str(result.get("slices", [])[:2]),
        )


def _run_query_verification() -> VerifyResult:
    """`fabric verify --level query` — machine-verifies the v0.4.0
    query-facade layer: qualified-syntax direct resolution bypassing BM25
    (T1), ambiguity-as-panorama with callers/blast_summary (T2), owner-cluster
    reranking for multi-word queries (T3), and candidate context fields (T4).
    See docs/adr/0004-query-facade.md."""
    report: list[str] = ["RepoWeaver verify --level query"]
    c = _Check(report)

    if not _FIXTURE.exists() or not _OVERLOADS_FIXTURE.exists():
        return _fixture_missing(report, c, "javademo + overloads fixtures present", _FIXTURE)

    with _built_fixture_repo() as repo_root:
        report.append("-- T1: qualified-syntax direct resolution --")
        _query_qualified_syntax_checks(repo_root, c)

        report.append("-- T2: bare-name ambiguity panorama --")
        _query_ambiguity_panorama_checks(repo_root, c)

    with _built_fixture_repo(_OVERLOADS_FIXTURE, "overloads") as overloads_root:
        report.append("-- T1/T2: signature-disambiguated overloads + panorama --")
        _query_overload_signature_checks(overloads_root, c)

    report.append("-- T3: owner-cluster reranking --")
    _query_cluster_rerank_checks(c)

    return _finish(report, c)


# ----------------------------------------------------------------------
# benchmark — gt_demo fixture regression + gate-mechanism check
# ----------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_GROUND_TRUTH = _PROJECT_ROOT / "benchmarks" / "ground_truth.yaml"
_GT_FIXTURE = _PROJECT_ROOT / "benchmarks" / "fixtures" / "gt_demo"
_SOTA_TARGETS = _PROJECT_ROOT / "benchmarks" / "sota-targets.yaml"


def _approx(target: float, tol: float = 1e-9) -> object:
    """Tiny float-equality helper so this module doesn't need a pytest import."""

    class _Approx:
        def __eq__(self, other: object) -> bool:
            return isinstance(other, (int, float)) and abs(other - target) < tol

    return _Approx()


def _benchmark_metric_checks(result: dict, c: _Check) -> None:
    c.check("run status is MEASURED", result.get("status") == "MEASURED", str(result))
    c.check("nodes == 16", result.get("nodes") == 16, str(result.get("nodes")))
    c.check(
        "edges_total == 10",
        result.get("edges_total") == 10,
        str(result.get("edges_total")),
    )
    c.check(
        "edges_resolved == 8 (confidence>=0.5, non-ambiguous)",
        result.get("edges_resolved") == 8,
        str(result.get("edges_resolved")),
    )
    c.check(
        "edges_ambiguous == 2 (the deliberate close() collision)",
        result.get("edges_ambiguous") == 2,
        str(result.get("edges_ambiguous")),
    )
    c.check(
        "ambiguous_edge_rate == 2/10",
        result.get("ambiguous_edge_rate") == _approx(2 / 10),
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


def _benchmark_correctness_checks(result: dict, c: _Check) -> None:
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


def _benchmark_gate_checks(result: dict, c: _Check) -> None:
    from repoweaver.benchmark.compare import evaluate_gates, load_release_gates

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
    from repoweaver.benchmark.runner import run_benchmark

    report: list[str] = ["RepoWeaver verify --level benchmark"]
    c = _Check(report)

    if not _GROUND_TRUTH.exists() or not _GT_FIXTURE.exists():
        return _fixture_missing(
            report, c, "ground truth fixture present", _GROUND_TRUTH
        )

    result = run_benchmark(
        repo=_GT_FIXTURE,
        name="gt_demo",
        adapter="repoweaver",
        ground_truth=_GROUND_TRUTH,
    )

    report.append("-- metric definitions (regression, exact values on gt_demo) --")
    _benchmark_metric_checks(result, c)

    report.append("-- correctness (ground truth) --")
    _benchmark_correctness_checks(result, c)

    report.append("-- compare() gate mechanism --")
    _benchmark_gate_checks(result, c)

    return _finish(report, c)


# ----------------------------------------------------------------------
# m2 — watcher/incremental guarantees
# ----------------------------------------------------------------------


def _m2_watch_latency_checks(
    repo_root: Path, db_path: Path, pkg: Path, c: _Check
) -> None:
    import threading
    import time

    from repoweaver.watcher import watch_and_sync

    stop = threading.Event()
    events: list = []

    def _run_watcher() -> None:
        with GraphStore(db_path) as watch_store:
            watch_and_sync(
                repo_root,
                watch_store,
                debounce_ms=200,
                stop_event=stop,
                on_sync=lambda ch, de, st: events.append((ch, de, st)),
            )

    t = threading.Thread(target=_run_watcher)
    t.start()
    time.sleep(0.3)
    started = time.monotonic()
    (pkg / "A.java").write_text(
        "package com.example;\n"
        "public class A { public void foo() {} public void bar() {} }\n"
    )
    deadline = started + 5.0
    while time.monotonic() < deadline and not events:
        time.sleep(0.05)
    elapsed = time.monotonic() - started
    stop.set()
    t.join(timeout=5)
    c.check(
        "watch latency <= 5s from edit to sync callback",
        bool(events) and elapsed <= 5.0,
        f"elapsed={elapsed:.2f}s events={len(events)}",
    )


def _m2_incremental_equivalence_checks(
    repo_root: Path, db_path: Path, pkg: Path, c: _Check
) -> None:
    from repoweaver.benchmark.metrics import graph_signature

    for i in range(3):
        (pkg / f"Extra{i}.java").write_text(
            f"package com.example;\n"
            f"public class Extra{i} {{ public void run() {{ new A().foo(); }} }}\n"
        )
    with GraphStore(db_path) as store:
        Indexer(repo_root, store).build()

    (pkg / "A.java").write_text(
        "package com.example;\n"
        "public class A { public void foo() {} public void baz() {} }\n"
    )
    renamed: set[str] = set()
    deleted_renamed: set[str] = set()
    for i in range(3):
        old = pkg / f"Extra{i}.java"
        new_rel = f"com/example/Renamed{i}.java"
        content = old.read_text().replace(f"Extra{i}", f"Renamed{i}")
        old.unlink()
        (repo_root / new_rel).write_text(content)
        deleted_renamed.add(f"com/example/Extra{i}.java")
        renamed.add(new_rel)
    (pkg / "ToDelete.java").write_text(
        "package com.example;\npublic class ToDelete {}\n"
    )
    with GraphStore(db_path) as store:
        Indexer(repo_root, store).build()
    (pkg / "ToDelete.java").unlink()

    with GraphStore(db_path) as store:
        Indexer(repo_root, store).build_incremental(
            changed={"com/example/A.java"} | renamed,
            deleted=deleted_renamed | {"com/example/ToDelete.java"},
        )
        incremental_sig = graph_signature(store)
        edges = store.conn.execute(
            "SELECT type, provenance, confidence FROM edge"
        ).fetchall()

    full_db_path = repo_root / ".repoweaver" / "graph_full_check.db"
    with GraphStore(full_db_path) as store2:
        Indexer(repo_root, store2).build()
        full_sig = graph_signature(store2)

    c.check(
        "incremental rebuild hash == full rebuild hash",
        incremental_sig == full_sig,
        f"{incremental_sig} != {full_sig}",
    )
    c.check(
        "every resolved edge has provenance + confidence in [0,1]",
        all(e["provenance"] and 0.0 <= e["confidence"] <= 1.0 for e in edges)
        and len(edges) > 0,
        str([dict(e) for e in edges]),
    )


def _m2_ambiguous_candidate_checks(repo_root: Path, db_path: Path, c: _Check) -> None:
    import json

    (repo_root / "com/example/Left.java").write_text(
        "package com.example;\npublic class Left { static void shared() {} }\n"
    )
    (repo_root / "com/example/Caller2.java").write_text(
        "package com.example;\npublic class Caller2 { void run() { shared(); } }\n"
    )
    with GraphStore(db_path) as store:
        Indexer(repo_root, store).build_incremental(
            changed={"com/example/Left.java", "com/example/Caller2.java"},
            deleted=set(),
        )
    (repo_root / "com/example/Right.java").write_text(
        "package com.example;\npublic class Right { static void shared() {} }\n"
    )
    with GraphStore(db_path) as store:
        Indexer(repo_root, store).build_incremental(
            changed={"com/example/Right.java"}, deleted=set()
        )
        rows = store.conn.execute(
            "SELECT candidates FROM unresolved_reference WHERE target_name = 'shared'"
        ).fetchall()
    c.check(
        "ambiguous call site tracked with >=2 candidates, no polluted edge",
        len(rows) == 1 and len(json.loads(rows[0]["candidates"])) == 2,
        str(rows),
    )


def _m2_entry_point_checks(repo_root: Path, db_path: Path, c: _Check) -> None:
    (repo_root / "com/example/Ctrl.java").write_text(
        "package com.example;\n"
        "public class Ctrl {\n"
        "    @GetMapping\n"
        "    public void handle() {}\n"
        "}\n"
    )
    with GraphStore(db_path) as store:
        Indexer(repo_root, store).build_incremental(
            changed={"com/example/Ctrl.java"}, deleted=set()
        )
        node = store.find_by_qualified_name("com.example.Ctrl#handle()")[0]
    c.check(
        "annotated method flagged as HTTP_ROUTE entry point",
        bool(node["is_entry_point"]) and node["entry_point_kind"] == "HTTP_ROUTE",
        str(node),
    )


def _run_m2_verification() -> VerifyResult:
    """`fabric verify --level m2` — machine-verifies the watcher/incremental
    guarantees this milestone adds: watch latency, incremental==full
    equivalence after edit/delete/rename, ambiguous-candidate bookkeeping,
    entry-point detection, and per-edge provenance/confidence — all against
    a disposable temp repo, never the bundled fixtures in place."""
    report: list[str] = ["RepoWeaver verify --level m2"]
    c = _Check(report)

    with tempfile.TemporaryDirectory(prefix="repoweaver-verify-m2-") as tmp:
        repo_root = Path(tmp) / "m2repo"
        pkg = repo_root / "com" / "example"
        pkg.mkdir(parents=True)
        (pkg / "A.java").write_text(
            "package com.example;\npublic class A { public void foo() {} }\n"
        )
        db_path = repo_root / ".repoweaver" / "graph.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with GraphStore(db_path) as store:
            Indexer(repo_root, store).build()

        report.append("-- watcher latency & freshness --")
        _m2_watch_latency_checks(repo_root, db_path, pkg, c)

        report.append("-- incremental == full rebuild after edit/delete/rename --")
        _m2_incremental_equivalence_checks(repo_root, db_path, pkg, c)

        report.append("-- ambiguous candidate bookkeeping --")
        _m2_ambiguous_candidate_checks(repo_root, db_path, c)

        report.append("-- entry-point detection --")
        _m2_entry_point_checks(repo_root, db_path, c)

    return _finish(report, c)


# ----------------------------------------------------------------------
# m3 — SCIP typed-overlay guarantees
# ----------------------------------------------------------------------

_M3_FIXTURE = _PROJECT_ROOT / "tests" / "fixtures" / "m3typed"


def _m3_dispatch_and_overload_checks(store: GraphStore) -> tuple[dict, dict]:
    from repoweaver.graph.store import edge_id

    pkg = "com.example.m3typed"
    dispatch_id = store.find_by_qualified_name(f"{pkg}.Caller#dispatch(Shape)")[0]["id"]
    shape_area_id = store.find_by_qualified_name(f"{pkg}.Shape#area()")[0]["id"]
    circle_area_id = store.find_by_qualified_name(f"{pkg}.Circle#area()")[0]["id"]
    run_id = store.find_by_qualified_name(f"{pkg}.Caller#run()")[0]["id"]
    process_int_id = store.find_by_qualified_name(f"{pkg}.Caller#process(int)")[0]["id"]
    process_str_id = store.find_by_qualified_name(f"{pkg}.Caller#process(String)")[0][
        "id"
    ]

    dispatch = {
        "typed_to_interface": store.conn.execute(
            "SELECT id FROM edge WHERE id = ?",
            (edge_id(dispatch_id, shape_area_id, "CALLS_TYPED"),),
        ).fetchone(),
        "typed_to_impl": store.conn.execute(
            "SELECT id FROM edge WHERE from_id = ? AND to_id = ?",
            (dispatch_id, circle_area_id),
        ).fetchone(),
    }
    overload = {
        "int_edge": store.conn.execute(
            "SELECT id FROM edge WHERE id = ?",
            (edge_id(run_id, process_int_id, "CALLS_TYPED"),),
        ).fetchone(),
        "str_edge": store.conn.execute(
            "SELECT id FROM edge WHERE id = ?",
            (edge_id(run_id, process_str_id, "CALLS_TYPED"),),
        ).fetchone(),
    }
    return dispatch, overload


def _run_m3_verification() -> VerifyResult:
    """`fabric verify --level m3` — machine-verifies the SCIP typed-overlay
    guarantees against the bundled m3typed fixture: interface-typed calls
    land precisely on the interface method (not an implementation),
    same-arity overloads are distinguished by argument type, merging typed
    with textual edges loses no edges, repeated overlay runs are idempotent,
    and the resulting graph hash is deterministic."""
    import shutil
    import tempfile

    from repoweaver.benchmark.metrics import graph_signature
    from repoweaver.typed.overlay import run_overlay

    report: list[str] = ["RepoWeaver verify --level m3"]
    c = _Check(report)

    if not _M3_FIXTURE.exists() or not (_M3_FIXTURE / "index.scip").exists():
        return _fixture_missing(
            report, c, "m3typed fixture + index.scip present", _M3_FIXTURE
        )

    with tempfile.TemporaryDirectory(prefix="repoweaver-verify-m3-") as tmp:
        repo_root = Path(tmp) / "m3typed"
        shutil.copytree(_M3_FIXTURE, repo_root)
        db_path = repo_root / ".repoweaver" / "graph.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with GraphStore(db_path) as store:
            Indexer(repo_root, store).build()
            edges_before = store.conn.execute("SELECT COUNT(*) FROM edge").fetchone()[0]

        report.append("-- overlay merge --")
        stats = run_overlay(repo_root, repo_root / "index.scip")
        c.check(
            "every SCIP reference in the fixture mapped (never guessed)",
            stats.unmapped_symbols == 0 and stats.mapped == stats.typed_refs,
            str(stats.as_dict()),
        )

        with GraphStore(db_path) as store:
            dispatch, overload = _m3_dispatch_and_overload_checks(store)

            report.append("-- interface dispatch precision --")
            c.check(
                "typed call from dispatch() lands on Shape#area()",
                dispatch["typed_to_interface"] is not None,
            )
            c.check(
                "typed call does NOT land on Circle#area() (the implementation)",
                dispatch["typed_to_impl"] is None,
            )

            report.append("-- overload disambiguation --")
            c.check(
                "process(int) and process(String) resolve to distinct typed edges",
                overload["int_edge"] is not None
                and overload["str_edge"] is not None
                and overload["int_edge"]["id"] != overload["str_edge"]["id"],
            )

            report.append("-- lossless merge --")
            edges_after = store.conn.execute("SELECT COUNT(*) FROM edge").fetchone()[0]
            c.check(
                "merging typed + textual edges drops no edges",
                edges_after >= edges_before,
                f"before={edges_before} after={edges_after}",
            )
            merged_typed = store.conn.execute(
                "SELECT COUNT(*) FROM edge WHERE type LIKE '%_TYPED' "
                "AND provenance = 'scip_java+tree_sitter_java'"
            ).fetchone()[0]
            c.check(
                "at least one edge upgraded via merge (both provenances kept)",
                merged_typed >= 1,
                str(merged_typed),
            )

            sig_before_rerun = graph_signature(store)

        report.append("-- idempotency & determinism --")
        stats2 = run_overlay(repo_root, repo_root / "index.scip")
        with GraphStore(db_path) as store:
            sig_after_rerun = graph_signature(store)
            edge_count_after_rerun = store.conn.execute(
                "SELECT COUNT(*) FROM edge"
            ).fetchone()[0]
        c.check(
            "repeated overlay run reports identical stats",
            stats.as_dict() == stats2.as_dict(),
            f"{stats.as_dict()} != {stats2.as_dict()}",
        )
        c.check(
            "repeated overlay run does not change edge count",
            edge_count_after_rerun == edges_after,
            f"{edge_count_after_rerun} != {edges_after}",
        )
        c.check(
            "graph_signature is stable across repeated overlay runs",
            sig_before_rerun == sig_after_rerun,
            f"{sig_before_rerun} != {sig_after_rerun}",
        )

    return _finish(report, c)
