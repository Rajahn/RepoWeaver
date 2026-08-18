"""SQLite-backed graph store for RepoWeaver.

See docs/schema.md (frozen v1) and docs/adr/0001-schema-and-explore-contract-v1.md
for the design rationale behind the replace-by-file algorithm and the FTS5 sync
triggers baked into schema.sql.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Self

_SCHEMA_PATH = Path(__file__).parent / "schema.sql"
_DEFAULT_BUSY_TIMEOUT_MS = 10_000


def edge_id(from_id: str, to_id: str, edge_type: str) -> str:
    digest = hashlib.sha256(f"{from_id}|{to_id}|{edge_type}".encode())
    return digest.hexdigest()[:16]


def evidence_id(edge_id_: str, file: str, line: int) -> str:
    digest = hashlib.sha256(f"{edge_id_}|{file}|{line}".encode())
    return digest.hexdigest()[:16]


def unresolved_reference_id(from_id: str, ref_type: str, target_name: str) -> str:
    digest = hashlib.sha256(f"{from_id}|{ref_type}|{target_name}".encode())
    return digest.hexdigest()[:16]


@dataclass
class NodeRow:
    """A fully-formed row for the `node` table."""

    id: str
    kind: str
    qualified_name: str
    simple_name: str
    file: str
    span_start: int
    span_end: int
    signature: str = ""
    repo: str = ""
    language: str = "java"
    commit_hash: str = ""
    indexed_at: int = 0
    is_entry_point: bool = False
    entry_point_kind: str = ""


@dataclass
class EdgeRow:
    """A single resolved reference (one call/extends/implements/imports site).

    Multiple EdgeRow instances with the same (from_id, to_id, type) collapse
    into one `edge` row with multiple `evidence` rows — see GraphStore.replace_file.
    A resolved edge always points at exactly one target; ambiguous candidate
    sets are never stored here — see UnresolvedReferenceRow.
    """

    from_id: str
    to_id: str
    type: str
    provenance: str
    confidence: float
    file: str
    line: int
    source_hash: str = ""
    ambiguous_candidates: list[str] = field(default_factory=list)


@dataclass
class UnresolvedReferenceRow:
    """A call/type reference that matched more than one equally-valid
    candidate — never counted as resolved coverage. See resolver.py."""

    from_id: str
    type: str
    target_name: str
    candidates: list[str]
    reason: str
    file: str
    line: int


class GraphStore:
    """Thin wrapper around an SQLite database that holds the call-graph."""

    def __init__(self, db_path: str | Path = ":memory:") -> None:
        self.db_path = str(db_path)
        self._conn: sqlite3.Connection | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def open(self) -> GraphStore:
        """Open (or create) the database and apply the schema."""
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        if self.db_path != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
        # WAL readers never block on a writer, but two processes writing at
        # once (e.g. `fabric watch` plus a manual `fabric build`) will —
        # without this, SQLite raises "database is locked" immediately
        # instead of waiting for the other writer to commit.
        busy_timeout_ms = int(
            os.environ.get("FABRIC_BUSY_TIMEOUT_MS", _DEFAULT_BUSY_TIMEOUT_MS)
        )
        self._conn.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._apply_schema()
        return self

    def close(self) -> None:
        """Flush and close the database connection."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> Self:
        return self.open()

    def __exit__(self, *_: object) -> None:
        self.close()

    @property
    def conn(self) -> sqlite3.Connection:
        assert self._conn is not None, "GraphStore is not open — call .open() first"
        return self._conn

    def _apply_schema(self) -> None:
        ddl = _SCHEMA_PATH.read_text(encoding="utf-8")
        self.conn.executescript(ddl)
        self._migrate_additive_columns()

    def _migrate_additive_columns(self) -> None:
        """v1.1 additive columns on `node` — `CREATE TABLE IF NOT EXISTS` is a
        no-op against a pre-existing v1 database, so a real ALTER is needed
        for graphs built by an older RepoWeaver version."""
        existing = {row[1] for row in self.conn.execute("PRAGMA table_info(node)")}
        if "is_entry_point" not in existing:
            self.conn.execute(
                "ALTER TABLE node ADD COLUMN is_entry_point INTEGER NOT NULL DEFAULT 0"
            )
        if "entry_point_kind" not in existing:
            self.conn.execute(
                "ALTER TABLE node ADD COLUMN entry_point_kind TEXT NOT NULL DEFAULT ''"
            )

    # ------------------------------------------------------------------
    # Replace-by-file (see ADR-0001 #3)
    # ------------------------------------------------------------------

    def known_files(self) -> set[str]:
        rows = self.conn.execute("SELECT file FROM file_meta").fetchall()
        return {row["file"] for row in rows}

    def delete_file(self, file: str) -> None:
        """Remove a file entirely: its nodes (cascades edges+evidence+unresolved
        references), its file_meta row, and its raw-refs cache entry."""
        self.conn.execute("DELETE FROM node WHERE file = ?", (file,))
        self.conn.execute("DELETE FROM file_meta WHERE file = ?", (file,))
        self.conn.execute("DELETE FROM file_refs_cache WHERE file = ?", (file,))

    def replace_file_nodes(self, file: str, nodes: list[NodeRow]) -> None:
        """Replace the node set for one file without cascading edges owned by other files.

        Unchanged ids are UPDATEd in place (no cascade). Ids no longer present are
        DELETEd (cascade is correct: the symbol genuinely disappeared). New ids are
        INSERTed. See ADR-0001 #3.
        """
        existing = {
            row["id"]
            for row in self.conn.execute("SELECT id FROM node WHERE file = ?", (file,))
        }
        incoming_ids = {n.id for n in nodes}

        removed = existing - incoming_ids
        if removed:
            self.conn.executemany(
                "DELETE FROM node WHERE id = ?", [(nid,) for nid in removed]
            )

        for n in nodes:
            self.conn.execute(
                """
                INSERT INTO node (
                    id, kind, language, repo, file, span_start, span_end,
                    qualified_name, simple_name, signature, commit_hash, indexed_at,
                    is_entry_point, entry_point_kind
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    kind=excluded.kind, language=excluded.language, repo=excluded.repo,
                    file=excluded.file, span_start=excluded.span_start, span_end=excluded.span_end,
                    qualified_name=excluded.qualified_name, simple_name=excluded.simple_name,
                    signature=excluded.signature, commit_hash=excluded.commit_hash,
                    indexed_at=excluded.indexed_at, is_entry_point=excluded.is_entry_point,
                    entry_point_kind=excluded.entry_point_kind
                """,
                (
                    n.id,
                    n.kind,
                    n.language,
                    n.repo,
                    n.file,
                    n.span_start,
                    n.span_end,
                    n.qualified_name,
                    n.simple_name,
                    n.signature,
                    n.commit_hash,
                    n.indexed_at,
                    int(n.is_entry_point),
                    n.entry_point_kind,
                ),
            )

    def replace_file_edges(
        self, file: str, edges: list[EdgeRow], parser_version: str
    ) -> None:
        """Replace every edge *emitted by* `file` (from_id belongs to this file)."""
        from_ids = {
            row["id"]
            for row in self.conn.execute("SELECT id FROM node WHERE file = ?", (file,))
        }
        if from_ids:
            placeholders = ",".join("?" * len(from_ids))
            self.conn.execute(
                f"DELETE FROM edge WHERE from_id IN ({placeholders})", tuple(from_ids)
            )

        merged: dict[str, EdgeRow] = {}
        candidates_by_id: dict[str, set[str]] = {}
        sites: dict[str, list[tuple[str, int]]] = {}
        for e in edges:
            eid = edge_id(e.from_id, e.to_id, e.type)
            sites.setdefault(eid, []).append((e.file, e.line))
            candidates_by_id.setdefault(eid, set()).update(e.ambiguous_candidates)
            if eid not in merged or e.confidence > merged[eid].confidence:
                merged[eid] = e

        now = int(time.time())
        for eid, e in merged.items():
            candidates = sorted(candidates_by_id[eid])
            self.conn.execute(
                """
                INSERT INTO edge (id, from_id, to_id, type, provenance, confidence,
                                   observed_at, source_hash, ambiguous_candidates)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    confidence=excluded.confidence, observed_at=excluded.observed_at,
                    source_hash=excluded.source_hash, ambiguous_candidates=excluded.ambiguous_candidates
                """,
                (
                    eid,
                    e.from_id,
                    e.to_id,
                    e.type,
                    e.provenance,
                    e.confidence,
                    now,
                    e.source_hash,
                    json.dumps(candidates),
                ),
            )
            self.conn.execute("DELETE FROM evidence WHERE edge_id = ?", (eid,))
            # The parser may emit the same resolved edge more than once on one
            # source line (for example chained calls). Evidence is a source
            # location, so identical (file, line) sites are deliberately
            # deduplicated rather than assigned synthetic identities.
            for site_file, line in sorted(set(sites[eid])):
                self.conn.execute(
                    """
                    INSERT INTO evidence (id, edge_id, file, line, parser_version,
                                           freshness_ts, verification_status)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        evidence_id(eid, site_file, line),
                        eid,
                        site_file,
                        line,
                        parser_version,
                        now,
                        "ambiguous" if candidates else "verified",
                    ),
                )

    def replace_file_unresolved(
        self, file: str, refs: list[UnresolvedReferenceRow]
    ) -> None:
        """Replace every unresolved reference *originating from* `file`.
        Mirrors replace_file_edges: rows sharing (from_id, type, target_name)
        merge into one row with a unioned candidate set and a site_count."""
        from_ids = {
            row["id"]
            for row in self.conn.execute("SELECT id FROM node WHERE file = ?", (file,))
        }
        if from_ids:
            placeholders = ",".join("?" * len(from_ids))
            self.conn.execute(
                f"DELETE FROM unresolved_reference WHERE from_id IN ({placeholders})",
                tuple(from_ids),
            )

        merged: dict[str, UnresolvedReferenceRow] = {}
        candidates_by_id: dict[str, set[str]] = {}
        sites_by_id: dict[str, set[tuple[str, int]]] = {}
        for r in refs:
            rid = unresolved_reference_id(r.from_id, r.type, r.target_name)
            candidates_by_id.setdefault(rid, set()).update(r.candidates)
            sites_by_id.setdefault(rid, set()).add((r.file, r.line))
            merged[rid] = r

        now = int(time.time())
        for rid, r in merged.items():
            candidates = sorted(candidates_by_id[rid])
            sites = sorted(sites_by_id[rid])
            first_file, first_line = sites[0]
            self.conn.execute(
                """
                INSERT INTO unresolved_reference
                    (id, from_id, type, target_name, candidates, reason,
                     file, line, site_count, observed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    candidates=excluded.candidates, reason=excluded.reason,
                    file=excluded.file, line=excluded.line,
                    site_count=excluded.site_count, observed_at=excluded.observed_at
                """,
                (
                    rid,
                    r.from_id,
                    r.type,
                    r.target_name,
                    json.dumps(candidates),
                    r.reason,
                    first_file,
                    first_line,
                    len(sites),
                    now,
                ),
            )

    def unresolved_count(self) -> int:
        (count,) = self.conn.execute(
            "SELECT COUNT(*) FROM unresolved_reference"
        ).fetchone()
        return int(count)

    def get_file_refs_cache(self, file: str) -> tuple[str, str] | None:
        """Returns (content_hash, payload_json) if a cache entry exists."""
        row = self.conn.execute(
            "SELECT content_hash, payload FROM file_refs_cache WHERE file = ?", (file,)
        ).fetchone()
        return (row["content_hash"], row["payload"]) if row else None

    def set_file_refs_cache(self, file: str, content_hash: str, payload: str) -> None:
        self.conn.execute(
            """
            INSERT INTO file_refs_cache (file, content_hash, payload)
            VALUES (?, ?, ?)
            ON CONFLICT(file) DO UPDATE SET
                content_hash=excluded.content_hash, payload=excluded.payload
            """,
            (file, content_hash, payload),
        )

    def upsert_file_meta(self, file: str, content_hash: str, node_count: int) -> None:
        self.conn.execute(
            """
            INSERT INTO file_meta (file, content_hash, indexed_at, node_count)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(file) DO UPDATE SET
                content_hash=excluded.content_hash, indexed_at=excluded.indexed_at,
                node_count=excluded.node_count
            """,
            (file, content_hash, int(time.time()), node_count),
        )

    def commit(self) -> None:
        self.conn.commit()

    def rollback(self) -> None:
        self.conn.rollback()

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def node_count(self) -> int:
        (count,) = self.conn.execute("SELECT COUNT(*) FROM node").fetchone()
        return int(count)

    def edge_count(self) -> int:
        (count,) = self.conn.execute("SELECT COUNT(*) FROM edge").fetchone()
        return int(count)

    def stats(self) -> dict:
        edge_types = self.conn.execute(
            "SELECT type, COUNT(*) AS n FROM edge GROUP BY type"
        ).fetchall()
        (entry_points,) = self.conn.execute(
            "SELECT COUNT(*) FROM node WHERE is_entry_point = 1"
        ).fetchone()
        return {
            "nodes": self.node_count(),
            "edges": self.edge_count(),
            "files": len(self.known_files()),
            "edge_types": {row["type"]: row["n"] for row in edge_types},
            "unresolved_references": self.unresolved_count(),
            "entry_points": int(entry_points),
        }

    def entry_points(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM node WHERE is_entry_point = 1 ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_node(self, node_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM node WHERE id = ?", (node_id,)
        ).fetchone()
        return dict(row) if row else None

    def find_by_qualified_name(self, qualified_name: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM node WHERE qualified_name = ?", (qualified_name,)
        ).fetchall()
        return [dict(r) for r in rows]

    def find_by_simple_name(self, simple_name: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM node WHERE simple_name = ?", (simple_name,)
        ).fetchall()
        return [dict(r) for r in rows]

    def fts_search(self, query: str, limit: int = 50) -> list[tuple[dict, float]]:
        """BM25 search over node_fts. Returns (node_dict, bm25_score) pairs.

        Lower raw bm25() values mean a better match; we negate so higher-is-better.
        """
        safe_query = _fts_escape(query)
        if not safe_query:
            return []
        try:
            rows = self.conn.execute(
                """
                SELECT node.*, bm25(node_fts) AS rank
                FROM node_fts
                JOIN node ON node.rowid = node_fts.rowid
                WHERE node_fts MATCH ?
                ORDER BY rank
                LIMIT ?
                """,
                (safe_query, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        return [(dict(r), -float(r["rank"])) for r in rows]

    def neighbors(
        self, node_id: str, direction: str = "out", min_confidence: float = 0.0
    ) -> list[tuple[dict, str, float]]:
        """Return (neighbor_node, edge_type, confidence) for edges touching node_id."""
        if direction == "out":
            sql = """
                SELECT node.*, edge.type AS edge_type, edge.confidence AS edge_confidence
                FROM edge JOIN node ON node.id = edge.to_id
                WHERE edge.from_id = ? AND edge.confidence >= ?
            """
        else:
            sql = """
                SELECT node.*, edge.type AS edge_type, edge.confidence AS edge_confidence
                FROM edge JOIN node ON node.id = edge.from_id
                WHERE edge.to_id = ? AND edge.confidence >= ?
            """
        rows = self.conn.execute(sql, (node_id, min_confidence)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            edge_type = d.pop("edge_type")
            confidence = d.pop("edge_confidence")
            out.append((d, edge_type, float(confidence)))
        return out

    def edge_between(self, from_id: str, to_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM edge WHERE from_id = ? AND to_id = ?", (from_id, to_id)
        ).fetchone()
        return dict(row) if row else None

    def evidence_for_edge(self, edge_id_: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM evidence WHERE edge_id = ?", (edge_id_,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Freshness
    # ------------------------------------------------------------------

    def is_fresh(self, current_hashes: dict[str, str]) -> tuple[bool, list[str]]:
        """Compare `current_hashes` (file -> sha256) against stored file_meta.

        Returns (fresh, stale_files) where stale_files includes new, changed, and
        removed files.
        """
        stored = {
            row["file"]: row["content_hash"]
            for row in self.conn.execute("SELECT file, content_hash FROM file_meta")
        }
        stale = []
        for f, h in current_hashes.items():
            if stored.get(f) != h:
                stale.append(f)
        for f in stored:
            if f not in current_hashes:
                stale.append(f)
        return (len(stale) == 0, sorted(stale))


def _fts_escape(query: str) -> str:
    """Turn free text into a safe FTS5 MATCH expression (prefix search per token)."""
    tokens = [t for t in "".join(c if c.isalnum() else " " for c in query).split() if t]
    if not tokens:
        return ""
    return " OR ".join(f'"{t}"*' for t in tokens)
