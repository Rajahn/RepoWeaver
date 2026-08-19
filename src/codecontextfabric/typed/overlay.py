"""`fabric overlay scip` — merge SCIP-derived typed edges into the graph.

See docs/adr/0003-typed-overlay.md for the full design. Summary:

- Every mapped (caller, target) pair becomes one of CALLS_TYPED /
  REFERENCES_TYPED / EXTENDS_TYPED / IMPLEMENTS_TYPED, confidence 0.95,
  provenance ``scip_java``.
- If a tree-sitter edge already exists for the same (from_id, to_id) at the
  corresponding untyped type (CALLS, REFERENCES, EXTENDS, IMPLEMENTS), the
  two merge into a single edge at the *_TYPED type: provenance becomes
  ``scip_java+tree_sitter_java``, confidence is the max of the two (never
  downgrades what tree-sitter already established), and evidence from both
  sides is preserved under the new edge id. No edge is ever dropped.
- Re-running the overlay with the same index is a no-op: edge/evidence ids
  are content-hash-derived, so repeated inserts collide harmlessly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from codecontextfabric.graph.store import GraphStore, edge_id, evidence_id
from codecontextfabric.typed.scip_index import (
    TypedOccurrenceRef,
    extract_typed_references,
)
from codecontextfabric.typed.scip_proto import decode_index
from codecontextfabric.typed.symbol_map import SymbolMapper

TYPED_EDGE_TYPES = (
    "CALLS_TYPED",
    "REFERENCES_TYPED",
    "EXTENDS_TYPED",
    "IMPLEMENTS_TYPED",
)
_BASE_OF_TYPED = {
    "CALLS_TYPED": "CALLS",
    "REFERENCES_TYPED": "REFERENCES",
    "EXTENDS_TYPED": "EXTENDS",
    "IMPLEMENTS_TYPED": "IMPLEMENTS",
}
_TYPED_CONFIDENCE = 0.95
_SCIP_PROVENANCE = "scip_java"
_MERGED_PROVENANCE = "scip_java+tree_sitter_java"
_PARSER_VERSION = "scip-java overlay"


@dataclass
class OverlayStats:
    typed_refs: int = 0
    mapped: int = 0
    merged: int = 0
    added: int = 0
    unmapped_symbols: int = 0
    skip_reasons: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "typed_refs": self.typed_refs,
            "mapped": self.mapped,
            "merged": self.merged,
            "added": self.added,
            "unmapped_symbols": self.unmapped_symbols,
            "skip_reasons": dict(self.skip_reasons),
        }


def classify_edge_type(
    caller_node: dict, target_node: dict, ref: TypedOccurrenceRef
) -> str:
    """Decide CALLS_TYPED / REFERENCES_TYPED / EXTENDS_TYPED / IMPLEMENTS_TYPED
    for a mapped (caller, target) pair.

    SCIP occurrences carry no syntactic edge-kind (unlike tree-sitter, which
    sees the `extends`/`implements` grammar node directly) — this uses the
    same "reference shares its source line with a type's own definition"
    heuristic tree-sitter's grammar makes explicit, plus the target's kind to
    pick EXTENDS vs IMPLEMENTS. See docs/adr/0003-typed-overlay.md for the
    known boundary this approximates (multi-line `implements` clauses, or
    interface-extends-interface, fall back to REFERENCES_TYPED /
    IMPLEMENTS_TYPED respectively rather than being misclassified as a call)."""
    if (
        caller_node["kind"] in ("class", "interface", "enum")
        and ref.is_header_line
        and target_node["kind"] in ("class", "interface")
    ):
        return "EXTENDS_TYPED" if target_node["kind"] == "class" else "IMPLEMENTS_TYPED"
    if target_node["kind"] in ("method", "constructor"):
        return "CALLS_TYPED"
    return "REFERENCES_TYPED"


def _merge_provenance(existing: str) -> str:
    parts = {p for p in existing.split("+") if p}
    parts.add(_SCIP_PROVENANCE)
    parts.add("tree_sitter_java")
    return "+".join(sorted(parts))


def _upsert_edge(
    store: GraphStore,
    eid: str,
    from_id: str,
    to_id: str,
    type_: str,
    provenance: str,
    confidence: float,
) -> None:
    store.conn.execute(
        """
        INSERT INTO edge (id, from_id, to_id, type, provenance, confidence,
                           observed_at, source_hash, ambiguous_candidates)
        VALUES (?, ?, ?, ?, ?, ?, 0, '', '[]')
        ON CONFLICT(id) DO UPDATE SET
            provenance=excluded.provenance, confidence=excluded.confidence
        """,
        (eid, from_id, to_id, type_, provenance, confidence),
    )


def _insert_evidence(store: GraphStore, eid: str, file: str, line: int) -> None:
    store.conn.execute(
        """
        INSERT INTO evidence (id, edge_id, file, line, parser_version,
                               freshness_ts, verification_status)
        VALUES (?, ?, ?, ?, ?, 0, 'verified')
        ON CONFLICT(id) DO NOTHING
        """,
        (evidence_id(eid, file, line), eid, file, line, _PARSER_VERSION),
    )


def _merge_pair(
    store: GraphStore, from_id: str, to_id: str, typed_type: str, file: str, line: int
) -> None:
    typed_id = edge_id(from_id, to_id, typed_type)
    already_typed = store.conn.execute(
        "SELECT id FROM edge WHERE id = ?", (typed_id,)
    ).fetchone()
    if already_typed:
        _insert_evidence(store, typed_id, file, line)
        return

    base_type = _BASE_OF_TYPED[typed_type]
    base_id = edge_id(from_id, to_id, base_type)
    base_row = store.conn.execute(
        "SELECT * FROM edge WHERE id = ?", (base_id,)
    ).fetchone()

    if base_row is None:
        _upsert_edge(
            store,
            typed_id,
            from_id,
            to_id,
            typed_type,
            _SCIP_PROVENANCE,
            _TYPED_CONFIDENCE,
        )
        _insert_evidence(store, typed_id, file, line)
        return

    provenance = _merge_provenance(base_row["provenance"])
    confidence = max(float(base_row["confidence"]), _TYPED_CONFIDENCE)
    old_evidence = store.conn.execute(
        "SELECT file, line FROM evidence WHERE edge_id = ?", (base_id,)
    ).fetchall()
    store.conn.execute(
        "DELETE FROM edge WHERE id = ?", (base_id,)
    )  # cascades old evidence
    _upsert_edge(store, typed_id, from_id, to_id, typed_type, provenance, confidence)
    for row in old_evidence:
        _insert_evidence(store, typed_id, row["file"], row["line"])
    _insert_evidence(store, typed_id, file, line)


def run_overlay(repo: Path, index_path: Path, dry_run: bool = False) -> OverlayStats:
    db_path = repo / ".repoweaver" / "graph.db"
    index = decode_index(index_path.read_bytes())
    refs = extract_typed_references(index)

    stats = OverlayStats(typed_refs=len(refs))
    unmapped_symbols: set[str] = set()

    with GraphStore(db_path) as store:
        mapper = SymbolMapper(store)
        pairs: list[tuple[str, str, str, str, int]] = []
        for ref in refs:
            caller = mapper.resolve(ref.caller_symbol)
            target = mapper.resolve(ref.target_symbol)
            if not caller.ok:
                unmapped_symbols.add(ref.caller_symbol)
                continue
            if not target.ok:
                unmapped_symbols.add(ref.target_symbol)
                continue
            stats.mapped += 1
            edge_type = classify_edge_type(caller.node, target.node, ref)
            pairs.append(
                (caller.node["id"], target.node["id"], edge_type, ref.file, ref.line)
            )

        stats.unmapped_symbols = len(unmapped_symbols)
        stats.skip_reasons = dict(mapper.skip_counts)

        if dry_run:
            store.rollback()
            return stats

        for from_id, to_id, edge_type, file, line in pairs:
            _merge_pair(store, from_id, to_id, edge_type, file, line)
        store.commit()

        typed_rows = store.conn.execute(
            f"SELECT provenance FROM edge WHERE type IN "
            f"({','.join('?' * len(TYPED_EDGE_TYPES))})",
            TYPED_EDGE_TYPES,
        ).fetchall()
        stats.merged = sum(
            1 for r in typed_rows if "tree_sitter_java" in r["provenance"]
        )
        stats.added = sum(
            1 for r in typed_rows if "tree_sitter_java" not in r["provenance"]
        )

    return stats
