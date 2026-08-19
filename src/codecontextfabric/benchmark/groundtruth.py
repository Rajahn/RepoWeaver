"""Ground-truth correctness evaluation: node recall, edge precision/recall,
and query top-k recall/MRR against a hand-authored, machine-verifiable
`ground_truth.yaml` fixture (see benchmarks/ground_truth.yaml).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import yaml

from codecontextfabric.benchmark.metrics import RESOLVED_MIN_CONFIDENCE
from codecontextfabric.graph.store import GraphStore
from codecontextfabric.search.engine import SearchEngine, SearchQuery


@dataclass
class GroundTruth:
    fixture: str
    nodes: list[dict]
    edges: list[dict]
    queries: list[dict]

    @classmethod
    def load(cls, path: str | Path) -> GroundTruth:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        return cls(
            fixture=data["fixture"],
            nodes=data.get("nodes", []),
            edges=data.get("edges", []),
            queries=data.get("queries", []),
        )


def _is_resolved_edge(row: dict) -> bool:
    return (
        row["confidence"] >= RESOLVED_MIN_CONFIDENCE
        and row["ambiguous_candidates"] == "[]"
    )


def evaluate_nodes(gt: GroundTruth, store: GraphStore) -> dict:
    total = len(gt.nodes)
    found = 0
    missing: list[str] = []
    for expected in gt.nodes:
        rows = store.find_by_qualified_name(expected["qualified_name"])
        if any(r["kind"] == expected["kind"] for r in rows):
            found += 1
        else:
            missing.append(f"{expected['kind']}:{expected['qualified_name']}")
    return {
        "node_recall": (found / total) if total else None,
        "nodes_expected": total,
        "nodes_found": found,
        "nodes_missing": missing,
    }


def _node_id_for(store: GraphStore, qualified_name: str) -> str | None:
    rows = store.find_by_qualified_name(qualified_name)
    return rows[0]["id"] if rows else None


def evaluate_edges(gt: GroundTruth, store: GraphStore) -> dict:
    expected_resolvable: set[tuple[str, str, str]] = set()
    expected_ambiguous: set[tuple[str, str, str]] = set()
    unresolved_gt_ids = 0

    for expected in gt.edges:
        from_id = _node_id_for(store, expected["from"])
        to_id = _node_id_for(store, expected["to"])
        if from_id is None or to_id is None:
            unresolved_gt_ids += 1
            continue
        key = (from_id, to_id, expected["type"])
        if expected["resolvable"]:
            expected_resolvable.add(key)
        else:
            expected_ambiguous.add(key)

    scope_files = {n["qualified_name"] for n in gt.nodes}
    rows = store.conn.execute(
        """
        SELECT edge.from_id, edge.to_id, edge.type, edge.confidence, edge.ambiguous_candidates,
               from_node.qualified_name AS from_qname, to_node.qualified_name AS to_qname
        FROM edge
        JOIN node AS from_node ON from_node.id = edge.from_id
        JOIN node AS to_node ON to_node.id = edge.to_id
        """
    ).fetchall()
    in_scope = [
        r
        for r in rows
        if r["from_qname"] in scope_files and r["to_qname"] in scope_files
    ]

    predicted_resolved = {
        (r["from_id"], r["to_id"], r["type"]) for r in in_scope if _is_resolved_edge(r)
    }

    # Ambiguous candidates never reach the `edge` table (see resolver.py /
    # UnresolvedReferenceRow) — they live in unresolved_reference as one row
    # per (from_id, type, target_name) with a unioned candidate list. A
    # (from, to, type) ground-truth triple counts as "correctly flagged
    # ambiguous" if `to` is one of that row's candidates.
    unresolved_rows = store.conn.execute(
        """
        SELECT ur.from_id, ur.type, ur.candidates, from_node.qualified_name AS from_qname
        FROM unresolved_reference AS ur
        JOIN node AS from_node ON from_node.id = ur.from_id
        """
    ).fetchall()
    predicted_ambiguous: set[tuple[str, str, str]] = set()
    for r in unresolved_rows:
        if r["from_qname"] not in scope_files:
            continue
        for candidate_id in json.loads(r["candidates"]):
            candidate_node = store.get_node(candidate_id)
            if candidate_node and candidate_node["qualified_name"] in scope_files:
                predicted_ambiguous.add((r["from_id"], candidate_id, r["type"]))

    true_positives = expected_resolvable & predicted_resolved
    precision = (
        len(true_positives) / len(predicted_resolved) if predicted_resolved else None
    )
    recall = (
        len(true_positives) / len(expected_resolvable) if expected_resolvable else None
    )
    ambiguity_recall = (
        len(expected_ambiguous & predicted_ambiguous) / len(expected_ambiguous)
        if expected_ambiguous
        else None
    )

    return {
        "edge_precision": precision,
        "edge_recall": recall,
        "edges_expected_resolvable": len(expected_resolvable),
        "edges_predicted_resolved_in_scope": len(predicted_resolved),
        "edges_true_positive": len(true_positives),
        "expected_ambiguous_correctly_flagged_rate": ambiguity_recall,
        "expected_ambiguous_total": len(expected_ambiguous),
        "ground_truth_ids_unresolved": unresolved_gt_ids,
    }


def evaluate_queries(gt: GroundTruth, store: GraphStore) -> dict:
    if not gt.queries:
        return {"query_topk_recall": None, "query_mrr": None, "query_cases": []}

    engine = SearchEngine(store)
    cases: list[dict] = []
    reciprocal_ranks: list[float] = []
    hits = 0

    for case in gt.queries:
        k = case.get("k", 5)
        results = engine.search(
            SearchQuery(
                query=case["query"],
                max_results=k,
                min_confidence=0.0,
                depth=2,
                task=case.get("task", "locate"),
            )
        )
        expected = set(case["expected_any_of"])
        rank = None
        for idx, hit in enumerate(results[:k], start=1):
            if hit.qualified_name in expected:
                rank = idx
                break
        if rank is not None:
            hits += 1
            reciprocal_ranks.append(1.0 / rank)
        else:
            reciprocal_ranks.append(0.0)
        cases.append(
            {
                "query": case["query"],
                "k": k,
                "expected_any_of": sorted(expected),
                "rank": rank,
                "top_k_qualified_names": [h.qualified_name for h in results[:k]],
            }
        )

    total = len(gt.queries)
    return {
        "query_topk_recall": hits / total if total else None,
        "query_mrr": sum(reciprocal_ranks) / total if total else None,
        "query_cases": cases,
    }


def evaluate(gt: GroundTruth, store: GraphStore) -> dict:
    result = {}
    result.update(evaluate_nodes(gt, store))
    result.update(evaluate_edges(gt, store))
    result.update(evaluate_queries(gt, store))
    return result


__all__ = [
    "GroundTruth",
    "evaluate",
    "evaluate_edges",
    "evaluate_nodes",
    "evaluate_queries",
]
