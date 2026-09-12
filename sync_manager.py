"""
Bidirectional sync with the remote Postgres instance (Dorn): the memory tree
(memory_sync) and the classic diary tables (memory_sync_diary), plus tombstone
purging for both. Last-write-wins via updated_at, soft-delete tombstones
propagate instead of resurrecting on the next sync.

Split out of the former diary_server.py monolith (v0.10.0).
"""
import json
import logging
from datetime import datetime

import psycopg
from psycopg.rows import dict_row

import diary_db
import diary_embed
from diary_bootstrap import mcp
from memory_service import _pgvector_ready

_log = logging.getLogger(__name__)

_SYNC_COLS = ("id::text, path, slug, type, title, body, tags, importance, "
              "valid_until, pin_triggers, origin, embedding, config, "
              "deleted_at, created_at, updated_at")
_SYNC_INSERT = """INSERT INTO memory_nodes
       (path, slug, type, title, body, tags, importance, valid_until, pin_triggers,
        origin, embedding, config, deleted_at, created_at, updated_at)
       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
       ON CONFLICT (path) DO UPDATE SET
           type=EXCLUDED.type, title=EXCLUDED.title, body=EXCLUDED.body,
           tags=EXCLUDED.tags, importance=EXCLUDED.importance,
           valid_until=EXCLUDED.valid_until, pin_triggers=EXCLUDED.pin_triggers,
           origin=EXCLUDED.origin, embedding=EXCLUDED.embedding,
           config=EXCLUDED.config, deleted_at=EXCLUDED.deleted_at,
           updated_at=EXCLUDED.updated_at"""


def _sync_row(n: dict) -> tuple:
    return (n["path"], n["slug"], n["type"], n["title"], n["body"], n["tags"],
            n["importance"], n["valid_until"], n["pin_triggers"] or [],
            n["origin"], n["embedding"],
            json.dumps(n["config"]) if n.get("config") is not None else "{}",
            n["deleted_at"], n["created_at"], n["updated_at"])


def _last_sync_key() -> str:
    """diary_meta key under which the last successful sync timestamp is stored.

    Scoped to the configured remote URL so different remotes track independently.
    """
    return f"last_sync:{diary_db.get_remote_url() or ''}"


def _read_last_sync(conn) -> "datetime | None":
    """Return the stored last-sync timestamp for the current remote, or None (first run)."""
    row = conn.execute(
        "SELECT value FROM diary_meta WHERE key = %s", (_last_sync_key(),)
    ).fetchone()
    if not row or not row["value"]:
        return None
    try:
        return datetime.fromisoformat(row["value"])
    except (ValueError, TypeError):
        return None


def _write_last_sync(conn, ts: datetime) -> None:
    """Persist the last successful sync timestamp for the current remote."""
    conn.execute(
        """INSERT INTO diary_meta (key, value, updated_at)
           VALUES (%s, %s, now())
           ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()""",
        (_last_sync_key(), ts.isoformat()),
    )


def _content_differs(a: dict, b: dict) -> bool:
    """True if two nodes diverge in any user-meaningful field (not just timestamps)."""
    fields = ("title", "body", "tags", "pin_triggers", "deleted_at")
    for f in fields:
        av, bv = a.get(f), b.get(f)
        # Normalise array-ish fields so [] and None compare equal.
        if f in ("tags", "pin_triggers"):
            av = list(av or [])
            bv = list(bv or [])
        if av != bv:
            return True
    return False


def _detect_conflicts(local_by_path: dict, remote_by_path: dict,
                      last_sync: "datetime | None") -> list[dict]:
    """Find paths edited on BOTH sides since the last sync with diverging content.

    Returns a list of {path, winner} dicts (winner = 'local'/'remote', the side whose
    updated_at is newer and therefore wins under last-write-wins). Empty on first run
    (last_sync is None) or when nothing diverged.
    """
    if last_sync is None:
        return []
    conflicts = []
    for path, local_n in local_by_path.items():
        remote_n = remote_by_path.get(path)
        if remote_n is None:
            continue
        l_upd, r_upd = local_n["updated_at"], remote_n["updated_at"]
        # Both sides must have changed since the last successful sync.
        if l_upd > last_sync and r_upd > last_sync and _content_differs(local_n, remote_n):
            winner = "local" if l_upd >= r_upd else "remote"
            conflicts.append({"path": path, "winner": winner})
    return conflicts


def _format_conflicts(conflicts: list[dict], limit: int = 8) -> str:
    """Render the conflict summary line for memory_sync's return string."""
    if not conflicts:
        return ""
    parts = []
    for c in conflicts[:limit]:
        parts.append(f"{c['path']} ({c['winner']} gewann)")
    more = len(conflicts) - limit
    if more > 0:
        parts.append(f"… +{more} weitere")
    return (f" ⚠ {len(conflicts)} Konflikt(e) (beidseitig geändert, "
            f"neuere Version gewann): " + ", ".join(parts))


# --- memory_links sync -------------------------------------------------------
# from_id/to_id are local UUIDs, independent per DB (memory_nodes itself is matched
# by path, not id — see _SYNC_INSERT above), so links can't be merged via ON CONFLICT
# on their own UNIQUE(from_id, to_id, rel_type). Instead they're matched via the
# (from_path, to_path, rel_type) triple, resolved through memory_nodes.path on each
# side. Requires memory_nodes to already be synced (this runs after node push/pull).
_LINKS_SELECT = """SELECT ml.rel_type, ml.note, ml.link_origin, ml.updated_at,
       fn.path AS from_path, tn.path AS to_path
       FROM memory_links ml
       JOIN memory_nodes fn ON ml.from_id = fn.id
       JOIN memory_nodes tn ON ml.to_id = tn.id"""

_LINKS_UPSERT = """INSERT INTO memory_links (from_id, to_id, rel_type, note, link_origin, updated_at)
       VALUES (%s, %s, %s, %s, %s, %s)
       ON CONFLICT (from_id, to_id, rel_type) DO UPDATE SET
           note = EXCLUDED.note, link_origin = EXCLUDED.link_origin, updated_at = EXCLUDED.updated_at"""


def _link_key(link: dict) -> tuple:
    return (link["from_path"], link["to_path"], link["rel_type"])


def _sync_link(conn, link: dict) -> bool:
    """Resolve from_path/to_path to this connection's local node ids and upsert the
    link. Returns False (no-op) if either node isn't present on this side yet."""
    ids = conn.execute(
        "SELECT (SELECT id FROM memory_nodes WHERE path = %s AND deleted_at IS NULL) AS from_id, "
        "(SELECT id FROM memory_nodes WHERE path = %s AND deleted_at IS NULL) AS to_id",
        (link["from_path"], link["to_path"]),
    ).fetchone()
    if not ids["from_id"] or not ids["to_id"]:
        return False
    conn.execute(_LINKS_UPSERT, (
        ids["from_id"], ids["to_id"], link["rel_type"], link["note"],
        link["link_origin"], link["updated_at"],
    ))
    return True


def _ensure_remote_schema(conn) -> None:
    """Creates / upgrades the memory_nodes table on the remote if needed."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memory_nodes (
            id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            parent_id UUID REFERENCES memory_nodes(id) ON DELETE CASCADE,
            path      TEXT NOT NULL UNIQUE,
            slug      TEXT NOT NULL,
            type      TEXT NOT NULL DEFAULT 'note',
            title     TEXT NOT NULL,
            body      TEXT,
            tags      TEXT[] DEFAULT '{}',
            importance   REAL DEFAULT 0.5,
            access_count INTEGER DEFAULT 0,
            accessed_at  TIMESTAMPTZ,
            valid_until  TIMESTAMPTZ,
            pin_triggers TEXT[] DEFAULT '{}',
            created_at TIMESTAMPTZ DEFAULT now(),
            updated_at TIMESTAMPTZ DEFAULT now()
        )
    """)
    conn.execute("ALTER TABLE memory_nodes ADD COLUMN IF NOT EXISTS importance REAL DEFAULT 0.5")
    conn.execute("ALTER TABLE memory_nodes ADD COLUMN IF NOT EXISTS valid_until TIMESTAMPTZ")
    conn.execute("ALTER TABLE memory_nodes ADD COLUMN IF NOT EXISTS origin TEXT NOT NULL DEFAULT 'curated'")
    conn.execute("ALTER TABLE memory_nodes ADD COLUMN IF NOT EXISTS embedding REAL[]")
    conn.execute("ALTER TABLE memory_nodes ADD COLUMN IF NOT EXISTS config JSONB DEFAULT '{}'")
    conn.execute("ALTER TABLE memory_nodes ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ")
    conn.execute("ALTER TABLE memory_nodes ADD COLUMN IF NOT EXISTS pin_triggers TEXT[] DEFAULT '{}'")
    # Migration: backfill pin_triggers from old boolean columns if they still exist, then drop them.
    conn.execute("""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'memory_nodes' AND column_name = 'auto_inject'
            ) THEN
                UPDATE memory_nodes SET pin_triggers = (
                    CASE
                        WHEN auto_inject AND reinject_on_compact THEN ARRAY['start','compact']
                        WHEN auto_inject THEN ARRAY['start']
                        WHEN reinject_on_compact THEN ARRAY['compact']
                        ELSE '{}'::TEXT[]
                    END
                );
                ALTER TABLE memory_nodes DROP COLUMN IF EXISTS auto_inject;
                ALTER TABLE memory_nodes DROP COLUMN IF EXISTS reinject_on_compact;
            END IF;
        END
        $$
    """)
    # Remove the legacy updated_at trigger if present — it corrupts last-write-wins sync.
    conn.execute("DROP TRIGGER IF EXISTS memory_nodes_updated_at ON memory_nodes")
    # If the remote has pgvector, give it the indexed vector column + HNSW too, so
    # semantic search runs natively there (not just numpy fallback).
    try:
        with conn.transaction():
            conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
    except Exception:
        pass
    if diary_db.has_pgvector(conn):
        conn.execute(f"ALTER TABLE memory_nodes ADD COLUMN IF NOT EXISTS embedding_v vector({diary_embed.EMBED_DIM})")
        conn.execute("CREATE INDEX IF NOT EXISTS memory_nodes_embv_idx "
                     "ON memory_nodes USING hnsw (embedding_v vector_cosine_ops)")
    # memory_links: needed for the (from_path,to_path,rel_type)-matched link sync.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memory_links (
            id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            from_id    UUID NOT NULL REFERENCES memory_nodes(id) ON DELETE CASCADE,
            to_id      UUID NOT NULL REFERENCES memory_nodes(id) ON DELETE CASCADE,
            rel_type   TEXT NOT NULL DEFAULT 'related',
            note       TEXT,
            created_at TIMESTAMPTZ DEFAULT now(),
            UNIQUE(from_id, to_id, rel_type)
        )
    """)
    conn.execute("ALTER TABLE memory_links ADD COLUMN IF NOT EXISTS link_origin TEXT NOT NULL DEFAULT 'explicit'")
    conn.execute("ALTER TABLE memory_links ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT now()")


@mcp.tool()
def memory_sync() -> str:
    """Bidirektionaler Sync des Memory-Trees mit der Remote-Postgres-Instanz.

    Last-write-wins: der Node mit dem neueren updated_at gewinnt. Öffnet bei Bedarf
    selbstständig einen ephemeren SSH-Tunnel (wenn DIARY_REMOTE_SSH_HOST gesetzt ist)
    und baut ihn nach dem Sync wieder ab.

    Voraussetzung: DIARY_REMOTE_URL (DB-Endpunkt wie er auf dem Remote-Host sichtbar ist)
    und optional DIARY_REMOTE_SSH_HOST (SSH-Alias, z.B. 'dorn').
    """
    if not diary_db.get_remote_url():
        return (
            "DIARY_REMOTE_URL ist nicht gesetzt.\n"
            "Beispiel: export DIARY_REMOTE_URL='postgresql://claude:pw@127.0.0.1:54320/diary_mcp'\n"
            "Für SSH-Tunnel zusätzlich: export DIARY_REMOTE_SSH_HOST='dorn'"
        )

    def depth(path: str) -> int:
        return path.count("/")

    try:
        with diary_db.get_db() as local_conn:
            # Capture the DB clock BEFORE reading rows: any edit that lands after this
            # point is treated as post-sync and will be caught by the next run's conflict
            # check. Using the DB clock keeps it consistent with updated_at (TIMESTAMPTZ).
            sync_started_at = local_conn.execute("SELECT now() AS ts").fetchone()["ts"]
            last_sync = _read_last_sync(local_conn)
            local_nodes = local_conn.execute(
                f"SELECT {_SYNC_COLS} FROM memory_nodes ORDER BY path"
            ).fetchall()
        local_by_path = {n["path"]: n for n in local_nodes}

        with diary_db.remote_db_url() as rurl:
            remote_conn = psycopg.connect(rurl, row_factory=dict_row)
            try:
                _ensure_remote_schema(remote_conn)
                remote_nodes = remote_conn.execute(
                    f"SELECT {_SYNC_COLS} FROM memory_nodes ORDER BY path"
                ).fetchall()
                remote_by_path = {n["path"]: n for n in remote_nodes}

                # Detect concurrent edits BEFORE applying last-write-wins: a path
                # present on both sides, changed on both since the last sync, with
                # genuinely diverging content. Resolution stays last-write-wins below.
                conflicts = _detect_conflicts(local_by_path, remote_by_path, last_sync)

                # Push local → remote (parents before children)
                pushed = 0
                for path in sorted(local_by_path, key=depth):
                    local_n = local_by_path[path]
                    remote_n = remote_by_path.get(path)
                    if not remote_n or local_n["updated_at"] > remote_n["updated_at"]:
                        remote_conn.execute(_SYNC_INSERT, _sync_row(local_n))
                        pushed += 1
                remote_conn.commit()
            finally:
                remote_conn.close()

        # Pull remote → local (parents before children)
        pulled = 0
        with diary_db.get_db() as local_conn:
            for path in sorted(remote_by_path, key=depth):
                remote_n = remote_by_path[path]
                local_n = local_by_path.get(path)
                if not local_n or remote_n["updated_at"] > local_n["updated_at"]:
                    local_conn.execute(_SYNC_INSERT, _sync_row(remote_n))
                    pulled += 1

        # --- Sync memory_links (after nodes, so both sides can resolve ids) ---
        with diary_db.get_db() as local_conn:
            local_links = local_conn.execute(_LINKS_SELECT).fetchall()
        local_by_key = {_link_key(l): l for l in local_links}

        links_pushed = 0
        with diary_db.remote_db_url() as rurl:
            rc = psycopg.connect(rurl, row_factory=dict_row)
            try:
                remote_links = rc.execute(_LINKS_SELECT).fetchall()
                remote_by_key = {_link_key(l): l for l in remote_links}
                for l in local_links:
                    r = remote_by_key.get(_link_key(l))
                    if r is None or l["updated_at"] > r["updated_at"]:
                        if _sync_link(rc, l):
                            links_pushed += 1
                rc.commit()
            finally:
                rc.close()

        links_pulled = 0
        with diary_db.get_db() as local_conn:
            for l in remote_links:
                loc = local_by_key.get(_link_key(l))
                if loc is None or l["updated_at"] > loc["updated_at"]:
                    if _sync_link(local_conn, l):
                        links_pulled += 1

        # Re-link parent_id on both sides + refresh vector index for newly pulled rows
        with diary_db.get_db() as local_conn:
            local_conn.execute(diary_db._BACKFILL_PARENT_SQL)
            if _pgvector_ready(local_conn):
                local_conn.execute(
                    "UPDATE memory_nodes SET embedding_v = embedding::vector "
                    "WHERE embedding IS NOT NULL AND embedding_v IS NULL"
                )
        with diary_db.remote_db_url() as rurl:
            rc = psycopg.connect(rurl)
            try:
                rc.execute(diary_db._BACKFILL_PARENT_SQL)
                # Populate the remote HNSW index from synced REAL[] embeddings (if pgvector).
                if diary_db.has_pgvector(rc):
                    rc.execute("UPDATE memory_nodes SET embedding_v = embedding::vector "
                               "WHERE embedding IS NOT NULL AND embedding_v IS NULL")
                rc.commit()
            finally:
                rc.close()

        # Sync succeeded: persist the new last_sync timestamp for this remote so the
        # next run can detect concurrent edits relative to this point.
        with diary_db.get_db() as local_conn:
            _write_last_sync(local_conn, sync_started_at)

        if conflicts:
            _log.warning(
                "memory_sync: %d concurrent-edit conflict(s) resolved last-write-wins: %s",
                len(conflicts),
                ", ".join(f"{c['path']}({c['winner']})" for c in conflicts),
            )

        tunnel_note = f" (via SSH-Tunnel {diary_db.get_remote_ssh_host()})" if diary_db.get_remote_ssh_host() else ""
        return (f"Sync abgeschlossen{tunnel_note}. Lokal→Remote: {pushed} gepusht. "
                f"Remote→Lokal: {pulled} gepullt. Links: {links_pushed} gepusht, "
                f"{links_pulled} gepullt." + _format_conflicts(conflicts))
    except Exception as exc:
        return f"Sync fehlgeschlagen: {exc}"


# =====================================================================
# DIARY TABLE SYNC (projects/milestones/tasks/logs/reminders/wiki_pages/
# errors_solutions) — separate from memory_sync() above so a failure in one
# doesn't block the other. Same last-write-wins model, but these tables use
# SERIAL PKs that are independent per DB instance, so they can't be matched by
# id like memory_nodes. Two match-key strategies:
#   - projects: matched by `name` (already UNIQUE) — no new column needed.
#   - everything else: matched by the `sync_id` UUID column added for this
#     purpose. The SERIAL `id` stays untouched — it's still the FK target and
#     what tools return to callers ("Meilenstein M{id}").
# Parent FKs (project_id, milestone_id) are resolved via the parent's stable
# key (name / sync_id) on each side rather than transferred as raw integers.
# =====================================================================

_DIARY_CHILD_TABLES = {
    # table:            data columns (excludes sync_id/project_id/updated_at/deleted_at)
    "milestones":       ("title", "completed", "completed_at"),
    "logs":             ("timestamp", "author", "entry", "level", "worker"),
    "reminders":        ("target_date", "note", "completed"),
    "wiki_pages":        ("title", "content"),
    "errors_solutions": ("error_msg", "solution_msg"),
}
_TASK_COLS = ("title", "completed")


def _ensure_remote_diary_schema(conn) -> None:
    """Idempotent fallback: adds the sync_id/updated_at/deleted_at columns this sync
    needs if the remote process hasn't been restarted since the migration landed in
    diary_db._SCHEMA (that restart normally does this already)."""
    conn.execute("ALTER TABLE projects ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ")
    for table in (*_DIARY_CHILD_TABLES, "tasks"):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS sync_id UUID DEFAULT gen_random_uuid() NOT NULL")
        conn.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT now()")
        conn.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ")
        conn.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS {table}_sync_id_idx ON {table}(sync_id)")


def _sync_projects(local_conn, remote_conn) -> tuple[int, int]:
    """Push/pull `projects`, matched by `name` (already UNIQUE)."""
    select_sql = "SELECT name, status, archived, config, deleted_at, created_at, updated_at FROM projects"
    insert_sql = """INSERT INTO projects (name, status, archived, config, deleted_at, created_at, updated_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (name) DO UPDATE SET
            status=EXCLUDED.status, archived=EXCLUDED.archived, config=EXCLUDED.config,
            deleted_at=EXCLUDED.deleted_at, updated_at=EXCLUDED.updated_at"""

    def values(r):
        return (r["name"], r["status"], r["archived"],
                json.dumps(r["config"]) if r.get("config") is not None else "{}",
                r["deleted_at"], r["created_at"], r["updated_at"])

    local_rows = local_conn.execute(select_sql).fetchall()
    remote_rows = remote_conn.execute(select_sql).fetchall()
    local_by_name = {r["name"]: r for r in local_rows}
    remote_by_name = {r["name"]: r for r in remote_rows}

    pushed_data = []
    for name, r in local_by_name.items():
        o = remote_by_name.get(name)
        if o is None or r["updated_at"] > o["updated_at"]:
            pushed_data.append(values(r))
    if pushed_data:
        with remote_conn.cursor() as cur:
            cur.executemany(insert_sql, pushed_data)

    pulled_data = []
    for name, r in remote_by_name.items():
        o = local_by_name.get(name)
        if o is None or r["updated_at"] > o["updated_at"]:
            pulled_data.append(values(r))
    if pulled_data:
        with local_conn.cursor() as cur:
            cur.executemany(insert_sql, pulled_data)
    return len(pushed_data), len(pulled_data)


def _resolve_project_id_any(conn, name: str):
    """Resolve a project's id by name, INCLUDING tombstoned projects.

    Unlike get_project_id() (which filters deleted_at IS NULL for normal tool use),
    sync's FK resolution must still find a project that was tombstoned in the very
    same sync run — e.g. delete_project() tombstones the project and its children
    together, and syncs projects before children; if this filtered out deleted
    projects, every child of a just-deleted project would silently fail to push its
    own tombstone (parent "not found" => skipped) and the deletion would never
    propagate.
    """
    row = conn.execute("SELECT id FROM projects WHERE name = %s", (name,)).fetchone()
    return row["id"] if row else None


def _sync_project_child(local_conn, remote_conn, table: str, cols: tuple) -> tuple[int, int]:
    """Push/pull a table whose parent is `projects`, matched by `sync_id`.

    The parent's project_id is transferred as its portable `project_name` and
    re-resolved to a local integer id on each side (_resolve_project_id_any).
    """
    col_list = ", ".join(f"t.{c}" for c in cols)
    select_sql = (
        f"SELECT t.sync_id, p.name AS project_name, {col_list}, t.updated_at, t.deleted_at "
        f"FROM {table} t JOIN projects p ON t.project_id = p.id"
    )
    insert_cols = ["sync_id", "project_id", *cols, "updated_at", "deleted_at"]
    placeholders = ", ".join(["%s"] * len(insert_cols))
    update_set = ", ".join(f"{c}=EXCLUDED.{c}" for c in ("project_id", *cols, "updated_at", "deleted_at"))
    insert_sql = (
        f"INSERT INTO {table} ({', '.join(insert_cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT (sync_id) DO UPDATE SET {update_set}"
    )

    def values(r, pid):
        return (r["sync_id"], pid, *(r[c] for c in cols), r["updated_at"], r["deleted_at"])

    local_rows = local_conn.execute(select_sql).fetchall()
    remote_rows = remote_conn.execute(select_sql).fetchall()
    local_by_id = {r["sync_id"]: r for r in local_rows}
    remote_by_id = {r["sync_id"]: r for r in remote_rows}

    remote_pid_cache = {}
    def get_remote_pid(name):
        if name not in remote_pid_cache:
            remote_pid_cache[name] = _resolve_project_id_any(remote_conn, name)
        return remote_pid_cache[name]

    local_pid_cache = {}
    def get_local_pid(name):
        if name not in local_pid_cache:
            local_pid_cache[name] = _resolve_project_id_any(local_conn, name)
        return local_pid_cache[name]

    pushed_data = []
    for sync_id, r in local_by_id.items():
        o = remote_by_id.get(sync_id)
        if o is None or r["updated_at"] > o["updated_at"]:
            pid = get_remote_pid(r["project_name"])
            if pid is None:
                continue  # parent project hasn't synced to this side (yet)
            pushed_data.append(values(r, pid))
    if pushed_data:
        with remote_conn.cursor() as cur:
            cur.executemany(insert_sql, pushed_data)

    pulled_data = []
    for sync_id, r in remote_by_id.items():
        o = local_by_id.get(sync_id)
        if o is None or r["updated_at"] > o["updated_at"]:
            pid = get_local_pid(r["project_name"])
            if pid is None:
                continue
            pulled_data.append(values(r, pid))
    if pulled_data:
        with local_conn.cursor() as cur:
            cur.executemany(insert_sql, pulled_data)
    return len(pushed_data), len(pulled_data)


def _sync_tasks(local_conn, remote_conn) -> tuple[int, int]:
    """Push/pull `tasks`, matched by `sync_id`; parent milestone resolved via the
    milestone's own `sync_id` (not project name — tasks hang off milestones)."""
    col_list = ", ".join(f"t.{c}" for c in _TASK_COLS)
    select_sql = (
        f"SELECT t.sync_id, m.sync_id AS milestone_sync_id, {col_list}, t.updated_at, t.deleted_at "
        f"FROM tasks t JOIN milestones m ON t.milestone_id = m.id"
    )
    insert_cols = ["sync_id", "milestone_id", *_TASK_COLS, "updated_at", "deleted_at"]
    placeholders = ", ".join(["%s"] * len(insert_cols))
    update_set = ", ".join(f"{c}=EXCLUDED.{c}" for c in ("milestone_id", *_TASK_COLS, "updated_at", "deleted_at"))
    insert_sql = (
        f"INSERT INTO tasks ({', '.join(insert_cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT (sync_id) DO UPDATE SET {update_set}"
    )

    def values(r, mid):
        return (r["sync_id"], mid, *(r[c] for c in _TASK_COLS), r["updated_at"], r["deleted_at"])

    def resolve_milestone_id(conn, milestone_sync_id):
        row = conn.execute("SELECT id FROM milestones WHERE sync_id = %s", (milestone_sync_id,)).fetchone()
        return row["id"] if row else None

    local_rows = local_conn.execute(select_sql).fetchall()
    remote_rows = remote_conn.execute(select_sql).fetchall()
    local_by_id = {r["sync_id"]: r for r in local_rows}
    remote_by_id = {r["sync_id"]: r for r in remote_rows}

    remote_mid_cache = {}
    def get_remote_mid(sync_id):
        if sync_id not in remote_mid_cache:
            remote_mid_cache[sync_id] = resolve_milestone_id(remote_conn, sync_id)
        return remote_mid_cache[sync_id]

    local_mid_cache = {}
    def get_local_mid(sync_id):
        if sync_id not in local_mid_cache:
            local_mid_cache[sync_id] = resolve_milestone_id(local_conn, sync_id)
        return local_mid_cache[sync_id]

    pushed_data = []
    for sync_id, r in local_by_id.items():
        o = remote_by_id.get(sync_id)
        if o is None or r["updated_at"] > o["updated_at"]:
            mid = get_remote_mid(r["milestone_sync_id"])
            if mid is None:
                continue
            pushed_data.append(values(r, mid))
    if pushed_data:
        with remote_conn.cursor() as cur:
            cur.executemany(insert_sql, pushed_data)

    pulled_data = []
    for sync_id, r in remote_by_id.items():
        o = local_by_id.get(sync_id)
        if o is None or r["updated_at"] > o["updated_at"]:
            mid = get_local_mid(r["milestone_sync_id"])
            if mid is None:
                continue
            pulled_data.append(values(r, mid))
    if pulled_data:
        with local_conn.cursor() as cur:
            cur.executemany(insert_sql, pulled_data)
    return len(pushed_data), len(pulled_data)


@mcp.tool()
def memory_sync_diary() -> str:
    """Bidirektionaler Sync der Diary-Tabellen (Projekte, Meilensteine, Aufgaben,
    Logs, Wiedervorlagen, Wiki-Seiten, Errors/Solutions) mit der Remote-Postgres-
    Instanz — analog zu memory_sync(), aber für die klassischen Diary-Tabellen statt
    den Memory-Tree. Last-write-wins über updated_at, Löschungen sind Tombstones
    (deleted_at) und propagieren statt zu resurrecten.

    Voraussetzung: DIARY_REMOTE_URL (+ optional DIARY_REMOTE_SSH_HOST), wie bei
    memory_sync().
    """
    if not diary_db.get_remote_url():
        return (
            "DIARY_REMOTE_URL ist nicht gesetzt.\n"
            "Beispiel: export DIARY_REMOTE_URL='postgresql://claude:pw@127.0.0.1:54320/diary_mcp'\n"
            "Für SSH-Tunnel zusätzlich: export DIARY_REMOTE_SSH_HOST='dorn'"
        )

    try:
        with diary_db.get_db() as local_conn:
            with diary_db.remote_db_url() as rurl:
                remote_conn = psycopg.connect(rurl, row_factory=dict_row)
                try:
                    _ensure_remote_diary_schema(remote_conn)

                    counts = {}
                    # Dependency order: projects before their children, milestones
                    # before tasks (tasks resolve their parent via milestone.sync_id).
                    counts["projects"] = _sync_projects(local_conn, remote_conn)
                    counts["milestones"] = _sync_project_child(
                        local_conn, remote_conn, "milestones", _DIARY_CHILD_TABLES["milestones"]
                    )
                    counts["tasks"] = _sync_tasks(local_conn, remote_conn)
                    for table in ("logs", "reminders", "wiki_pages", "errors_solutions"):
                        counts[table] = _sync_project_child(
                            local_conn, remote_conn, table, _DIARY_CHILD_TABLES[table]
                        )

                    remote_conn.commit()
                finally:
                    remote_conn.close()
    except Exception as exc:
        return f"Sync fehlgeschlagen: {exc}"

    tunnel_note = f" (via SSH-Tunnel {diary_db.get_remote_ssh_host()})" if diary_db.get_remote_ssh_host() else ""
    total_pushed = sum(p for p, _ in counts.values())
    total_pulled = sum(u for _, u in counts.values())
    detail = ", ".join(f"{t}: {p}↑/{u}↓" for t, (p, u) in counts.items())
    return (f"Diary-Sync abgeschlossen{tunnel_note}. Gesamt: {total_pushed} gepusht, "
            f"{total_pulled} gepullt.\n{detail}")


@mcp.tool()
def memory_purge_tombstones_diary(older_than_days: int = 30) -> str:
    """Entfernt endgültig (HARD-DELETE) alte Diary-Tombstones (Projekte, Meilensteine,
    Aufgaben, Logs, Wiedervorlagen, Wiki-Seiten, Errors/Solutions), lokal und remote
    (falls konfiguriert) — analog zu memory_purge_tombstones(), aber für die
    Diary-Tabellen. Kind-Tabellen zuerst (FK-Reihenfolge), dann Projekte.

    older_than_days: Mindestalter eines Tombstones in Tagen (Default 30).
    """
    if older_than_days < 0:
        return "Fehler: older_than_days darf nicht negativ sein."

    interval = f"{int(older_than_days)} days"
    # Child-before-parent order so a project purge doesn't strand orphaned rows
    # under FK constraints (tasks -> milestones -> projects; the rest hang off
    # projects directly).
    tables = ("tasks", "milestones", "logs", "reminders", "wiki_pages", "errors_solutions", "projects")

    def purge(conn) -> dict:
        counts = {}
        for table in tables:
            counts[table] = conn.execute(
                f"DELETE FROM {table} WHERE deleted_at IS NOT NULL AND deleted_at < now() - %s::interval",
                (interval,),
            ).rowcount
        return counts

    with diary_db.get_db() as conn:
        local_counts = purge(conn)
    local_total = sum(local_counts.values())

    remote_note = ""
    if diary_db.get_remote_url():
        try:
            with diary_db.remote_db_url() as rurl:
                rc = psycopg.connect(rurl, row_factory=dict_row)
                try:
                    remote_counts = purge(rc)
                    rc.commit()
                finally:
                    rc.close()
            tunnel = f" (via SSH-Tunnel {diary_db.get_remote_ssh_host()})" if diary_db.get_remote_ssh_host() else ""
            remote_note = f" Remote{tunnel}: {sum(remote_counts.values())} entfernt."
        except Exception as exc:  # noqa: BLE001
            remote_note = f" Remote-Purge fehlgeschlagen: {exc}"
    else:
        remote_note = " (keine Remote konfiguriert — nur lokal)"

    return f"Diary-Tombstone-Purge (>{older_than_days}d): Lokal {local_total} entfernt.{remote_note}"
