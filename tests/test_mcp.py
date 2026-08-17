import asyncio

from repoweaver.server.mcp import mcp


def test_only_explore_is_public_by_default():
    tools = asyncio.run(mcp.list_tools())
    assert [tool.name for tool in tools] == ["explore"]
