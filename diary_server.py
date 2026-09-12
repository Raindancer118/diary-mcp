"""
diary-mcp main server entry point (FastMCP, stdio transport).

Thin by design (v0.10.0 architecture review): this file only bootstraps the
process (DB init, embedding warmup) and re-exports every tool implementation
from its owning module, so `diary_server.<tool_name>` keeps working for the
test suite and for anyone poking at the module directly — but the actual
logic/SQL lives in focused files:

  diary_project_tools.py — projects/logs/errors/milestones/tasks/reminders/wiki
  memory_service.py      — memory tree CRUD, two-tier lifecycle, pinning
  search_engine.py       — hybrid search, ranking, extracted-tier tripwire
  graph_core.py          — memory_link / memory_get_links (kept on this server)
  graph_admin.py          — graph introspection/maintenance (admin-only, see below)
  sync_manager.py        — memory_sync / memory_sync_diary / tombstone purges

graph_admin.py's tools (memory_infer_links, memory_explain, memory_path,
memory_graph_stats, memory_query_graph, memory_report) are intentionally NOT
registered on THIS server's `mcp` instance — they're audit/maintenance tools a
human invokes explicitly, not something an agent should see in its everyday
tool list. They live on the separate `diary-admin-mcp` entry point
(diary_admin_server.py) and are re-exported here as plain functions only so
they stay directly testable/importable.
"""
import threading

import diary_embed
from diary_bootstrap import mcp
from diary_db import init_db

init_db()

# Loading the embedding model (heavy import + ONNX session init) takes seconds
# and would otherwise happen synchronously inside whichever tool call embeds
# first (memory_search always embeds the query). Warm it in the background so
# it's typically ready before the user's first real request lands; _get_model's
# lock makes this race-safe against a real request that beats the warmup.
threading.Thread(target=diary_embed.is_available, daemon=True, name="embed-warmup").start()

# Import for @mcp.tool() registration side effects.
import diary_project_tools  # noqa: E402
import memory_service  # noqa: E402
import search_engine  # noqa: E402
import graph_core  # noqa: E402
import sync_manager  # noqa: E402
import graph_admin  # noqa: E402 — NOT registered on `mcp`, see module docstring above.

# --- Re-exports: diary_project_tools -----------------------------------------
from diary_project_tools import (  # noqa: E402,F401
    get_global_config, update_global_config, get_project_config, update_project_config,
    sync_remote_logs, search_global, filter_logs,
    get_projects, get_project, add_project, delete_project, archive_project, update_status,
    add_log_entry, edit_log_entry, delete_log_entry,
    add_error_solution, get_errors_solutions,
    add_milestone, toggle_milestone, edit_milestone, delete_milestone,
    get_active_tasks, add_task, toggle_task, edit_task, delete_task,
    add_reminder, edit_reminder, snooze_reminder, toggle_reminder, delete_reminder,
    add_wiki_page, edit_wiki_page, get_wiki_pages, get_wiki_page, search_wiki,
)

# --- Re-exports: memory_service -----------------------------------------------
from memory_service import (  # noqa: E402,F401
    memory_context, memory_tree, memory_list_by_tag, memory_get, memory_upsert, memory_save_extracted,
    memory_promote, memory_prune_extracted, memory_delete, memory_merge, memory_purge_tombstones,
    memory_set_importance, memory_pin, memory_unpin, memory_project_context,
    memory_set_project_config, memory_get_project_config,
    memory_set_project_dir, memory_unset_project_dir,
    _pgvector_cache, _pgvector_ready,
)

# --- Re-exports: search_engine -------------------------------------------------
from search_engine import memory_search, memory_recall, memory_search_semantic, memory_reembed_all  # noqa: E402,F401

# --- Re-exports: graph_core (also registered as tools on `mcp`) ---------------
from graph_core import memory_link, memory_get_links  # noqa: E402,F401

# --- Re-exports: graph_admin (plain functions, NOT tools on this server) ------
from graph_admin import (  # noqa: E402,F401
    memory_infer_links, memory_explain, memory_path, memory_graph_stats,
    memory_query_graph, memory_report, memory_consolidate_report,
)

# --- Re-exports: sync_manager --------------------------------------------------
from sync_manager import memory_sync, memory_sync_diary, memory_purge_tombstones_diary  # noqa: E402,F401


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
