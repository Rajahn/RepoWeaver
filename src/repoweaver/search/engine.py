"""Hybrid BM25 + simplified Personalized PageRank retrieval for the call-graph.

The "simplified PPR" here is a bounded random-walk-with-restart: starting from
the BM25 seed set, we relax scores outward hop-by-hop, multiplying by edge
confidence and a damping factor each hop, and stop after ``depth`` hops (or
once contributions become negligible). This avoids pulling in a linear-algebra
dependency for what is, at repo scale, a shallow, sparse graph.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from repoweaver.graph.store import GraphStore

DAMPING = 0.85
MIN_CONTRIBUTION = 1e-4
BM25_WEIGHT = 0.6
PAGERANK_WEIGHT = 0.4


@dataclass
class SearchResult:
    """A single ranked symbol returned by the search engine."""

    node_id: str
    qualified_name: str
    simple_name: str
    score: float
    bm25_score: float = 0.0
    pagerank_score: float = 0.0
    kind: str = ""
    file: str = ""
    span_start: int = 0
    span_end: int = 0
    signature: str = ""


@dataclass
class SearchQuery:
    """Parameters for a search request."""

    query: str
    max_results: int = 20
    min_confidence: float = 0.5
    depth: int = 2
    task: str = "understand"  # understand | impact | locate | debug


class SearchEngine:
    """Hybrid retrieval: BM25 full-text seed + PageRank-weighted graph proximity."""

    def __init__(self, store: GraphStore) -> None:
        self.store = store

    def search(self, query: SearchQuery) -> list[SearchResult]:
        bm25_hits = self.bm25_candidates(
            query.query, limit=max(50, query.max_results * 3)
        )
        exact = self._exact_symbol_hits(query.query)
        by_id: dict[str, SearchResult] = {}
        for hit in bm25_hits + exact:
            existing = by_id.get(hit.node_id)
            if existing is None or hit.bm25_score > existing.bm25_score:
                by_id[hit.node_id] = hit

        if not by_id:
            return []

        max_bm25 = max((r.bm25_score for r in by_id.values()), default=1.0) or 1.0
        seeds = {nid: r.bm25_score / max_bm25 for nid, r in by_id.items()}
        pagerank_scores = personalized_pagerank(
            self.store, seeds, depth=query.depth, min_confidence=query.min_confidence
        )
        max_pr = max(pagerank_scores.values(), default=1.0) or 1.0

        results = self.pagerank_rerank(list(by_id.values()), pagerank_scores, max_pr)
        results.sort(key=lambda r: r.score, reverse=True)
        return results[: query.max_results]

    def bm25_candidates(self, text: str, limit: int = 100) -> list[SearchResult]:
        hits = self.store.fts_search(text, limit=limit)
        out = []
        for node, score in hits:
            out.append(
                SearchResult(
                    node_id=node["id"],
                    qualified_name=node["qualified_name"],
                    simple_name=node["simple_name"],
                    score=0.0,
                    bm25_score=score,
                    kind=node["kind"],
                    file=node["file"],
                    span_start=node["span_start"],
                    span_end=node["span_end"],
                    signature=node["signature"],
                )
            )
        return out

    def pagerank_rerank(
        self,
        candidates: list[SearchResult],
        pagerank_scores: dict[str, float],
        max_pr: float = 1.0,
    ) -> list[SearchResult]:
        max_bm25 = max((r.bm25_score for r in candidates), default=1.0) or 1.0
        out = []
        for r in candidates:
            pr = pagerank_scores.get(r.node_id, 0.0)
            r.pagerank_score = pr
            r.score = BM25_WEIGHT * (r.bm25_score / max_bm25) + PAGERANK_WEIGHT * (
                pr / max_pr
            )
            out.append(r)
        return out

    def _exact_symbol_hits(self, query: str) -> list[SearchResult]:
        q = query.strip()
        if not q:
            return []
        out = []
        for node in self.store.find_by_qualified_name(
            q
        ) + self.store.find_by_simple_name(q):
            out.append(
                SearchResult(
                    node_id=node["id"],
                    qualified_name=node["qualified_name"],
                    simple_name=node["simple_name"],
                    score=0.0,
                    bm25_score=1.0,  # exact match beats any BM25 rank
                    kind=node["kind"],
                    file=node["file"],
                    span_start=node["span_start"],
                    span_end=node["span_end"],
                    signature=node["signature"],
                )
            )
        return out


def personalized_pagerank(
    store: GraphStore,
    seeds: dict[str, float],
    depth: int = 2,
    min_confidence: float = 0.0,
    damping: float = DAMPING,
) -> dict[str, float]:
    """Bounded random-walk-with-restart PageRank approximation from `seeds`."""
    scores: dict[str, float] = dict(seeds)
    frontier: dict[str, float] = dict(seeds)

    for _ in range(max(depth, 0)):
        next_frontier: dict[str, float] = {}
        for nid, weight in frontier.items():
            for direction in ("out", "in"):
                for neighbor, _edge_type, confidence in store.neighbors(
                    nid, direction=direction, min_confidence=min_confidence
                ):
                    contribution = weight * confidence * damping
                    if contribution < MIN_CONTRIBUTION:
                        continue
                    nb_id = neighbor["id"]
                    scores[nb_id] = scores.get(nb_id, 0.0) + contribution
                    next_frontier[nb_id] = next_frontier.get(nb_id, 0.0) + contribution
        frontier = next_frontier
        if not frontier:
            break

    return scores
