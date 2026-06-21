#!/usr/bin/env python3
"""
Transcript → extracted memories (tier 2) — fully deterministic, NO AI / NO API.

Mechanically captures the substantive user turns from a Claude Code session
transcript (JSONL) and stores each as an `extracted` memory (origin='extracted',
low importance) under /projects/<slug>/auto/<...>. No LLM, no summarization, no
external calls — just text capture. Each saved memory is embedded by the local
model so it is findable later via semantic search.

These land in the second tier: NOT loaded or searched by default, only via
memory_search(query, include_extracted=True). Promote good ones with
memory_promote(path).

Usage:
    extract_memories.py <transcript_path> [project_slug]

Activation:
    DIARY_AUTO_EXTRACT=1   (the only switch — no API key, no model API)

Designed to run detached in the background from the SessionEnd hook; never raises
into the caller. Logs to ~/.claude/diary-extract.log.
"""
import hashlib
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

LOG = Path.home() / ".claude" / "diary-extract.log"
MIN_LEN = 40          # skip trivial user turns
MAX_BODY = 2000       # cap stored body length
MAX_PER_SESSION = 40  # safety cap on memories per session


def _log(msg: str) -> None:
    try:
        with LOG.open("a", encoding="utf-8") as f:
            f.write(f"{datetime.now().isoformat()[:19]} {msg}\n")
    except OSError:
        pass


def _user_turns(path: str) -> list[str]:
    """Extract substantive user message texts from a Claude Code JSONL transcript."""
    turns: list[str] = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                msg = rec.get("message") or rec
                if (msg.get("role") or rec.get("type")) != "user":
                    continue
                content = msg.get("content")
                text = ""
                if isinstance(content, str):
                    text = content
                elif isinstance(content, list):
                    chunks = []
                    for c in content:
                        if isinstance(c, dict) and c.get("type") == "text":
                            chunks.append(c.get("text", ""))
                        # skip tool_result / image blocks — not user prose
                    text = "\n".join(chunks)
                text = text.strip()
                if _is_substantive(text):
                    turns.append(text)
    except OSError as exc:
        _log(f"transcript unreadable: {exc}")
    return turns


def _is_substantive(text: str) -> bool:
    if len(text) < MIN_LEN:
        return False
    # Skip slash commands, pasted command output, and harness/system noise.
    if text.startswith("/") or text.startswith("!"):
        return False
    if text.startswith("<") and text.endswith(">"):
        return False
    if "[Request interrupted" in text or "caveat:" in text.lower():
        return False
    return True


def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60] or "turn"


def _title_of(text: str) -> str:
    first = text.strip().splitlines()[0]
    return (first[:117] + "...") if len(first) > 120 else first


def main() -> None:
    if os.environ.get("DIARY_AUTO_EXTRACT") != "1":
        return
    if len(sys.argv) < 2:
        return
    transcript_path = sys.argv[1]
    slug = sys.argv[2] if len(sys.argv) > 2 else "misc"

    turns = _user_turns(transcript_path)
    if not turns:
        return
    turns = turns[:MAX_PER_SESSION]

    # Import here so the disabled path stays instant and dependency-free.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    try:
        from diary_server import memory_save_extracted
    except Exception as exc:  # noqa: BLE001
        _log(f"import failed: {exc}")
        return

    saved = 0
    for text in turns:
        body = text[:MAX_BODY]
        # Path is content-hash-based (no date) so the same user turn captured on
        # multiple days upserts the same node instead of creating duplicates.
        h = hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]
        path = f"/projects/{slug}/auto/{h}-{_slugify(_title_of(text))}"
        try:
            memory_save_extracted(path=path, title=_title_of(text), body=body, type="note")
            saved += 1
        except Exception as exc:  # noqa: BLE001
            _log(f"save failed for {path}: {exc}")
    _log(f"{slug}: captured {saved} user-turn memories (deterministic)")


if __name__ == "__main__":
    main()
