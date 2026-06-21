#!/usr/bin/env python3
"""
Claude Code SessionStart hook — auto-injects a project's auto_inject memories.

Reads the hook JSON on stdin (contains `cwd`), derives a project slug from the
working directory, and prints the project's auto_inject memories (plus global
/user and /feedback auto_inject memories) as additionalContext so they land in
Claude's context automatically at session start.

Deliberately lean: connects to Postgres directly, never imports the MCP server
or the embedding model, so it adds negligible startup latency. Fails silent
(empty output) if the DB is unreachable — a hook must never block a session.

Register in ~/.claude/settings.json:
  "hooks": {
    "SessionStart": [
      {"hooks": [{"type": "command",
                  "command": "python3 /path/to/scripts/session_start_hook.py"}]}
    ]
  }
"""
import json
import os
import re
import sys


def _slug_from_cwd(cwd: str) -> str:
    base = os.path.basename(cwd.rstrip("/"))
    slug = re.sub(r"[^a-z0-9]+", "-", base.lower()).strip("-")
    return slug


def _database_url() -> str:
    return os.environ.get("DIARY_DATABASE_URL", "postgresql://localhost/diary_mcp")


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        payload = {}

    cwd = payload.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    slug = _slug_from_cwd(cwd)
    if not slug:
        return

    try:
        import psycopg
        from psycopg.rows import dict_row
    except Exception:
        return

    base = f"/projects/{slug}"
    try:
        with psycopg.connect(_database_url(), row_factory=dict_row, connect_timeout=3) as conn:
            rows = conn.execute(
                "SELECT path, type, title, body, importance FROM memory_nodes "
                "WHERE auto_inject AND origin = 'curated' AND ("
                "  path = %s OR path LIKE %s "
                "  OR path LIKE '/user/%%' OR path LIKE '/feedback/%%') "
                "ORDER BY (path LIKE %s) DESC, importance DESC, path",
                (base, f"{base}/%", f"{base}%"),
            ).fetchall()
    except Exception:
        return

    if not rows:
        return

    project_rows = [r for r in rows if r["path"].startswith(base)]
    global_rows = [r for r in rows if not r["path"].startswith(base)]

    lines = [f"# Auto-Inject Memories — Projekt '{slug}'",
             "(Automatisch aus dem diary-mcp Memory-Tree geladen.)", ""]
    for r in project_rows:
        lines.append(f"## [{r['type']}] {r['title']}  ⟨{r['path']}⟩")
        lines.append((r["body"] or "").strip())
        lines.append("")
    if global_rows:
        lines.append("# Globale Auto-Inject-Memories (User & Feedback)")
        lines.append("")
        for r in global_rows:
            lines.append(f"## [{r['type']}] {r['title']}  ⟨{r['path']}⟩")
            lines.append((r["body"] or "").strip())
            lines.append("")

    context = "\n".join(lines).strip()
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context,
        }
    }))


if __name__ == "__main__":
    main()
