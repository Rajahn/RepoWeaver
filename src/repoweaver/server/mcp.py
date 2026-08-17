"""FastMCP server — exposes the explore() tool to AI coding agents."""

import os

from fastmcp import FastMCP

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
    return {
        "query": query,
        "task": task,
        "slices": [],
        "stats": {
            "nodes_visited": 0,
            "edges_traversed": 0,
            "tokens_estimated": 0,
            "freshness": "ok",
        },
        "blind_spots": (
            "Static analysis only. Not represented: Spring bean injection dispatch beyond "
            "declared type, MQ listener call targets, reflection, config-driven routing, "
            "generated code (MyBatis Example, etc.). "
            "'No callers found' != dead code. Always verify with grep/source before concluding."
        ),
        "_note": "stub — run `fabric build` first",
    }


# Hidden diagnostic tools — enabled via FABRIC_MCP_TOOLS env var
_extra_tools = os.environ.get("FABRIC_MCP_TOOLS", "").split(",")

if "status" in _extra_tools:

    @mcp.tool()
    def status(repo: str = ".") -> dict:
        """Index status, freshness, last build time."""
        return {"status": "stub"}


if "reindex" in _extra_tools:

    @mcp.tool()
    def reindex(repo: str = ".", full: bool = False) -> dict:
        """Trigger incremental or full rebuild."""
        return {"status": "stub"}
