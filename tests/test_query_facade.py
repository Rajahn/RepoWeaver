"""v0.4.0 query-facade tests (T1-T5, see ADR-0004 and explore-contract v1.2).

T1: qualified-syntax direct resolution (`Class#method` / `Class.method` /
    `method(Sig)`) bypasses BM25 when it lands on exactly one node.
T2: ambiguity panorama — every candidate carries callers + blast_summary.
T3: multi-word queries cluster same-owner hits before ranking.
T4: candidate context fields (file/span/signature).
T5: `.repoweaver/entrypoints.yaml` merge/replace overrides.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from codecontextfabric.explore import explore
from codecontextfabric.graph.store import GraphStore
from codecontextfabric.indexer import Indexer, load_entry_point_annotations

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture()
def built_overloads(tmp_path: Path) -> Path:
    dest = tmp_path / "overloads"
    shutil.copytree(FIXTURES / "overloads", dest)
    db_path = dest / ".repoweaver" / "graph.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with GraphStore(db_path) as store:
        Indexer(dest, store).build()
    return dest


@pytest.fixture()
def built_cluster_repo(tmp_path: Path) -> Path:
    """A DutyJudgementService (four matching members) vs. a
    StockOutRuleStrategyTest (one method that repeats the same vocabulary)
    — the fixture T3's acceptance scenario describes."""
    pkg = tmp_path / "com" / "example" / "duty"
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
    db_path = tmp_path / ".repoweaver" / "graph.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with GraphStore(db_path) as store:
        Indexer(tmp_path, store).build()
    return tmp_path


# ---------------------------------------------------------------------------
# T1 — qualified syntax direct resolution
# ---------------------------------------------------------------------------

QUALIFIED_FORMS = ["Formatter#format", "Formatter.format", "format(String)"]
TASKS = ["understand", "impact", "locate", "debug"]


@pytest.mark.parametrize("query", QUALIFIED_FORMS)
@pytest.mark.parametrize("task", TASKS)
def test_qualified_syntax_resolves_directly(built_javademo, query, task):
    result = explore(query=query, task=task, repo=str(built_javademo))
    assert "candidates" not in result, result
    assert result["slices"], result
    assert (
        result["slices"][0]["qualified_name"]
        == "com.example.demo.Formatter#format(String)"
    )


def test_qualified_syntax_with_signature_disambiguates_overload(built_overloads):
    result = explore(
        query="Codec#fromJson(String,Class)",
        task="understand",
        repo=str(built_overloads),
    )
    assert "candidates" not in result, result
    assert (
        result["slices"][0]["qualified_name"]
        == "com.example.overloads.Codec#fromJson(String,Class)"
    )


def test_qualified_syntax_normalizes_generics_and_whitespace(built_overloads):
    result = explore(
        query="Codec#fromJson(String, Class<?>)",
        task="understand",
        repo=str(built_overloads),
    )
    assert "candidates" not in result, result
    assert (
        result["slices"][0]["qualified_name"]
        == "com.example.overloads.Codec#fromJson(String,Class)"
    )


def test_qualified_syntax_short_owner_matches_bare_sig_form(built_overloads):
    result = explore(
        query="write(String)", task="understand", repo=str(built_overloads)
    )
    assert "candidates" not in result, result
    assert (
        result["slices"][0]["qualified_name"]
        == "com.example.overloads.Codec#write(String)"
    )


def test_unresolvable_qualified_query_falls_back_to_normal_search(built_javademo):
    """A fully-qualified type name parses as a dot-form owner.name too, but
    resolves to zero members — it must fall back to BM25, not return empty."""
    result = explore(
        query="com.example.demo.Formatter", task="understand", repo=str(built_javademo)
    )
    assert result["slices"], result


# ---------------------------------------------------------------------------
# T2 — ambiguity is the answer: panorama with callers + blast_summary
# ---------------------------------------------------------------------------


def test_bare_name_ambiguity_panorama_has_callers_and_blast_summary(built_javademo):
    result = explore(query="greet", task="understand", repo=str(built_javademo))
    assert "candidates" in result, result
    assert len(result["candidates"]) >= 2
    for c in result["candidates"]:
        assert isinstance(c["callers"], list)
        assert len(c["callers"]) <= 5
        for caller in c["callers"]:
            assert "confidence" in caller and "qualified_name" in caller
        assert isinstance(c["blast_summary"], dict)


def test_qualified_multi_match_returns_panorama(built_overloads):
    result = explore(
        query="Codec#fromJson", task="understand", repo=str(built_overloads)
    )
    assert "candidates" in result, result
    assert len(result["candidates"]) == 3
    assert result["slices"] == []
    names = {c["qualified_name"] for c in result["candidates"]}
    assert "com.example.overloads.Codec#fromJson(String,Class)" in names


def test_ambiguity_panorama_skipped_for_debug_task(built_overloads):
    """debug needs a concrete start node to search a call path from, so
    ambiguity must not short-circuit it (pre-existing contract behaviour,
    unaffected by the qualified-syntax fast path)."""
    result = explore(query="Codec#fromJson", task="debug", repo=str(built_overloads))
    assert "candidates" not in result


# ---------------------------------------------------------------------------
# T3 — owner-cluster reranking for multi-word queries
# ---------------------------------------------------------------------------


def test_multiword_query_ranks_owner_cluster_over_loud_test_method(built_cluster_repo):
    result = explore(
        query="duty judgement decide rule",
        task="locate",
        repo=str(built_cluster_repo),
    )
    assert result["slices"], result
    top = result["slices"][0]["qualified_name"]
    assert "DutyJudgementService" in top, result["slices"][:3]
    assert "StockOutRuleStrategyTest" not in top


def test_singleword_query_unaffected_by_cluster_rerank(built_javademo):
    result = explore(query="Greeter", task="understand", repo=str(built_javademo))
    assert result["slices"][0]["qualified_name"] == "com.example.demo.Greeter"


# ---------------------------------------------------------------------------
# T4 — candidate context fields
# ---------------------------------------------------------------------------


def test_candidates_carry_file_span_and_signature(built_javademo):
    result = explore(query="greet", task="understand", repo=str(built_javademo))
    for c in result["candidates"]:
        assert c["file"].endswith(".java")
        assert c["span_start"] >= 1
        assert c["span_end"] >= c["span_start"]
        assert "signature" in c


# ---------------------------------------------------------------------------
# T5 — configurable entry-point annotations
# ---------------------------------------------------------------------------


def _write_job_repo(tmp_path: Path) -> Path:
    pkg = tmp_path / "com" / "example"
    pkg.mkdir(parents=True)
    (pkg / "Job.java").write_text(
        "package com.example;\n"
        "public class Job {\n"
        "    @MyBatchJob\n"
        "    public void run() {}\n"
        "}\n"
    )
    return tmp_path


def test_entrypoints_yaml_merge_adds_custom_annotation(tmp_path):
    repo_root = _write_job_repo(tmp_path)
    config_dir = repo_root / ".repoweaver"
    config_dir.mkdir(parents=True)
    (config_dir / "entrypoints.yaml").write_text(
        "mode: merge\nannotations:\n  MyBatchJob: SCHEDULED\n"
    )
    db_path = config_dir / "graph.db"
    with GraphStore(db_path) as store:
        Indexer(repo_root, store).build()
        node = store.find_by_qualified_name("com.example.Job#run()")[0]

    assert bool(node["is_entry_point"])
    assert node["entry_point_kind"] == "SCHEDULED"
    assert "RestController" in load_entry_point_annotations(repo_root)


def test_entrypoints_yaml_replace_drops_builtins(tmp_path):
    repo_root = _write_job_repo(tmp_path)
    config_dir = repo_root / ".repoweaver"
    config_dir.mkdir(parents=True)
    (config_dir / "entrypoints.yaml").write_text(
        "mode: replace\nannotations:\n  MyBatchJob: SCHEDULED\n"
    )
    table = load_entry_point_annotations(repo_root)
    assert table == {"MyBatchJob": "SCHEDULED"}


def test_entrypoints_yaml_absent_uses_builtin_defaults(tmp_path):
    from codecontextfabric.indexer import ENTRY_POINT_ANNOTATIONS

    assert load_entry_point_annotations(tmp_path) == ENTRY_POINT_ANNOTATIONS
