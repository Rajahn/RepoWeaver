"""Repo-wide symbol resolution and orchestration for `fabric build`/`fabric watch`.

A single file's AST (see `repoweaver.parser.java`) never has enough information
to resolve a call target, a superclass, or an import: that requires the whole
repo's symbol table. `repoweaver.resolver` builds that table and resolves every
raw reference into an `EdgeRow` (or, when ambiguous, an `UnresolvedReferenceRow`)
with provenance/confidence — never guessing past what the evidence supports
(docs/adr/0001-schema-and-explore-contract-v1.md #4,
docs/adr/0002-m2-resolution-and-freshness.md). This module owns parsing
(full or cached), calling the resolver, and persisting through `GraphStore`.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import yaml

from repoweaver.graph.store import EdgeRow, GraphStore, NodeRow, UnresolvedReferenceRow
from repoweaver.parser.java import (
    PARSER_VERSION,
    CallRef,
    ImportRef,
    JavaParser,
    NodeRecord,
    ParsedFile,
    TypeRef,
    TypeUseRef,
)
from repoweaver.resolver import (
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
    return json.dumps(
        {
            "file": pf.file,
            "package": pf.package,
            "imports": [asdict(i) for i in pf.imports],
            "nodes": [asdict(n) for n in pf.nodes],
            "top_level_types": pf.top_level_types,
            "type_refs": [asdict(t) for t in pf.type_refs],
            "calls": [asdict(c) for c in pf.calls],
            "type_uses": [asdict(t) for t in pf.type_uses],
        }
    )


def _parsed_file_from_json(payload: str, repo_root: Path) -> ParsedFile | None:
    """Reconstructs a ParsedFile from a cache row, reading `source` back off
    disk (matching how `JavaParser.parse_file` decodes it). A payload from an
    older RepoWeaver version may still carry an embedded `source` key — it's
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


class Indexer:
    """Parses `*.java` files under `repo_root` and (re)builds the graph.

    `build()` always re-parses every file (used for `fabric build` and as the
    determinism cross-check in the benchmark harness). `build_incremental()`
    re-parses only the given changed files, reads cached raw references for
    everything else, and re-runs resolution globally — so the result is
    byte-for-byte identical to a full rebuild's `graph_signature()` (see
    docs/adr/0002-m2-resolution-and-freshness.md).
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
        return self._sync(changed=all_files, deleted=deleted)

    def build_incremental(
        self, changed: set[str], deleted: set[str] | None = None
    ) -> BuildStats:
        """`changed`/`deleted` are repo-relative paths (as produced by the file
        watcher). Files outside both sets are read from `file_refs_cache`."""
        return self._sync(changed=set(changed), deleted=set(deleted or set()))

    def _sync(self, changed: set[str], deleted: set[str]) -> BuildStats:
        started = time.monotonic()
        parser = JavaParser(self.repo_root)
        commit_hash = _git_head(self.repo_root)
        now = int(time.time())

        for stale_file in deleted:
            self.store.delete_file(stale_file)

        all_files = (_discover_files(self.repo_root) | changed) - deleted
        parsed_files: list[ParsedFile] = []
        vanished: set[str] = set()
        for rel in sorted(all_files):
            abs_path = self.repo_root / rel
            content_hash = _safe_file_hash(abs_path)
            if content_hash is None:
                # Watcher reported this file as present, but it's gone by the
                # time we got here (rename/delete race) — treat it as deleted
                # rather than crashing the watch process.
                vanished.add(rel)
                continue

            if rel in changed:
                pf = _safe_parse(parser, abs_path)
                if pf is None:
                    vanished.add(rel)
                    continue
                self.store.set_file_refs_cache(
                    rel, content_hash, _parsed_file_to_json(pf)
                )
            else:
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
                    pf = _safe_parse(parser, abs_path)
                    if pf is None:
                        vanished.add(rel)
                        continue
                    self.store.set_file_refs_cache(
                        rel, content_hash, _parsed_file_to_json(pf)
                    )
            parsed_files.append(pf)

        for rel in vanished:
            self.store.delete_file(rel)

        node_rows_by_file: dict[str, list[NodeRow]] = {}
        all_node_rows: list[NodeRow] = []
        for pf in parsed_files:
            rows = []
            for rec in pf.nodes:
                nid = node_id(rec.kind, self.repo_root, pf.file, rec.qualified_name)
                kind = _entry_point_kind(rec.annotations, self.entry_point_annotations)
                if not kind and rec.kind in _TYPE_KINDS_ENTRY:
                    kind = _pattern_entry_kind(
                        pf, rec.qualified_name, self.entry_point_patterns
                    )
                is_entry = bool(kind)
                row = NodeRow(
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
                rows.append(row)
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
            abs_path = self.repo_root / pf.file
            self.store.upsert_file_meta(
                pf.file, file_hash(abs_path), len(node_rows_by_file[pf.file])
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
