"""
Shared helper: resolve a cwd to a project slug via directory aliases stored
in the /projects/<slug> memory node's config.dirs list.

Resolution order:
  1. Exact match — config.dirs contains cwd exactly.
  2. Longest-prefix match — cwd starts with a registered dir
     (so sub-directories resolve to the owning project).
  3. Fallback — basename(cwd) lowercased + slugified (original heuristic).

Deliberately lean: one direct SQL query, no MCP import, no heavy deps.
Fails silent — any exception returns the basename fallback.
"""
import os
import re


def _database_url() -> str:
    return os.environ.get("DIARY_DATABASE_URL", "postgresql://localhost/diary_mcp")


def slug_from_cwd(cwd: str) -> str:
    """Return the project slug for cwd, using dir aliases when available."""
    cwd = cwd.rstrip("/")
    try:
        import psycopg
        from psycopg.rows import dict_row

        with psycopg.connect(_database_url(), row_factory=dict_row, connect_timeout=3) as conn:
            rows = conn.execute(
                """SELECT slug, config->'dirs' AS dirs
                   FROM memory_nodes
                   WHERE path LIKE '/projects/%'
                     AND path NOT LIKE '/projects/%/%'
                     AND deleted_at IS NULL
                     AND config ? 'dirs'""",
            ).fetchall()
    except Exception:
        return _basename_slug(cwd)

    best_slug: str | None = None
    best_prefix_len: int = -1

    for row in rows:
        slug = row["slug"]
        dirs = row["dirs"]
        if not isinstance(dirs, list):
            continue
        for d in dirs:
            if not isinstance(d, str):
                continue
            d = d.rstrip("/")
            if cwd == d:
                # Exact match — immediately return.
                return slug
            if cwd.startswith(d + "/") and len(d) > best_prefix_len:
                best_prefix_len = len(d)
                best_slug = slug

    if best_slug is not None:
        return best_slug

    return _basename_slug(cwd)


def _basename_slug(cwd: str) -> str:
    base = os.path.basename(cwd.rstrip("/"))
    return re.sub(r"[^a-z0-9]+", "-", base.lower()).strip("-") or "misc"
