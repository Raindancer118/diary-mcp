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

Manual, explicit tool calls only in this phase (no automatic background
sync) — same pattern as memory_sync()/memory_sync_diary().
"""
import base64
import json

import httpx
from nacl.public import Box, PrivateKey, PublicKey

import diary_db
from diary_bootstrap import mcp
from memory_service import memory_upsert


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
    """Listet alle bestehenden Diary-Links (Alias, Anzeigename, letzter Sync)."""
    with diary_db.get_db() as conn:
        links = conn.execute("SELECT * FROM diary_links ORDER BY established_at").fetchall()
    if not links:
        return "Keine Diary-Links."
    lines = ["Diary-Links:"]
    for l in links:
        last = l["last_synced_at"] or "nie"
        lines.append(f"  {l['peer_alias']} ({l['peer_display_name']}) — zuletzt gesynct: {last}")
    return "\n".join(lines)


@mcp.tool()
def diary_link_sync(alias: str, tag: str) -> str:
    """Synct kuratierte Memories mit einem verknüpften Diary: verschlüsselt alle
    kuratierten Nodes mit `tag` einzeln für den Peer (E2EE, PyNaCl Box) und pusht
    sie über den Relay; pullt neue Nachrichten des Peers seit dem letzten Sync,
    entschlüsselt sie und legt sie lokal unter /links/<alias>/<pfad> ab, getaggt
    mit 'from:<alias>' — überschreibt nie den eigenen Tree.

    Manueller, expliziter Aufruf (kein automatischer Hintergrund-Sync in dieser Phase).
    """
    with diary_db.get_db() as conn:
        identity = _get_identity(conn)
        if not identity:
            return "Keine Diary-Identity — zuerst diary_link_init(display_name, relay_url) aufrufen."
        link = conn.execute("SELECT * FROM diary_links WHERE peer_alias = %s", (alias,)).fetchone()
        if not link:
            return f"Kein Link mit Alias '{alias}' — zuerst diary_link_redeem_pairing_code() aufrufen."

        own_priv = PrivateKey(bytes(identity["private_key"]))
        peer_pub = PublicKey(bytes(link["peer_public_key"]))
        box = Box(own_priv, peer_pub)

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

        params = {"since": link["last_synced_at"].isoformat()} if link["last_synced_at"] else {}
        pulled = _relay_get(identity["relay_url"], f"/links/{link['relay_link_id']}/messages",
                             token=identity["relay_token"], params=params)

        received = 0
        for msg in pulled["messages"]:
            plaintext = box.decrypt(_unb64(msg["ciphertext"]))
            data = json.loads(plaintext)
            local_path = f"/links/{alias}{data['path']}"
            memory_upsert(path=local_path, title=data["title"], body=data.get("body") or "",
                          type=data.get("type", "note"), tags=f"from:{alias}")
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
