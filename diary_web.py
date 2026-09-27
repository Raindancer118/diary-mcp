"""
diary-web „Observatorium" — localhost-only web UI for the diary-mcp memory tree.
Frontend lives in diary_web_assets/ (see Design.md).

Start:  diary-web [--port 8765]
Stop:   Ctrl-C
"""
from __future__ import annotations

import argparse
import signal
import sys
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from diary_db import get_db, init_db

app = FastAPI(docs_url=None, redoc_url=None, title="Diary Memory Browser")


# ── API ──────────────────────────────────────────────────────────────────────

def _serial(row) -> dict[str, Any]:
    d = dict(row)
    for k in ("created_at", "updated_at", "accessed_at", "valid_until"):
        if k in d and d[k] is not None:
            d[k] = str(d[k])[:19]
    if "id" in d and d["id"] is not None:
        d["id"] = str(d["id"])
    if "parent_id" in d and d["parent_id"] is not None:
        d["parent_id"] = str(d["parent_id"])
    if "tags" in d and d["tags"] is None:
        d["tags"] = []
    return d


@app.get("/api/tree")
def api_tree(path: str = "/", include_extracted: bool = False):
    cols = ("path, slug, type, title, importance, access_count, updated_at, "
            "valid_until, pin_triggers, origin")
    origin_clause = "" if include_extracted else "AND origin = 'curated'"
    with get_db() as conn:
        if path == "/":
            rows = conn.execute(
                f"SELECT {cols} FROM memory_nodes WHERE deleted_at IS NULL {origin_clause} ORDER BY path"
            ).fetchall()
        else:
            rows = conn.execute(
                f"SELECT {cols} FROM memory_nodes "
                f"WHERE (path = %s OR path LIKE %s) AND deleted_at IS NULL {origin_clause} ORDER BY path",
                (path, f"{path}/%"),
            ).fetchall()
    return [_serial(r) for r in rows]


@app.get("/api/node")
def api_node(path: str):
    with get_db() as conn:
        node = conn.execute(
            "SELECT * FROM memory_nodes WHERE path = %s AND deleted_at IS NULL", (path,)
        ).fetchone()
        if not node:
            raise HTTPException(404, f"Node not found: {path}")
        links_out = conn.execute(
            "SELECT ml.rel_type, ml.note, mn.path AS target_path, mn.title AS target_title "
            "FROM memory_links ml JOIN memory_nodes mn ON ml.to_id = mn.id WHERE ml.from_id = %s",
            (node["id"],),
        ).fetchall()
        links_in = conn.execute(
            "SELECT ml.rel_type, mn.path AS source_path, mn.title AS source_title "
            "FROM memory_links ml JOIN memory_nodes mn ON ml.from_id = mn.id WHERE ml.to_id = %s",
            (node["id"],),
        ).fetchall()
        conn.execute(
            "UPDATE memory_nodes SET access_count = access_count + 1, accessed_at = now() WHERE path = %s",
            (path,),
        )
    result = _serial(node)
    result["links_out"] = [dict(l) for l in links_out]
    result["links_in"] = [dict(l) for l in links_in]
    return result


@app.get("/api/search")
def api_search(q: str = "", include_extracted: bool = False):
    if not q.strip():
        return []
    origin_clause = "" if include_extracted else "AND origin = 'curated'"
    with get_db() as conn:
        rows = conn.execute(
            "SELECT path, title, type, importance, origin, "
            "ts_headline('german', coalesce(body,''), plainto_tsquery('german', %s), "
            "'MaxWords=20,MinWords=8,StartSel=«,StopSel=»') AS snippet "
            "FROM memory_nodes "
            "WHERE deleted_at IS NULL "
            "AND to_tsvector('german', coalesce(title,'') || ' ' || coalesce(body,'')) "
            f"@@ plainto_tsquery('german', %s) {origin_clause} "
            "ORDER BY ts_rank("
            "to_tsvector('german', coalesce(title,'') || ' ' || coalesce(body,'')), "
            "plainto_tsquery('german', %s)) DESC LIMIT 30",
            (q, q, q),
        ).fetchall()
        if not rows:
            rows = conn.execute(
                "SELECT path, title, type, importance, origin, "
                "substr(coalesce(body,''), 1, 200) AS snippet "
                f"FROM memory_nodes WHERE deleted_at IS NULL "
                f"AND (title ILIKE %s OR body ILIKE %s) {origin_clause} LIMIT 30",
                (f"%{q}%", f"%{q}%"),
            ).fetchall()
    return [_serial(r) for r in rows]


@app.get("/api/health")
def api_health():
    issues = []
    with get_db() as conn:
        for e in conn.execute(
            "SELECT path, title, valid_until FROM memory_nodes "
            "WHERE deleted_at IS NULL AND valid_until IS NOT NULL AND valid_until < now()"
        ).fetchall():
            issues.append({"kind": "expired", "path": e["path"], "title": e["title"],
                           "detail": f"Abgelaufen {str(e['valid_until'])[:10]}"})
        for o in conn.execute(
            "SELECT m.path, m.title FROM memory_nodes m WHERE m.type = 'category' AND m.deleted_at IS NULL "
            "AND NOT EXISTS (SELECT 1 FROM memory_nodes c WHERE c.parent_id = m.id AND c.deleted_at IS NULL)"
        ).fetchall():
            issues.append({"kind": "empty_category", "path": o["path"], "title": o["title"],
                           "detail": "Kategorie ohne Kinder"})
        for n in conn.execute(
            "SELECT n.path, n.title FROM memory_nodes n WHERE n.type != 'category' AND n.deleted_at IS NULL "
            "AND (n.body IS NULL OR n.body = '') "
            "AND NOT EXISTS (SELECT 1 FROM memory_nodes c WHERE c.parent_id = n.id AND c.deleted_at IS NULL)"
        ).fetchall():
            issues.append({"kind": "empty_node", "path": n["path"], "title": n["title"],
                           "detail": "Kein Inhalt"})
        stats = conn.execute(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN body IS NOT NULL AND body != '' THEN 1 ELSE 0 END) AS with_content, "
            "ROUND(AVG(importance)::numeric, 2) AS avg_importance "
            "FROM memory_nodes WHERE deleted_at IS NULL"
        ).fetchone()
        by_type = conn.execute(
            "SELECT type, COUNT(*) AS c FROM memory_nodes WHERE deleted_at IS NULL GROUP BY type ORDER BY c DESC"
        ).fetchall()
    return {"issues": issues, "stats": dict(stats) if stats else {}, "by_type": [dict(r) for r in by_type]}


@app.get("/api/stats")
def api_stats(days: int = 30, project: str = ""):
    import memory_stats
    with get_db() as conn:
        return memory_stats.collect_stats(conn, days=days, project_slug=project)


@app.get("/api/graph")
def api_graph(scope: str = "", include_extracted: bool = False):
    """Knowledge-Graph als Node/Edge-Liste für die interaktive Visualisierung (/ graph)."""
    from graph_admin import _fetch_link_graph
    with get_db() as conn:
        nodes, links = _fetch_link_graph(conn, scope_path=scope, include_extracted=include_extracted)
    degree: dict = {nid: 0 for nid in nodes}
    for l in links:
        degree[l["from_id"]] = degree.get(l["from_id"], 0) + 1
        degree[l["to_id"]] = degree.get(l["to_id"], 0) + 1
    return {
        "nodes": [
            {"id": str(nid), "path": n["path"], "title": n["title"], "type": n["type"],
             "importance": n["importance"], "degree": degree.get(nid, 0)}
            for nid, n in nodes.items()
        ],
        "edges": [
            {"from": str(l["from_id"]), "to": str(l["to_id"]), "rel_type": l["rel_type"], "origin": l["origin"],
             "confidence": l.get("confidence")}
            for l in links
        ],
    }


def _same_origin_json(request: Request) -> None:
    """Guard for state-changing endpoints: the server only binds to localhost,
    but any website open in the same browser could still POST to it. Browsers
    always send Origin on such requests, and a JSON body can't be sent
    cross-site without a CORS preflight this app never answers."""
    origin = request.headers.get("origin")
    if origin:
        host = request.headers.get("host", "")
        if origin not in (f"http://{host}", f"https://{host}"):
            raise HTTPException(403, "cross-site request blocked")
    if not request.headers.get("content-type", "").startswith("application/json"):
        raise HTTPException(415, "JSON required")


@app.get("/api/suggestions")
def api_suggestions(limit: int = 100, min_confidence: float = 0.0):
    import link_inference
    total, items = link_inference.list_pending(limit, min_confidence)
    return {"pending": total, "items": items,
            "rel_types": list(link_inference.REL_TYPES),
            "thresholds": {"auto": link_inference.AUTO_CONFIDENCE, "suggest": link_inference.SUGGEST_CONFIDENCE}}


@app.post("/api/suggestions/decide")
async def api_suggestions_decide(request: Request):
    import link_inference
    _same_origin_json(request)  # before parsing, so a cross-site form never reaches the body
    try:
        payload = await request.json()
    except ValueError:
        raise HTTPException(400, "invalid JSON")
    if not isinstance(payload, dict):
        raise HTTPException(400, "JSON object expected")
    ids = payload.get("ids") or []
    if not isinstance(ids, list):
        raise HTTPException(400, "ids must be a list")
    try:
        done, missing = await run_in_threadpool(
            link_inference.decide, ids, str(payload.get("decision", "")),
            str(payload.get("rel_type", "related")), str(payload.get("note", "")))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"done": done, "missing": missing}


@app.get("/api/activity")
def api_activity(days: int = 365):
    import memory_stats
    with get_db() as conn:
        return memory_stats.activity_series(conn, days=days)


# ── Frontend ─────────────────────────────────────────────────────────────────

_ASSETS = Path(__file__).resolve().parent / "diary_web_assets"
app.mount("/assets", StaticFiles(directory=_ASSETS), name="assets")

_CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; font-src 'self'; connect-src 'self'; "
        "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


@app.middleware("http")
async def _security_headers(request, call_next):
    response = await call_next(request)
    response.headers["Content-Security-Policy"] = _CSP
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@app.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse((_ASSETS / "index.html").read_text(encoding="utf-8"))


# ── sync endpoint ─────────────────────────────────────────────────────────────

@app.post("/api/sync")
def api_sync(request: Request):
    """Trigger memory sync with remote Postgres (DIARY_REMOTE_URL)."""
    _same_origin_json(request)
    from diary_server import memory_sync
    result = memory_sync()
    return {"message": result}


# ── CLI entry point ───────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="diary-web — localhost memory browser")
    parser.add_argument("--port", type=int, default=8765, help="Port (default: 8765)")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    args = parser.parse_args()

    init_db()

    def _shutdown(sig, frame):
        print("\ndiary-web stopped.")
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    print(f"\n  ✦ diary — observatorium")
    print(f"  http://localhost:{args.port}\n")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
