"""Pattern-based entry-point rules (implements/extends suffix matching)."""

from __future__ import annotations

import shutil
from pathlib import Path

from codecontextfabric.graph.store import GraphStore
from codecontextfabric.indexer import Indexer

FIXTURE = Path(__file__).parent / "fixtures" / "javademo"


def _repo_with_config(tmp_path: Path, yaml_body: str) -> Path:
    repo = tmp_path / "repo"
    shutil.copytree(FIXTURE, repo)
    cfg = repo / ".repoweaver" / "entrypoints.yaml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(yaml_body, encoding="utf-8")
    return repo


def _build(repo: Path) -> GraphStore:
    db = repo / ".repoweaver" / "graph.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    store = GraphStore(db).open()
    Indexer(repo, store).build()
    return store


def test_implements_suffix_pattern_marks_entry_point(tmp_path: Path) -> None:
    repo = _repo_with_config(
        tmp_path,
        "mode: merge\nimplements_patterns:\n    Greeter: RPC_PROVIDER\n",
    )
    store = _build(repo)
    try:
        # EnglishGreeter implements Greeter -> RPC_PROVIDER entry
        node = store.find_by_qualified_name("com.example.demo.EnglishGreeter")[0]
        assert node["is_entry_point"] == 1
        assert node["entry_point_kind"] == "RPC_PROVIDER"
        # a class without the pattern-matching supertype stays unmarked
        other = store.find_by_qualified_name("com.example.demo.App")[0]
        assert other["is_entry_point"] == 0
    finally:
        store.close()


def test_without_config_no_pattern_entries(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    shutil.copytree(FIXTURE, repo)
    store = _build(repo)
    try:
        node = store.find_by_qualified_name("com.example.demo.EnglishGreeter")[0]
        assert node["is_entry_point"] == 0
    finally:
        store.close()


def test_malformed_config_never_crashes_build(tmp_path: Path) -> None:
    repo = _repo_with_config(tmp_path, "::: not yaml [[[")
    store = _build(repo)  # must not raise
    store.close()
