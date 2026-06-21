#!/usr/bin/env python3
"""
Claude Code SessionStart hook — context injection from the diary-mcp memory tree.

Behaviour depends on the hook's `source` (startup | resume | clear | compact):
  • source == "compact"  → injects memories with 'compact' in pin_triggers
    (critical info that must survive a context compaction).
  • any other source     → injects memories with 'start' in pin_triggers
    (the project's most important facts, loaded at session start).

In both cases ONLY explicitly-pinned memories are injected — never arbitrary
ones — scoped to the project derived from cwd, plus globally-pinned /user and
/feedback memories.

Deliberately lean: direct Postgres query, no MCP/model import, fails silent.
A hook must never block a session.

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
import sys
from pathlib import Path

# Allow importing the shared helper when this script is run standalone.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _slug_resolve import slug_from_cwd  # noqa: E402


def _database_url() -> str:
    return os.environ.get("DIARY_DATABASE_URL", "postgresql://localhost/diary_mcp")


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        payload = {}

    source = payload.get("source") or "startup"
    cwd = payload.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    slug = slug_from_cwd(cwd)
    if not slug:
        return

    # Compaction → 'compact' pin; otherwise → 'start' pin.
    if source == "compact":
        trigger, heading = "compact", "Nach Kompaktierung neu geladene Memories"
    else:
        trigger, heading = "start", "Gepinnte Memories (Session-Start)"

    try:
        import psycopg
        from psycopg.rows import dict_row
    except Exception:
        return

    base = f"/projects/{slug}"
    try:
        with psycopg.connect(_database_url(), row_factory=dict_row, connect_timeout=3) as conn:
            rows = conn.execute(
                "SELECT path, type, title, body FROM memory_nodes "
                "WHERE %s = ANY(pin_triggers) AND origin = 'curated' AND deleted_at IS NULL AND ("
                "  path = %s OR path LIKE %s "
                "  OR path LIKE '/user/%%' OR path LIKE '/feedback/%%') "
                "ORDER BY (path LIKE %s) DESC, importance DESC, path",
                (trigger, base, f"{base}/%", f"{base}%"),
            ).fetchall()
    except Exception:
        return

    if not rows:
        return

    project_rows = [r for r in rows if r["path"].startswith(base)]
    global_rows = [r for r in rows if not r["path"].startswith(base)]

    lines = [f"# {heading} — Projekt '{slug}'",
             "(Automatisch aus dem diary-mcp Memory-Tree geladen.)", ""]
    for r in project_rows:
        lines.append(f"## [{r['type']}] {r['title']}  ⟨{r['path']}⟩")
        lines.append((r["body"] or "").strip())
        lines.append("")
    if global_rows:
        lines.append("# Globale Memories (User & Feedback)")
        lines.append("")
        for r in global_rows:
            lines.append(f"## [{r['type']}] {r['title']}  ⟨{r['path']}⟩")
            lines.append((r["body"] or "").strip())
            lines.append("")

    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": "\n".join(lines).strip(),
        }
    }))


if __name__ == "__main__":
    main()
