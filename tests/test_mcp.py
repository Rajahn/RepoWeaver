import asyncio
import importlib

from codecontextfabric.server import mcp as mcp_module
from codecontextfabric.server.mcp import mcp


def test_only_explore_is_public_by_default():
    tools = asyncio.run(mcp.list_tools())
    assert [tool.name for tool in tools] == ["explore"]


def test_debug_graph_hidden_by_default():
    tools = asyncio.run(mcp.list_tools())
    assert "debug_graph" not in [tool.name for tool in tools]


def test_debug_graph_exposed_only_with_others_selected(monkeypatch):
    monkeypatch.setenv("FABRIC_MCP_TOOLS", "debug_graph")
    reloaded = importlib.reload(mcp_module)
    try:
        tools = asyncio.run(reloaded.mcp.list_tools())
        assert sorted(tool.name for tool in tools) == ["debug_graph", "explore"]
    finally:
        importlib.reload(mcp_module)


def test_debug_graph_not_indexed_returns_error(tmp_path, monkeypatch):
    monkeypatch.setenv("FABRIC_MCP_TOOLS", "debug_graph")
    reloaded = importlib.reload(mcp_module)
    try:
        result = reloaded.debug_graph(symbol="Greeter", repo=str(tmp_path))
        assert result == {"error": "not_indexed", "hint": "run: ccf build"}
    finally:
        importlib.reload(mcp_module)


def test_debug_graph_dumps_node_and_edges(built_javademo, monkeypatch):
    monkeypatch.setenv("FABRIC_MCP_TOOLS", "debug_graph")
    reloaded = importlib.reload(mcp_module)
    try:
        result = reloaded.debug_graph(symbol="Greeter", repo=str(built_javademo))
        assert result["symbol"] == "Greeter"
        assert result["nodes"]
        entry = result["nodes"][0]
        assert entry["node"]["simple_name"] == "Greeter"
        for edge in entry["outgoing_edges"] + entry["incoming_edges"]:
            assert {"type", "confidence", "provenance", "ambiguous_candidates"} <= set(
                edge
            )
        assert isinstance(entry["ambiguous_candidates"], list)
    finally:
        importlib.reload(mcp_module)


def test_debug_graph_unknown_symbol_returns_empty_nodes(built_javademo, monkeypatch):
    monkeypatch.setenv("FABRIC_MCP_TOOLS", "debug_graph")
    reloaded = importlib.reload(mcp_module)
    try:
        result = reloaded.debug_graph(symbol="NoSuchSymbol", repo=str(built_javademo))
        assert result == {"symbol": "NoSuchSymbol", "nodes": []}
    finally:
        importlib.reload(mcp_module)
