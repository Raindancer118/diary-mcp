#!/usr/bin/env python3
"""
Periodic batch runner for diary_link_sync() (v0.16.0).

diary_link_sync(alias, tag) itself stays a manual, explicit tool (see
diary_link.py) — this script is the opt-in automatic layer on top: it reads
every diary_links row's `sync_tags` (set via diary_link_set_sync_tags(),
default empty = link not auto-synced) and calls the plain diary_link_sync()
function once per (link, tag) pair. A link with no sync_tags configured is
never touched here.

Same pattern as scripts/link_inference_cron.py/memory_backup_export.py: not a
Claude Code hook (no stdin JSON, no hookSpecificOutput), a plain script meant
for a systemd user timer, invoked with the diary-mcp tool's own installed
Python so `import diary_link`/`import diary_db` resolve (uv tool install .,
see pyproject.toml's py-modules).

One (link, tag) failure (e.g. relay unreachable, peer's identity revoked)
is logged and skipped rather than aborting the whole run — same reasoning as
_sanitize_incoming_path skipping one bad message instead of failing the sync.

Suggested systemd user units (see README.md "Automatic linking"):
  ~/.config/systemd/user/diary-link-sync.service
  ~/.config/systemd/user/diary-link-sync.timer
"""
import sys


def main() -> None:
    try:
        import diary_db
        from diary_link import diary_link_sync
    except Exception as exc:  # noqa: BLE001
        print(f"diary-link-sync-cron: import failed: {exc}", file=sys.stderr)
        sys.exit(1)

    with diary_db.get_db() as conn:
        links = conn.execute(
            "SELECT peer_alias, sync_tags FROM diary_links WHERE sync_tags <> '{}'"
        ).fetchall()

    if not links:
        print("diary-link-sync-cron: keine Links mit Auto-Sync-Tags konfiguriert.")
        return

    for link in links:
        for tag in link["sync_tags"]:
            try:
                result = diary_link_sync(link["peer_alias"], tag)
                print(f"[{link['peer_alias']}/{tag}] {result}")
            except Exception as exc:  # noqa: BLE001
                print(f"[{link['peer_alias']}/{tag}] FEHLER: {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()
