"""
Reviewable memory consolidation — diary-mcp's "dreaming".

A nightly pass (scripts/consolidation_cron.py) PROPOSES changes and applies
none. Tom decides per proposal (memory_consolidation_decide or diary-web);
only then the change runs, through the normal service functions (merge,
tombstone, re-embed). Every proposal keeps its outcome as history, so it is
always visible what consolidation changed — the gap people criticise in
Claude Code's Auto Dream.

Kinds and the decisions they accept:
  duplicate      two curated memories ≥ DUP_SIMILARITY apart      merge | supersede | reject
  expired        valid_until passed more than EXPIRED_GRACE_DAYS  apply (soft delete) | reject
  relative_date  "gestern/heute/…" in a memory written on one day apply (absolute dates) | reject
  stale          importance ≤ 0.4, unpinned, untouched STALE_DAYS apply (archive: valid_until=now) | reject

Before applying, each proposal checks that its memories are unchanged since it
was made; otherwise it is marked 'outdated' and nothing happens. Rejected
proposals are never proposed again (fingerprint).
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date, timedelta
from zoneinfo import ZoneInfo

import diary_db
from diary_bootstrap import mcp

TZ = ZoneInfo("Europe/Berlin")
DUP_SIMILARITY = 0.93
EXPIRED_GRACE_DAYS = 14
STALE_DAYS = 180
STALE_MAX_IMPORTANCE = 0.4
KINDS = ("duplicate", "expired", "relative_date", "stale")
DECISIONS = {
    "duplicate": ("merge", "supersede", "reject"),
    "expired": ("apply", "reject"),
    "relative_date": ("apply", "reject"),
    "stale": ("apply", "reject"),
}

_LIVE = "deleted_at IS NULL AND origin = 'curated'"

# Lowercase-only for words that double as nouns ("Morgen" = morning).
_REL_DAYS = [
    (re.compile(r"\b(vorgestern)\b", re.I), -2),
    (re.compile(r"\b(gestern|yesterday)\b", re.I), -1),
    (re.compile(r"\b(heute|today)\b", re.I), 0),
    (re.compile(r"\b(tomorrow)\b", re.I), 1),
    (re.compile(r"(?<![A-Za-zÄÖÜäöüß])(morgen|übermorgen)\b(?!\s*früh)"), None),
]
_CODE = re.compile(r"```.*?```|`[^`\n]*`", re.S)


def _fingerprint(kind: str, paths: list[str], extra: str = "") -> str:
    return hashlib.sha256(f"{kind}|{'|'.join(sorted(paths))}|{extra}".encode()).hexdigest()[:32]


def _stamp(row) -> str:
    return row["updated_at"].isoformat()


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

def _duplicates(conn) -> list[dict]:
    import numpy as np
    rows = conn.execute(
        f"SELECT path, title, importance, access_count, length(coalesce(body,'')) AS len, "
        f"updated_at, embedding FROM memory_nodes WHERE {_LIVE} AND embedding IS NOT NULL "
        f"AND type <> 'category' AND (valid_until IS NULL OR valid_until >= now())"
    ).fetchall()
    if len(rows) < 2:
        return []
    m = np.array([r["embedding"] for r in rows], dtype=np.float32)
    m /= np.maximum(np.linalg.norm(m, axis=1, keepdims=True), 1e-9)
    sims = m @ m.T
    out = []
    for i, j in zip(*np.where(np.triu(sims, 1) >= DUP_SIMILARITY)):
        a, b = rows[int(i)], rows[int(j)]
        # Keep the more important one, then the more used, then the newer.
        key = lambda r: (float(r["importance"] or 0), int(r["access_count"] or 0), r["updated_at"])
        keep, drop = (a, b) if key(a) >= key(b) else (b, a)
        out.append({"kind": "duplicate", "paths": [keep["path"], drop["path"]],
                    "detail": {"keep": keep["path"], "drop": drop["path"],
                               "keep_title": keep["title"], "drop_title": drop["title"],
                               "similarity": round(float(sims[i, j]), 4)},
                    "stamps": {keep["path"]: _stamp(keep), drop["path"]: _stamp(drop)}})
    return out


def _expired(conn) -> list[dict]:
    rows = conn.execute(
        f"SELECT path, title, valid_until, updated_at FROM memory_nodes WHERE {_LIVE} "
        f"AND valid_until < now() - %s::interval",
        (f"{EXPIRED_GRACE_DAYS} days",),
    ).fetchall()
    return [{"kind": "expired", "paths": [r["path"]],
             "detail": {"title": r["title"], "valid_until": r["valid_until"].isoformat()},
             "stamps": {r["path"]: _stamp(r)}} for r in rows]


def relative_date_replacements(body: str, anchor: date) -> list[list[str]]:
    """[[word, ISO date], …] for relative day words outside code."""
    text = _CODE.sub(lambda m: " " * len(m.group(0)), body or "")
    found: list[list[str]] = []
    for rx, offset in _REL_DAYS:
        for m in rx.finditer(text):
            word = m.group(1)
            off = offset if offset is not None else (2 if word.lower() == "übermorgen" else 1)
            pair = [word, (anchor + timedelta(days=off)).isoformat()]
            if pair not in found:
                found.append(pair)
    return found


def _relative_dates(conn) -> list[dict]:
    rows = conn.execute(
        f"SELECT path, title, body, created_at, updated_at FROM memory_nodes WHERE {_LIVE} "
        r"AND body ~* '\m(heute|gestern|vorgestern|morgen|übermorgen|today|yesterday|tomorrow)\M'"
    ).fetchall()
    out = []
    for r in rows:
        created, updated = r["created_at"].astimezone(TZ).date(), r["updated_at"].astimezone(TZ).date()
        if created != updated:  # edited on a later day: the word may stem from either day
            continue
        reps = relative_date_replacements(r["body"], created)
        if reps:
            out.append({"kind": "relative_date", "paths": [r["path"]],
                        "detail": {"title": r["title"], "anchor": created.isoformat(), "replacements": reps},
                        "stamps": {r["path"]: _stamp(r)}})
    return out


def _stale(conn) -> list[dict]:
    rows = conn.execute(
        f"SELECT path, title, importance, updated_at, COALESCE(accessed_at, updated_at) AS last_touch "
        f"FROM memory_nodes WHERE {_LIVE} AND importance <= %s AND type <> 'category' "
        f"AND coalesce(pin_triggers, '{{}}') = '{{}}' AND valid_until IS NULL "
        f"AND COALESCE(accessed_at, updated_at) < now() - %s::interval",
        (STALE_MAX_IMPORTANCE, f"{STALE_DAYS} days"),
    ).fetchall()
    return [{"kind": "stale", "paths": [r["path"]],
             "detail": {"title": r["title"], "importance": float(r["importance"]),
                        "last_touch": r["last_touch"].isoformat()},
             "stamps": {r["path"]: _stamp(r)}} for r in rows]


def propose() -> dict[str, int]:
    """Detect candidates and queue new proposals. Changes no memory."""
    counts = {k: 0 for k in KINDS}
    with diary_db.get_db() as conn:
        found = _duplicates(conn) + _expired(conn) + _relative_dates(conn) + _stale(conn)
        for p in found:
            extra = json.dumps(p["detail"].get("replacements", ""), ensure_ascii=False)
            fp = _fingerprint(p["kind"], p["paths"], extra)
            row = conn.execute(
                "INSERT INTO consolidation_proposals (kind, paths, detail, stamps, fingerprint) "
                "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (fingerprint) DO NOTHING RETURNING id",
                (p["kind"], p["paths"], json.dumps(p["detail"], ensure_ascii=False),
                 json.dumps(p["stamps"]), fp),
            ).fetchone()
            if row:
                counts[p["kind"]] += 1
        conn.execute(
            "INSERT INTO diary_meta (key, value) VALUES ('consolidation_last_run', now()::text) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value")
    return counts


# ---------------------------------------------------------------------------
# Review + apply
# ---------------------------------------------------------------------------

def list_pending(limit: int = 50) -> tuple[int, list[dict]]:
    with diary_db.get_db() as conn:
        total = conn.execute(
            "SELECT count(*) AS n FROM consolidation_proposals WHERE status = 'pending'").fetchone()["n"]
        rows = conn.execute(
            "SELECT id::text AS id, kind, paths, detail, created_at FROM consolidation_proposals "
            "WHERE status = 'pending' ORDER BY array_position(%s::text[], kind), created_at LIMIT %s",
            (list(KINDS), limit),
        ).fetchall()
    return total, [{**r, "decisions": list(DECISIONS[r["kind"]]),
                    "created_at": r["created_at"].isoformat()} for r in rows]


def history(limit: int = 20) -> list[dict]:
    with diary_db.get_db() as conn:
        rows = conn.execute(
            "SELECT id::text AS id, kind, paths, status, decision, result, decided_at "
            "FROM consolidation_proposals WHERE status <> 'pending' "
            "ORDER BY decided_at DESC NULLS LAST LIMIT %s", (limit,),
        ).fetchall()
    return [{**r, "decided_at": r["decided_at"].isoformat() if r["decided_at"] else None} for r in rows]


def _unchanged(conn, stamps: dict) -> bool:
    for path, stamp in stamps.items():
        row = conn.execute("SELECT updated_at FROM memory_nodes WHERE path = %s AND deleted_at IS NULL",
                           (path,)).fetchone()
        if not row or row["updated_at"].isoformat() != stamp:
            return False
    return True


def supersede(new_path: str, old_path: str, note: str = "") -> str:
    """old_path is replaced by new_path: link new→old 'supersedes', old expires
    now (hidden from search and injection, still readable, reversible)."""
    import graph_core
    if new_path == old_path:
        raise ValueError("new_path und old_path dürfen nicht identisch sein")
    res = graph_core.memory_link(new_path, old_path, "supersedes", note)
    if "nicht gefunden" in res or res.startswith("Ungültig"):
        raise ValueError(res)
    with diary_db.get_db() as conn:
        conn.execute("UPDATE memory_nodes SET valid_until = now(), updated_at = now() "
                     "WHERE path = %s AND deleted_at IS NULL", (old_path,))
    return f"{old_path} ersetzt durch {new_path}"


def _apply(p: dict, decision: str) -> str:
    import memory_service
    kind, detail, paths = p["kind"], p["detail"], p["paths"]
    if kind == "duplicate" and decision == "merge":
        return "merge: " + memory_service.memory_merge(detail["keep"], detail["drop"])
    if kind == "duplicate" and decision == "supersede":
        return "supersede: " + supersede(detail["keep"], detail["drop"], "Konsolidierung")
    if kind == "expired":
        return "soft delete: " + memory_service.memory_delete(paths[0])
    if kind == "stale":
        with diary_db.get_db() as conn:
            conn.execute("UPDATE memory_nodes SET valid_until = now(), updated_at = now() WHERE path = %s",
                         (paths[0],))
        return f"archiviert: {paths[0]} (valid_until=now, rückgängig per memory_upsert mit valid_until='')"
    if kind == "relative_date":
        return "absolute Daten: " + _rewrite_dates(paths[0], detail["replacements"])
    raise ValueError(f"unbekannte Art {kind}")


def _rewrite_dates(path: str, replacements: list[list[str]]) -> str:
    import diary_embed
    from memory_service import _refresh_vector
    with diary_db.get_db() as conn:
        row = conn.execute("SELECT title, body FROM memory_nodes WHERE path = %s", (path,)).fetchone()
    parts = re.split(r"(```.*?```|`[^`\n]*`)", row["body"] or "", flags=re.S)
    for word, iso in replacements:
        rx = re.compile(rf"(?<![A-Za-zÄÖÜäöüß]){re.escape(word)}\b")
        parts = [s if s.startswith("`") else rx.sub(iso, s) for s in parts]
    body = "".join(parts)
    embedding = diary_embed.embed(f"{row['title']}\n{body}")  # outside any transaction (v0.8.1)
    with diary_db.get_db() as conn:
        conn.execute("UPDATE memory_nodes SET body = %s, embedding = COALESCE(%s, embedding), "
                     "updated_at = now() WHERE path = %s", (body, embedding, path))
        if embedding is not None:
            _refresh_vector(conn, path, embedding)
    return ", ".join(f"{w} → {d}" for w, d in replacements)


def decide(ids, decision: str) -> tuple[int, int]:
    """Apply or reject proposals. Returns (done, skipped) — skipped are
    unknown/already decided ids and proposals whose memories changed."""
    done = skipped = 0
    for raw in ids:
        with diary_db.get_db() as conn:
            try:
                p = conn.execute("SELECT * FROM consolidation_proposals WHERE id = %s::uuid "
                                 "AND status = 'pending'", (str(raw),)).fetchone()
            except Exception:
                p = None
            if not p:
                skipped += 1
                continue
            if decision not in DECISIONS[p["kind"]]:
                raise ValueError(f"'{decision}' passt nicht zu {p['kind']} "
                                 f"(erlaubt: {', '.join(DECISIONS[p['kind']])})")
            if decision == "reject":
                conn.execute("UPDATE consolidation_proposals SET status = 'rejected', decision = 'reject', "
                             "decided_at = now() WHERE id = %s", (p["id"],))
                done += 1
                continue
            if not _unchanged(conn, p["stamps"]):
                conn.execute("UPDATE consolidation_proposals SET status = 'outdated', decision = %s, "
                             "result = 'Memory seit dem Vorschlag geändert — nichts angewendet', "
                             "decided_at = now() WHERE id = %s", (decision, p["id"]))
                skipped += 1
                continue
        result = _apply(p, decision)  # own transactions; never inside the read above
        with diary_db.get_db() as conn:
            conn.execute("UPDATE consolidation_proposals SET status = 'applied', decision = %s, result = %s, "
                         "decided_at = now() WHERE id = %s", (decision, result[:2000], p["id"]))
        done += 1
    return done, skipped


# ---------------------------------------------------------------------------
# MCP tools — the review list is only processed on Tom's explicit request.
# ---------------------------------------------------------------------------

def _describe(p: dict) -> str:
    d = p["detail"]
    if p["kind"] == "duplicate":
        return (f"Duplikat ({d['similarity']:.3f}): behalten {d['keep']} ({d['keep_title']}), "
                f"weg {d['drop']} ({d['drop_title']})")
    if p["kind"] == "expired":
        return f"abgelaufen seit {d['valid_until'][:10]}: {p['paths'][0]} ({d['title']})"
    if p["kind"] == "relative_date":
        reps = ", ".join(f"„{w}“ → {iso}" for w, iso in d["replacements"])
        return f"relative Daten in {p['paths'][0]} (geschrieben {d['anchor']}): {reps}"
    return (f"veraltet: {p['paths'][0]} ({d['title']}), importance {d['importance']}, "
            f"zuletzt {d['last_touch'][:10]}")


@mcp.tool()
def memory_consolidation_run() -> str:
    """Sucht jetzt Konsolidierungs-Kandidaten (Duplikate, lange Abgelaufenes,
    relative Daten, Veraltetes) und legt Vorschläge an. Ändert KEINE Memory."""
    counts = propose()
    new = sum(counts.values())
    return f"{new} neue Vorschläge ({', '.join(f'{k}: {v}' for k, v in counts.items())})."


@mcp.tool()
def memory_consolidation_proposals(limit: int = 30) -> str:
    """Offene Konsolidierungs-Vorschläge. Nur auf Toms ausdrückliche Anfrage
    durchgehen und entscheiden — nie eigenmächtig."""
    total, items = list_pending(limit)
    if not total:
        return "Keine offenen Konsolidierungs-Vorschläge."
    lines = [f"{total} offene Vorschläge (zeige {len(items)}):"]
    for p in items:
        lines.append(f"  {p['id']}  {_describe(p)}  [{' | '.join(p['decisions'])}]")
    lines.append("\nEntscheiden: memory_consolidation_decide(ids='id1,id2', decision=…)")
    return "\n".join(lines)


@mcp.tool()
def memory_consolidation_decide(ids: str, decision: str) -> str:
    """Wendet Vorschläge an oder lehnt sie ab. decision je nach Art:
    duplicate → merge | supersede | reject; sonst apply | reject.
    Nur mit Toms ausdrücklicher Freigabe aufrufen."""
    try:
        done, skipped = decide([i.strip() for i in ids.split(",") if i.strip()], decision)
    except ValueError as e:
        return f"Fehler: {e}"
    verb = "abgelehnt" if decision == "reject" else "angewendet"
    return f"{done} {verb}, {skipped} übersprungen (unbekannt, schon entschieden oder inzwischen geändert)."


@mcp.tool()
def memory_supersede(new_path: str, old_path: str, note: str = "") -> str:
    """Markiert old_path als ersetzt durch new_path (z.B. neuer Projektstand):
    Link 'supersedes' + old_path läuft sofort ab (aus Suche/Injection raus,
    per memory_get weiter lesbar). Statt alte Stände zu löschen."""
    try:
        return supersede(new_path, old_path, note)
    except ValueError as e:
        return f"Fehler: {e}"
