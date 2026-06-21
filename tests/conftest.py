"""
Test fixtures for diary-mcp.

Uses a dedicated test database (diary_mcp_pytest) isolated from the real diary_mcp.
A second database (diary_mcp_pytest_remote) is used for sync round-trip tests.

The fixture creates both databases fresh at session start via a maintenance
connection to the "postgres" database, runs init_db(), and drops them at teardown.
"""
from __future__ import annotations

import importlib
import os
import sys

import psycopg
import pytest

# Ensure the project root is on sys.path so all diary_* modules are importable
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)


_MAINTENANCE_DSN = os.environ.get(
    "DIARY_MAINTENANCE_URL",
    "postgresql://localhost/postgres",
)

TEST_DB = "diary_mcp_pytest"
REMOTE_DB = "diary_mcp_pytest_remote"


def _drop_db(db_name: str) -> None:
    conn = psycopg.connect(_MAINTENANCE_DSN, autocommit=True)
    try:
        conn.execute(f"DROP DATABASE IF EXISTS {db_name} WITH (FORCE)")
    finally:
        conn.close()


def _create_db(db_name: str) -> None:
    conn = psycopg.connect(_MAINTENANCE_DSN, autocommit=True)
    try:
        conn.execute(f"CREATE DATABASE {db_name}")
    finally:
        conn.close()


def _build_test_url(base_url: str, db_name: str) -> str:
    """Replace the database name component of a postgres URL."""
    # Handle both "postgresql://host/db" and "postgresql://user:pw@host/db"
    idx = base_url.rfind("/")
    return base_url[:idx + 1] + db_name


@pytest.fixture(scope="session", autouse=True)
def test_databases():
    """Session-scoped fixture: create both test databases, init schema, tear down after."""
    import diary_db

    # Determine base URL (without db name component)
    base_url = os.environ.get("DIARY_DATABASE_URL", "postgresql://localhost/diary_mcp")

    test_url = _build_test_url(base_url, TEST_DB)
    remote_url = _build_test_url(base_url, REMOTE_DB)

    # --- create databases ---
    _drop_db(TEST_DB)
    _create_db(TEST_DB)
    _drop_db(REMOTE_DB)
    _create_db(REMOTE_DB)

    # Point diary_db at the test database for this session.
    os.environ["DIARY_DATABASE_URL"] = test_url
    os.environ["DIARY_REMOTE_URL"] = remote_url
    # SSH tunnel must NOT be used for the local-only test remote
    os.environ.pop("DIARY_REMOTE_SSH_HOST", None)

    # diary_db reads the env var via get_database_url() — reload so the cached
    # value (if any) is refreshed; same for diary_server.
    importlib.reload(diary_db)
    import diary_embed
    importlib.reload(diary_embed)
    import diary_server
    importlib.reload(diary_server)

    # Init schema on local test DB
    diary_db.init_db()

    # Init schema on remote test DB (swap env var temporarily)
    os.environ["DIARY_DATABASE_URL"] = remote_url
    importlib.reload(diary_db)
    diary_db.init_db()

    # Switch back to local
    os.environ["DIARY_DATABASE_URL"] = test_url
    importlib.reload(diary_db)
    importlib.reload(diary_server)

    # Clear the pgvector cache so each DB is detected fresh
    diary_server._pgvector_cache.clear()

    yield {"local_url": test_url, "remote_url": remote_url}

    # --- teardown ---
    os.environ.pop("DIARY_DATABASE_URL", None)
    os.environ.pop("DIARY_REMOTE_URL", None)
    _drop_db(TEST_DB)
    _drop_db(REMOTE_DB)


@pytest.fixture(autouse=True)
def clean_memory_nodes(test_databases):
    """Per-test fixture: truncate memory_nodes (and links) so tests are independent."""
    import diary_db
    import diary_server

    # Make sure we're pointing at the local test DB
    os.environ["DIARY_DATABASE_URL"] = test_databases["local_url"]
    importlib.reload(diary_db)

    # Clear memory_nodes; cascade wipes memory_links
    with diary_db.get_db() as conn:
        conn.execute("DELETE FROM memory_nodes")
        # Reset local-only sync bookkeeping so each test starts from a clean
        # "never synced" state (otherwise a prior test's last_sync would leak
        # and trip false conflict detection).
        conn.execute("DELETE FROM diary_meta")

    # Re-seed categories (init_db seeds them, but we deleted everything)
    diary_db.init_db()

    # Clear pgvector cache so it re-detects per connection
    diary_server._pgvector_cache.clear()

    yield
