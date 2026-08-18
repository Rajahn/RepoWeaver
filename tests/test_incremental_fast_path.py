"""Correctness anchor for the P0-A incremental fast path (`Indexer._try_fast_incremental`).

Every case here builds twice — once via `build_incremental` (which may take the
fast path or fall back to `_full_sync`), once via a from-scratch `build()` — and
asserts `graph_signature` is byte-identical between the two. That equivalence is
the non-negotiable invariant; which path got taken is a secondary assertion
(`stats.incremental`) used to confirm the fast path is actually being exercised
and not silently always falling back.
"""

from __future__ import annotations

import random
from pathlib import Path

from repoweaver.benchmark.metrics import graph_signature
from repoweaver.graph.store import GraphStore
from repoweaver.indexer import Indexer


def _write(repo: Path, rel: str, content: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _build(root: Path) -> None:
    db_path = root / ".repoweaver" / "graph.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with GraphStore(db_path) as store:
        Indexer(root, store).build()


def _sync_incremental(root: Path, changed: set[str], deleted: set[str] | None = None):
    db_path = root / ".repoweaver" / "graph.db"
    with GraphStore(db_path) as store:
        stats = Indexer(root, store).build_incremental(changed=changed, deleted=deleted)
        sig = graph_signature(store)
    return stats, sig


def _full_signature(root: Path) -> str:
    full_db = root / ".repoweaver" / "graph_full_check.db"
    with GraphStore(full_db) as store:
        Indexer(root, store).build()
        return graph_signature(store)


def _base_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    _write(
        root,
        "com/example/A.java",
        "package com.example;\n"
        "public class A {\n"
        "    public void foo() { bar(); }\n"
        "    public void bar() { System.out.println(1); }\n"
        "}\n",
    )
    _write(
        root,
        "com/example/B.java",
        "package com.example;\npublic class B extends A { public void run() { foo(); } }\n",
    )
    _build(root)
    return root


def test_body_only_edit_takes_fast_path_and_matches_full_rebuild(tmp_path: Path) -> None:
    root = _base_repo(tmp_path)
    _write(
        root,
        "com/example/A.java",
        "package com.example;\n"
        "public class A {\n"
        "    public void foo() { bar(); bar(); }\n"  # body edit only
        "    public void bar() { System.out.println(2); }\n"
        "}\n",
    )
    stats, incremental_sig = _sync_incremental(root, changed={"com/example/A.java"})
    assert stats.incremental is True
    assert incremental_sig == _full_signature(root)


def test_signature_change_escalates_but_still_matches_full_rebuild(tmp_path: Path) -> None:
    root = _base_repo(tmp_path)
    _write(
        root,
        "com/example/A.java",
        "package com.example;\n"
        "public class A {\n"
        "    public void foo() { bar(1); }\n"
        "    public void bar(int x) { System.out.println(x); }\n"  # new arity
        "}\n",
    )
    stats, incremental_sig = _sync_incremental(root, changed={"com/example/A.java"})
    assert stats.incremental is False
    assert incremental_sig == _full_signature(root)


def test_supertype_change_escalates_but_still_matches_full_rebuild(tmp_path: Path) -> None:
    root = _base_repo(tmp_path)
    _write(
        root,
        "com/example/C.java",
        "package com.example;\npublic class C { public void run() {} }\n",
    )
    _build(root)
    _write(
        root,
        "com/example/B.java",
        "package com.example;\npublic class B extends C { public void run() { foo(); } }\n",
    )
    stats, incremental_sig = _sync_incremental(root, changed={"com/example/B.java"})
    assert stats.incremental is False
    assert incremental_sig == _full_signature(root)


def test_new_file_introducing_ambiguity_escalates_but_still_matches_full_rebuild(
    tmp_path: Path,
) -> None:
    root = _base_repo(tmp_path)
    _write(
        root,
        "com/example/D.java",
        "package com.example;\npublic class D { public void foo() {} }\n",
    )
    stats, incremental_sig = _sync_incremental(root, changed={"com/example/D.java"})
    assert stats.incremental is False
    assert incremental_sig == _full_signature(root)


def test_batch_of_body_only_edits_takes_fast_path(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    for i in range(15):
        _write(
            root,
            f"com/example/F{i}.java",
            f"package com.example;\npublic class F{i} {{ public void run() {{ }} }}\n",
        )
    _build(root)

    changed = set()
    for i in range(15):
        rel = f"com/example/F{i}.java"
        _write(
            root,
            rel,
            f"package com.example;\npublic class F{i} {{ public void run() {{ int x = {i}; }} }}\n",
        )
        changed.add(rel)

    stats, incremental_sig = _sync_incremental(root, changed=changed)
    assert stats.incremental is True
    assert incremental_sig == _full_signature(root)


def test_random_watch_sequence_matches_full_rebuild_at_every_step(tmp_path: Path) -> None:
    """Simulates a `fabric watch` session: a mix of body edits (should stay on
    the fast path), signature/supertype edits and new files (should escalate),
    and deletions — applied one batch at a time. After every batch the
    incrementally-synced store's signature must equal a from-scratch rebuild's."""
    rng = random.Random(20260818)
    root = tmp_path / "repo"
    for i in range(8):
        _write(
            root,
            f"com/example/G{i}.java",
            f"package com.example;\n"
            f"public class G{i} {{ public void run() {{ helper(); }} "
            f"public void helper() {{ }} }}\n",
        )
    _build(root)

    live = {f"com/example/G{i}.java" for i in range(8)}
    fast_path_hits = 0

    for step in range(12):
        kind = rng.choice(["body", "signature", "new", "delete"])
        changed: set[str] = set()
        deleted: set[str] = set()

        if kind == "body" or len(live) <= 2:
            rel = rng.choice(sorted(live))
            n = rng.randint(0, 999)
            _write(
                root,
                rel,
                f"package com.example;\n"
                f"public class {Path(rel).stem} {{ public void run() {{ helper(); }} "
                f"public void helper() {{ int v = {n}; }} }}\n",
            )
            changed = {rel}
        elif kind == "signature":
            rel = rng.choice(sorted(live))
            n = rng.randint(0, 999)
            _write(
                root,
                rel,
                f"package com.example;\n"
                f"public class {Path(rel).stem} {{ public void run() {{ helper({n}); }} "
                f"public void helper(int x) {{ }} }}\n",
            )
            changed = {rel}
        elif kind == "new":
            rel = f"com/example/New{step}.java"
            _write(
                root,
                rel,
                f"package com.example;\npublic class New{step} {{ public void run() {{ }} }}\n",
            )
            changed = {rel}
            live.add(rel)
        else:  # delete
            rel = rng.choice(sorted(live))
            (root / rel).unlink()
            deleted = {rel}
            live.discard(rel)

        stats, incremental_sig = _sync_incremental(root, changed=changed, deleted=deleted)
        if stats.incremental:
            fast_path_hits += 1
        assert incremental_sig == _full_signature(root), f"mismatch at step {step} ({kind})"

    assert fast_path_hits > 0, "fast path should win at least one step in this sequence"
