"""
Client-side integration for diary-relay (github.com/Raindancer118/diary-relay)
— E2EE federation with another person's diary-mcp instance.

Crypto: PyNaCl `Box` (X25519 + XSalsa20-Poly1305 authenticated encryption).
Each local diary generates ONE long-lived keypair (stored in `diary_identity`,
private key unencrypted in the local Postgres DB — consistent with this
system's existing trust model, where the DB already holds sensitive plaintext
content; the threat model here is the relay/transport, not a compromised
local machine). Messages are encrypted individually with
Box(own_private_key, peer_public_key) before ever leaving this process — the
relay only ever sees ciphertext.

Only a tag-scoped subset of curated memories is ever shared (memory_upsert's
`tags`, queried the same way as memory_list_by_tag). Incoming synced content
is namespaced under /links/<alias>/... and tagged `from:<alias>` so it can
never silently overwrite the receiver's own tree.

diary_link_sync() itself stays a manual, explicit tool call — same pattern as
memory_sync()/memory_sync_diary(). Automatic scheduling (v0.16.0) is a
separate opt-in layer on top: diary_link_set_sync_tags(alias, tags) records
which tags a link should auto-sync, and scripts/diary_link_sync_cron.py (a
systemd user timer, same pattern as link_inference_cron.py/
memory_backup_export.py) calls the plain diary_link_sync() function for every
link/tag pair on a schedule. A link with no sync_tags configured is never
touched by the cron job.
"""
import base64
import json

import httpx
from nacl.public import Box, PrivateKey, PublicKey

import diary_db
from diary_bootstrap import mcp
from memory_service import memory_upsert

# Incoming memories come from a paired peer (authenticated by Box decryption
# succeeding), but the CONTENT of that decrypted JSON is still untrusted —
# a buggy or malicious peer could try to point `path` outside the /links/<alias>/
# namespace. _sanitize_incoming_path rejects anything that isn't a clean,
# traversal-free absolute path; alias itself is locally chosen (not
# peer-controlled) but gets the same treatment as defense-in-depth.
_VALID_MEMORY_TYPES = {"user", "feedback", "project", "reference", "note", "category"}
_MAX_TITLE_LEN = 500
_MAX_BODY_LEN = 200_000


def _is_safe_path_segment(alias_or_segment: str) -> bool:
    return bool(alias_or_segment) and alias_or_segment not in (".", "..") and "/" not in alias_or_segment


def _sanitize_incoming_path(alias: str, raw_path) -> str | None:
    """Builds the local /links/<alias>/... path for a peer-supplied path,
    returning None if it isn't a clean, traversal-free absolute path (empty,
    missing leading slash, or containing '.'/'..' segments)."""
    if not isinstance(raw_path, str) or not raw_path.startswith("/"):
        return None
    segments = raw_path.split("/")[1:]  # raw_path starts with '/', so [0] is always ''
    if not segments or any(not _is_safe_path_segment(s) for s in segments):
        return None
    return f"/links/{alias}/" + "/".join(segments)


def _relay_post(relay_url: str, path: str, token: str | None = None, json_body: dict | None = None) -> dict:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    resp = httpx.post(f"{relay_url}{path}", json=json_body, headers=headers, timeout=10)
    resp.raise_for_status()
    return resp.json()


def _relay_get(relay_url: str, path: str, token: str, params: dict | None = None) -> dict:
    resp = httpx.get(f"{relay_url}{path}", headers={"Authorization": f"Bearer {token}"},
                      params=params or {}, timeout=10)
    resp.raise_for_status()
    return resp.json()


def _relay_delete(relay_url: str, path: str, token: str) -> dict:
    resp = httpx.delete(f"{relay_url}{path}", headers={"Authorization": f"Bearer {token}"}, timeout=10)
    resp.raise_for_status()
    return resp.json()


def _get_identity(conn):
    return conn.execute("SELECT * FROM diary_identity LIMIT 1").fetchone()


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def _unb64(s: str) -> bytes:
    return base64.b64decode(s.encode())


@mcp.tool()
def diary_link_init(display_name: str, relay_url: str) -> str:
    """Erstellt die lokale Diary-Identity (X25519-Keypair) und registriert sie
    beim diary-relay-Server. Einmalig pro diary-mcp-Instanz — Voraussetzung für
    diary_link_create_pairing_code/diary_link_redeem_pairing_code/diary_link_sync.

    relay_url: Basis-URL des diary-relay-Servers, z.B. 'https://diary-relay.volantic.de'.
    """
    relay_url = relay_url.rstrip("/")
    with diary_db.get_db() as conn:
        existing = _get_identity(conn)
        if existing:
            return (f"Diary-Identity existiert bereits ('{existing['display_name']}', "
                    f"Relay {existing['relay_url']}) — nur eine Identity pro Instanz.")

        priv = PrivateKey.generate()
        pub = priv.public_key
        result = _relay_post(relay_url, "/diaries/register", json_body={
            "display_name": display_name,
            "public_key": _b64(bytes(pub)),
        })
        conn.execute(
            "INSERT INTO diary_identity "
            "(display_name, private_key, public_key, relay_url, relay_diary_id, relay_token) "
            "VALUES (%s,%s,%s,%s,%s,%s)",
            (display_name, bytes(priv), bytes(pub), relay_url, result["diary_id"], result["auth_token"]),
        )
    return f"Diary-Identity '{display_name}' registriert bei {relay_url} (diary_id {result['diary_id']})."


@mcp.tool()
def diary_link_create_pairing_code() -> str:
    """Erzeugt einen kurzlebigen (10 min), einmal verwendbaren Pairing-Code über
    den diary-relay-Server — außerhalb dieses Chats an die andere Person weitergeben,
    die ihn dann mit diary_link_redeem_pairing_code() einlöst."""
    with diary_db.get_db() as conn:
        identity = _get_identity(conn)
    if not identity:
        return "Keine Diary-Identity — zuerst diary_link_init(display_name, relay_url) aufrufen."
    result = _relay_post(identity["relay_url"], "/pairing/create", token=identity["relay_token"])
    return (f"Pairing-Code: {result['code']} (gültig bis {result['expires_at']}) — "
            f"an die andere Person außerhalb dieses Chats weitergeben.")


@mcp.tool()
def diary_link_redeem_pairing_code(code: str, alias: str) -> str:
    """Löst einen von der anderen Person geteilten Pairing-Code ein und legt die
    Verknüpfung lokal unter `alias` an (frei wählbarer Name, z.B. der Vorname
    der Person — muss lokal eindeutig sein)."""
    if not _is_safe_path_segment(alias):
        return "Alias darf kein '/', '.' oder '..' enthalten und nicht leer sein."
    with diary_db.get_db() as conn:
        identity = _get_identity(conn)
        if not identity:
            return "Keine Diary-Identity — zuerst diary_link_init(display_name, relay_url) aufrufen."
        existing = conn.execute(
            "SELECT id FROM diary_links WHERE peer_alias = %s", (alias,)
        ).fetchone()
        if existing:
            return f"Alias '{alias}' ist schon vergeben — anderen Alias wählen."

        result = _relay_post(identity["relay_url"], "/pairing/redeem",
                              token=identity["relay_token"], json_body={"code": code})
        conn.execute(
            "INSERT INTO diary_links (relay_link_id, peer_alias, peer_display_name, peer_public_key) "
            "VALUES (%s,%s,%s,%s)",
            (result["link_id"], alias, result["peer_display_name"], _unb64(result["peer_public_key"])),
        )
    return f"Verknüpft mit '{result['peer_display_name']}' als Alias '{alias}' (link_id {result['link_id']})."


@mcp.tool()
def diary_link_check_pairing_code(code: str, alias: str) -> str:
    """Prüft, ob ein selbst erzeugter Pairing-Code (diary_link_create_pairing_code)
    schon von der anderen Person eingelöst wurde, und legt bei Erfolg die
    Verknüpfung lokal unter `alias` an — Gegenstück zu diary_link_redeem_pairing_code
    für die Seite, die den Code ERSTELLT hat: der Redeemer erfährt link_id/Peer-Info
    direkt aus der Redeem-Antwort, die Ersteller-Seite muss aktiv nachfragen."""
    if not _is_safe_path_segment(alias):
        return "Alias darf kein '/', '.' oder '..' enthalten und nicht leer sein."
    with diary_db.get_db() as conn:
        identity = _get_identity(conn)
        if not identity:
            return "Keine Diary-Identity — zuerst diary_link_init(display_name, relay_url) aufrufen."
        existing = conn.execute("SELECT id FROM diary_links WHERE peer_alias = %s", (alias,)).fetchone()
        if existing:
            return f"Alias '{alias}' ist schon vergeben — anderen Alias wählen."

        status = _relay_get(identity["relay_url"], f"/pairing/{code}", token=identity["relay_token"])
        if not status.get("redeemed"):
            return f"Pairing-Code '{code}' wurde noch nicht eingelöst — später erneut prüfen."

        conn.execute(
            "INSERT INTO diary_links (relay_link_id, peer_alias, peer_display_name, peer_public_key) "
            "VALUES (%s,%s,%s,%s)",
            (status["link_id"], alias, status["peer_display_name"], _unb64(status["peer_public_key"])),
        )
    return f"Verknüpft mit '{status['peer_display_name']}' als Alias '{alias}' (link_id {status['link_id']})."


@mcp.tool()
def diary_link_list() -> str:
    """Listet alle bestehenden Diary-Links (Alias, Anzeigename, letzter Sync, Auto-Sync-Tags)."""
    with diary_db.get_db() as conn:
        links = conn.execute("SELECT * FROM diary_links ORDER BY established_at").fetchall()
    if not links:
        return "Keine Diary-Links."
    lines = ["Diary-Links:"]
    for l in links:
        last = l["last_synced_at"] or "nie"
        auto = ", ".join(l["sync_tags"]) if l["sync_tags"] else "aus"
        lines.append(f"  {l['peer_alias']} ({l['peer_display_name']}) — zuletzt gesynct: {last} — Auto-Sync-Tags: {auto}")
    return "\n".join(lines)


@mcp.tool()
def diary_link_set_sync_tags(alias: str, tags: str) -> str:
    """Legt fest, welche Tags für den Link `alias` automatisch gesynct werden.

    Zwei Mechanismen nutzen das (analog zum Auto-Linking-Muster: write-time +
    periodischer Batch): (1) JEDER memory_upsert() eines kuratierten Nodes mit
    einem dieser Tags pusht den Node SOFORT ("on change") verschlüsselt an
    diesen Link (memory_service.py ruft dafür push_node_on_upsert() auf, nachdem
    die eigene DB-Transaktion geschlossen ist — kein Netzwerkaufruf in einer
    offenen Transaktion, s. Project.md v0.8.1-Postmortem). (2) Der periodische
    Cron-Job (scripts/diary_link_sync_cron.py, systemd-User-Timer) ruft zusätzlich
    täglich das volle diary_link_sync(alias, tag) auf — das holt eingehende
    Nachrichten des Peers ab (Pull) und fängt Nodes ab, die z.B. während eines
    Relay-Ausfalls nicht sofort rausgingen.

    `tags`: kommagetrennte Liste (wie memory_upsert's tags-Parameter), z.B.
    'projekt-x,rezepte'. Leerer String schaltet Auto-Sync für diesen Link wieder
    aus (Default: aus — ein Link muss hierüber explizit opt-in gemacht werden).

    Für neu hinzugekommene Tags (in `tags`, aber noch nicht vorher konfiguriert)
    wird EINMALIG sofort ein voller Push aller schon bestehenden, so getaggten
    Nodes ausgelöst — sonst würden Nodes, die schon vor dem Opt-in existierten
    und seither nicht mehr editiert wurden, nie automatisch beim Peer ankommen
    (weder Write-Time-Push noch der Delta-Push von diary_link_sync würden sie
    erfassen). Bereits konfigurierte Tags lösen das nicht erneut aus.
    """
    tag_list = [t.strip() for t in tags.split(",") if t.strip()]
    with diary_db.get_db() as conn:
        link = conn.execute("SELECT * FROM diary_links WHERE peer_alias = %s", (alias,)).fetchone()
        if not link:
            return f"Kein Link mit Alias '{alias}'."
        old_tags = set(link["sync_tags"] or [])
        conn.execute("UPDATE diary_links SET sync_tags = %s WHERE id = %s", (tag_list, link["id"]))
        identity = _get_identity(conn)

    if not tag_list:
        return f"Auto-Sync für '{alias}' deaktiviert."

    msg = f"Auto-Sync für '{alias}' aktiviert für Tags: {', '.join(tag_list)}."

    # Initial catch-up push happens in its own DB round-trip, deliberately
    # AFTER the transaction above is closed — same reasoning as
    # push_node_on_upsert: a network call to the relay must never sit inside
    # an open DB transaction (Project.md v0.8.1 postmortem).
    newly_added = [t for t in dict.fromkeys(tag_list) if t not in old_tags]
    if newly_added and identity:
        own_priv = PrivateKey(bytes(identity["private_key"]))
        pushed_total = 0
        with diary_db.get_db() as conn:
            for tag in newly_added:
                try:
                    pushed_total += _push_nodes_for_tag(conn, identity, own_priv, link, tag, since=None)
                except Exception:  # noqa: BLE001 — a relay hiccup during setup shouldn't fail the config change
                    continue
        if pushed_total:
            msg += f" ({pushed_total} bestehende Node(s) initial gepusht.)"
    return msg


def _push_nodes_for_tag(conn, identity, own_priv, link, tag: str, since=None) -> int:
    """Encrypts+pushes every curated node tagged `tag` to `link` and returns how
    many. `since` (a timestamp or None) restricts to nodes touched after it —
    None means "all of them" (used for the one-time catch-up when a tag is
    newly enabled, and for a link's very first sync). Shared by diary_link_sync
    (delta push, see its docstring) and diary_link_set_sync_tags' initial
    catch-up push, so both stay in sync with a single implementation."""
    peer_pub = PublicKey(bytes(link["peer_public_key"]))
    box = Box(own_priv, peer_pub)
    if since:
        nodes = conn.execute(
            "SELECT path, title, body, type FROM memory_nodes "
            "WHERE deleted_at IS NULL AND origin = 'curated' AND %s = ANY(tags) AND updated_at > %s",
            (tag, since),
        ).fetchall()
    else:
        nodes = conn.execute(
            "SELECT path, title, body, type FROM memory_nodes "
            "WHERE deleted_at IS NULL AND origin = 'curated' AND %s = ANY(tags)",
            (tag,),
        ).fetchall()
    pushed = 0
    for n in nodes:
        payload = json.dumps({
            "path": n["path"], "title": n["title"], "body": n["body"], "type": n["type"],
        }).encode()
        ciphertext = bytes(box.encrypt(payload))
        _relay_post(identity["relay_url"], f"/links/{link['relay_link_id']}/messages",
                    token=identity["relay_token"], json_body={"ciphertext": _b64(ciphertext)})
        pushed += 1
    return pushed


def push_node_on_upsert(path: str, title: str, body: str, node_type: str, tag_list: list[str]) -> list[str]:
    """Write-time Auto-Push (v0.16.0): wird von memory_service.memory_upsert()
    NACH dem Schließen der eigenen DB-Transaktion aufgerufen (der Netzwerk-Call
    zum Relay darf keine offene Transaktion blockieren). Pusht genau DIESEN
    einen Node — nicht den ganzen Tag-Scope wie diary_link_sync() — an jeden
    Link, dessen sync_tags eines von `tag_list` enthält.

    Nie fatal: kein Identity/kein passender Link/ein nicht erreichbarer Relay
    bedeutet einfach "an diesen Link nicht gepusht", nie einen Fehler, der den
    eigentlichen memory_upsert()-Aufruf kaputt machen dürfte. Berührt bewusst
    NICHT last_synced_at (das steuert den Pull-Cursor von diary_link_sync() und
    bleibt dessen alleinige Zuständigkeit).
    """
    if not tag_list:
        return []
    pushed_to: list[str] = []
    try:
        with diary_db.get_db() as conn:
            identity = _get_identity(conn)
            if not identity:
                return []
            links = conn.execute(
                "SELECT * FROM diary_links WHERE sync_tags && %s", (tag_list,)
            ).fetchall()
            if not links:
                return []
            own_priv = PrivateKey(bytes(identity["private_key"]))
            payload = json.dumps({"path": path, "title": title, "body": body, "type": node_type}).encode()
            for link in links:
                try:
                    peer_pub = PublicKey(bytes(link["peer_public_key"]))
                    box = Box(own_priv, peer_pub)
                    ciphertext = bytes(box.encrypt(payload))
                    _relay_post(identity["relay_url"], f"/links/{link['relay_link_id']}/messages",
                                token=identity["relay_token"], json_body={"ciphertext": _b64(ciphertext)})
                    pushed_to.append(link["peer_alias"])
                except Exception:  # noqa: BLE001 — one unreachable link must not break the others or the save
                    continue
    except Exception:  # noqa: BLE001 — DB hiccup here must not break the save either
        return pushed_to
    return pushed_to


@mcp.tool()
def diary_link_sync(alias: str, tag: str) -> str:
    """Synct kuratierte Memories mit einem verknüpften Diary: verschlüsselt
    kuratierte Nodes mit `tag` für den Peer (E2EE, PyNaCl Box) und pusht sie über
    den Relay; pullt neue Nachrichten des Peers seit dem letzten Sync,
    entschlüsselt sie und legt sie lokal unter /links/<alias>/<pfad> ab, getaggt
    mit 'from:<alias>' — überschreibt nie den eigenen Tree.

    Push ist DELTA seit dem letzten Sync (nur Nodes mit updated_at > letztem
    last_synced_at) — beim allerersten Sync mit diesem Link (last_synced_at
    NULL) werden alle passenden Nodes gepusht. Das verhindert, dass ein
    wiederholter Aufruf (insbesondere der nächtliche Cron, s.u.) jede Nacht
    den kompletten Tag-Scope erneut als frische Relay-Nachrichten verschickt —
    unveränderte Nodes wurden beim letzten Mal schon (oder per Write-Time-Push,
    s. push_node_on_upsert) übertragen.

    Manueller, expliziter Tool-Aufruf; wird zusätzlich vom periodischen Cron-Job
    (scripts/diary_link_sync_cron.py) für jedes per diary_link_set_sync_tags()
    konfigurierte Tag aufgerufen (Fangnetz für alles, was der Write-Time-Push
    z.B. wegen eines Relay-Ausfalls verpasst hat, plus der einzige Ort, der pullt).
    """
    with diary_db.get_db() as conn:
        identity = _get_identity(conn)
        if not identity:
            return "Keine Diary-Identity — zuerst diary_link_init(display_name, relay_url) aufrufen."
        link = conn.execute("SELECT * FROM diary_links WHERE peer_alias = %s", (alias,)).fetchone()
        if not link:
            return f"Kein Link mit Alias '{alias}' — zuerst diary_link_redeem_pairing_code() aufrufen."

        own_priv = PrivateKey(bytes(identity["private_key"]))
        box = Box(own_priv, PublicKey(bytes(link["peer_public_key"])))

        pushed = _push_nodes_for_tag(conn, identity, own_priv, link, tag, since=link["last_synced_at"])

        params = {"since": link["last_synced_at"].isoformat()} if link["last_synced_at"] else {}
        pulled = _relay_get(identity["relay_url"], f"/links/{link['relay_link_id']}/messages",
                             token=identity["relay_token"], params=params)

        received = 0
        for msg in pulled["messages"]:
            plaintext = box.decrypt(_unb64(msg["ciphertext"]))
            data = json.loads(plaintext)
            # `data` was authenticated by successful Box decryption (it did come
            # from the paired peer), but its CONTENT is still untrusted — a
            # buggy or malicious peer could try to point `path` outside the
            # /links/<alias>/ namespace (e.g. '/../feedback/x') or send an
            # oversized payload. Skip anything that doesn't check out rather
            # than let one bad message break the whole sync.
            local_path = _sanitize_incoming_path(alias, data.get("path"))
            if local_path is None:
                continue
            node_type = data.get("type") if data.get("type") in _VALID_MEMORY_TYPES else "note"
            title = str(data.get("title") or "")[:_MAX_TITLE_LEN]
            body = str(data.get("body") or "")[:_MAX_BODY_LEN]
            memory_upsert(path=local_path, title=title, body=body, type=node_type, tags=f"from:{alias}")
            received += 1

        conn.execute("UPDATE diary_links SET last_synced_at = now() WHERE id = %s", (link["id"],))

    return f"Sync mit '{alias}' (Tag '{tag}'): {pushed} gepusht, {received} empfangen."


@mcp.tool()
def diary_link_unlink(alias: str) -> str:
    """Entfernt einen Diary-Link lokal und (best effort) auf dem Relay-Server."""
    with diary_db.get_db() as conn:
        identity = _get_identity(conn)
        link = conn.execute("SELECT * FROM diary_links WHERE peer_alias = %s", (alias,)).fetchone()
        if not link:
            return f"Kein Link mit Alias '{alias}'."
        if identity:
            try:
                _relay_delete(identity["relay_url"], f"/links/{link['relay_link_id']}",
                               token=identity["relay_token"])
            except Exception:  # noqa: BLE001 — local cleanup must still happen if the relay is unreachable
                pass
        conn.execute("DELETE FROM diary_links WHERE id = %s", (link["id"],))
    return f"Link '{alias}' entfernt."
