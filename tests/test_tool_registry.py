"""Guards the MCP tool surface: 0.32.0 slipped a helper between @mcp.tool() and
memory_upsert, which silently unregistered the core write tool."""
from __future__ import annotations

import asyncio

CORE_TOOLS = {
    "memory_context", "memory_project_context", "memory_get", "memory_upsert",
    "memory_search", "memory_search_semantic", "memory_delete", "memory_tree",
}


def _tool_names() -> set[str]:
    import diary_server
    return {t.name for t in asyncio.run(diary_server.mcp.list_tools())}


def test_core_tools_registered():
    assert CORE_TOOLS <= _tool_names()


def test_no_private_helpers_exposed():
    assert not [n for n in _tool_names() if n.startswith("_")]
