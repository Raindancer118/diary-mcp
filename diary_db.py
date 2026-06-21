"""
PostgreSQL backend for diary-mcp.

Connection is controlled via the DIARY_DATABASE_URL environment variable
(default: postgresql://localhost/diary_mcp — uses the OS user via peer/trust auth).

For the remote sync target set DIARY_REMOTE_URL (e.g. to Dorn's Postgres).
"""
import logging
import os
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Generator

import psycopg
from psycopg.rows import dict_row

_log = logging.getLogger(__name__)

DATA_DIR_PATH = None  # kept for diary_http_receiver compatibility; actual DB is Postgres


def get_database_url() -> str:
    return os.environ.get("DIARY_DATABASE_URL", "postgresql://localhost/diary_mcp")


def get_remote_url() -> str | None:
    return os.environ.get("DIARY_REMOTE_URL")


@contextmanager
def get_db() -> Generator:
    conn = psycopg.connect(get_database_url(), row_factory=dict_row)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def get_remote_db() -> Generator:
    url = get_remote_url()
    if not url:
        raise ValueError("DIARY_REMOTE_URL not configured")
    conn = psycopg.connect(url, row_factory=dict_row)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_project_id(conn: psycopg.Connection, project_name: str) -> int | None:
    row = conn.execute("SELECT id FROM projects WHERE name = %s", (project_name,)).fetchone()
    return row["id"] if row else None


def apply_log_retention(conn: psycopg.Connection, project_id: int) -> None:
    row = conn.execute("SELECT config FROM projects WHERE id = %s", (project_id,)).fetchone()
    if not row or not row["config"]:
        return
    try:
        cfg = row["config"]  # JSONB → Python dict already
        retention_days = int(cfg.get("log_retention_days", 0))
        if retention_days > 0:
            cutoff = datetime.now() - timedelta(days=retention_days)
            conn.execute(
                "DELETE FROM logs WHERE project_id = %s AND timestamp < %s",
                (project_id, cutoff),
            )
    except (TypeError, ValueError, KeyError):
        _log.warning("Invalid config for project_id=%s, skipping log retention", project_id)


_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS projects (
        id          SERIAL PRIMARY KEY,
        name        TEXT UNIQUE NOT NULL,
        status      TEXT,
        archived    BOOLEAN DEFAULT FALSE,
        config      JSONB DEFAULT '{}',
        created_at  TIMESTAMPTZ DEFAULT now(),
        updated_at  TIMESTAMPTZ DEFAULT now()
    )""",
    """CREATE TABLE IF NOT EXISTS milestones (
        id           SERIAL PRIMARY KEY,
        project_id   INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        title        TEXT NOT NULL,
        completed    BOOLEAN DEFAULT FALSE,
        created_at   TIMESTAMPTZ DEFAULT now(),
        completed_at TIMESTAMPTZ
    )""",
    """CREATE TABLE IF NOT EXISTS tasks (
        id           SERIAL PRIMARY KEY,
        milestone_id INTEGER NOT NULL REFERENCES milestones(id) ON DELETE CASCADE,
        title        TEXT NOT NULL,
        completed    BOOLEAN DEFAULT FALSE,
        created_at   TIMESTAMPTZ DEFAULT now()
    )""",
    """CREATE TABLE IF NOT EXISTS logs (
        id         SERIAL PRIMARY KEY,
        project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        timestamp  TIMESTAMPTZ DEFAULT now(),
        author     TEXT,
        entry      TEXT,
        level      TEXT DEFAULT 'INFO',
        worker     TEXT DEFAULT 'System'
    )""",
    """CREATE TABLE IF NOT EXISTS reminders (
        id          SERIAL PRIMARY KEY,
        project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        target_date TEXT NOT NULL,
        note        TEXT,
        completed   BOOLEAN DEFAULT FALSE,
        created_at  TIMESTAMPTZ DEFAULT now()
    )""",
    """CREATE TABLE IF NOT EXISTS wiki_pages (
        id         SERIAL PRIMARY KEY,
        project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        title      TEXT NOT NULL,
        content    TEXT,
        created_at TIMESTAMPTZ DEFAULT now(),
        updated_at TIMESTAMPTZ DEFAULT now()
    )""",
    """CREATE TABLE IF NOT EXISTS errors_solutions (
        id           SERIAL PRIMARY KEY,
        project_id   INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        error_msg    TEXT,
        solution_msg TEXT,
        created_at   TIMESTAMPTZ DEFAULT now()
    )""",
    # Memory tree — UUID PKs enable bidirectional sync with Dorn
    # importance: 0.0–1.0 salience score (OpenMemory-inspired), used for ranking/decay
    # access_count / accessed_at: usage tracking for composite recall scoring
    """CREATE TABLE IF NOT EXISTS memory_nodes (
        id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        parent_id    UUID REFERENCES memory_nodes(id) ON DELETE CASCADE,
        path         TEXT NOT NULL UNIQUE,
        slug         TEXT NOT NULL,
        type         TEXT NOT NULL DEFAULT 'note',
        title        TEXT NOT NULL,
        body         TEXT,
        tags         TEXT[] DEFAULT '{}',
        importance   REAL DEFAULT 0.5 CHECK (importance >= 0 AND importance <= 1),
        access_count INTEGER DEFAULT 0,
        accessed_at  TIMESTAMPTZ,
        valid_until  TIMESTAMPTZ,
        created_at   TIMESTAMPTZ DEFAULT now(),
        updated_at   TIMESTAMPTZ DEFAULT now()
    )""",
    # Migration: add valid_until to existing tables (idempotent)
    "ALTER TABLE memory_nodes ADD COLUMN IF NOT EXISTS valid_until TIMESTAMPTZ",
    # Knowledge-graph: associative links between memory nodes
    # rel_type: related | supports | contradicts | requires | derived_from
    """CREATE TABLE IF NOT EXISTS memory_links (
        id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        from_id    UUID NOT NULL REFERENCES memory_nodes(id) ON DELETE CASCADE,
        to_id      UUID NOT NULL REFERENCES memory_nodes(id) ON DELETE CASCADE,
        rel_type   TEXT NOT NULL DEFAULT 'related',
        note       TEXT,
        created_at TIMESTAMPTZ DEFAULT now(),
        UNIQUE(from_id, to_id, rel_type)
    )""",
    "CREATE INDEX IF NOT EXISTS memory_nodes_parent_idx  ON memory_nodes(parent_id)",
    "CREATE INDEX IF NOT EXISTS memory_nodes_type_idx    ON memory_nodes(type)",
    "CREATE INDEX IF NOT EXISTS memory_nodes_path_idx    ON memory_nodes(path text_pattern_ops)",
    "CREATE INDEX IF NOT EXISTS memory_nodes_valid_idx   ON memory_nodes(valid_until) WHERE valid_until IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS memory_links_from_idx    ON memory_links(from_id)",
    "CREATE INDEX IF NOT EXISTS memory_links_to_idx      ON memory_links(to_id)",
    """CREATE INDEX IF NOT EXISTS memory_nodes_fts_idx ON memory_nodes
        USING GIN(to_tsvector('german', coalesce(title,'') || ' ' || coalesce(body,'')))""",
    """CREATE OR REPLACE FUNCTION update_updated_at()
       RETURNS TRIGGER LANGUAGE plpgsql AS $$
       BEGIN NEW.updated_at = now(); RETURN NEW; END; $$""",
    """CREATE OR REPLACE TRIGGER memory_nodes_updated_at
       BEFORE UPDATE ON memory_nodes
       FOR EACH ROW EXECUTE FUNCTION update_updated_at()""",
    # Backfill parent_id from path for any unlinked nodes (idempotent self-heal).
    # The parent path is the node path with its last "/segment" stripped; true roots
    # (e.g. /user) map to '' which matches nothing, so they correctly stay NULL.
    """UPDATE memory_nodes child SET parent_id = parent.id
       FROM memory_nodes parent
       WHERE child.parent_id IS NULL
         AND parent.path = regexp_replace(child.path, '/[^/]+$', '')
         AND regexp_replace(child.path, '/[^/]+$', '') <> ''""",
]

_SEED_CATEGORIES = [
    ("/user",       None,    "user",      "User"),
    ("/feedback",   None,    "feedback",  "Feedback"),
    ("/projects",   None,    "project",   "Projects"),
    ("/references", None,    "reference", "References"),
]


def init_db() -> None:
    with get_db() as conn:
        for stmt in _SCHEMA:
            conn.execute(stmt)
        for path, _, type_, title in _SEED_CATEGORIES:
            slug = path.strip("/")
            conn.execute(
                """INSERT INTO memory_nodes (path, slug, type, title)
                   VALUES (%s, %s, %s, %s) ON CONFLICT (path) DO NOTHING""",
                (path, slug, type_, title),
            )
