"""
diary-admin-mcp — separate MCP entry point for knowledge-graph introspection
and maintenance tools (memory_infer_links, memory_explain, memory_path,
memory_graph_stats, memory_query_graph, memory_report).

Deliberately NOT part of the main diary-mcp server's tool surface: these are
audit/maintenance operations invoked explicitly by a human ("show me clusters",
"clean up the graph", periodic architecture reviews) rather than something an
agent reaches for during normal work. Splitting them out keeps the main
server's day-to-day tool list focused. See graph_admin.py's module docstring
for the full rationale.

Add to .mcp.json only when you actually want these tools available:
  {
    "mcpServers": {
      "diary-admin": {
        "command": "diary-admin-mcp",
        "env": {"DIARY_DATABASE_URL": "postgresql://localhost/diary_mcp"}
      }
    }
  }
"""
from diary_admin_bootstrap import admin_mcp
from diary_db import init_db

init_db()

import graph_admin  # noqa: F401,E402 — import for its @admin_mcp.tool() side effects


def main() -> None:
    admin_mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
