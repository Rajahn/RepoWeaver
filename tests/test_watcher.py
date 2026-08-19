"""Real-filesystem watcher tests: latency, deletes, batch renames, and the
resulting graph's equivalence to a from-scratch full build."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from codecontextfabric.benchmark.metrics import graph_signature
from codecontextfabric.graph.store import GraphStore
from codecontextfabric.indexer import Indexer
from codecontextfabric.watcher import watch_and_sync

PKG = Path("com/example")


def _write(repo: Path, rel: str, content: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    _write(
        root,
        "com/example/A.java",
        "package com.example;\npublic class A { public void foo() {} }\n",
    )
    db_path = root / ".repoweaver" / "graph.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with GraphStore(db_path) as store:
        Indexer(root, store).build()
    return root


def _run_watcher(
    repo: Path, stop: threading.Event, events: list, debounce_ms: int = 200
):
    db_path = repo / ".repoweaver" / "graph.db"
    with GraphStore(db_path) as store:
        watch_and_sync(
            repo_root=repo,
            store=store,
            debounce_ms=debounce_ms,
            stop_event=stop,
            on_sync=lambda changed, deleted, stats: events.append(
                (changed, deleted, stats)
            ),
        )


def test_watcher_syncs_within_five_seconds_of_an_edit(repo: Path) -> None:
    stop = threading.Event()
    events: list = []
    t = threading.Thread(target=_run_watcher, args=(repo, stop, events))
    t.start()
    try:
        time.sleep(0.3)
        started = time.monotonic()
        _write(
            repo,
            "com/example/A.java",
            "package com.example;\npublic class A { public void foo() {} public void bar() {} }\n",
        )
        deadline = started + 5.0
        while time.monotonic() < deadline and not events:
            time.sleep(0.05)
        elapsed = time.monotonic() - started
        assert events, "watcher did not fire on_sync within 5s"
        assert elapsed <= 5.0
        changed, deleted, stats = events[0]
        assert changed == {"com/example/A.java"}
        assert deleted == set()
        assert stats.nodes >= 2  # class + both methods
    finally:
        stop.set()
        t.join(timeout=5)

    db_path = repo / ".repoweaver" / "graph.db"
    with GraphStore(db_path) as store:
        response_freshness_ok = store.is_fresh(
            Indexer(repo, store).current_file_hashes()
        )[0]
    assert response_freshness_ok is True


def test_watcher_handles_delete(repo: Path) -> None:
    _write(
        repo,
        "com/example/B.java",
        "package com.example;\npublic class B { public void bar() {} }\n",
    )
    db_path = repo / ".repoweaver" / "graph.db"
    with GraphStore(db_path) as store:
        Indexer(repo, store).build()

    stop = threading.Event()
    events: list = []
    t = threading.Thread(target=_run_watcher, args=(repo, stop, events))
    t.start()
    try:
        time.sleep(0.3)
        (repo / "com/example/B.java").unlink()
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not events:
            time.sleep(0.05)
        assert events
        _changed, deleted, _stats = events[0]
        assert deleted == {"com/example/B.java"}
    finally:
        stop.set()
        t.join(timeout=5)

    with GraphStore(db_path) as store:
        assert store.find_by_qualified_name("com.example.B") == []


def test_batch_rename_of_ten_files_matches_full_rebuild(repo: Path) -> None:
    for i in range(10):
        _write(
            repo,
            f"com/example/Old{i}.java",
            f"package com.example;\npublic class Old{i} {{ public void run() {{ new A().foo(); }} }}\n",
        )
    db_path = repo / ".repoweaver" / "graph.db"
    with GraphStore(db_path) as store:
        Indexer(repo, store).build()

    changed: set[str] = set()
    deleted: set[str] = set()
    for i in range(10):
        old_rel = f"com/example/Old{i}.java"
        new_rel = f"com/example/New{i}.java"
        content = (repo / old_rel).read_text().replace(f"Old{i}", f"New{i}")
        (repo / old_rel).unlink()
        _write(repo, new_rel, content)
        deleted.add(old_rel)
        changed.add(new_rel)

    with GraphStore(db_path) as store:
        Indexer(repo, store).build_incremental(changed=changed, deleted=deleted)
        incremental_sig = graph_signature(store)

    full_db_path = repo / ".repoweaver" / "graph_full.db"
    with GraphStore(full_db_path) as store2:
        Indexer(repo, store2).build()
        full_sig = graph_signature(store2)

    assert incremental_sig == full_sig


def test_ambiguous_candidates_survive_incremental_sync(repo: Path) -> None:
    _write(
        repo,
        "com/example/Caller.java",
        "package com.example;\npublic class Caller { void run() { shared(); } }\n",
    )
    _write(
        repo,
        "com/example/Left.java",
        "package com.example;\npublic class Left { static void shared() {} }\n",
    )
    db_path = repo / ".repoweaver" / "graph.db"
    with GraphStore(db_path) as store:
        Indexer(repo, store).build()

    _write(
        repo,
        "com/example/Right.java",
        "package com.example;\npublic class Right { static void shared() {} }\n",
    )
    with GraphStore(db_path) as store:
        Indexer(repo, store).build_incremental(
            changed={"com/example/Right.java"}, deleted=set()
        )
        rows = store.conn.execute(
            "SELECT candidates FROM unresolved_reference WHERE target_name = 'shared'"
        ).fetchall()
        assert len(rows) == 1
        assert len(__import__("json").loads(rows[0]["candidates"])) == 2


def test_entry_point_annotation_survives_incremental_sync(repo: Path) -> None:
    db_path = repo / ".repoweaver" / "graph.db"
    _write(
        repo,
        "com/example/Ctrl.java",
        "package com.example;\n"
        "public class Ctrl {\n"
        "    @GetMapping\n"
        "    public void handle() {}\n"
        "}\n",
    )
    with GraphStore(db_path) as store:
        Indexer(repo, store).build_incremental(
            changed={"com/example/Ctrl.java"}, deleted=set()
        )
        node = store.find_by_qualified_name("com.example.Ctrl#handle()")[0]
        assert node["is_entry_point"] == 1
        assert node["entry_point_kind"] == "HTTP_ROUTE"
