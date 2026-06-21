#!/usr/bin/env python3
"""
One-time import of all existing file-based memory files into the Postgres memory tree.

Usage:
    python -m scripts.import_memories [--dry-run] [--verbose]

Maps file paths → memory tree paths:
  ~/.claude/projects/-home-tom/memory/*.md          → /type/name
  ~/.claude/projects/-home-tom-Proj-Foo/memory/*.md → /projects/foo/name
  ~/.claude/memory/*.md                              → /type/name
"""
import argparse
import re
import sys
from pathlib import Path

# Allow running from the project root
sys.path.insert(0, str(Path(__file__).parent.parent))

import psycopg
from psycopg.rows import dict_row

from diary_db import get_database_url, init_db


def parse_frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    fm_text = text[3:end].strip()
    body = text[end + 4:].strip()
    meta: dict = {}
    for line in fm_text.splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            k = key.strip()
            v = value.strip()
            if k and k not in ("metadata",):
                meta[k] = v
    return meta, body


_TYPE_MAP = {
    "user":      "user",
    "feedback":  "feedback",
    "project":   "project",
    "reference": "reference",
    "note":      "note",
}

_ROOT_FOR_TYPE = {
    "user":      "/user",
    "feedback":  "/feedback",
    "project":   "/projects",
    "reference": "/references",
    "note":      "/references",  # notes → references as fallback
}


def project_slug_from_dir(dir_name: str) -> str | None:
    """Convert a project directory name to a project slug, or None for global."""
    suffix = dir_name.removeprefix("-home-tom")
    if suffix.startswith("-"):
        suffix = suffix[1:]
    suffix = suffix.strip("-")
    if not suffix:
        return None  # global context
    # Lowercase, replace consecutive hyphens with single hyphen
    slug = re.sub(r"-+", "-", suffix.lower()).strip("-")
    # Remove common path prefixes to keep it shorter
    for prefix in ("projekte-se-projects-", "projekte-", "antigravity-", "dokumente-"):
        if slug.startswith(prefix):
            slug = slug[len(prefix):]
            break
    return slug or None


def memory_path(dir_name: str, node_type: str, name: str) -> str:
    slug = project_slug_from_dir(dir_name)
    if slug is None:
        root = _ROOT_FOR_TYPE.get(node_type, "/notes")
        return f"{root}/{name}"
    else:
        return f"/projects/{slug}/{name}"


def ensure_parent(conn, path: str) -> None:
    parts = path.strip("/").split("/")
    for depth in range(1, len(parts)):
        parent_path = "/" + "/".join(parts[:depth])
        slug = parts[depth - 1]
        conn.execute(
            """INSERT INTO memory_nodes (path, slug, type, title)
               VALUES (%s, %s, 'category', %s)
               ON CONFLICT (path) DO NOTHING""",
            (parent_path, slug, slug.replace("-", " ").title()),
        )


def import_file(conn, path: Path, dir_name: str, dry_run: bool, verbose: bool) -> str | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return f"SKIP (unreadable): {exc}"

    if not text.strip():
        return "SKIP (empty)"

    meta, body = parse_frontmatter(text)
    if not body and not meta:
        return "SKIP (no content)"

    raw_type = meta.get("type") or meta.get("metadata.type") or "note"
    node_type = _TYPE_MAP.get(raw_type.lower(), "note")
    title = meta.get("name") or meta.get("title") or path.stem.replace("_", " ").replace("-", " ").title()
    file_slug = path.stem  # e.g. "feedback_commits"

    mem_path = memory_path(dir_name, node_type, file_slug)
    importance_str = meta.get("importance", "0.5")
    try:
        importance = float(importance_str)
    except ValueError:
        importance = 0.5

    if dry_run:
        print(f"  [DRY RUN] {path.name} → {mem_path}")
        return None

    ensure_parent(conn, mem_path)
    slug = mem_path.strip("/").split("/")[-1]
    conn.execute(
        """INSERT INTO memory_nodes (path, slug, type, title, body, importance)
           VALUES (%s, %s, %s, %s, %s, %s)
           ON CONFLICT (path) DO UPDATE SET
               title = EXCLUDED.title,
               body = EXCLUDED.body,
               importance = EXCLUDED.importance,
               updated_at = now()""",
        (mem_path, slug, node_type, title, body, importance),
    )
    if verbose:
        print(f"  ✓ {path.name} → {mem_path}")
    return None


def find_memory_files() -> list[tuple[Path, str]]:
    """Return list of (file_path, project_dir_name) for all memory files."""
    results: list[tuple[Path, str]] = []
    claude_dir = Path.home() / ".claude"

    # Global memory dir
    global_mem = claude_dir / "memory"
    if global_mem.exists():
        for f in sorted(global_mem.glob("*.md")):
            if f.name != "MEMORY.md":
                results.append((f, "-home-tom"))

    # Per-project memory dirs
    projects_dir = claude_dir / "projects"
    if projects_dir.exists():
        for proj_dir in sorted(projects_dir.iterdir()):
            if not proj_dir.is_dir():
                continue
            mem_dir = proj_dir / "memory"
            if not mem_dir.exists():
                continue
            for f in sorted(mem_dir.glob("*.md")):
                if f.name != "MEMORY.md":
                    results.append((f, proj_dir.name))

    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Import file-based memories into Postgres memory tree")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be imported without writing")
    parser.add_argument("--verbose", "-v", action="store_true", help="Print each imported file")
    args = parser.parse_args()

    if not args.dry_run:
        init_db()

    files = find_memory_files()
    print(f"Found {len(files)} memory files to import.")

    imported = 0
    skipped = 0

    if args.dry_run:
        for f, dir_name in files:
            meta, body = parse_frontmatter(f.read_text(encoding="utf-8") if f.exists() else "")
            raw_type = meta.get("type") or "note"
            node_type = _TYPE_MAP.get(raw_type.lower(), "note")
            slug = project_slug_from_dir(dir_name)
            mem_path = memory_path(dir_name, node_type, f.stem)
            print(f"  {f.name:40s} → {mem_path}")
        print("\n(dry run — nothing written)")
        return

    with psycopg.connect(get_database_url(), row_factory=dict_row) as conn:
        for f, dir_name in files:
            result = import_file(conn, f, dir_name, dry_run=False, verbose=args.verbose)
            if result and result.startswith("SKIP"):
                skipped += 1
                if args.verbose:
                    print(f"  ! {f.name}: {result}")
            else:
                imported += 1
        conn.commit()

    print(f"\nImport complete: {imported} imported, {skipped} skipped.")
    print("Run 'memory_tree /' via the MCP to verify the result.")


if __name__ == "__main__":
    main()
