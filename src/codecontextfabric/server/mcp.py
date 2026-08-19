"""FastMCP server — exposes the explore() tool to AI coding agents."""

import os

from fastmcp import FastMCP

from codecontextfabric.explore import explore as _explore

mcp = FastMCP("Code Context Fabric")


@mcp.tool()
def explore(
    query: str,
    task: str = "understand",
    repo: str = ".",
    max_tokens: int = 4000,
    depth: int = 2,
    min_confidence: float = 0.5,
) -> dict:
    """Code context fabric: symbols, call paths, blast radius. task: understand|impact|locate|debug"""
    return _explore(
        query=query,
        task=task,
        repo=repo,
        max_tokens=max_tokens,
        depth=depth,
        min_confidence=min_confidence,
    )


# Hidden diagnostic tools — enabled via FABRIC_MCP_TOOLS env var
_extra_tools = os.environ.get("FABRIC_MCP_TOOLS", "").split(",")

if "status" in _extra_tools:

    @mcp.tool()
    def status(repo: str = ".") -> dict:
        """Index status, freshness, last build time."""
        from pathlib import Path

        from codecontextfabric.explore import db_path_for
        from codecontextfabric.graph.store import GraphStore
        from codecontextfabric.indexer import Indexer

        db_path = db_path_for(repo)
        if not db_path.exists():
            return {"indexed": False}
        with GraphStore(db_path) as store:
            indexer = Indexer(Path(repo).resolve(), store)
            fresh, stale = store.is_fresh(indexer.current_file_hashes())
            return {
                "indexed": True,
                "fresh": fresh,
                "stale_files": stale,
                **store.stats(),
            }


if "reindex" in _extra_tools:

    @mcp.tool()
    def reindex(repo: str = ".", full: bool = False) -> dict:
        """Trigger a full rebuild (incremental rebuild is an M2 feature)."""
        from pathlib import Path

        from codecontextfabric.explore import db_path_for
        from codecontextfabric.graph.store import GraphStore
        from codecontextfabric.indexer import Indexer

        db_path = db_path_for(repo)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with GraphStore(db_path) as store:
            stats = Indexer(Path(repo).resolve(), store).build()
            return {"files": stats.files, "nodes": stats.nodes, "edges": stats.edges}


if "debug_graph" in _extra_tools:

    @mcp.tool()
    def debug_graph(symbol: str, repo: str = ".") -> dict:
        """Raw node/edge dump for a symbol — diagnosis only, not part of the
        explore() contract. No ranking, no trimming, no blind_spots."""
        from codecontextfabric.explore import db_path_for
        from codecontextfabric.graph.store import GraphStore

        db_path = db_path_for(repo)
        if not db_path.exists():
            return {"error": "not_indexed", "hint": "run: ccf build"}

        with GraphStore(db_path) as store:
            nodes = store.find_by_qualified_name(symbol) or store.find_by_simple_name(
                symbol
            )
            dumped = []
            for node in nodes:
                node_id = node["id"]
                outgoing = store.conn.execute(
                    "SELECT to_id, type, confidence, provenance, ambiguous_candidates "
                    "FROM edge WHERE from_id = ? ORDER BY to_id, type",
                    (node_id,),
                ).fetchall()
                incoming = store.conn.execute(
                    "SELECT from_id, type, confidence, provenance, ambiguous_candidates "
                    "FROM edge WHERE to_id = ? ORDER BY from_id, type",
                    (node_id,),
                ).fetchall()
                unresolved = store.conn.execute(
                    "SELECT type, target_name, candidates, reason, site_count "
                    "FROM unresolved_reference WHERE from_id = ? ORDER BY type, target_name",
                    (node_id,),
                ).fetchall()
                dumped.append(
                    {
                        "node": node,
                        "outgoing_edges": [dict(r) for r in outgoing],
                        "incoming_edges": [dict(r) for r in incoming],
                        "ambiguous_candidates": [dict(r) for r in unresolved],
                    }
                )
            return {"symbol": symbol, "nodes": dumped}
