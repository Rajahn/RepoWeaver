"""pytest configuration and shared fixtures for RepoWeaver tests."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from repoweaver.graph.store import GraphStore

FIXTURE_SRC = Path(__file__).parent / "fixtures" / "javademo"


@pytest.fixture()
def in_memory_store() -> GraphStore:
    """Return an open in-memory GraphStore for use in tests."""
    store = GraphStore(":memory:")
    store.open()
    yield store
    store.close()


@pytest.fixture()
def javademo_repo(tmp_path: Path) -> Path:
    """Copy the bundled Java fixture repo into a temp dir so tests can mutate it freely."""
    dest = tmp_path / "javademo"
    shutil.copytree(FIXTURE_SRC, dest)
    return dest


@pytest.fixture()
def built_javademo(javademo_repo: Path):
    """A javademo repo already indexed into `.repoweaver/graph.db`."""
    from repoweaver.indexer import Indexer

    db_path = javademo_repo / ".repoweaver" / "graph.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with GraphStore(db_path) as store:
        Indexer(javademo_repo, store).build()
    return javademo_repo
