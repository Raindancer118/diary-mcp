#!/usr/bin/env python3
"""
Nightly confidence-scored linking (v0.24.0, systemd user timer
diary-link-inference.timer, 04:30).

Refits the link model on the deliberate links, links high-confidence pairs,
queues medium-confidence pairs in link_suggestions and re-scores existing
inferred links. Write-time linking (memory_upsert) already handles each
fresh save; this pass catches what it can't — e.g. a mention written before
its target existed, or pairs capped by AUTO_LINK_MAX_NEW.

Not a Claude Code hook: a plain script run with the tool's installed Python
so `import link_inference` resolves. The review list is never processed here.
"""
import sys


def main() -> None:
    try:
        import diary_db
        import link_inference
        diary_db.init_db()  # the timer may run before any MCP server applied new migrations
    except Exception as exc:  # noqa: BLE001
        print(f"diary-link-inference-cron: import failed: {exc}", file=sys.stderr)
        sys.exit(1)
    try:
        report = link_inference.run()
    except Exception as exc:  # noqa: BLE001
        print(f"diary-link-inference-cron: run failed: {exc}", file=sys.stderr)
        sys.exit(1)
    print(link_inference.format_report(report))


if __name__ == "__main__":
    main()
