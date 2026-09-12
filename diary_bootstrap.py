"""
Shared FastMCP instance for the main diary-mcp server.

Split out of diary_server.py so tool-implementation modules (diary_project_tools,
memory_service, search_engine, graph_core, sync_manager) can register their
@mcp.tool() functions without importing diary_server itself (which would be
circular — diary_server imports all of them to re-export their names).

diary_admin_server.py (the separate diary-admin-mcp entry point for graph
introspection/maintenance tools) has its OWN FastMCP instance, not this one —
those tools are intentionally not exposed on the main server's tool surface.
"""
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("Diary")
