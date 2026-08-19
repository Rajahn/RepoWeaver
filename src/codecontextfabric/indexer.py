"""Repo-wide symbol resolution and orchestration for `fabric build`/`fabric watch`.

A single file's AST (see `codecontextfabric.parser.java`) never has enough information
to resolve a call target, a superclass, or an import: that requires the whole
repo's symbol table. `codecontextfabric.resolver` builds that table and resolves every
raw reference into an `EdgeRow` (or, when ambiguous, an `UnresolvedReferenceRow`)
with provenance/confidence — never guessing past what the evidence supports
(docs/adr/0001-schema-and-explore-contract-v1.md #4,
docs/adr/0002-m2-resolution-and-freshness.md). This module owns parsing
(full or cached), calling the resolver, and persisting through `GraphStore`.

Two performance mechanisms live here (docs/adr/0005-incremental-symbol-table-and-parallel-parse.md):
  - parallel parsing (`_parse_many`) for the CPU-bound, side-effect-free parse
    step of a full build.
  - a provably-safe incremental fast path (`_try_fast_incremental`) for
    `build_incremental` that skips re-resolving files no change could have
    touched, falling back to the full (always-correct) resolve whenever that
    safety proof doesn't hold.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import yaml

from codecontextfabric.graph.store import (
    EdgeRow,
    GraphStore,
    NodeRow,
    UnresolvedReferenceRow,
)
from codecontextfabric.parser.java import (
    PARSER_VERSION,
    CallRef,
    ImportRef,
    JavaParser,
    NodeRecord,
    ParsedFile,
    TypeRef,
    TypeUseRef,
)
from codecontextfabric.resolver import (
    SymbolTable,
    build_file_context,
    build_supertypes,
    resolve_calls,
    resolve_imports,
    resolve_type_refs,
    resolve_type_uses,
)

_SKIP_DIRS = {".git", "target", "build", "out", "node_modules", ".repoweaver"}

# Fixed annotation -> entry-point-kind taxonomy (see docs/adr/0002-*.md).
# Deliberately NOT modeled as a self-loop edge or a synthetic edge type —
# an entry point is an attribute of the node itself.
ENTRY_POINT_ANNOTATIONS: dict[str, str] = {
    "RestController": "HTTP_CONTROLLER",
    "Controller": "HTTP_CONTROLLER",
    "RequestMapping": "HTTP_ROUTE",
    "GetMapping": "HTTP_ROUTE",
    "PostMapping": "HTTP_ROUTE",
    "PutMapping": "HTTP_ROUTE",
    "DeleteMapping": "HTTP_ROUTE",
    "PatchMapping": "HTTP_ROUTE",
    "Scheduled": "SCHEDULED",
    "KafkaListener": "MESSAGE_LISTENER",
    "JmsListener": "MESSAGE_LISTENER",
    "RabbitListener": "MESSAGE_LISTENER",
}


def repo_slug(repo_root: Path) -> str:
    name = repo_root.resolve().name
    return name or "repo"


def node_id(kind: str, repo_root: Path, file: str, qualified_name: str) -> str:
    return f"{kind}:{repo_slug(repo_root)}:{file}:{qualified_name}"


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _safe_file_hash(path: Path) -> str | None:
    try:
        return file_hash(path)
    except OSError:
        return None


def _safe_parse(parser: JavaParser, path: Path) -> ParsedFile | None:
    try:
        return parser.parse_file(path)
    except OSError:
        return None


def _entry_point_kind(annotations: list[str], table: dict[str, str]) -> str:
    for name in annotations:
        kind = table.get(name)
        if kind:
            return kind
    return ""


_TYPE_KINDS_ENTRY = {"class", "interface", "enum", "annotation"}


def _pattern_entry_kind(pf, qualified_name: str, patterns: dict[str, str]) -> str:
    """Entry kind from implements/extends simple-name suffix patterns.
    Only type declarations are candidates (a method never implements
    anything). Uses the file's raw type_refs — supertype simple names —
    which is exactly what the patterns match against."""
    if not patterns:
        return ""
    supertype_names = {
        ref.supertype_simple_name
        for ref in pf.type_refs
        if ref.subtype_qualified_name == qualified_name
    }
    for name in supertype_names:
        for suffix, kind in patterns.items():
            if name.endswith(suffix):
                return kind
    return ""


_ENTRYPOINTS_CONFIG_REL = Path(".repoweaver") / "entrypoints.yaml"

_BUILTIN_ENTRY_POINT_PATTERNS: dict[str, str] = {
    # Suffix/substring patterns on the *declared* supertype list. Empty by
    # default in the public build — enterprise thrift/RPC and MQ frameworks
    # use site-local naming (e.g. `implements XxxService.Iface`,
    # `extends AbstractMessageProcessor<T>`), which a public tool must not
    # guess. Sites configure theirs in entrypoints.yaml.
}


def load_entry_point_annotations(repo_root: Path) -> dict[str, str]:
    """Built-in public-annotation table, optionally overridden by
    `.repoweaver/entrypoints.yaml`.

    File format:
        mode: merge      # "merge" (default, adds to/overrides built-ins) or "replace"
        annotations:
            MyController: HTTP_CONTROLLER
            MyBatchJob: SCHEDULED
        implements_patterns:      # class implements a supertype whose simple name
            Iface: RPC_PROVIDER   #   ends with this suffix -> entry kind
        extends_patterns:         # class extends a base whose simple name
            AbstractMessageProcessor: MESSAGE_LISTENER

    A missing, empty, or malformed config file silently falls back to the
    built-in table — entry-point detection must never crash a build over a
    bad config file.
    """
    config_path = Path(repo_root) / _ENTRYPOINTS_CONFIG_REL
    if not config_path.exists():
        return dict(ENTRY_POINT_ANNOTATIONS)

    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return dict(ENTRY_POINT_ANNOTATIONS)

    if not isinstance(raw, dict):
        return dict(ENTRY_POINT_ANNOTATIONS)

    overrides = raw.get("annotations")
    if not isinstance(overrides, dict):
        overrides = {}
    overrides = {
        str(k): str(v) for k, v in overrides.items() if isinstance(k, str) and v
    }

    mode = raw.get("mode", "merge")
    if mode == "replace":
        return overrides
    merged = dict(ENTRY_POINT_ANNOTATIONS)
    merged.update(overrides)
    return merged


def load_entry_point_patterns(repo_root: Path) -> dict[str, str]:
    """Implements/extends simple-name suffix patterns -> entry kind. Same
    merge/replace semantics and same never-crash fallback as
    load_entry_point_annotations. Both annotations and patterns can appear in
    one file; this reads the pattern keys."""
    config_path = Path(repo_root) / _ENTRYPOINTS_CONFIG_REL
    if not config_path.exists():
        return dict(_BUILTIN_ENTRY_POINT_PATTERNS)
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return dict(_BUILTIN_ENTRY_POINT_PATTERNS)
    if not isinstance(raw, dict):
        return dict(_BUILTIN_ENTRY_POINT_PATTERNS)

    def _collect(key: str) -> dict[str, str]:
        section = raw.get(key)
        if not isinstance(section, dict):
            return {}
        return {str(k): str(v) for k, v in section.items() if isinstance(k, str) and v}

    if raw.get("mode") == "replace":
        return _collect("implements_patterns") | _collect("extends_patterns")
    merged = dict(_BUILTIN_ENTRY_POINT_PATTERNS)
    merged.update(_collect("implements_patterns"))
    merged.update(_collect("extends_patterns"))
    return merged


@dataclass
class BuildStats:
    files: int
    nodes: int
    edges: int
    elapsed_seconds: float
    unresolved: int = 0
    changed_files: int = 0
    incremental: bool = False


def _discover_files(repo_root: Path) -> set[str]:
    out = set()
    for java_file in repo_root.rglob("*.java"):
        rel = java_file.relative_to(repo_root)
        if any(part in _SKIP_DIRS for part in rel.parts):
            continue
        out.add(str(rel).replace("\\", "/"))
    return out


def _parsed_file_to_json(pf: ParsedFile) -> str:
    """Serializes everything the resolver needs except `source` — the file's own
    bytes on disk are the source of truth, and duplicating them into every
    cache row made `file_refs_cache` the dominant share of `graph.db`
    (see docs/adr/0002-m2-resolution-and-freshness.md and the M2 audit note
    in CHANGELOG.md)."""
    # All ref/record dataclasses here are flat (str/int/bool/list-of-primitive
    # fields only, no nested dataclasses), so a shallow `vars()` dict is
    # identical in content to `asdict()` without its recursive deepcopy cost —
    # this loop was previously the dominant share of full-build wall time.
    return json.dumps(
        {
            "file": pf.file,
            "package": pf.package,
            "imports": [vars(i) for i in pf.imports],
            "nodes": [vars(n) for n in pf.nodes],
            "top_level_types": pf.top_level_types,
            "type_refs": [vars(t) for t in pf.type_refs],
            "calls": [vars(c) for c in pf.calls],
            "type_uses": [vars(t) for t in pf.type_uses],
        }
    )


def _parsed_file_from_json(payload: str, repo_root: Path) -> ParsedFile | None:
    """Reconstructs a ParsedFile from a cache row, reading `source` back off
    disk (matching how `JavaParser.parse_file` decodes it). A payload from an
    older Code Context Fabric version may still carry an embedded `source` key — it's
    simply ignored, no migration needed since a content-hash mismatch already
    forces a re-parse on any real drift. Returns None (cache miss) if the file
    can no longer be read, so the caller re-parses instead of guessing."""
    d = json.loads(payload)
    try:
        source = (repo_root / d["file"]).read_bytes().decode("utf-8", errors="replace")
    except OSError:
        return None
    return ParsedFile(
        file=d["file"],
        package=d["package"],
        imports=[ImportRef(**i) for i in d["imports"]],
        nodes=[NodeRecord(**n) for n in d["nodes"]],
        top_level_types=d["top_level_types"],
        type_refs=[TypeRef(**t) for t in d["type_refs"]],
        calls=[CallRef(**c) for c in d["calls"]],
        type_uses=[TypeUseRef(**t) for t in d.get("type_uses", [])],
        source=source,
    )


# ----------------------------------------------------------------------------
# Parallel parsing (P0-B). Parsing is pure CPU with no shared state — safe to
# fan out across processes. Resolution and every store write stay strictly
# single-threaded (SQLite is single-writer; the resolver's SymbolTable is a
# plain in-process dict tree with no locking).
#
# tree-sitter `Parser`/`Language` objects are not picklable, so a worker must
# build its own `JavaParser` rather than receiving the parent's — the
# initializer below runs once per forked/spawned worker process and is
# module-level (required for the `spawn` start method used by default on
# Windows/macOS, which re-imports this module in the child instead of
# inheriting memory).
# ----------------------------------------------------------------------------

_PARALLEL_PARSE_THRESHOLD = 24
_MAX_PARSE_WORKERS = 8

_worker_parser: JavaParser | None = None
_worker_repo_root: str = ""


def _init_parse_worker(repo_root_str: str) -> None:
    global _worker_parser, _worker_repo_root
    _worker_repo_root = repo_root_str
    _worker_parser = JavaParser(repo_root_str)


def _parse_in_worker(rel: str) -> tuple[str, ParsedFile | None]:
    assert _worker_parser is not None, "worker pool initializer did not run"
    return (rel, _safe_parse(_worker_parser, Path(_worker_repo_root) / rel))


def _parse_many(
    repo_root: Path, parser: JavaParser, rels: list[str]
) -> dict[str, ParsedFile | None]:
    """Parses every file in `rels` (already known to need parsing). Below
    `_PARALLEL_PARSE_THRESHOLD` this runs inline — process-pool startup and
    per-task pickling cost more than a small batch saves."""
    if not rels:
        return {}
    max_workers = min(_MAX_PARSE_WORKERS, os.cpu_count() or 1)
    if len(rels) < _PARALLEL_PARSE_THRESHOLD or max_workers <= 1:
        return {rel: _safe_parse(parser, repo_root / rel) for rel in rels}
    chunksize = max(1, len(rels) // (max_workers * 4))
    with ProcessPoolExecutor(
        max_workers=max_workers,
        initializer=_init_parse_worker,
        initargs=(str(repo_root),),
    ) as pool:
        return dict(pool.map(_parse_in_worker, rels, chunksize=chunksize))


def _node_identity(row: NodeRow) -> tuple[str, str, str, str]:
    """The subset of a NodeRow that the resolver's SymbolTable indexes on.
    Two builds that agree on this tuple for every node agree on every lookup
    the resolver can perform — see `_try_fast_incremental`."""
    return (row.kind, row.qualified_name, row.simple_name, row.signature)


class Indexer:
    """Parses `*.java` files under `repo_root` and (re)builds the graph.

    `build()` always re-parses every file (used for `fabric build` and as the
    determinism cross-check in the benchmark harness).

    `build_incremental()` first attempts a provably-safe fast path
    (`_try_fast_incremental`) that re-parses and re-resolves only the changed
    files, reusing the repo's existing node/edge tables as a lightweight
    symbol index. That path commits only when it can prove no other file's
    resolution could have changed (see the ADR); otherwise it falls back to
    `_full_sync`, the same full-repo resolve `build()` uses, which is always
    correct by construction. Either way the result is byte-for-byte identical
    to a full rebuild's `graph_signature()` (docs/adr/0002-m2-resolution-and-
    freshness.md, docs/adr/0005-incremental-symbol-table-and-parallel-parse.md).
    """

    def __init__(self, repo_root: str | Path, store: GraphStore) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.store = store
        self.entry_point_annotations = load_entry_point_annotations(self.repo_root)
        self.entry_point_patterns = load_entry_point_patterns(self.repo_root)

    def build(self) -> BuildStats:
        all_files = _discover_files(self.repo_root)
        known = self.store.known_files()
        deleted = known - all_files
        return self._full_sync(changed=all_files, deleted=deleted)

    def build_incremental(
        self, changed: set[str], deleted: set[str] | None = None
    ) -> BuildStats:
        """`changed`/`deleted` are repo-relative paths (as produced by the file
        watcher). Files outside both sets are read from `file_refs_cache`."""
        changed = set(changed)
        deleted = set(deleted or set())
        if deleted or not changed:
            # Deletions always take the full path: removing a file changes the
            # repo's symbol set by definition, so the fast path's "nothing
            # outside `changed` could be affected" proof never holds for it.
            return self._full_sync(changed=changed, deleted=deleted)
        fast = self._try_fast_incremental(changed)
        if fast is not None:
            return fast
        return self._full_sync(changed=changed, deleted=deleted)

    def _build_node_rows(
        self, pf: ParsedFile, commit_hash: str, now: int
    ) -> list[NodeRow]:
        rows = []
        for rec in pf.nodes:
            nid = node_id(rec.kind, self.repo_root, pf.file, rec.qualified_name)
            kind = _entry_point_kind(rec.annotations, self.entry_point_annotations)
            if not kind and rec.kind in _TYPE_KINDS_ENTRY:
                kind = _pattern_entry_kind(
                    pf, rec.qualified_name, self.entry_point_patterns
                )
            is_entry = bool(kind)
            rows.append(
                NodeRow(
                    id=nid,
                    kind=rec.kind,
                    qualified_name=rec.qualified_name,
                    simple_name=rec.simple_name,
                    file=pf.file,
                    span_start=rec.span_start,
                    span_end=rec.span_end,
                    signature=rec.signature,
                    repo=str(self.repo_root),
                    commit_hash=commit_hash,
                    indexed_at=now,
                    is_entry_point=is_entry,
                    entry_point_kind=kind,
                )
            )
        return rows

    # ------------------------------------------------------------------
    # Incremental fast path (P0-A)
    # ------------------------------------------------------------------

    def _try_fast_incremental(self, changed: set[str]) -> BuildStats | None:
        """Re-resolves only `changed` files, reusing the store's existing
        node/edge tables as the symbol index (no ParsedFile deserialization
        for any other file). Returns None — meaning "not provably safe, the
        caller must fall back to `_full_sync`" — the moment either check
        below fails; it never writes anything before both checks pass, so a
        None return leaves the store untouched.

        Why this is sound: the resolver's SymbolTable is built purely from
        (kind, qualified_name, simple_name, signature) across every node in
        the repo, plus a supertypes map built purely from EXTENDS/IMPLEMENTS
        edges across the repo. If every changed file's post-edit node set is
        identical to its pre-edit node set under that same key (check #1),
        every SymbolTable index is repo-wide identical to before — no other
        file's lookups can change. If, given that same (proven-unchanged)
        table, re-resolving each changed file's own extends/implements
        clauses reproduces exactly the edges/unresolved-rows already on file
        for it (check #2), the supertypes map is repo-wide identical too — so
        no other file's supertype-aware BFS (declared_lookup) can change
        either. Both proofs hold *before* anything is written, so only the
        changed files' own rows (which always get rewritten regardless, since
        their raw calls/type-uses may have changed) ever move.
        """
        started = time.monotonic()
        parser = JavaParser(self.repo_root)
        commit_hash = _git_head(self.repo_root)
        now = int(time.time())
        changed_sorted = sorted(changed)

        content_hashes: dict[str, str] = {}
        for rel in changed_sorted:
            content_hash = _safe_file_hash(self.repo_root / rel)
            if content_hash is None:
                return None  # vanished mid-sync — let the full path's
                # delete/vanished bookkeeping handle it correctly.
            content_hashes[rel] = content_hash

        parsed_map = _parse_many(self.repo_root, parser, changed_sorted)
        parsed_by_file: dict[str, ParsedFile] = {}
        for rel in changed_sorted:
            pf = parsed_map.get(rel)
            if pf is None:
                return None
            parsed_by_file[rel] = pf

        placeholders = ",".join("?" * len(changed_sorted))
        old_identity: dict[str, set[tuple[str, str, str, str]]] = {
            rel: set() for rel in changed_sorted
        }
        for row in self.store.conn.execute(
            f"SELECT file, kind, qualified_name, simple_name, signature "
            f"FROM node WHERE file IN ({placeholders})",
            tuple(changed_sorted),
        ):
            old_identity[row["file"]].add(
                (
                    row["kind"],
                    row["qualified_name"],
                    row["simple_name"],
                    row["signature"],
                )
            )

        node_rows_by_file: dict[str, list[NodeRow]] = {}
        for rel in changed_sorted:
            node_rows_by_file[rel] = self._build_node_rows(
                parsed_by_file[rel], commit_hash, now
            )

        for rel in changed_sorted:
            new_ids = {_node_identity(r) for r in node_rows_by_file[rel]}
            if new_ids != old_identity[rel]:
                return None  # public surface changed — unsafe to skip anyone

        # Check #1 passed: the repo-wide SymbolTable is provably unchanged.
        # Build it straight from the store's `node` table — the exact
        # "lightweight index" substitute for full ParsedFile deserialization.
        all_rows = [
            NodeRow(
                id=row["id"],
                kind=row["kind"],
                qualified_name=row["qualified_name"],
                simple_name=row["simple_name"],
                file=row["file"],
                span_start=0,
                span_end=0,
                signature=row["signature"],
            )
            for row in self.store.conn.execute(
                "SELECT id, kind, qualified_name, simple_name, file, signature FROM node"
            )
        ]
        symtab = SymbolTable(all_rows)

        changed_node_ids = {r.id for rows in node_rows_by_file.values() for r in rows}

        file_ctx_by_file: dict[str, object] = {}
        src_hash_by_file: dict[str, str] = {}
        type_edges_by_file: dict[str, list[EdgeRow]] = {}
        type_unresolved_by_file: dict[str, list[UnresolvedReferenceRow]] = {}
        for rel in changed_sorted:
            pf = parsed_by_file[rel]
            ctx = build_file_context(pf, symtab)
            file_ctx_by_file[rel] = ctx
            src_hash = hashlib.sha256(pf.source.encode("utf-8")).hexdigest()
            src_hash_by_file[rel] = src_hash
            edges, unresolved = resolve_type_refs(pf, ctx, symtab, src_hash)
            type_edges_by_file[rel] = edges
            type_unresolved_by_file[rel] = unresolved

        if changed_node_ids:
            id_placeholders = ",".join("?" * len(changed_node_ids))
            old_super_edges = {
                (row["from_id"], row["to_id"], row["type"])
                for row in self.store.conn.execute(
                    f"SELECT from_id, to_id, type FROM edge "
                    f"WHERE type IN ('EXTENDS','IMPLEMENTS') "
                    f"AND from_id IN ({id_placeholders})",
                    tuple(changed_node_ids),
                )
            }
            old_super_unresolved = {
                (
                    row["from_id"],
                    row["type"],
                    row["target_name"],
                    tuple(sorted(json.loads(row["candidates"]))),
                )
                for row in self.store.conn.execute(
                    f"SELECT from_id, type, target_name, candidates "
                    f"FROM unresolved_reference "
                    f"WHERE type IN ('EXTENDS','IMPLEMENTS') "
                    f"AND from_id IN ({id_placeholders})",
                    tuple(changed_node_ids),
                )
            }
        else:
            old_super_edges = set()
            old_super_unresolved = set()

        new_super_edges = {
            (e.from_id, e.to_id, e.type)
            for edges in type_edges_by_file.values()
            for e in edges
        }
        new_super_unresolved = {
            (u.from_id, u.type, u.target_name, tuple(sorted(u.candidates)))
            for unresolved in type_unresolved_by_file.values()
            for u in unresolved
        }
        if (
            new_super_edges != old_super_edges
            or new_super_unresolved != old_super_unresolved
        ):
            return None  # this batch's own supertype graph moved — anyone
            # else's declared_lookup BFS through it could too.

        # Check #2 passed: the repo-wide supertypes map is provably unchanged
        # too (this batch's contribution to it, freshly resolved, matches
        # exactly what was already on file for it). Populate it from the
        # existing edge table rather than re-deriving it from every file.
        for row in self.store.conn.execute(
            """
            SELECT n1.qualified_name AS from_q, n2.qualified_name AS to_q
            FROM edge e
            JOIN node n1 ON n1.id = e.from_id
            JOIN node n2 ON n2.id = e.to_id
            WHERE e.type IN ('EXTENDS', 'IMPLEMENTS')
            """
        ):
            existing = symtab.supertypes.setdefault(row["from_q"], [])
            if row["to_q"] not in existing:
                existing.append(row["to_q"])

        edges_by_file: dict[str, list[EdgeRow]] = {}
        unresolved_by_file: dict[str, list[UnresolvedReferenceRow]] = {}
        for rel in changed_sorted:
            pf = parsed_by_file[rel]
            ctx = file_ctx_by_file[rel]
            src_hash = src_hash_by_file[rel]
            call_edges, call_unresolved = resolve_calls(pf, ctx, symtab, src_hash)
            import_edges = resolve_imports(pf, symtab, src_hash)
            reference_edges, reference_unresolved = resolve_type_uses(
                pf, ctx, symtab, src_hash
            )
            edges_by_file[rel] = (
                type_edges_by_file[rel] + call_edges + import_edges + reference_edges
            )
            unresolved_by_file[rel] = (
                type_unresolved_by_file[rel] + call_unresolved + reference_unresolved
            )

        for rel in changed_sorted:
            self.store.set_file_refs_cache(
                rel, content_hashes[rel], _parsed_file_to_json(parsed_by_file[rel])
            )
            self.store.replace_file_nodes(rel, node_rows_by_file[rel])
        for rel in changed_sorted:
            self.store.replace_file_edges(rel, edges_by_file[rel], PARSER_VERSION)
            self.store.replace_file_unresolved(rel, unresolved_by_file[rel])
            self.store.upsert_file_meta(
                rel, content_hashes[rel], len(node_rows_by_file[rel])
            )
        self.store.commit()

        elapsed = time.monotonic() - started
        return BuildStats(
            files=len(self.store.known_files()),
            nodes=self.store.node_count(),
            edges=self.store.edge_count(),
            elapsed_seconds=elapsed,
            unresolved=self.store.unresolved_count(),
            changed_files=len(changed),
            incremental=True,
        )

    # ------------------------------------------------------------------
    # Full resolve (always correct; used by build() and as the fast path's
    # fallback whenever the safety proof above doesn't hold)
    # ------------------------------------------------------------------

    def _full_sync(self, changed: set[str], deleted: set[str]) -> BuildStats:
        started = time.monotonic()
        parser = JavaParser(self.repo_root)
        commit_hash = _git_head(self.repo_root)
        now = int(time.time())

        for stale_file in deleted:
            self.store.delete_file(stale_file)

        all_files = (_discover_files(self.repo_root) | changed) - deleted
        content_hashes: dict[str, str] = {}
        vanished: set[str] = set()
        need_parse: list[str] = []
        cached_pf: dict[str, ParsedFile] = {}
        for rel in sorted(all_files):
            abs_path = self.repo_root / rel
            content_hash = _safe_file_hash(abs_path)
            if content_hash is None:
                # Watcher reported this file as present, but it's gone by the
                # time we got here (rename/delete race) — treat it as deleted
                # rather than crashing the watch process.
                vanished.add(rel)
                continue
            content_hashes[rel] = content_hash

            if rel in changed:
                need_parse.append(rel)
                continue

            cached = self.store.get_file_refs_cache(rel)
            pf = None
            if cached is not None and cached[0] == content_hash and cached[1]:
                try:
                    pf = _parsed_file_from_json(cached[1], self.repo_root)
                except (ValueError, KeyError, TypeError):
                    pf = None  # corrupt/legacy payload — treat as a miss
            if pf is None:
                # Cache miss (first-ever incremental call, drift, or the
                # cached source can no longer be read back off disk) —
                # never guess stale data; re-parse to stay correct.
                need_parse.append(rel)
            else:
                cached_pf[rel] = pf

        parsed_map = _parse_many(self.repo_root, parser, need_parse)

        parsed_files: list[ParsedFile] = []
        for rel in sorted(all_files):
            if rel in vanished:
                continue
            pf = cached_pf.get(rel)
            if pf is None:
                pf = parsed_map.get(rel)
                if pf is None:
                    vanished.add(rel)
                    continue
                self.store.set_file_refs_cache(
                    rel, content_hashes[rel], _parsed_file_to_json(pf)
                )
            parsed_files.append(pf)

        for rel in vanished:
            self.store.delete_file(rel)

        node_rows_by_file: dict[str, list[NodeRow]] = {}
        all_node_rows: list[NodeRow] = []
        for pf in parsed_files:
            rows = self._build_node_rows(pf, commit_hash, now)
            node_rows_by_file[pf.file] = rows
            all_node_rows.extend(rows)

        symtab = SymbolTable(all_node_rows)

        type_edges_by_file: dict[str, list[EdgeRow]] = {}
        type_unresolved_by_file: dict[str, list[UnresolvedReferenceRow]] = {}
        file_ctx_by_file = {}
        for pf in parsed_files:
            ctx = build_file_context(pf, symtab)
            file_ctx_by_file[pf.file] = ctx
            src_hash = hashlib.sha256(pf.source.encode("utf-8")).hexdigest()
            edges, unresolved = resolve_type_refs(pf, ctx, symtab, src_hash)
            type_edges_by_file[pf.file] = edges
            type_unresolved_by_file[pf.file] = unresolved

        build_supertypes(type_edges_by_file, symtab)

        edges_by_file: dict[str, list[EdgeRow]] = {}
        unresolved_by_file: dict[str, list[UnresolvedReferenceRow]] = {}
        for pf in parsed_files:
            ctx = file_ctx_by_file[pf.file]
            src_hash = hashlib.sha256(pf.source.encode("utf-8")).hexdigest()
            call_edges, call_unresolved = resolve_calls(pf, ctx, symtab, src_hash)
            import_edges = resolve_imports(pf, symtab, src_hash)
            reference_edges, reference_unresolved = resolve_type_uses(
                pf, ctx, symtab, src_hash
            )

            edges_by_file[pf.file] = (
                type_edges_by_file[pf.file]
                + call_edges
                + import_edges
                + reference_edges
            )
            unresolved_by_file[pf.file] = (
                type_unresolved_by_file[pf.file]
                + call_unresolved
                + reference_unresolved
            )

        for pf in parsed_files:
            self.store.replace_file_nodes(pf.file, node_rows_by_file[pf.file])
        for pf in parsed_files:
            self.store.replace_file_edges(
                pf.file, edges_by_file[pf.file], PARSER_VERSION
            )
        for pf in parsed_files:
            self.store.replace_file_unresolved(pf.file, unresolved_by_file[pf.file])
        for pf in parsed_files:
            self.store.upsert_file_meta(
                pf.file, content_hashes[pf.file], len(node_rows_by_file[pf.file])
            )
        self.store.commit()

        elapsed = time.monotonic() - started
        return BuildStats(
            files=len(parsed_files),
            nodes=sum(len(v) for v in node_rows_by_file.values()),
            edges=sum(len(v) for v in edges_by_file.values()),
            elapsed_seconds=elapsed,
            unresolved=sum(len(v) for v in unresolved_by_file.values()),
            changed_files=len(changed),
            incremental=False,
        )

    def current_file_hashes(self) -> dict[str, str]:
        out = {}
        for rel in sorted(_discover_files(self.repo_root)):
            out[rel] = file_hash(self.repo_root / rel)
        return out


def _git_head(repo_root: Path) -> str:
    git_dir = repo_root / ".git"
    if not git_dir.is_dir():
        return ""
    head_file = git_dir / "HEAD"
    try:
        head = head_file.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    if head.startswith("ref:"):
        ref_path = git_dir / head.split(" ", 1)[1].strip()
        try:
            return ref_path.read_text(encoding="utf-8").strip()
        except OSError:
            return ""
    return head


__all__ = [
    "ENTRY_POINT_ANNOTATIONS",
    "BuildStats",
    "Indexer",
    "file_hash",
    "load_entry_point_annotations",
    "load_entry_point_patterns",
    "node_id",
    "repo_slug",
]
