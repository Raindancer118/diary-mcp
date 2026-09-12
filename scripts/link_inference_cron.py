#!/usr/bin/env python3
"""
Periodic batch runner for memory_infer_links() (v0.13.0).

Write-time auto-linking (memory_upsert's _auto_link_new_node, see
memory_service.py) already catches the common case — a freshly saved/edited
curated node gets compared against the existing tree immediately. This cron
job covers what that can't: pairs whose *relative* similarity only became
relevant later (e.g. one side's embedding was recomputed after a model
change/memory_reembed_all, or AUTO_LINK_MAX_NEW capped how many links a single
upsert could create and left some pairs unlinked). Run this occasionally
(daily is plenty) over the whole curated tree instead of relying only on the
per-upsert pass.

Unlike the SessionStart/UserPromptSubmit hooks in this directory, this is NOT
a Claude Code hook (no stdin JSON payload, no hookSpecificOutput) — it's a
plain script meant for a systemd user timer or cron entry, invoked with the
diary-mcp tool's own installed Python so `import graph_admin` resolves
(installed via `uv tool install .`, see pyproject.toml's py-modules).

Deliberately reuses graph_admin.memory_infer_links() itself rather than
reimplementing the pairwise-similarity logic — same tested code path as the
manual admin tool and the write-time auto-linker, just invoked on a schedule
across the whole tree instead of one new node.

Threshold/cap are separately configurable from AUTO_LINK_THRESHOLD/
AUTO_LINK_MAX_NEW (memory_service.py) — this runs far less often, so a lower
threshold and a much higher cap are reasonable here without turning into a
per-save flood.

Env:
  DIARY_LINK_INFERENCE_THRESHOLD (default 0.82, same default as memory_infer_links)
  DIARY_LINK_INFERENCE_MAX_NEW   (default 50)

Suggested systemd user units (see README.md "Automatic linking"):
  ~/.config/systemd/user/diary-link-inference.service
  ~/.config/systemd/user/diary-link-inference.timer
"""
import os
import sys


def main() -> None:
    threshold = float(os.environ.get("DIARY_LINK_INFERENCE_THRESHOLD", "0.82"))
    max_new = int(os.environ.get("DIARY_LINK_INFERENCE_MAX_NEW", "50"))

    try:
        import graph_admin
    except Exception as exc:  # noqa: BLE001
        print(f"diary-link-inference-cron: import failed: {exc}", file=sys.stderr)
        sys.exit(1)

    try:
        result = graph_admin.memory_infer_links(threshold=threshold, max_new=max_new)
    except Exception as exc:  # noqa: BLE001
        print(f"diary-link-inference-cron: run failed: {exc}", file=sys.stderr)
        sys.exit(1)

    print(result)


if __name__ == "__main__":
    main()
