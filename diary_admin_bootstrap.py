"""FastMCP instance for the separate diary-admin-mcp entry point (graph
introspection/maintenance tools — see graph_admin.py)."""
from mcp.server.fastmcp import FastMCP

admin_mcp = FastMCP("Diary-Admin")
