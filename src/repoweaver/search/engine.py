"""Hybrid BM25 + PageRank search engine for the RepoWeaver call-graph."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from repoweaver.graph.store import GraphStore


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


@dataclass
class SearchQuery:
    """Parameters for a search request."""

    query: str
    max_results: int = 20
    min_confidence: float = 0.5
    depth: int = 2
    task: str = "understand"  # understand | impact | locate | debug


class SearchEngine:
    """
    Hybrid retrieval engine combining BM25 full-text search with
    PageRank-weighted graph proximity.

    This is a stub implementation. Full BM25 and PageRank logic are
    implemented in milestone T0.1.
    """

    def __init__(self, store: "GraphStore") -> None:
        self.store = store

    def search(self, query: SearchQuery) -> list[SearchResult]:
        """
        Execute a hybrid search and return ranked results.

        Returns an empty list until the backend is implemented.
        """
        return []

    def bm25_candidates(self, text: str, limit: int = 100) -> list[SearchResult]:
        """
        Run a BM25 full-text query against the node_fts virtual table.

        Stub — returns empty list.
        """
        return []

    def pagerank_rerank(
        self,
        candidates: list[SearchResult],
        depth: int = 2,
    ) -> list[SearchResult]:
        """
        Rerank BM25 candidates using PageRank scores propagated through
        the call-graph up to ``depth`` hops.

        Stub — returns candidates unchanged.
        """
        return candidates
