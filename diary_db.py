"""
PostgreSQL backend for diary-mcp.

Connection is controlled via the DIARY_DATABASE_URL environment variable
(default: postgresql://localhost/diary_mcp — uses the OS user via peer/trust auth).

For the remote sync target set DIARY_REMOTE_URL (e.g. to Dorn's Postgres).
"""
import logging
import os
import socket
import subprocess
import time
import urllib.parse
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Generator

import psycopg
from psycopg.rows import dict_row

from diary_embed import EMBED_DIM

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


def get_remote_ssh_host() -> str | None:
    """If set, memory_sync opens an SSH tunnel to this host to reach the remote DB."""
    return os.environ.get("DIARY_REMOTE_SSH_HOST")


def _rebuild_netloc(parsed: urllib.parse.ParseResult, host: str, port: int) -> str:
    auth = ""
    if parsed.username:
        auth = parsed.username
        if parsed.password:
            auth += f":{parsed.password}"
        auth += "@"
    return f"{auth}{host}:{port}"


@contextmanager
def remote_db_url() -> Generator[str, None, None]:
    """Yield a connectable remote DB URL.

    If DIARY_REMOTE_SSH_HOST is set, opens an ephemeral SSH tunnel to the host's
    DB endpoint (parsed from DIARY_REMOTE_URL) on a free local port, yields the
    rewritten URL, and tears the tunnel down on exit. Otherwise yields
    DIARY_REMOTE_URL unchanged.
    """
    url = get_remote_url()
    if not url:
        raise ValueError("DIARY_REMOTE_URL not configured")

    ssh_host = get_remote_ssh_host()
    if not ssh_host:
        yield url
        return

    parsed = urllib.parse.urlparse(url)
    target_host = parsed.hostname or "127.0.0.1"
    target_port = parsed.port or 5432

    # Reserve a free local port for the tunnel.
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    local_port = s.getsockname()[1]
    s.close()

    proc = subprocess.Popen(
        ["ssh", "-N",
         "-o", "ExitOnForwardFailure=yes",
         "-o", "BatchMode=yes",
         "-o", "ServerAliveInterval=5",
         "-L", f"{local_port}:{target_host}:{target_port}", ssh_host],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    try:
        deadline = time.time() + 15
        while time.time() < deadline:
            if proc.poll() is not None:
                err = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
                raise RuntimeError(f"SSH tunnel to {ssh_host} exited early: {err.strip()}")
            try:
                with socket.create_connection(("127.0.0.1", local_port), timeout=1):
                    break
            except OSError:
                time.sleep(0.3)
        else:
            raise RuntimeError(f"SSH tunnel to {ssh_host} did not come up within 15s")

        tunneled = parsed._replace(netloc=_rebuild_netloc(parsed, "127.0.0.1", local_port))
        yield urllib.parse.urlunparse(tunneled)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


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
        auto_inject  BOOLEAN DEFAULT FALSE,
        origin       TEXT NOT NULL DEFAULT 'curated',
        created_at   TIMESTAMPTZ DEFAULT now(),
        updated_at   TIMESTAMPTZ DEFAULT now()
    )""",
    # Migrations: add columns to existing tables (idempotent)
    "ALTER TABLE memory_nodes ADD COLUMN IF NOT EXISTS valid_until TIMESTAMPTZ",
    "ALTER TABLE memory_nodes ADD COLUMN IF NOT EXISTS auto_inject BOOLEAN DEFAULT FALSE",
    # reinject_on_compact: re-inject this memory into context after a compaction
    # (for setups that compact often). Independent of auto_inject (session start).
    "ALTER TABLE memory_nodes ADD COLUMN IF NOT EXISTS reinject_on_compact BOOLEAN DEFAULT FALSE",
    # origin: 'curated' = von Claude bewusst gespeichert (Default-Suche).
    #         'extracted' = automatisch aus Chat-Transkripten geerntet (nur auf Anfrage).
    "ALTER TABLE memory_nodes ADD COLUMN IF NOT EXISTS origin TEXT NOT NULL DEFAULT 'curated'",
    # Semantic search: embedding vector stored as REAL[] (cosine computed in Python,
    # no pgvector needed at this scale). NULL until embedded.
    "ALTER TABLE memory_nodes ADD COLUMN IF NOT EXISTS embedding REAL[]",
    # Per-node settings (e.g. per-project {"auto_extract": true}). JSONB for extensibility.
    "ALTER TABLE memory_nodes ADD COLUMN IF NOT EXISTS config JSONB DEFAULT '{}'",
    "CREATE INDEX IF NOT EXISTS memory_nodes_autoinject_idx ON memory_nodes(auto_inject) WHERE auto_inject",
    "CREATE INDEX IF NOT EXISTS memory_nodes_origin_idx ON memory_nodes(origin)",
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
    # NOTE: no updated_at trigger — it would fire on the parent_id backfill below and
    # bump timestamps, corrupting the last-write-wins sync (endless ping-pong / stale
    # overwrites). All app-level UPDATEs set updated_at=now() explicitly instead.
    "DROP TRIGGER IF EXISTS memory_nodes_updated_at ON memory_nodes",
]

# Backfill parent_id from path for any unlinked nodes (idempotent self-heal).
# The parent path is the node path with its last "/segment" stripped; true roots
# (e.g. /user) map to '' which matches nothing, so they correctly stay NULL.
# Reused by init_db and after sync upserts (which insert by path, not parent_id).
_BACKFILL_PARENT_SQL = """UPDATE memory_nodes child SET parent_id = parent.id
       FROM memory_nodes parent
       WHERE child.parent_id IS NULL
         AND parent.path = regexp_replace(child.path, '/[^/]+$', '')
         AND regexp_replace(child.path, '/[^/]+$', '') <> ''"""

_SCHEMA.append(_BACKFILL_PARENT_SQL)

_SEED_CATEGORIES = [
    ("/user",       None,    "user",      "User"),
    ("/feedback",   None,    "feedback",  "Feedback"),
    ("/projects",   None,    "project",   "Projects"),
    ("/references", None,    "reference", "References"),
]


def has_pgvector(conn) -> bool:
    """True if the pgvector extension is installed in the connected database."""
    row = conn.execute("SELECT 1 FROM pg_extension WHERE extname = 'vector'").fetchone()
    return row is not None


def _try_enable_pgvector(conn) -> bool:
    """Create the pgvector extension and the indexed vector column if possible.

    Returns True if pgvector is available afterwards. Degrades gracefully: if the
    extension can't be created (not installed / no privilege), embeddings still
    work via the REAL[] column + numpy fallback.
    """
    try:
        # Savepoint: if CREATE EXTENSION fails (not installed / no privilege), only
        # this is rolled back — NOT the surrounding init transaction (which holds the
        # embedding column and all other schema). A bare conn.rollback() here would
        # silently undo the whole init.
        with conn.transaction():
            conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
    except Exception as exc:  # noqa: BLE001
        _log.info("pgvector not enabled (%s); using REAL[] + numpy fallback", exc)
        return False
    if not has_pgvector(conn):
        return False
    # Indexed vector column derived from the canonical REAL[] `embedding`.
    conn.execute(f"ALTER TABLE memory_nodes ADD COLUMN IF NOT EXISTS embedding_v vector({EMBED_DIM})")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS memory_nodes_embv_idx "
        "ON memory_nodes USING hnsw (embedding_v vector_cosine_ops)"
    )
    return True


def init_db() -> None:
    with get_db() as conn:
        for stmt in _SCHEMA:
            conn.execute(stmt)
        _try_enable_pgvector(conn)
        for path, _, type_, title in _SEED_CATEGORIES:
            slug = path.strip("/")
            conn.execute(
                """INSERT INTO memory_nodes (path, slug, type, title)
                   VALUES (%s, %s, %s, %s) ON CONFLICT (path) DO NOTHING""",
                (path, slug, type_, title),
            )
