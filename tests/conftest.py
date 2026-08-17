"""pytest configuration and shared fixtures for RepoWeaver tests."""

from __future__ import annotations

import pytest

from repoweaver.graph.store import GraphStore


@pytest.fixture()
def in_memory_store() -> GraphStore:
    """Return an open in-memory GraphStore for use in tests."""
    store = GraphStore(":memory:")
    store.open()
    yield store
    store.close()
