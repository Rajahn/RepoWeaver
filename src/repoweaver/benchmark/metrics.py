"""Metric definitions for `fabric benchmark run`.

Every field here has one job: make it impossible to accidentally count a
low-confidence/ambiguous edge as "resolved." See docs/benchmark-methodology.md
for why coverage must always be read alongside ambiguous_edge_rate and edge
precision — a coverage number on its own can be inflated by simply lowering
the confidence bar or keeping ambiguous candidates in the denominator.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path

import tree_sitter_java
from tree_sitter import Language, Parser

from repoweaver.graph.store import GraphStore
from repoweaver.indexer import Indexer

RESOLVED_MIN_CONFIDENCE = 0.5

_SKIP_DIRS = {".git", "target", "build", "out", "node_modules", ".repoweaver"}
_LANGUAGE = Language(tree_sitter_java.language())


@dataclass
class QuerySample:
    query: str
    latency_ms: float
    context_tokens: int


@dataclass
class BenchmarkMetrics:
    """One benchmark run's full metric set. `None` means "not measured" —
    never a stand-in for zero or for "not applicable"."""

    name: str
    repo: str
    adapter: str
    commit: str | None = None

    java_files: int | None = None
    symbol_files: int | None = None
    parse_error_count: int | None = None
    parse_error_rate: float | None = None

    nodes: int | None = None
    edges_total: int | None = None
    edges_resolved: int | None = None
    edges_ambiguous: int | None = None
    ambiguous_edge_rate: float | None = None

    cross_file_dependent_total: int | None = None
    cross_file_dependent_resolved: int | None = None
    cross_file_dependent_coverage: float | None = None

    index_time_sec: float | None = None
    db_size_bytes: int | None = None

    query_latency_ms_p50: float | None = None
    query_latency_ms_p95: float | None = None
    context_tokens_p50: float | None = None
    context_tokens_p95: float | None = None

    deterministic_rebuild: bool | None = None
    deterministic_rebuild_hash: str | None = None

    correctness: dict | None = None
    status: str = "UNKNOWN"

    def to_dict(self) -> dict:
        return asdict(self)


def count_java_files(repo_root: Path) -> int:
    return sum(1 for _ in _iter_java_files(repo_root))


def _iter_java_files(repo_root: Path) -> Iterable[Path]:
    for path in sorted(repo_root.rglob("*.java")):
        rel = path.relative_to(repo_root)
        if any(part in _SKIP_DIRS for part in rel.parts):
            continue
        yield path


def count_parse_errors(repo_root: Path) -> int:
    """Independent syntax-error check (tree.root_node.has_error) — does not
    reuse or alter repoweaver.parser.java, which never records this today."""
    parser = Parser(_LANGUAGE)
    errors = 0
    for path in _iter_java_files(repo_root):
        tree = parser.parse(path.read_bytes())
        if tree.root_node.has_error:
            errors += 1
    return errors


def graph_signature(store: GraphStore) -> str:
    """Canonical content hash of the graph, excluding volatile fields
    (indexed_at/observed_at/commit_hash) so identical source always hashes
    the same regardless of when or where it was indexed. Includes
    unresolved_reference rows so an incremental rebuild that differs only in
    ambiguity bookkeeping is correctly detected as non-identical."""
    import hashlib

    nodes = store.conn.execute(
        """
        SELECT id, kind, qualified_name, simple_name, file, span_start, span_end,
               signature, is_entry_point, entry_point_kind
        FROM node ORDER BY id
        """
    ).fetchall()
    edges = store.conn.execute(
        """
        SELECT id, from_id, to_id, type, confidence, ambiguous_candidates
        FROM edge ORDER BY id
        """
    ).fetchall()
    unresolved = store.conn.execute(
        """
        SELECT id, from_id, type, target_name, candidates, reason, site_count
        FROM unresolved_reference ORDER BY id
        """
    ).fetchall()
    payload = json.dumps(
        {
            "nodes": [tuple(r) for r in nodes],
            "edges": [tuple(r) for r in edges],
            "unresolved": [tuple(r) for r in unresolved],
        },
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def edge_counts(store: GraphStore) -> tuple[int, int, int]:
    """(edges_total, edges_resolved, edges_ambiguous).

    resolved: confidence >= 0.5 AND ambiguous_candidates is empty. The resolver
    pipeline (resolver.py) never puts an ambiguous candidate set into the
    `edge` table at all anymore — see UnresolvedReferenceRow — so this filter
    is a defense-in-depth invariant, not something the normal path relies on.
    ambiguous: sum of unresolved_reference candidate-list lengths. Each row
    unions every equally-valid candidate for one (from_id, type, target_name)
    call site, so summing lengths reproduces the exact same "one ambiguous
    edge per candidate" counting convention M1 used when it stored these as
    N separate `edge` rows — a pure storage relocation, not a metric change.
    """
    (resolved,) = store.conn.execute(
        """
        SELECT COUNT(*) FROM edge
        WHERE confidence >= ? AND ambiguous_candidates = '[]'
        """,
        (RESOLVED_MIN_CONFIDENCE,),
    ).fetchone()
    candidate_lists = store.conn.execute(
        "SELECT candidates FROM unresolved_reference"
    ).fetchall()
    ambiguous = sum(len(json.loads(row["candidates"])) for row in candidate_lists)
    total = int(resolved) + ambiguous
    return total, int(resolved), ambiguous


def cross_file_dependent_coverage(store: GraphStore) -> tuple[int, int]:
    """(symbol_bearing_files, files_with_resolved_incoming_cross_file_edge).

    Strict definition (deliberately not "files with any cross-file edge
    attempted", which would let a pile of ambiguous incoming edges count):
    a symbol-bearing file counts only if at least one edge lands on one of
    its nodes from a *different* file, with confidence >= 0.5 and an empty
    ambiguous_candidates set.
    """
    files = {
        row["file"]
        for row in store.conn.execute("SELECT DISTINCT file FROM node")
        if row["file"]
    }
    if not files:
        return 0, 0

    covered_rows = store.conn.execute(
        """
        SELECT DISTINCT to_node.file AS covered_file
        FROM edge
        JOIN node AS to_node ON to_node.id = edge.to_id
        JOIN node AS from_node ON from_node.id = edge.from_id
        WHERE edge.confidence >= ?
          AND edge.ambiguous_candidates = '[]'
          AND to_node.file != from_node.file
        """,
        (RESOLVED_MIN_CONFIDENCE,),
    ).fetchall()
    covered = {row["covered_file"] for row in covered_rows}
    return len(files), len(covered & files)


def symbol_bearing_file_count(store: GraphStore) -> int:
    (count,) = store.conn.execute("SELECT COUNT(DISTINCT file) FROM node").fetchone()
    return int(count)


def fixed_query_set(store: GraphStore, limit: int = 20) -> list[str]:
    """Deterministic, repo-agnostic query set: the simple names of the first
    `limit` symbol-bearing nodes in id order, deduplicated. Derived from the
    graph itself rather than hardcoded so it works on any repo."""
    rows = store.conn.execute(
        "SELECT DISTINCT simple_name FROM node WHERE simple_name != '' ORDER BY id LIMIT ?",
        (limit,),
    ).fetchall()
    seen: list[str] = []
    for row in rows:
        name = row["simple_name"]
        if name not in seen:
            seen.append(name)
    return seen


def measure_query_latency_and_tokens(
    repo_root: Path,
    store: GraphStore,
    queries: list[str],
    *,
    max_tokens: int = 4000,
) -> list[QuerySample]:
    """Measure the indexed retrieval path against the exact benchmark DB.

    The benchmark index lives in an isolated work directory, not under the
    source repository. Calling public ``explore(repo=...)`` here would silently
    read a different (or stale) ``.repoweaver/graph.db``. Measure SearchEngine
    directly and estimate the same verbatim-source context budget instead.
    """
    from repoweaver.search.engine import SearchEngine, SearchQuery

    engine = SearchEngine(store)
    samples: list[QuerySample] = []
    for query in queries:
        started = time.perf_counter()
        hits = engine.search(
            SearchQuery(
                query=query,
                max_results=20,
                min_confidence=0.5,
                depth=2,
                task="locate",
            )
        )
        elapsed_ms = (time.perf_counter() - started) * 1000

        tokens = 0
        for hit in hits:
            node = store.get_node(hit.node_id)
            if node is None:
                continue
            try:
                lines = (
                    (repo_root / node["file"])
                    .read_text(encoding="utf-8", errors="replace")
                    .splitlines()
                )
            except OSError:
                continue
            source = "\n".join(lines[node["span_start"] - 1 : node["span_end"]])
            remaining = max_tokens - tokens
            if remaining <= 0:
                break
            tokens += min(max(1, len(source) // 4), remaining)

        samples.append(
            QuerySample(query=query, latency_ms=elapsed_ms, context_tokens=tokens)
        )
    return samples


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    k = (len(ordered) - 1) * pct
    lo, hi = int(k), min(int(k) + 1, len(ordered) - 1)
    if lo == hi:
        return ordered[lo]
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)


def summarize_query_samples(samples: list[QuerySample]) -> dict:
    latencies = [s.latency_ms for s in samples]
    tokens = [float(s.context_tokens) for s in samples]
    return {
        "query_latency_ms_p50": _percentile(latencies, 0.50),
        "query_latency_ms_p95": _percentile(latencies, 0.95),
        "context_tokens_p50": _percentile(tokens, 0.50),
        "context_tokens_p95": _percentile(tokens, 0.95),
    }


def db_file_size(db_path: Path, store: GraphStore) -> int:
    store.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return db_path.stat().st_size if db_path.exists() else 0


def _git_commit(repo_root: Path) -> str | None:
    from repoweaver.indexer import _git_head

    head = _git_head(repo_root)
    return head or None


def collect_metrics(
    repo_root: Path,
    name: str,
    workdir: Path,
    adapter: str = "repoweaver",
) -> BenchmarkMetrics:
    """Build the index once and derive every metric from that single build,
    plus one extra rebuild (into a separate DB) to check determinism."""
    db_path = workdir / "graph.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    with GraphStore(db_path) as store:
        stats = Indexer(repo_root, store).build()
        store.commit()

        java_files = count_java_files(repo_root)
        parse_errors = count_parse_errors(repo_root)
        edges_total, edges_resolved, edges_ambiguous = edge_counts(store)
        symbol_files = symbol_bearing_file_count(store)
        cfd_total, cfd_resolved = cross_file_dependent_coverage(store)
        rebuild_hash = graph_signature(store)
        queries = fixed_query_set(store)
        query_samples = (
            measure_query_latency_and_tokens(repo_root, store, queries)
            if queries
            else []
        )
        query_summary = summarize_query_samples(query_samples)
        db_size = db_file_size(db_path, store)

    second_db = workdir / "graph_rebuild.db"
    with GraphStore(second_db) as store2:
        Indexer(repo_root, store2).build()
        store2.commit()
        rebuild_hash_2 = graph_signature(store2)
    deterministic = rebuild_hash == rebuild_hash_2

    metrics = BenchmarkMetrics(
        name=name,
        repo=str(repo_root),
        adapter=adapter,
        commit=_git_commit(repo_root),
        java_files=java_files,
        symbol_files=symbol_files,
        parse_error_count=parse_errors,
        parse_error_rate=(parse_errors / java_files) if java_files else 0.0,
        nodes=stats.nodes,
        edges_total=edges_total,
        edges_resolved=edges_resolved,
        edges_ambiguous=edges_ambiguous,
        ambiguous_edge_rate=(edges_ambiguous / edges_total) if edges_total else 0.0,
        cross_file_dependent_total=cfd_total,
        cross_file_dependent_resolved=cfd_resolved,
        cross_file_dependent_coverage=(cfd_resolved / cfd_total) if cfd_total else 0.0,
        index_time_sec=stats.elapsed_seconds,
        db_size_bytes=db_size,
        deterministic_rebuild=deterministic,
        deterministic_rebuild_hash=rebuild_hash,
        **query_summary,
    )
    return metrics


__all__ = [
    "RESOLVED_MIN_CONFIDENCE",
    "BenchmarkMetrics",
    "QuerySample",
    "collect_metrics",
    "count_java_files",
    "count_parse_errors",
    "cross_file_dependent_coverage",
    "edge_counts",
    "fixed_query_set",
    "graph_signature",
    "measure_query_latency_and_tokens",
    "summarize_query_samples",
    "symbol_bearing_file_count",
]
