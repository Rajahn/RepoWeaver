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


def _entry_point_kind(annotations: list[str]) -> str:
    for name in annotations:
        kind = ENTRY_POINT_ANNOTATIONS.get(name)
        if kind:
            return kind
    return ""


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
            "source": pf.source,
        }
    )


def _parsed_file_from_json(payload: str) -> ParsedFile:
    d = json.loads(payload)
    return ParsedFile(
        file=d["file"],
        package=d["package"],
        imports=[ImportRef(**i) for i in d["imports"]],
        nodes=[NodeRecord(**n) for n in d["nodes"]],
        top_level_types=d["top_level_types"],
        type_refs=[TypeRef(**t) for t in d["type_refs"]],
        calls=[CallRef(**c) for c in d["calls"]],
        type_uses=[TypeUseRef(**t) for t in d.get("type_uses", [])],
        source=d["source"],
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
        for rel in sorted(all_files):
            if rel in changed:
                pf = parser.parse_file(self.repo_root / rel)
                content_hash = file_hash(self.repo_root / rel)
                self.store.set_file_refs_cache(
                    rel, content_hash, _parsed_file_to_json(pf)
                )
            else:
                cached = self.store.get_file_refs_cache(rel)
                content_hash = file_hash(self.repo_root / rel)
                if cached is not None and cached[0] == content_hash:
                    pf = _parsed_file_from_json(cached[1])
                else:
                    # Cache miss (first-ever incremental call, or drift) —
                    # never guess stale data; re-parse to stay correct.
                    pf = parser.parse_file(self.repo_root / rel)
                    self.store.set_file_refs_cache(
                        rel, content_hash, _parsed_file_to_json(pf)
                    )
            parsed_files.append(pf)

        node_rows_by_file: dict[str, list[NodeRow]] = {}
        all_node_rows: list[NodeRow] = []
        for pf in parsed_files:
            rows = []
            for rec in pf.nodes:
                nid = node_id(rec.kind, self.repo_root, pf.file, rec.qualified_name)
                is_entry, kind = (
                    (True, _entry_point_kind(rec.annotations))
                    if _entry_point_kind(rec.annotations)
                    else (False, "")
                )
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
    "node_id",
    "repo_slug",
]
