"""`fabric watch` — keep the index fresh as `*.java` files change on disk.

Delegates batching/debounce to `watchfiles.watch`, which already coalesces a
burst of filesystem events (including the delete+create pair a rename shows
up as on most backends) into one `set[(Change, path)]` per debounce window.
Each batch is reduced to `changed`/`deleted` repo-relative path sets and
handed to `Indexer.build_incremental`, whose `_sync()` always re-resolves the
*whole* symbol table from every current file (see indexer.py) — so a watched
incremental sync is byte-for-byte equivalent to a full `fabric build` on the
same end state, not an approximation of one.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from watchfiles import Change, watch

from codecontextfabric.graph.store import GraphStore
from codecontextfabric.indexer import BuildStats, Indexer

_SKIP_DIRS = {".git", "target", "build", "out", "node_modules", ".repoweaver"}

DEFAULT_DEBOUNCE_MS = 2000


def _is_watched_java_file(path: Path, repo_root: Path) -> bool:
    if path.suffix != ".java":
        return False
    try:
        rel = path.relative_to(repo_root)
    except ValueError:
        return False
    return not any(part in _SKIP_DIRS for part in rel.parts)


def _watch_filter(repo_root: Path) -> Callable[[Change, str], bool]:
    def _filter(change: Change, path: str) -> bool:
        return _is_watched_java_file(Path(path), repo_root)

    return _filter


def watch_and_sync(
    repo_root: str | Path,
    store: GraphStore,
    *,
    debounce_ms: int = DEFAULT_DEBOUNCE_MS,
    stop_event: object | None = None,
    on_sync: Callable[[set[str], set[str], BuildStats], None] | None = None,
) -> None:
    """Blocks, syncing `store` on every debounced batch of `*.java` changes
    under `repo_root` until `stop_event` is set (or the process is
    interrupted). `on_sync(changed, deleted, stats)` fires after each sync —
    the hook tests use to observe latency/results without polling the
    filesystem themselves.

    `store` must be opened on the same thread this function runs on: sqlite3
    connections are thread-affine, so a caller running this in a background
    thread (as tests do, to assert on real filesystem events) must construct
    and open its `GraphStore` inside that thread's target function, not pass
    one opened on the calling thread."""
    repo_root = Path(repo_root).resolve()
    indexer = Indexer(repo_root, store)

    for batch in watch(
        repo_root,
        watch_filter=_watch_filter(repo_root),
        debounce=debounce_ms,
        stop_event=stop_event,
    ):
        touched: set[str] = set()
        for _change, raw_path in batch:
            path = Path(raw_path)
            try:
                touched.add(str(path.relative_to(repo_root)).replace("\\", "/"))
            except ValueError:
                continue
        if not touched:
            continue

        # A rename shows up as delete+create (possibly two different paths,
        # possibly across two debounce windows); a rapid edit can show up as
        # delete+recreate of the same path. Rather than trust event order,
        # ask disk for the true end state of every touched path.
        changed = {p for p in touched if (repo_root / p).exists()}
        deleted = touched - changed

        stats = indexer.build_incremental(changed=changed, deleted=deleted)
        if on_sync is not None:
            on_sync(changed, deleted, stats)


__all__ = ["DEFAULT_DEBOUNCE_MS", "watch_and_sync"]
