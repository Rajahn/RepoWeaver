from __future__ import annotations

from repoweaver.search.engine import SearchEngine, SearchQuery, personalized_pagerank


def test_bm25_candidates_and_exact_hit(built_javademo):
    from repoweaver.graph.store import GraphStore

    with GraphStore(built_javademo / ".repoweaver" / "graph.db") as store:
        engine = SearchEngine(store)
        results = engine.search(SearchQuery(query="Greeter", max_results=10))
        assert results
        assert any(r.simple_name == "Greeter" for r in results)


def test_personalized_pagerank_propagates_to_neighbors(built_javademo):
    from repoweaver.graph.store import GraphStore

    with GraphStore(built_javademo / ".repoweaver" / "graph.db") as store:
        greeter = store.find_by_qualified_name("com.example.demo.Greeter")[0]
        scores = personalized_pagerank(
            store, {greeter["id"]: 1.0}, depth=2, min_confidence=0.0
        )
        # EnglishGreeter/AbstractGreeter both point IMPLEMENTS -> Greeter, so Greeter
        # should propagate score back to them via the "in" direction walk.
        english = store.find_by_qualified_name("com.example.demo.EnglishGreeter")[0]
        assert scores.get(english["id"], 0.0) > 0.0


def test_search_ranks_exact_match_first(built_javademo):
    from repoweaver.graph.store import GraphStore

    with GraphStore(built_javademo / ".repoweaver" / "graph.db") as store:
        engine = SearchEngine(store)
        results = engine.search(
            SearchQuery(query="com.example.demo.Greeter", max_results=5)
        )
        assert results
        assert results[0].qualified_name == "com.example.demo.Greeter"
