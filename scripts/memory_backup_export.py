#!/usr/bin/env python3
"""
Periodic Markdown export of the curated memory tree into a separate, private
git repo (v0.14.0) — a human-readable, diffable point-in-time backup
independent of Postgres. Postgres backups protect against server loss; this
protects against a bad write (e.g. a wrong memory_merge/memory_delete call)
by giving every export run its own git commit to roll back to.

NOT a Claude Code hook — a plain script for a scheduler (systemd user timer,
see README.md "Backup export"). Deliberately lean: direct psycopg, no MCP
import (this only reads memory_nodes, no need for diary_bootstrap/FastMCP).

Behaviour: mirrors every curated, non-tombstoned node as one Markdown file
under <BACKUP_REPO_DIR>/memory-tree/<path>.md (frontmatter: title, type, tags,
importance, valid_until, pin_triggers, updated_at; body below), wiping and
rewriting the whole memory-tree/ directory each run so deletions/renames show
up as real git diffs instead of leaving orphaned files behind. Commits and
pushes only when something actually changed.

Env:
  DIARY_DATABASE_URL  (default postgresql://localhost/diary_mcp)
  BACKUP_REPO_DIR      (default ~/Projekte/SEProjects/diary-mcp-backup)
"""
import os
import subprocess
import sys
from pathlib import Path

import psycopg
from psycopg.rows import dict_row


def _database_url() -> str:
    return os.environ.get("DIARY_DATABASE_URL", "postgresql://localhost/diary_mcp")


def _repo_dir() -> Path:
    return Path(os.environ.get(
        "BACKUP_REPO_DIR",
        str(Path.home() / "Projekte" / "SEProjects" / "diary-mcp-backup"),
    ))


def _safe_file_path(tree_dir: Path, node_path: str) -> Path:
    """Map a memory path ('/user/foo') to a filesystem path under tree_dir,
    rejecting anything that could escape it (defense in depth — paths come
    from our own DB, but a node path is still user-influenced input)."""
    rel = node_path.strip("/")
    target = (tree_dir / f"{rel}.md").resolve()
    if not str(target).startswith(str(tree_dir.resolve()) + os.sep):
        raise ValueError(f"unsafe memory path, refusing to export: {node_path!r}")
    return target


def _frontmatter(node: dict) -> str:
    tags = ", ".join(node["tags"] or [])
    pins = ", ".join(node["pin_triggers"] or [])
    lines = [
        "---",
        f"path: {node['path']}",
        f"title: {node['title']}",
        f"type: {node['type']}",
        f"importance: {node['importance']}",
        f"tags: [{tags}]",
        f"pin_triggers: [{pins}]",
        f"valid_until: {node['valid_until'] or ''}",
        f"updated_at: {node['updated_at']}",
        "---",
        "",
    ]
    return "\n".join(lines)


def export_tree(conn, tree_dir: Path) -> int:
    nodes = conn.execute(
        "SELECT path, title, type, tags, importance, valid_until, pin_triggers, updated_at, body "
        "FROM memory_nodes WHERE deleted_at IS NULL AND origin = 'curated' ORDER BY path"
    ).fetchall()

    if tree_dir.exists():
        for f in tree_dir.rglob("*.md"):
            f.unlink()
    tree_dir.mkdir(parents=True, exist_ok=True)

    for node in nodes:
        target = _safe_file_path(tree_dir, node["path"])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_frontmatter(node) + (node["body"] or "") + "\n", encoding="utf-8")

    # Prune now-empty directories left over from a node that no longer exists.
    for d in sorted(tree_dir.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if d.is_dir() and not any(d.iterdir()):
            d.rmdir()

    return len(nodes)


def _run(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)


def commit_and_push(repo_dir: Path, node_count: int) -> str:
    status = _run(["git", "status", "--porcelain"], repo_dir)
    if status.returncode != 0:
        return f"git status fehlgeschlagen: {status.stderr.strip()}"
    if not status.stdout.strip():
        return f"Export lief ({node_count} Nodes), keine Änderungen seit letztem Lauf."

    _run(["git", "add", "-A"], repo_dir)
    commit = _run(
        ["git", "commit", "-m", f"Backup-Export: {node_count} kuratierte Memories"],
        repo_dir,
    )
    if commit.returncode != 0:
        return f"git commit fehlgeschlagen: {commit.stderr.strip()}"
    push = _run(["git", "push"], repo_dir)
    if push.returncode != 0:
        return f"Committed, aber git push fehlgeschlagen: {push.stderr.strip()}"
    return f"Export committed + gepusht ({node_count} Nodes)."


def main() -> None:
    repo_dir = _repo_dir()
    if not (repo_dir / ".git").is_dir():
        print(f"memory-backup-export: '{repo_dir}' ist kein Git-Repo — abgebrochen.", file=sys.stderr)
        sys.exit(1)

    tree_dir = repo_dir / "memory-tree"
    try:
        with psycopg.connect(_database_url(), row_factory=dict_row, connect_timeout=5) as conn:
            node_count = export_tree(conn, tree_dir)
    except Exception as exc:  # noqa: BLE001
        print(f"memory-backup-export: DB-Zugriff fehlgeschlagen: {exc}", file=sys.stderr)
        sys.exit(1)

    print(commit_and_push(repo_dir, node_count))


if __name__ == "__main__":
    main()
