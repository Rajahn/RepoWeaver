"""FastMCP server — exposes the explore() tool to AI coding agents."""

import os

from fastmcp import FastMCP

from repoweaver.explore import explore as _explore

mcp = FastMCP("RepoWeaver")


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

        from repoweaver.explore import db_path_for
        from repoweaver.graph.store import GraphStore
        from repoweaver.indexer import Indexer

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

        from repoweaver.explore import db_path_for
        from repoweaver.graph.store import GraphStore
        from repoweaver.indexer import Indexer

        db_path = db_path_for(repo)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with GraphStore(db_path) as store:
            stats = Indexer(Path(repo).resolve(), store).build()
            return {"files": stats.files, "nodes": stats.nodes, "edges": stats.edges}
