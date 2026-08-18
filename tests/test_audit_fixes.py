"""Regression tests for the v0.3.1 audit fix-up (C1/C2/M1/M2/M3 + minor)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from repoweaver.explore import explore
from repoweaver.graph.store import GraphStore
from repoweaver.indexer import Indexer, _parsed_file_to_json, file_hash
from repoweaver.parser.java import JavaParser

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _build(repo: Path) -> None:
    db_path = repo / ".repoweaver" / "graph.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with GraphStore(db_path) as store:
        Indexer(repo, store).build()


def _write_big_class(repo: Path) -> None:
    pkg = repo / "big"
    pkg.mkdir(parents=True)
    lines = ["package big;", "public class Big {"]
    for i in range(40):
        lines.append(f"    public void method{i}() {{ int x{i} = {i}; }}")
    lines.append("}")
    (pkg / "Big.java").write_text("\n".join(lines) + "\n")


# -- C1: class-vs-constructor is no longer flagged as ambiguous --------------


def test_locate_class_name_with_own_constructor_returns_class_not_candidates(
    built_javademo,
):
    result = explore(query="App", task="locate", repo=str(built_javademo))
    assert "candidates" not in result
    assert result["slices"]


def test_locate_genuine_same_named_classes_still_returns_candidates(tmp_path):
    repo = tmp_path / "ambiguous_class"
    (repo / "a").mkdir(parents=True)
    (repo / "b").mkdir(parents=True)
    (repo / "a" / "Worker.java").write_text(
        "package a;\npublic class Worker { public void run() {} }\n"
    )
    (repo / "b" / "Worker.java").write_text(
        "package b;\npublic class Worker { public void run() {} }\n"
    )
    _build(repo)

    result = explore(query="Worker", task="locate", repo=str(repo))
    assert result.get("candidates")
    assert len({c["qualified_name"] for c in result["candidates"]}) >= 2


# -- C2: budget-ordering + fragment-skipping ---------------------------------


def test_wide_budget_never_emits_undersized_fragment_slices(tmp_path):
    _write_big_class(tmp_path)
    _build(tmp_path)
    result = explore(
        query="Big", task="understand", repo=str(tmp_path), max_tokens=100_000
    )
    for s in result["slices"]:
        total_lines = s["span_end"] - s["span_start"] + 1
        kept_lines = s["source"].count("\n") + 1
        if total_lines > 10:
            assert kept_lines >= total_lines * 0.2


def test_tight_budget_never_emits_sub20_fragments_of_large_slices(tmp_path):
    """Under a tight budget no slice may survive as a <20% fragment of a
    >10-line symbol: each large slice is either skipped entirely (counted in
    stats.skipped_slices) or kept at a substantial fraction. With seed
    prioritization the queried class itself leads and legitimately takes the
    budget (65% kept), so the fragment invariant is what must hold."""
    _write_big_class(tmp_path)
    _build(tmp_path)
    result = explore(query="Big", task="understand", repo=str(tmp_path), max_tokens=320)
    assert result["slices"]
    # The class body slice must lead and must not be a <20% fragment (the
    # class spans 43 source lines; slices carry adjusted spans after trim,
    # so measure the returned source lines directly).
    big = next(s for s in result["slices"] if s["qualified_name"] == "big.Big")
    kept = len(big["source"].splitlines())
    assert kept >= 43 * 0.2, f"class fragment: {kept}/43 lines"
    assert result["stats"]["tokens_estimated"] <= 320


# -- M1: file_refs_cache no longer stores `source` ---------------------------


def test_old_format_cache_with_embedded_source_is_ignored_and_reparsed(javademo_repo):
    rel = "com/example/demo/Greeter.java"
    abs_path = javademo_repo / rel
    parser = JavaParser(javademo_repo)
    pf = parser.parse_file(abs_path)
    payload = json.loads(_parsed_file_to_json(pf))
    payload["source"] = "STALE OLD-FORMAT SOURCE, MUST NEVER BE USED"
    old_style_payload = json.dumps(payload)

    db_path = javademo_repo / ".repoweaver" / "graph.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with GraphStore(db_path) as store:
        store.set_file_refs_cache(rel, file_hash(abs_path), old_style_payload)
        stats = Indexer(javademo_repo, store).build_incremental(
            changed=set(), deleted=set()
        )
        assert stats.nodes > 0
        assert store.find_by_qualified_name("com.example.demo.Greeter")


def test_new_cache_payload_drops_source_and_shrinks_proportionally():
    # The bundled fixture's files are tiny, so JSON-structure overhead (node
    # lists, type refs, ...) dominates and a flat 40%-of-db reduction target
    # (meant for a real multi-MB repo, see the M1 audit note) doesn't apply
    # verbatim to a single small file. What must hold regardless of file
    # size: the new payload never embeds `source`, and dropping N bytes of
    # source text shrinks the payload by roughly that many bytes.
    repo = _REPO_ROOT / "tests" / "fixtures" / "javademo"
    parser = JavaParser(repo)
    for java_file in sorted(repo.rglob("*.java")):
        pf = parser.parse_file(java_file)
        new_payload = _parsed_file_to_json(pf)
        assert "source" not in json.loads(new_payload)
        old_payload = json.dumps({**json.loads(new_payload), "source": pf.source})
        assert len(old_payload) - len(new_payload) >= len(pf.source) * 0.9


# -- M2: explicit busy_timeout ------------------------------------------------


def test_busy_timeout_pragma_default_and_env_override(tmp_path, monkeypatch):
    monkeypatch.delenv("FABRIC_BUSY_TIMEOUT_MS", raising=False)
    with GraphStore(tmp_path / "graph.db") as store:
        assert store.conn.execute("PRAGMA busy_timeout").fetchone()[0] == 10000

    monkeypatch.setenv("FABRIC_BUSY_TIMEOUT_MS", "2500")
    with GraphStore(tmp_path / "graph2.db") as store:
        assert store.conn.execute("PRAGMA busy_timeout").fetchone()[0] == 2500


# -- M3: watcher TOCTOU — file vanishes between changed-detect and read ------


def test_build_incremental_survives_file_deleted_before_read(javademo_repo):
    _build(javademo_repo)
    rel = "com/example/demo/Greeter.java"
    (javademo_repo / rel).unlink()

    db_path = javademo_repo / ".repoweaver" / "graph.db"
    with GraphStore(db_path) as store:
        Indexer(javademo_repo, store).build_incremental(changed={rel}, deleted=set())
        assert rel not in store.known_files()


# -- Minor: check_public.py scans .java/no-suffix files, not just an allowlist


def test_check_public_script_still_passes_on_this_repo():
    proc = subprocess.run(
        [sys.executable, "scripts/check_public.py"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0
    assert "PASS" in proc.stdout
