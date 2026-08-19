from __future__ import annotations

from codecontextfabric.graph.store import GraphStore
from codecontextfabric.indexer import Indexer


def test_check_reports_ok_immediately_after_build(built_javademo):
    with GraphStore(built_javademo / ".repoweaver" / "graph.db") as store:
        indexer = Indexer(built_javademo, store)
        fresh, stale = store.is_fresh(indexer.current_file_hashes())
        assert fresh
        assert stale == []


def test_check_reports_stale_after_file_content_changes(built_javademo):
    target = built_javademo / "com/example/demo/Formatter.java"
    target.write_text(target.read_text() + "\n// changed\n")

    with GraphStore(built_javademo / ".repoweaver" / "graph.db") as store:
        indexer = Indexer(built_javademo, store)
        fresh, stale = store.is_fresh(indexer.current_file_hashes())
        assert not fresh
        assert "com/example/demo/Formatter.java" in stale


def test_check_reports_stale_after_new_file_added(built_javademo):
    new_file = built_javademo / "com/example/demo/Extra.java"
    new_file.write_text("package com.example.demo;\n\npublic class Extra {\n}\n")
    with GraphStore(built_javademo / ".repoweaver" / "graph.db") as store:
        indexer = Indexer(built_javademo, store)
        fresh, stale = store.is_fresh(indexer.current_file_hashes())
        assert not fresh
        assert "com/example/demo/Extra.java" in stale


def test_rebuild_picks_up_deleted_file(built_javademo):
    target = built_javademo / "com/example/demo/Level.java"
    target.unlink()

    with GraphStore(built_javademo / ".repoweaver" / "graph.db") as store:
        Indexer(built_javademo, store).build()
        assert store.find_by_qualified_name("com.example.demo.Level") == []
