from __future__ import annotations

from codecontextfabric.graph.store import EdgeRow, GraphStore, NodeRow


def _node(nid: str, qname: str, simple: str, file: str = "A.java") -> NodeRow:
    return NodeRow(
        id=nid,
        kind="class",
        qualified_name=qname,
        simple_name=simple,
        file=file,
        span_start=1,
        span_end=10,
        signature=f"class {simple}",
    )


def test_replace_file_nodes_insert_update_delete(in_memory_store: GraphStore):
    store = in_memory_store
    store.replace_file_nodes(
        "A.java", [_node("n1", "pkg.A", "A"), _node("n2", "pkg.B", "B")]
    )
    store.commit()
    assert store.node_count() == 2

    # Re-index the same file with n2 removed and n1 updated (new span).
    updated = NodeRow(
        id="n1",
        kind="class",
        qualified_name="pkg.A",
        simple_name="A",
        file="A.java",
        span_start=5,
        span_end=20,
        signature="class A",
    )
    store.replace_file_nodes("A.java", [updated])
    store.commit()
    assert store.node_count() == 1
    assert store.get_node("n1")["span_start"] == 5
    assert store.get_node("n2") is None


def test_replace_file_edges_merges_duplicates_and_tracks_evidence(
    in_memory_store: GraphStore,
):
    store = in_memory_store
    store.replace_file_nodes(
        "A.java", [_node("n1", "pkg.A", "A"), _node("n2", "pkg.B", "B")]
    )
    store.commit()

    edges = [
        EdgeRow(
            from_id="n1",
            to_id="n2",
            type="CALLS",
            provenance="tree_sitter_java",
            confidence=0.7,
            file="A.java",
            line=5,
        ),
        EdgeRow(
            from_id="n1",
            to_id="n2",
            type="CALLS",
            provenance="tree_sitter_java",
            confidence=0.7,
            file="A.java",
            line=9,
        ),
    ]
    store.replace_file_edges("A.java", edges, parser_version="test-1")
    store.commit()

    assert store.edge_count() == 1
    edge = store.edge_between("n1", "n2")
    assert edge is not None
    assert store.evidence_for_edge(edge["id"]).__len__() == 2


def test_replace_file_edges_deduplicates_identical_evidence_site(
    in_memory_store: GraphStore,
):
    store = in_memory_store
    store.replace_file_nodes(
        "A.java", [_node("n1", "pkg.A", "A"), _node("n2", "pkg.B", "B")]
    )
    duplicate = EdgeRow(
        from_id="n1",
        to_id="n2",
        type="CALLS",
        provenance="tree_sitter_java",
        confidence=0.7,
        file="A.java",
        line=5,
    )
    store.replace_file_edges("A.java", [duplicate, duplicate], parser_version="test-1")
    store.commit()

    edge = store.edge_between("n1", "n2")
    assert edge is not None
    assert len(store.evidence_for_edge(edge["id"])) == 1


def test_fts_search_finds_by_simple_name(in_memory_store: GraphStore):
    store = in_memory_store
    store.replace_file_nodes("A.java", [_node("n1", "pkg.Greeter", "Greeter")])
    store.commit()
    hits = store.fts_search("Greeter")
    assert len(hits) == 1
    assert hits[0][0]["id"] == "n1"


def test_neighbors_respects_min_confidence(in_memory_store: GraphStore):
    store = in_memory_store
    store.replace_file_nodes(
        "A.java", [_node("n1", "pkg.A", "A"), _node("n2", "pkg.B", "B")]
    )
    store.commit()
    store.replace_file_edges(
        "A.java",
        [
            EdgeRow(
                from_id="n1",
                to_id="n2",
                type="CALLS",
                provenance="p",
                confidence=0.3,
                file="A.java",
                line=1,
            )
        ],
        parser_version="v1",
    )
    store.commit()
    assert store.neighbors("n1", "out", min_confidence=0.5) == []
    assert len(store.neighbors("n1", "out", min_confidence=0.0)) == 1


def test_is_fresh(in_memory_store: GraphStore):
    store = in_memory_store
    store.upsert_file_meta("A.java", "hash1", 1)
    store.commit()
    fresh, stale = store.is_fresh({"A.java": "hash1"})
    assert fresh
    assert stale == []

    fresh, stale = store.is_fresh({"A.java": "hash2"})
    assert not fresh
    assert stale == ["A.java"]

    fresh, stale = store.is_fresh({"A.java": "hash1", "B.java": "hash3"})
    assert not fresh
    assert stale == ["B.java"]


def test_delete_file_cascades_edges(in_memory_store: GraphStore):
    store = in_memory_store
    store.replace_file_nodes(
        "A.java", [_node("n1", "pkg.A", "A"), _node("n2", "pkg.B", "B", file="B.java")]
    )
    store.commit()
    store.replace_file_edges(
        "A.java",
        [
            EdgeRow(
                from_id="n1",
                to_id="n2",
                type="CALLS",
                provenance="p",
                confidence=1.0,
                file="A.java",
                line=1,
            )
        ],
        parser_version="v1",
    )
    store.upsert_file_meta("A.java", "h", 1)
    store.commit()
    assert store.edge_count() == 1

    store.delete_file("A.java")
    store.commit()
    assert store.node_count() == 1  # n2 (owned by B.java) survives
    assert store.edge_count() == 0  # edge cascaded away with n1
