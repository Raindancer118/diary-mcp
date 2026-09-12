"""
Memory-tree CRUD and lifecycle: context/tree/get, upsert, extracted-tier
promote/prune, tombstone delete/purge, importance, pinning, and per-project
config. The two-tier model (curated vs extracted) and pin_triggers live here.

Split out of the former diary_server.py monolith (v0.10.0).
"""
import json
from datetime import datetime, timedelta

import psycopg
from psycopg.rows import dict_row

import diary_db
import diary_embed
from diary_bootstrap import mcp

# Extracted memories auto-expire after this many days unless promoted to curated.
EXTRACTED_TTL_DAYS = 90

# Write-time auto-linking (v0.13.0): same similarity bar as memory_infer_links'
# default, but capped much lower per call since this fires on every curated
# upsert — a handful of new links per save is useful context, dozens would be
# graph spam. The periodic batch job (scripts/link_inference_cron.py) uses the
# higher max_new from memory_infer_links for its less-frequent, whole-tree pass.
AUTO_LINK_THRESHOLD = 0.82
AUTO_LINK_MAX_NEW = 3

# memory_context() session snapshot bounds. The "recently updated" section dumps
# full bodies; without caps it can exceed the MCP client's token limit when many
# nodes were touched recently (e.g. right after seeding the DB). Cap node count
# and per-body length; full content is always available via memory_get(path).
CONTEXT_RECENT_LIMIT = 15
CONTEXT_BODY_MAX_CHARS = 1200


def _ensure_memory_parent(conn, path: str):
    """Ensures all parent nodes exist for the given path; returns immediate parent id."""
    parts = path.strip("/").split("/")
    if len(parts) <= 1:
        return None

    parent_path = "/" + "/".join(parts[:-1])
    row = conn.execute(
        "SELECT id FROM memory_nodes WHERE path = %s AND deleted_at IS NULL", (parent_path,)
    ).fetchone()
    if row:
        return row["id"]

    grandparent_id = _ensure_memory_parent(conn, parent_path)
    parent_slug = parts[-2]
    # Revive a tombstoned parent path if one exists (ON CONFLICT), clearing deleted_at.
    row = conn.execute(
        """INSERT INTO memory_nodes (parent_id, path, slug, type, title)
           VALUES (%s, %s, %s, 'category', %s)
           ON CONFLICT (path) DO UPDATE SET updated_at = now(), deleted_at = NULL
           RETURNING id""",
        (grandparent_id, parent_path, parent_slug, parent_slug.capitalize()),
    ).fetchone()
    return row["id"]


def _contradiction_warnings(conn, node_ids: list) -> dict:
    """Just-in-time contradiction surfacing: {node_id: [(other_path, other_title), ...]}
    for any 'contradicts' link touching one of node_ids.

    Replaces relying on manual memory_health() review for this specific issue type —
    a contradiction is now visible exactly where it matters (memory_get/memory_search
    on the affected node), not only on an explicit, easy-to-forget health check.
    """
    if not node_ids:
        return {}
    rows = conn.execute(
        """SELECT n1.id AS from_id, n2.id AS to_id,
                  n1.path AS from_path, n1.title AS from_title,
                  n2.path AS to_path, n2.title AS to_title
           FROM memory_links ml
           JOIN memory_nodes n1 ON ml.from_id = n1.id
           JOIN memory_nodes n2 ON ml.to_id = n2.id
           WHERE ml.rel_type = 'contradicts'
             AND (ml.from_id = ANY(%s) OR ml.to_id = ANY(%s))
             AND n1.deleted_at IS NULL AND n2.deleted_at IS NULL""",
        (node_ids, node_ids),
    ).fetchall()
    warnings: dict = {}
    for r in rows:
        warnings.setdefault(r["from_id"], []).append((r["to_path"], r["to_title"]))
        warnings.setdefault(r["to_id"], []).append((r["from_path"], r["from_title"]))
    return warnings


@mcp.tool()
def memory_context() -> str:
    """Session-Start-Snapshot: liefert den kompletten Memory-Tree als Übersicht + kürzlich geänderte Nodes mit vollem Inhalt."""
    with diary_db.get_db() as conn:
        nodes = conn.execute(
            "SELECT path, type, title, updated_at FROM memory_nodes "
            "WHERE origin = 'curated' AND deleted_at IS NULL ORDER BY path"
        ).fetchall()
        recent_cutoff = datetime.now() - timedelta(days=14)
        recent = conn.execute(
            "SELECT path, title, body, updated_at FROM memory_nodes "
            "WHERE origin = 'curated' AND deleted_at IS NULL "
            "AND body IS NOT NULL AND body != '' AND updated_at > %s "
            "ORDER BY updated_at DESC LIMIT %s",
            (recent_cutoff, CONTEXT_RECENT_LIMIT + 1),
        ).fetchall()
        # Keep one extra to detect (but not render) overflow.
        recent_overflow = len(recent) > CONTEXT_RECENT_LIMIT
        recent = recent[:CONTEXT_RECENT_LIMIT]
        extracted_count = conn.execute(
            "SELECT COUNT(*) AS c FROM memory_nodes WHERE origin = 'extracted' AND deleted_at IS NULL"
        ).fetchone()["c"]

    lines = ["=== Claude Memory Context ===\n", "MEMORY TREE:"]
    for node in nodes:
        depth = node["path"].count("/") - 1
        indent = "  " * max(0, depth)
        updated = str(node["updated_at"])[:10]
        lines.append(f"{indent}[{node['type']}] {node['path']} — {node['title']} ({updated})")

    if recent:
        lines.append(
            f"\n\nRECENTLY UPDATED (last 14 days, {len(recent)} most recent):"
        )
        for node in recent:
            lines.append(f"\n--- {node['path']} ---")
            lines.append(f"Titel: {node['title']}")
            lines.append(f"Geändert: {str(node['updated_at'])[:10]}")
            body = node["body"] or ""
            if len(body) > CONTEXT_BODY_MAX_CHARS:
                body = (
                    body[:CONTEXT_BODY_MAX_CHARS].rstrip()
                    + f"\n… [gekürzt — vollständig via memory_get(\"{node['path']}\")]"
                )
            lines.append(body)
            lines.append("---")
        if recent_overflow:
            lines.append(
                f"\n(+ weitere kürzlich geänderte Nodes nicht gezeigt — nur die "
                f"{CONTEXT_RECENT_LIMIT} neuesten. Tree oben listet alle; "
                f"Details via memory_get(path).)"
            )

    if extracted_count:
        lines.append(
            f"\n\n(+ {extracted_count} auto-extrahierte Memories aus Chat-Transkripten — "
            f"standardmäßig NICHT geladen/durchsucht, da kostspieliger. "
            f"Bei Bedarf gezielt via memory_search(query, include_extracted=True).)"
        )

    return "\n".join(lines)


@mcp.tool()
def memory_tree(path: str = "/", include_extracted: bool = False) -> str:
    """Gibt den Memory-Tree ab einem bestimmten Pfad aus (default: Wurzel).

    include_extracted=False (Default) blendet die automatisch aus Transkripten
    extrahierten Memories aus, damit der Baum kompakt und hochwertig bleibt.
    """
    origin_clause = "" if include_extracted else "AND origin = 'curated'"
    with diary_db.get_db() as conn:
        if path == "/":
            nodes = conn.execute(
                f"SELECT path, type, title, updated_at FROM memory_nodes "
                f"WHERE deleted_at IS NULL {origin_clause} ORDER BY path"
            ).fetchall()
        else:
            nodes = conn.execute(
                f"SELECT path, type, title, updated_at FROM memory_nodes "
                f"WHERE (path = %s OR path LIKE %s) AND deleted_at IS NULL {origin_clause} ORDER BY path",
                (path, f"{path}/%"),
            ).fetchall()

    if not nodes:
        return f"Kein Memory-Node unter '{path}' gefunden."

    lines = [f"Memory Tree: {path}"]
    for node in nodes:
        depth = node["path"].count("/") - 1
        indent = "  " * max(0, depth)
        updated = str(node["updated_at"])[:10]
        lines.append(f"{indent}[{node['type']}] {node['path']} — {node['title']} ({updated})")
    return "\n".join(lines)


@mcp.tool()
def memory_get(path: str) -> str:
    """Gibt den vollen Inhalt eines Memory-Nodes zurück und trackt den Zugriff."""
    with diary_db.get_db() as conn:
        node = conn.execute(
            "SELECT * FROM memory_nodes WHERE path = %s AND deleted_at IS NULL", (path,)
        ).fetchone()
        if not node:
            return f"Kein Memory-Node unter '{path}' gefunden."

        # Track access (OpenMemory-inspired composite scoring input)
        conn.execute(
            "UPDATE memory_nodes SET access_count = access_count + 1, accessed_at = now() WHERE path = %s",
            (path,),
        )

        # Fetch outgoing links
        links = conn.execute(
            """SELECT ml.rel_type, ml.note, mn.path AS target_path, mn.title AS target_title
               FROM memory_links ml
               JOIN memory_nodes mn ON ml.to_id = mn.id
               WHERE ml.from_id = %s""",
            (node["id"],),
        ).fetchall()

        # Just-in-time contradiction surfacing (see _contradiction_warnings docstring).
        contradictions = _contradiction_warnings(conn, [node["id"]]).get(node["id"], [])

        # Warn if expired
        expired_note = ""
        _now_aware = datetime.now().astimezone()
        if node.get("valid_until") and node["valid_until"].astimezone() < _now_aware:
            expired_note = f"\n⚠️  ABGELAUFEN seit {str(node['valid_until'])[:10]}"

    triggers = node.get("pin_triggers") or []
    pin_str = f" | PIN: {', '.join(triggers)}" if triggers else ""
    lines = [
        f"=== Memory: {node['title']} ==={expired_note}",
        f"Pfad:       {node['path']}",
        f"Typ:        {node['type']}",
        f"Wichtigkeit: {node['importance']:.1f} | Zugriffe: {node['access_count']}{pin_str}",
        f"Tags:       {', '.join(node['tags']) if node['tags'] else '—'}",
        f"Gültig bis: {str(node['valid_until'])[:10] if node['valid_until'] else '—'}",
        f"Erstellt:   {str(node['created_at'])[:10]} | Geändert: {str(node['updated_at'])[:10]}",
        "",
        node["body"] or "(kein Inhalt)",
    ]
    if contradictions:
        lines.append("")
        for other_path, other_title in contradictions:
            lines.append(f"⚠ WIDERSPRUCH: widerspricht {other_path} ({other_title}) — bitte gegenprüfen.")
    if links:
        lines.append("\nVerknüpfungen:")
        for lnk in links:
            note = f" — {lnk['note']}" if lnk.get("note") else ""
            lines.append(f"  [{lnk['rel_type']}] {lnk['target_path']} ({lnk['target_title']}){note}")
    return "\n".join(lines)


_pgvector_cache: dict[str, bool] = {}


def _pgvector_ready(conn) -> bool:
    """Cache whether the connected DB has pgvector (per database URL)."""
    from diary_db import get_database_url, has_pgvector
    url = get_database_url()
    if url not in _pgvector_cache:
        _pgvector_cache[url] = has_pgvector(conn)
    return _pgvector_cache[url]


def _refresh_vector(conn, path: str, embedding) -> None:
    """Populate the indexed embedding_v column from the REAL[] embedding (pgvector only)."""
    if embedding is None or not _pgvector_ready(conn):
        return
    conn.execute(
        "UPDATE memory_nodes SET embedding_v = embedding::vector WHERE path = %s",
        (path,),
    )


def _auto_link_new_node(conn, node_id, embedding) -> list[str]:
    """Compares a freshly upserted curated node against every other curated,
    embedded node and inserts an inferred 'related' link for pairs above
    AUTO_LINK_THRESHOLD — capped at AUTO_LINK_MAX_NEW (highest-similarity
    first). Skips pairs that already have ANY link (explicit or inferred, any
    rel_type) so it never overrides or duplicates one. Returns the paths of
    newly-linked nodes for the caller's result message."""
    if embedding is None:
        return []
    others = conn.execute(
        "SELECT id, path, embedding FROM memory_nodes "
        "WHERE deleted_at IS NULL AND origin = 'curated' AND embedding IS NOT NULL AND id != %s",
        (node_id,),
    ).fetchall()
    if not others:
        return []

    unit_new = diary_embed.normalize(embedding)
    scored = []
    for o in others:
        sim = diary_embed.dot(unit_new, diary_embed.normalize(o["embedding"]))
        if sim >= AUTO_LINK_THRESHOLD:
            scored.append((sim, o))
    scored.sort(key=lambda s: s[0], reverse=True)

    created = []
    for sim, o in scored:
        if len(created) >= AUTO_LINK_MAX_NEW:
            break
        existing = conn.execute(
            "SELECT id FROM memory_links WHERE (from_id=%s AND to_id=%s) OR (from_id=%s AND to_id=%s)",
            (node_id, o["id"], o["id"], node_id),
        ).fetchone()
        if existing:
            continue
        conn.execute(
            "INSERT INTO memory_links (from_id, to_id, rel_type, link_origin) "
            "VALUES (%s, %s, 'related', 'inferred') "
            "ON CONFLICT (from_id, to_id, rel_type) DO NOTHING",
            (node_id, o["id"]),
        )
        created.append(o["path"])
    return created


@mcp.tool()
def memory_upsert(
    path: str,
    title: str,
    body: str,
    type: str = "note",
    tags: str = "",
    importance: float = 0.5,
    valid_until: str = "",
    origin: str = "curated",
) -> str:
    """Erstellt oder aktualisiert einen Memory-Node.

    path:        z.B. '/feedback/commit-style' oder '/projects/eduvault4/status'
    type:        user | feedback | project | reference | note | category
    tags:        kommagetrennte Tags, optional
    importance:  0.0–1.0 Wichtigkeitsscore (default 0.5)
    valid_until: ISO-Datum bis wann die Info gültig ist, z.B. '2026-07-15' (optional)
    origin:      'curated' (Default — bewusst gespeichert, Standard-Suche) oder
                 'extracted' (automatisch aus Transkript geerntet, nur auf Anfrage).
                 Für extrahierte Memories besser memory_save_extracted() nutzen.

    Hinweis: Das Pinning (automatisches Injizieren beim Session-Start oder nach
    Kompaktierung) wird separat über memory_pin() / memory_unpin() gesteuert.
    """
    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []
    valid_until_val = valid_until.strip() if valid_until else None
    embedding = diary_embed.embed(f"{title}\n{body}")  # None if model unavailable

    with diary_db.get_db() as conn:
        parent_id = _ensure_memory_parent(conn, path)
        slug = path.strip("/").split("/")[-1]

        existing = conn.execute("SELECT id FROM memory_nodes WHERE path = %s", (path,)).fetchone()
        if existing:
            # Upserting a tombstoned path revives it (deleted_at=NULL).
            conn.execute(
                "UPDATE memory_nodes SET title=%s, body=%s, type=%s, tags=%s, "
                "importance=%s, valid_until=%s, origin=%s, embedding=%s, "
                "deleted_at=NULL, updated_at=now() WHERE path=%s",
                (title, body, type, tag_list, importance, valid_until_val, origin, embedding, path),
            )
            _refresh_vector(conn, path, embedding)
            node_id = existing["id"]
            msg = f"Memory '{path}' aktualisiert."
        else:
            inserted = conn.execute(
                "INSERT INTO memory_nodes (parent_id, path, slug, type, title, body, tags, importance, valid_until, origin, embedding) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                (parent_id, path, slug, type, title, body, tag_list, importance, valid_until_val, origin, embedding),
            ).fetchone()
            _refresh_vector(conn, path, embedding)
            node_id = inserted["id"]
            msg = f"Memory '{path}' erstellt."

        # Auto-link at write time (v0.13.0): only for curated nodes — the
        # embedding just computed above is reused here for free, so a fresh
        # or edited memory picks up its strongest related links immediately
        # instead of waiting for a manual memory_infer_links() call or the
        # periodic batch cron (scripts/link_inference_cron.py).
        if origin == "curated":
            auto_linked = _auto_link_new_node(conn, node_id, embedding)
            if auto_linked:
                msg += f" (+{len(auto_linked)} auto-verlinkt: {', '.join(auto_linked)})"
        return msg


@mcp.tool()
def memory_save_extracted(path: str, title: str, body: str, type: str = "note",
                          tags: str = "", importance: float = 0.3) -> str:
    """Speichert ein automatisch aus einem Chat-Transkript extrahiertes Memory (origin='extracted').

    Diese Memories landen in der ZWEITEN Stufe: sie werden standardmäßig NICHT
    in memory_context/memory_tree/memory_search geladen, weil sie zahlreich und
    roh sind. Sie sind nur über memory_search(query, include_extracted=True)
    auffindbar. Gedacht für eine Extraktions-Pipeline (z.B. SessionEnd-Hook),
    nicht für bewusst kuratiertes Wissen — dafür memory_upsert() verwenden.

    Standard-importance ist bewusst niedrig (0.3), da unkuratiert.

    Extrahierte Memories erhalten automatisch ein Ablaufdatum von EXTRACTED_TTL_DAYS
    (aktuell 90 Tage), damit sie nicht unbegrenzt wachsen. Wird ein Memory via
    memory_promote() zu curated befördert, wird valid_until gelöscht.
    """
    ttl = (datetime.now() + timedelta(days=EXTRACTED_TTL_DAYS)).strftime("%Y-%m-%d")
    return memory_upsert(path=path, title=title, body=body, type=type, tags=tags,
                         importance=importance, valid_until=ttl, origin="extracted")


@mcp.tool()
def memory_promote(path: str, importance: float = 0.7) -> str:
    """Befördert ein auto-extrahiertes Memory zu einem kuratierten (origin='curated').

    Nutze dies, wenn du beim Durchsuchen der extrahierten Memories eines findest,
    das dauerhaft wertvoll ist und künftig standardmäßig verfügbar sein soll.

    Setzt origin='curated', erhöht importance und löscht valid_until, damit das
    Memory dauerhaft verfügbar bleibt (kein automatisches Ablaufen mehr).
    """
    with diary_db.get_db() as conn:
        result = conn.execute(
            "UPDATE memory_nodes SET origin='curated', importance=%s, valid_until=NULL, updated_at=now() "
            "WHERE path=%s AND deleted_at IS NULL RETURNING origin",
            (importance, path),
        ).fetchone()
        if not result:
            return f"Node '{path}' nicht gefunden."
    return f"'{path}' zu kuratiertem Memory befördert (importance {importance:.1f}, valid_until gelöscht)."


@mcp.tool()
def memory_prune_extracted(expired_only: bool = True, keep_per_project: int = 0) -> str:
    """Bereinigt extrahierte Memories (origin='extracted') durch Soft-Delete (Tombstone).

    expired_only=True (Default): tombstonet nur Memories, deren valid_until abgelaufen ist.
    expired_only=False: tombstonet ALLE extracted Memories (z.B. für einen Kalt-Reset).

    keep_per_project>0: Behält die N neuesten extracted Memories pro /projects/<slug>
    und tombstonet den Rest (wird zusätzlich zu expired_only ausgeführt). Nützlich,
    um die Tier-2-Größe pro Projekt zu begrenzen, ohne alle zu löschen.
    HINWEIS: Wenn keep_per_project>0 und expired_only=False, werden zunächst die N
    neuesten behalten und der Rest tombstonet — nicht alles.

    Gibt einen Bericht zurück, wie viele Memories tombstonet wurden.
    """
    tombstoned_expired = 0
    tombstoned_overflow = 0

    with diary_db.get_db() as conn:
        # --- 1. Per-project overflow (keep_per_project newest) ---
        # Runs BEFORE the bulk tombstone so the keep list is still alive.
        if keep_per_project > 0:
            # Find all distinct project slugs that have extracted memories
            slug_rows = conn.execute(
                """SELECT DISTINCT regexp_replace(path, '^/projects/([^/]+)/.*$', '\\1') AS slug
                   FROM memory_nodes
                   WHERE origin = 'extracted' AND deleted_at IS NULL
                     AND path ~ '^/projects/[^/]+/.*$'"""
            ).fetchall()

            for row in slug_rows:
                slug = row["slug"]
                base = f"/projects/{slug}/"
                # Keep the N most recently created; tombstone the rest
                keep_rows = conn.execute(
                    "SELECT id FROM memory_nodes "
                    "WHERE origin = 'extracted' AND deleted_at IS NULL "
                    "AND path LIKE %s ORDER BY created_at DESC LIMIT %s",
                    (f"{base}%", keep_per_project),
                ).fetchall()
                keep_ids = [r["id"] for r in keep_rows]

                if keep_ids:
                    placeholders = ",".join(["%s"] * len(keep_ids))
                    result = conn.execute(
                        f"UPDATE memory_nodes SET deleted_at = now(), updated_at = now() "
                        f"WHERE origin = 'extracted' AND deleted_at IS NULL "
                        f"AND path LIKE %s AND id NOT IN ({placeholders})",
                        [f"{base}%"] + keep_ids,
                    )
                    tombstoned_overflow += result.rowcount
                else:
                    result = conn.execute(
                        "UPDATE memory_nodes SET deleted_at = now(), updated_at = now() "
                        "WHERE origin = 'extracted' AND deleted_at IS NULL AND path LIKE %s",
                        (f"{base}%",),
                    )
                    tombstoned_overflow += result.rowcount

        # --- 2. Expired / bulk tombstone (runs after overflow so counts are separate) ---
        if expired_only:
            result = conn.execute(
                "UPDATE memory_nodes SET deleted_at = now(), updated_at = now() "
                "WHERE origin = 'extracted' AND deleted_at IS NULL "
                "AND valid_until IS NOT NULL AND valid_until < now()"
            )
            tombstoned_expired = result.rowcount
        else:
            result = conn.execute(
                "UPDATE memory_nodes SET deleted_at = now(), updated_at = now() "
                "WHERE origin = 'extracted' AND deleted_at IS NULL"
            )
            tombstoned_expired = result.rowcount

    parts = []
    if expired_only:
        parts.append(f"{tombstoned_expired} abgelaufene extracted Memories tombstonet")
    else:
        parts.append(f"{tombstoned_expired} extracted Memories tombstonet (alle)")
    if keep_per_project > 0:
        parts.append(f"{tombstoned_overflow} Overflow-Memories per keep_per_project={keep_per_project} tombstonet")
    total = tombstoned_expired + tombstoned_overflow
    parts.append(f"Gesamt: {total} Tombstones gesetzt")
    return " | ".join(parts) + "."


@mcp.tool()
def memory_delete(path: str) -> str:
    """Löscht einen Memory-Node und alle seine Kinder (Unterknoten).

    Soft-Delete: setzt deleted_at (Tombstone) statt hart zu löschen. So wird die
    Löschung beim Sync per last-write-wins zur Remote propagiert, statt beim
    nächsten Sync wieder „aufzuerstehen". Tombstones werden aus allen Lese-Pfaden
    ausgeblendet und können später via memory_purge_tombstones endgültig entfernt werden.
    """
    with diary_db.get_db() as conn:
        count_row = conn.execute(
            "SELECT COUNT(*) AS c FROM memory_nodes "
            "WHERE (path = %s OR path LIKE %s) AND deleted_at IS NULL",
            (path, f"{path}/%"),
        ).fetchone()
        count = count_row["c"]
        if count == 0:
            return f"Kein Node unter '{path}' gefunden."
        conn.execute(
            "UPDATE memory_nodes SET deleted_at = now(), updated_at = now() "
            "WHERE (path = %s OR path LIKE %s) AND deleted_at IS NULL",
            (path, f"{path}/%"),
        )
    return f"{count} Memory-Node(s) unter '{path}' gelöscht (Tombstone)."


@mcp.tool()
def memory_merge(keep_path: str, merge_path: str) -> str:
    """Führt zwei kuratierte Memory-Nodes zusammen: merge_path wird in keep_path
    eingefaltet und danach als Tombstone gelöscht (memory_delete-Semantik, kein
    Kaskadieren auf Kinder von merge_path — für Merges gedacht, nicht für
    Teilbäume). Gegenstück zu memory_consolidate_report's Near-Duplicate-Vorschlägen:
    dieses Tool führt die Zusammenführung explizit aus (keine automatische Aktion).

    Ablauf:
      1. merge_path's body wird unter einem Trenner an keep_path's body angehängt
         (kein Content-Verlust; bei echten Duplikaten entsteht dadurch Redundanz,
         die man danach manuell kürzen kann — sicherer als automatisch zu deduplizieren).
      2. keep_path's Embedding wird aus dem neuen, kombinierten Text neu berechnet.
      3. Alle Links, die auf merge_path zeigten, werden auf keep_path umgehängt
         (sonst würde der Graph nach dem Tombstone Links auf einen gelöschten
         Node zeigen); bereits existierende identische Links auf keep_path werden
         nicht dupliziert.
      4. merge_path wird per Tombstone gelöscht (wie memory_delete).
    """
    if keep_path == merge_path:
        return "Fehler: keep_path und merge_path dürfen nicht identisch sein."

    with diary_db.get_db() as conn:
        keep = conn.execute(
            "SELECT id, title, body FROM memory_nodes WHERE path = %s AND deleted_at IS NULL",
            (keep_path,),
        ).fetchone()
        if not keep:
            return f"Ziel-Node '{keep_path}' nicht gefunden."
        merge = conn.execute(
            "SELECT id, title, body FROM memory_nodes WHERE path = %s AND deleted_at IS NULL",
            (merge_path,),
        ).fetchone()
        if not merge:
            return f"Quell-Node '{merge_path}' nicht gefunden."

        combined_body = (
            f"{keep['body'] or ''}\n\n--- merged from {merge_path} ({merge['title']}) ---\n"
            f"{merge['body'] or ''}"
        ).strip()
        new_embedding = diary_embed.embed(f"{keep['title']}\n{combined_body}")

        conn.execute(
            "UPDATE memory_nodes SET body = %s, embedding = %s, updated_at = now() WHERE id = %s",
            (combined_body, new_embedding, keep["id"]),
        )
        _refresh_vector(conn, keep_path, new_embedding)

        # Repoint merge_path's links onto keep_path instead of leaving them
        # dangling on a soon-to-be-tombstoned node. Skip self-loops (a link
        # between merge_path and keep_path itself) and exact duplicates of a
        # link keep_path already has.
        outgoing = conn.execute(
            "SELECT id, to_id, rel_type FROM memory_links WHERE from_id = %s", (merge["id"],)
        ).fetchall()
        for l in outgoing:
            if l["to_id"] == keep["id"]:
                conn.execute("DELETE FROM memory_links WHERE id = %s", (l["id"],))
                continue
            conn.execute(
                "UPDATE memory_links SET from_id = %s WHERE id = %s "
                "AND NOT EXISTS (SELECT 1 FROM memory_links "
                "WHERE from_id = %s AND to_id = %s AND rel_type = %s)",
                (keep["id"], l["id"], keep["id"], l["to_id"], l["rel_type"]),
            )
        incoming = conn.execute(
            "SELECT id, from_id, rel_type FROM memory_links WHERE to_id = %s", (merge["id"],)
        ).fetchall()
        for l in incoming:
            if l["from_id"] == keep["id"]:
                conn.execute("DELETE FROM memory_links WHERE id = %s", (l["id"],))
                continue
            conn.execute(
                "UPDATE memory_links SET to_id = %s WHERE id = %s "
                "AND NOT EXISTS (SELECT 1 FROM memory_links "
                "WHERE from_id = %s AND to_id = %s AND rel_type = %s)",
                (keep["id"], l["id"], l["from_id"], keep["id"], l["rel_type"]),
            )
        # Any repoint that hit the NOT EXISTS guard (duplicate) leaves the old
        # row pointing at merge_path — clean those up so no link references
        # the soon-to-be-tombstoned node.
        conn.execute(
            "DELETE FROM memory_links WHERE from_id = %s OR to_id = %s",
            (merge["id"], merge["id"]),
        )

        conn.execute(
            "UPDATE memory_nodes SET deleted_at = now(), updated_at = now() WHERE id = %s",
            (merge["id"],),
        )

    return (f"'{merge_path}' in '{keep_path}' gemergt (Body angehängt, Embedding neu berechnet, "
            f"Links umgehängt) und als Tombstone gelöscht.")


@mcp.tool()
def memory_purge_tombstones(older_than_days: int = 30) -> str:
    """Entfernt endgültig (HARD-DELETE) alle Tombstones, deren Löschung älter als N Tage ist.

    Tombstones (deleted_at gesetzt) bleiben eine Weile bestehen, damit die Löschung
    per Sync zur Remote propagiert. Diese Funktion räumt sie auf BEIDEN Seiten auf
    (lokal + Remote, falls erreichbar), damit sie nicht ewig wachsen. Nur Tombstones
    mit deleted_at < now() - older_than_days werden hart gelöscht — frische Tombstones
    bleiben erhalten, bis beide Seiten synchronisiert sind.

    older_than_days: Mindestalter eines Tombstones in Tagen (Default 30).
    """
    if older_than_days < 0:
        return "Fehler: older_than_days darf nicht negativ sein."

    purge_sql = (
        "DELETE FROM memory_nodes "
        "WHERE deleted_at IS NOT NULL AND deleted_at < now() - %s::interval"
    )
    interval = f"{int(older_than_days)} days"

    with diary_db.get_db() as conn:
        local = conn.execute(purge_sql, (interval,)).rowcount

    remote_note = ""
    if diary_db.get_remote_url():
        try:
            with diary_db.remote_db_url() as rurl:
                rc = psycopg.connect(rurl, row_factory=dict_row)
                try:
                    remote = rc.execute(purge_sql, (interval,)).rowcount
                    rc.commit()
                finally:
                    rc.close()
            tunnel = f" (via SSH-Tunnel {diary_db.get_remote_ssh_host()})" if diary_db.get_remote_ssh_host() else ""
            remote_note = f" Remote{tunnel}: {remote} entfernt."
        except Exception as exc:  # noqa: BLE001
            remote_note = f" Remote-Purge fehlgeschlagen: {exc}"
    else:
        remote_note = " (keine Remote konfiguriert — nur lokal)"

    return f"Tombstone-Purge (>{older_than_days}d): Lokal {local} entfernt.{remote_note}"


@mcp.tool()
def memory_set_importance(path: str, importance: float) -> str:
    """Setzt den Wichtigkeitsscore eines Memory-Nodes manuell (0.0–1.0)."""
    if not 0.0 <= importance <= 1.0:
        return "Fehler: importance muss zwischen 0.0 und 1.0 liegen."
    with diary_db.get_db() as conn:
        result = conn.execute(
            "UPDATE memory_nodes SET importance = %s, updated_at = now() "
            "WHERE path = %s AND deleted_at IS NULL RETURNING path",
            (importance, path),
        ).fetchone()
        if not result:
            return f"Node '{path}' nicht gefunden."
    return f"Wichtigkeit von '{path}' auf {importance:.2f} gesetzt."


@mcp.tool()
def memory_pin(path: str, on_start: bool = True, on_compact: bool = False) -> str:
    """Pinnt ein Memory für automatisches Injizieren — sparsam einsetzen, da jeder Pin Kontext kostet!

    on_start:    Wenn True, wird das Memory beim Session-Start automatisch in Claudes
                 Kontext geladen (setzt 'start' in pin_triggers). Gut für dauerhaft
                 wichtige Projekt-Fakten, die Claude immer kennen muss.
    on_compact:  Wenn True, wird das Memory nach einer Kontext-Kompaktierung erneut
                 injiziert (setzt 'compact' in pin_triggers). Gut für Infos, die eine
                 Kompaktierung „überleben" müssen. Noch kostspieliger als on_start —
                 nur für absolut kritische Nodes.

    Gibt die resultierende pin_triggers-Liste zurück.
    """
    triggers: list[str] = []
    if on_start:
        triggers.append("start")
    if on_compact:
        triggers.append("compact")

    with diary_db.get_db() as conn:
        result = conn.execute(
            "UPDATE memory_nodes SET pin_triggers = %s, updated_at = now() "
            "WHERE path = %s AND deleted_at IS NULL RETURNING path",
            (triggers, path),
        ).fetchone()
        if not result:
            return f"Node '{path}' nicht gefunden."
    trigger_str = ", ".join(triggers) if triggers else "(keine)"
    return f"Pin für '{path}' gesetzt: [{trigger_str}]."


@mcp.tool()
def memory_unpin(path: str) -> str:
    """Entfernt alle Pin-Trigger eines Memory-Nodes (kein automatisches Injizieren mehr).

    Setzt pin_triggers auf ein leeres Array. Das Memory bleibt erhalten und
    ist weiterhin über Suche auffindbar — es wird nur nicht mehr automatisch
    in den Kontext geladen.
    """
    with diary_db.get_db() as conn:
        result = conn.execute(
            "UPDATE memory_nodes SET pin_triggers = '{}', updated_at = now() "
            "WHERE path = %s AND deleted_at IS NULL RETURNING path",
            (path,),
        ).fetchone()
        if not result:
            return f"Node '{path}' nicht gefunden."
    return f"Pin für '{path}' entfernt (pin_triggers leer)."


@mcp.tool()
def memory_project_context(project_slug: str, only_pinned: bool = True) -> str:
    """Liefert den Memory-Kontext für ein Projekt — gedacht zum automatischen Injizieren beim Projektstart.

    project_slug: z.B. 'eduvault4' (ohne /projects/-Präfix) — wird auf /projects/<slug>/... gematcht.
    only_pinned:  wenn True (default), nur Memories mit 'start' in pin_triggers; sonst alle.

    Gibt die vollständigen Inhalte zurück, sodass Claude sie direkt verwenden kann.
    Globale /user- und /feedback-Memories mit 'start' in pin_triggers werden immer mitgeliefert.
    """
    base = f"/projects/{project_slug.strip('/')}"
    with diary_db.get_db() as conn:
        if only_pinned:
            rows = conn.execute(
                "SELECT path, type, title, body, importance FROM memory_nodes "
                "WHERE 'start' = ANY(pin_triggers) AND deleted_at IS NULL AND (path = %s OR path LIKE %s "
                "  OR ((path LIKE '/user/%%' OR path LIKE '/feedback/%%'))) "
                "ORDER BY (path LIKE %s) DESC, importance DESC, path",
                (base, f"{base}/%", f"{base}%"),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT path, type, title, body, importance FROM memory_nodes "
                "WHERE (path = %s OR path LIKE %s) AND type != 'category' AND deleted_at IS NULL "
                "ORDER BY importance DESC, path",
                (base, f"{base}/%"),
            ).fetchall()

    if not rows:
        scope = "gepinnten " if only_pinned else ""
        return f"Keine {scope}Memories für Projekt '{project_slug}' gefunden."

    lines = [f"=== Gepinnte Memory-Kontext: {project_slug} ===",
             f"({len(rows)} Memories automatisch geladen)\n"]
    for r in rows:
        lines.append(f"--- [{r['type']}] {r['path']} — {r['title']} (Wichtigkeit {r['importance']:.1f}) ---")
        lines.append(r["body"] or "(kein Inhalt)")
        lines.append("")
    return "\n".join(lines)


def _ensure_project_node(conn, slug: str) -> str:
    """Ensure the /projects/<slug> category node exists; return its path."""
    path = f"/projects/{slug.strip('/')}"
    parent_id = _ensure_memory_parent(conn, path)
    conn.execute(
        """INSERT INTO memory_nodes (parent_id, path, slug, type, title)
           VALUES (%s, %s, %s, 'category', %s)
           ON CONFLICT (path) DO UPDATE SET deleted_at = NULL, updated_at = now()""",
        (parent_id, path, slug.strip("/"), slug.strip("/").replace("-", " ").title()),
    )
    return path


@mcp.tool()
def memory_set_project_config(project_slug: str, auto_extract: bool = None) -> str:
    """Setzt projektspezifische Einstellungen (gespeichert am /projects/<slug>-Node).

    auto_extract: Wenn True, erfasst der SessionEnd-Hook für DIESES Projekt
    deterministisch die User-Turns als tier-2 Memories (kein AI/API). Pro Projekt
    einzeln steuerbar; Default ist AUS. None lässt die Einstellung unverändert.
    """
    with diary_db.get_db() as conn:
        path = _ensure_project_node(conn, project_slug)
        row = conn.execute("SELECT config FROM memory_nodes WHERE path = %s", (path,)).fetchone()
        cfg = dict(row["config"] or {}) if row else {}
        if auto_extract is not None:
            cfg["auto_extract"] = bool(auto_extract)
        conn.execute(
            "UPDATE memory_nodes SET config = %s, updated_at = now() WHERE path = %s",
            (json.dumps(cfg), path),
        )
    return f"Projekt-Config für '{project_slug}': {json.dumps(cfg)}"


@mcp.tool()
def memory_get_project_config(project_slug: str) -> str:
    """Zeigt die projektspezifischen Einstellungen des /projects/<slug>-Nodes.

    Enthält u.a. 'auto_extract' (bool) und 'dirs' (Liste absoluter Pfade),
    die per memory_set_project_dir gepflegt werden.
    """
    path = f"/projects/{project_slug.strip('/')}"
    with diary_db.get_db() as conn:
        row = conn.execute(
            "SELECT config FROM memory_nodes WHERE path = %s AND deleted_at IS NULL", (path,)
        ).fetchone()
    if not row:
        return f"Projekt '{project_slug}' hat noch keinen Node/Config."
    cfg = row["config"] or {}
    return f"Config für '{project_slug}': {json.dumps(cfg, ensure_ascii=False)}"


@mcp.tool()
def memory_set_project_dir(project_slug: str, dir_path: str) -> str:
    """Verknüpft ein absolutes Verzeichnis mit einem Projekt (config.dirs).

    Damit erkennen die Session-Hooks den Projektkontext anhand des cwd — auch
    wenn der Ordnername vom Slug abweicht oder das Projekt in einem Unterordner
    geöffnet wird. Mehrfaches Hinzufügen desselben Pfads ist idempotent.

    project_slug: z.B. 'diary-mcp' (ohne /projects/-Präfix)
    dir_path:     absoluter Pfad, z.B. '/home/tom/Projekte/SE Projects/diary-mcp'

    Der /projects/<slug>-Node wird automatisch angelegt, falls er noch nicht
    existiert.
    """
    dir_path = dir_path.rstrip("/")
    if not dir_path.startswith("/"):
        return "Fehler: dir_path muss ein absoluter Pfad sein (beginnt mit '/')."
    with diary_db.get_db() as conn:
        path = _ensure_project_node(conn, project_slug)
        row = conn.execute("SELECT config FROM memory_nodes WHERE path = %s", (path,)).fetchone()
        cfg = dict(row["config"] or {}) if row else {}
        dirs: list[str] = cfg.get("dirs") or []
        if dir_path not in dirs:
            dirs.append(dir_path)
            cfg["dirs"] = dirs
            conn.execute(
                "UPDATE memory_nodes SET config = %s, updated_at = now() WHERE path = %s",
                (json.dumps(cfg), path),
            )
            return f"Verzeichnis '{dir_path}' zu Projekt '{project_slug}' hinzugefügt. dirs: {dirs}"
    return f"Verzeichnis '{dir_path}' war bereits in Projekt '{project_slug}' eingetragen. dirs: {dirs}"


@mcp.tool()
def memory_unset_project_dir(project_slug: str, dir_path: str) -> str:
    """Entfernt ein Verzeichnis aus der config.dirs-Liste eines Projekts.

    project_slug: z.B. 'diary-mcp'
    dir_path:     absoluter Pfad, der entfernt werden soll
    """
    dir_path = dir_path.rstrip("/")
    path = f"/projects/{project_slug.strip('/')}"
    with diary_db.get_db() as conn:
        row = conn.execute(
            "SELECT config FROM memory_nodes WHERE path = %s AND deleted_at IS NULL", (path,)
        ).fetchone()
        if not row:
            return f"Projekt '{project_slug}' hat noch keinen Node/Config."
        cfg = dict(row["config"] or {})
        dirs: list[str] = cfg.get("dirs") or []
        if dir_path not in dirs:
            return f"Verzeichnis '{dir_path}' war nicht in Projekt '{project_slug}' eingetragen."
        dirs.remove(dir_path)
        cfg["dirs"] = dirs
        conn.execute(
            "UPDATE memory_nodes SET config = %s, updated_at = now() WHERE path = %s",
            (json.dumps(cfg), path),
        )
    return f"Verzeichnis '{dir_path}' aus Projekt '{project_slug}' entfernt. dirs: {dirs}"
