"""Repo-wide symbol resolution and orchestration for `fabric build`.

A single file's AST (see `repoweaver.parser.java`) never has enough information
to resolve a call target, a superclass, or an import: that requires the whole
repo's symbol table. This module builds that table, resolves every raw
reference into an `EdgeRow` with provenance/confidence (never guessing past what
the evidence supports — see docs/adr/0001-schema-and-explore-contract-v1.md #4),
and persists everything through `GraphStore` in one transaction.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from pathlib import Path

from repoweaver.graph.store import EdgeRow, GraphStore, NodeRow
from repoweaver.parser.java import PARSER_VERSION, JavaParser, ParsedFile

_TYPE_KINDS = {"class", "interface", "enum"}
_CALLABLE_KINDS = {"method", "constructor"}

_AMBIGUOUS_CONFIDENCE = 0.35
_UNIQUE_UNTYPED_CONFIDENCE = 0.70
_TYPE_NARROWED_CONFIDENCE = 0.85
_STRUCTURAL_CONFIDENCE = 1.00
_STRUCTURAL_AMBIGUOUS_CONFIDENCE = 0.50


def repo_slug(repo_root: Path) -> str:
    name = repo_root.resolve().name
    return name or "repo"


def node_id(kind: str, repo_root: Path, file: str, qualified_name: str) -> str:
    return f"{kind}:{repo_slug(repo_root)}:{file}:{qualified_name}"


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@dataclass
class BuildStats:
    files: int
    nodes: int
    edges: int
    elapsed_seconds: float


class Indexer:
    """Parses every `*.java` file under `repo_root` and rebuilds the graph."""

    def __init__(self, repo_root: str | Path, store: GraphStore) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.store = store

    def build(self) -> BuildStats:
        started = time.monotonic()
        parser = JavaParser(self.repo_root)
        parsed_files: list[ParsedFile] = list(parser.walk_repo())

        node_rows_by_file: dict[str, list[NodeRow]] = {}
        node_by_qname: dict[str, list[str]] = {}
        node_by_simple: dict[str, list[str]] = {}
        node_kind_by_id: dict[str, str] = {}
        commit_hash = _git_head(self.repo_root)
        now = int(time.time())

        for pf in parsed_files:
            rows = []
            for rec in pf.nodes:
                nid = node_id(rec.kind, self.repo_root, pf.file, rec.qualified_name)
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
                )
                rows.append(row)
                node_by_qname.setdefault(rec.qualified_name, []).append(nid)
                node_by_simple.setdefault(rec.simple_name, []).append(nid)
                node_kind_by_id[nid] = rec.kind
            node_rows_by_file[pf.file] = rows

        edges_by_file: dict[str, list[EdgeRow]] = {pf.file: [] for pf in parsed_files}
        current_files = {pf.file for pf in parsed_files}

        for pf in parsed_files:
            src_hash = hashlib.sha256(pf.source.encode("utf-8")).hexdigest()
            import_map = _build_import_map(pf, node_by_qname)
            edges_by_file[pf.file].extend(
                _resolve_type_refs(pf, self.repo_root, node_by_qname, node_by_simple, node_kind_by_id, import_map, src_hash)
            )
            edges_by_file[pf.file].extend(
                _resolve_calls(pf, self.repo_root, node_by_qname, node_by_simple, node_kind_by_id, import_map, src_hash)
            )
            edges_by_file[pf.file].extend(
                _resolve_imports(pf, self.repo_root, node_by_qname, src_hash)
            )

        known = self.store.known_files()
        for stale_file in known - current_files:
            self.store.delete_file(stale_file)

        for pf in parsed_files:
            self.store.replace_file_nodes(pf.file, node_rows_by_file[pf.file])
        for pf in parsed_files:
            self.store.replace_file_edges(pf.file, edges_by_file[pf.file], PARSER_VERSION)
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
        )

    def current_file_hashes(self) -> dict[str, str]:
        parser = JavaParser(self.repo_root)
        skip_dirs = {".git", "target", "build", "out", "node_modules", ".repoweaver"}
        out = {}
        for java_file in sorted(self.repo_root.rglob("*.java")):
            rel = java_file.relative_to(self.repo_root)
            if any(part in skip_dirs for part in rel.parts):
                continue
            out[str(rel).replace("\\", "/")] = file_hash(java_file)
        del parser
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


def _build_import_map(pf: ParsedFile, node_by_qname: dict[str, list[str]]) -> dict[str, str]:
    """simple_name -> fully-qualified name, for imports that resolve to an indexed node."""
    out: dict[str, str] = {}
    for imp in pf.imports:
        if imp.is_wildcard or imp.is_static:
            continue
        if imp.imported_name in node_by_qname:
            simple = imp.imported_name.rsplit(".", 1)[-1]
            out[simple] = imp.imported_name
    return out


def _resolve_type_refs(
    pf: ParsedFile,
    repo_root: Path,
    node_by_qname: dict[str, list[str]],
    node_by_simple: dict[str, list[str]],
    node_kind_by_id: dict[str, str],
    import_map: dict[str, str],
    src_hash: str,
) -> list[EdgeRow]:
    out: list[EdgeRow] = []
    for ref in pf.type_refs:
        from_candidates = node_by_qname.get(ref.subtype_qualified_name, [])
        if not from_candidates:
            continue
        from_id = from_candidates[0]

        target_qname = import_map.get(ref.supertype_simple_name)
        if target_qname and target_qname in node_by_qname:
            to_candidates = [i for i in node_by_qname[target_qname] if node_kind_by_id[i] in _TYPE_KINDS]
        else:
            to_candidates = [
                nid
                for nid in node_by_simple.get(ref.supertype_simple_name, [])
                if node_kind_by_id[nid] in _TYPE_KINDS
            ]

        if not to_candidates:
            continue  # external/JDK supertype, not in index — expected, not an error
        if len(to_candidates) == 1:
            out.append(
                EdgeRow(
                    from_id=from_id,
                    to_id=to_candidates[0],
                    type=ref.edge_type,
                    provenance="tree_sitter_java",
                    confidence=_STRUCTURAL_CONFIDENCE,
                    file=pf.file,
                    line=ref.line,
                    source_hash=src_hash,
                )
            )
        else:
            for cand in to_candidates:
                out.append(
                    EdgeRow(
                        from_id=from_id,
                        to_id=cand,
                        type=ref.edge_type,
                        provenance="tree_sitter_java",
                        confidence=_STRUCTURAL_AMBIGUOUS_CONFIDENCE,
                        file=pf.file,
                        line=ref.line,
                        source_hash=src_hash,
                        ambiguous_candidates=sorted(to_candidates),
                    )
                )
    return out


def _resolve_calls(
    pf: ParsedFile,
    repo_root: Path,
    node_by_qname: dict[str, list[str]],
    node_by_simple: dict[str, list[str]],
    node_kind_by_id: dict[str, str],
    import_map: dict[str, str],
    src_hash: str,
) -> list[EdgeRow]:
    out: list[EdgeRow] = []
    for call in pf.calls:
        from_candidates = node_by_qname.get(call.caller_qualified_name, [])
        if not from_candidates:
            continue
        from_id = from_candidates[0]

        pool = [
            nid for nid in node_by_simple.get(call.method_simple_name, [])
            if node_kind_by_id[nid] in _CALLABLE_KINDS
        ]
        if not pool:
            continue  # JDK/external call — not in index, expected

        narrowed = pool
        was_type_narrowed = False
        if call.receiver_hint:
            target_qname = import_map.get(call.receiver_hint)
            if target_qname:
                by_type = [nid for nid in pool if _owner_qname(nid).startswith(target_qname)]
            else:
                by_type = [nid for nid in pool if _owner_simple(nid) == call.receiver_hint]
            if by_type:
                narrowed = by_type
                was_type_narrowed = True

        if len(narrowed) == 1:
            confidence = _TYPE_NARROWED_CONFIDENCE if was_type_narrowed else _UNIQUE_UNTYPED_CONFIDENCE
            out.append(
                EdgeRow(
                    from_id=from_id,
                    to_id=narrowed[0],
                    type="CALLS",
                    provenance="tree_sitter_java",
                    confidence=confidence,
                    file=pf.file,
                    line=call.line,
                    source_hash=src_hash,
                )
            )
        else:
            for cand in narrowed:
                out.append(
                    EdgeRow(
                        from_id=from_id,
                        to_id=cand,
                        type="CALLS",
                        provenance="tree_sitter_java",
                        confidence=_AMBIGUOUS_CONFIDENCE,
                        file=pf.file,
                        line=call.line,
                        source_hash=src_hash,
                        ambiguous_candidates=sorted(narrowed),
                    )
                )
    return out


def _resolve_imports(
    pf: ParsedFile,
    repo_root: Path,
    node_by_qname: dict[str, list[str]],
    src_hash: str,
) -> list[EdgeRow]:
    out: list[EdgeRow] = []
    for imp in pf.imports:
        if imp.is_wildcard or imp.is_static:
            continue  # wildcard/static imports can't be resolved without per-usage analysis (M1 blind spot)
        to_candidates = node_by_qname.get(imp.imported_name, [])
        if not to_candidates:
            continue
        for top_level_qname in pf.top_level_types:
            from_candidates = node_by_qname.get(top_level_qname, [])
            if not from_candidates:
                continue
            out.append(
                EdgeRow(
                    from_id=from_candidates[0],
                    to_id=to_candidates[0],
                    type="IMPORTS",
                    provenance="tree_sitter_java",
                    confidence=_STRUCTURAL_CONFIDENCE,
                    file=pf.file,
                    line=imp.line,
                    source_hash=src_hash,
                )
            )
    return out


def _owner_qname(node_id_str: str) -> str:
    """Extract the qualified_name portion of a node id and strip any member suffix."""
    qname = node_id_str.split(":", 3)[-1]
    return qname.split("#", 1)[0]


def _owner_simple(node_id_str: str) -> str:
    owner = _owner_qname(node_id_str)
    return owner.rsplit(".", 1)[-1]
