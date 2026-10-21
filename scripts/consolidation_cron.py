#!/usr/bin/env python3
"""
Nightly consolidation proposals (v0.29.0, systemd user timer
diary-consolidation.timer, 04:45 — after link inference at 04:30).

Only PROPOSES: duplicates, long-expired memories, relative dates, stale
memories land in consolidation_proposals. Nothing is applied here; Tom
decides in diary-web or via memory_consolidation_decide.
"""
import sys


def main() -> None:
    try:
        import diary_db
        import consolidation
        diary_db.init_db()  # the timer may run before any MCP server applied new migrations
    except Exception as exc:  # noqa: BLE001
        print(f"diary-consolidation-cron: import failed: {exc}", file=sys.stderr)
        sys.exit(1)
    try:
        counts = consolidation.propose()
    except Exception as exc:  # noqa: BLE001
        print(f"diary-consolidation-cron: propose failed: {exc}", file=sys.stderr)
        sys.exit(1)
    print("diary-consolidation: " + ", ".join(f"{k} +{v}" for k, v in counts.items()))


if __name__ == "__main__":
    main()
