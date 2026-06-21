#!/usr/bin/env python3
"""
Claude Code SessionEnd hook — kicks off transcript memory extraction (tier 2).

Auto-extraction is configured PER PROJECT: it runs only if the current project's
/projects/<slug> node has config {"auto_extract": true} (set via
memory_set_project_config). A global DIARY_AUTO_EXTRACT=1 env var force-enables it
everywhere (useful for testing). Default: off.

Reads the hook JSON on stdin (transcript_path, cwd), derives the project slug, and
— if enabled for that project — spawns extract_memories.py fully detached so
session teardown is never blocked. Returns immediately. Extraction is purely
deterministic (no AI/API).

Register in ~/.claude/settings.json under hooks.SessionEnd.
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path


def _slug_from_cwd(cwd: str) -> str:
    base = os.path.basename(cwd.rstrip("/"))
    return re.sub(r"[^a-z0-9]+", "-", base.lower()).strip("-") or "misc"


def _project_auto_extract(slug: str) -> bool:
    """Lean per-project lookup of config.auto_extract. Fails closed (False)."""
    try:
        import psycopg
        url = os.environ.get("DIARY_DATABASE_URL", "postgresql://localhost/diary_mcp")
        with psycopg.connect(url, connect_timeout=3) as conn:
            row = conn.execute(
                "SELECT (config->>'auto_extract')::bool FROM memory_nodes WHERE path = %s",
                (f"/projects/{slug}",),
            ).fetchone()
            return bool(row and row[0])
    except Exception:
        return False


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return

    transcript = payload.get("transcript_path")
    if not transcript or not os.path.exists(transcript):
        return
    cwd = payload.get("cwd") or os.getcwd()
    slug = _slug_from_cwd(cwd)

    enabled = os.environ.get("DIARY_AUTO_EXTRACT") == "1" or _project_auto_extract(slug)
    if not enabled:
        return

    script = str(Path(__file__).resolve().parent / "extract_memories.py")
    try:
        subprocess.Popen(
            [sys.executable, script, transcript, slug],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL, start_new_session=True,
            env={**os.environ, "DIARY_AUTO_EXTRACT": "1"},  # hook already gated; tell child it's authorized
        )
    except Exception:
        pass


if __name__ == "__main__":
    main()
