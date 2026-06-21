#!/usr/bin/env python3
"""
Claude Code SessionEnd hook — kicks off transcript memory extraction (tier 2).

Reads the hook JSON on stdin (transcript_path, cwd), derives the project slug,
and spawns extract_memories.py fully detached so session teardown is never
blocked. Returns immediately. Extraction itself is a no-op unless
DIARY_AUTO_EXTRACT=1 and ANTHROPIC_API_KEY are set.

Register in ~/.claude/settings.json:
  "hooks": {
    "SessionEnd": [
      {"hooks": [{"type": "command",
                  "command": "python3 /path/to/scripts/session_end_hook.py"}]}
    ]
  }
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


def main() -> None:
    # Cheap gate first: do nothing unless extraction is explicitly enabled.
    if os.environ.get("DIARY_AUTO_EXTRACT") != "1":
        return
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return

    transcript = payload.get("transcript_path")
    if not transcript or not os.path.exists(transcript):
        return
    cwd = payload.get("cwd") or os.getcwd()
    slug = _slug_from_cwd(cwd)

    script = str(Path(__file__).resolve().parent / "extract_memories.py")
    try:
        subprocess.Popen(
            [sys.executable, script, transcript, slug],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL, start_new_session=True,
        )
    except Exception:
        pass


if __name__ == "__main__":
    main()
