"""
Memory Browser — localhost-only web UI for the diary-mcp memory tree.

Start:  diary-web [--port 8765]
Stop:   Ctrl-C
"""
from __future__ import annotations

import argparse
import signal
import sys
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse

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


@app.get("/api/graph")
def api_graph(scope: str = "", include_extracted: bool = False):
    """Knowledge-Graph als Node/Edge-Liste für die interaktive Visualisierung (/ graph)."""
    from diary_server import _fetch_link_graph
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
            {"from": str(l["from_id"]), "to": str(l["to_id"]), "rel_type": l["rel_type"], "origin": l["origin"]}
            for l in links
        ],
    }


@app.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse(_HTML)


# ── HTML ─────────────────────────────────────────────────────────────────────

_HTML = r"""<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>/ memory</title>
<style>
  /* ── tokens ── */
  :root {
    --ground:    #091918;
    --surface:   #0f2726;
    --border:    #1c3c3a;
    --text:      #c8dcdc;
    --muted:     #5a7878;
    --accent:    #e8a84c;
    --accent-dim:#7a5520;
    --teal:      #5aabb8;
    --teal-dim:  #20424a;
    --red:       #c05a5a;
    --green:     #5ab880;
    --font-mono: 'Cascadia Code','JetBrains Mono','Fira Code','Menlo',monospace;
    --font-ui:   'Inter','Segoe UI',system-ui,sans-serif;
    --radius:    4px;
  }

  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  body {
    background: var(--ground);
    color: var(--text);
    font-family: var(--font-ui);
    font-size: 14px;
    height: 100vh;
    display: grid;
    grid-template-rows: 48px 1fr;
    overflow: hidden;
  }

  /* ── topbar ── */
  #topbar {
    background: var(--surface);
    border-bottom: 1px solid var(--border);
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 0 16px;
    position: relative;
    z-index: 10;
  }
  #logo {
    font-family: var(--font-mono);
    font-size: 16px;
    color: var(--accent);
    letter-spacing: -0.5px;
    white-space: nowrap;
    user-select: none;
  }
  #logo span { color: var(--muted); }
  #search-wrap {
    flex: 1;
    max-width: 520px;
    position: relative;
  }
  #search {
    width: 100%;
    background: var(--ground);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    color: var(--text);
    font-family: var(--font-mono);
    font-size: 13px;
    padding: 6px 10px 6px 30px;
    outline: none;
    transition: border-color .15s;
  }
  #search:focus { border-color: var(--teal); }
  #search::placeholder { color: var(--muted); }
  #search-icon {
    position: absolute;
    left: 9px;
    top: 50%;
    transform: translateY(-50%);
    color: var(--muted);
    font-size: 13px;
    pointer-events: none;
  }
  #search-results {
    position: absolute;
    top: calc(100% + 4px);
    left: 0; right: 0;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    max-height: 420px;
    overflow-y: auto;
    display: none;
    z-index: 100;
  }
  #search-results.open { display: block; }
  .sr-item {
    padding: 10px 14px;
    cursor: pointer;
    border-bottom: 1px solid var(--border);
    transition: background .1s;
  }
  .sr-item:last-child { border-bottom: none; }
  .sr-item:hover { background: var(--teal-dim); }
  .sr-path { font-family: var(--font-mono); font-size: 11px; color: var(--teal); }
  .sr-title { font-size: 13px; color: var(--text); margin-top: 2px; }
  .sr-snippet { font-size: 12px; color: var(--muted); margin-top: 4px; line-height: 1.5; }

  .topbtn {
    background: none;
    border: 1px solid var(--border);
    border-radius: var(--radius);
    color: var(--muted);
    cursor: pointer;
    font-family: var(--font-mono);
    font-size: 12px;
    padding: 4px 10px;
    transition: color .15s, border-color .15s;
    white-space: nowrap;
  }
  .topbtn:hover { color: var(--accent); border-color: var(--accent-dim); }

  /* ── main layout ── */
  #main {
    display: grid;
    grid-template-columns: 260px 1fr 240px;
    overflow: hidden;
  }

  /* ── tree sidebar ── */
  #sidebar {
    background: var(--surface);
    border-right: 1px solid var(--border);
    overflow-y: auto;
    padding: 12px 0;
  }
  #sidebar::-webkit-scrollbar { width: 4px; }
  #sidebar::-webkit-scrollbar-track { background: transparent; }
  #sidebar::-webkit-scrollbar-thumb { background: var(--border); }

  .tree-root { padding: 0 8px; }

  .tree-node { position: relative; }
  .tree-label {
    display: flex;
    align-items: center;
    gap: 5px;
    padding: 3px 6px;
    border-radius: var(--radius);
    cursor: pointer;
    transition: background .1s;
    user-select: none;
    min-height: 26px;
  }
  .tree-label:hover { background: rgba(90,171,184,.08); }
  .tree-label.active {
    background: rgba(232,168,76,.1);
    color: var(--accent);
  }
  .tree-label.active .tree-slug { color: var(--accent); }

  .tree-toggle {
    width: 14px;
    flex-shrink: 0;
    color: var(--muted);
    font-size: 10px;
    text-align: center;
    transition: transform .15s;
  }
  .tree-toggle.open { transform: rotate(90deg); }
  .tree-toggle.leaf { opacity: 0; pointer-events: none; }

  .tree-dot {
    width: 6px;
    height: 6px;
    border-radius: 50%;
    flex-shrink: 0;
  }

  .tree-slug {
    font-family: var(--font-mono);
    font-size: 12px;
    color: var(--text);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    flex: 1;
  }

  .tree-children {
    padding-left: 14px;
    border-left: 1px solid var(--border);
    margin-left: 19px;
    display: none;
  }
  .tree-children.open { display: block; }

  /* ── content panel ── */
  #content {
    overflow-y: auto;
    padding: 28px 32px;
    display: flex;
    flex-direction: column;
    gap: 20px;
  }
  #content::-webkit-scrollbar { width: 4px; }
  #content::-webkit-scrollbar-track { background: transparent; }
  #content::-webkit-scrollbar-thumb { background: var(--border); }

  #empty-state {
    margin: auto;
    text-align: center;
    color: var(--muted);
  }
  #empty-state .slash {
    font-family: var(--font-mono);
    font-size: 72px;
    color: var(--border);
    line-height: 1;
    margin-bottom: 16px;
    display: block;
  }
  #empty-state p { font-size: 13px; }

  #node-header {}
  #node-breadcrumb {
    font-family: var(--font-mono);
    font-size: 12px;
    color: var(--muted);
    margin-bottom: 8px;
  }
  #node-breadcrumb .bc-sep { color: var(--accent); margin: 0 2px; }
  #node-breadcrumb .bc-part { color: var(--teal); cursor: pointer; }
  #node-breadcrumb .bc-part:hover { color: var(--accent); }

  #node-title {
    font-size: 22px;
    font-weight: 600;
    color: var(--text);
    line-height: 1.3;
    margin-bottom: 12px;
  }

  #node-meta-strip {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    align-items: center;
  }
  .meta-chip {
    font-family: var(--font-mono);
    font-size: 11px;
    padding: 2px 8px;
    border-radius: 99px;
    border: 1px solid var(--border);
    color: var(--muted);
  }
  .meta-chip.type { border-color: var(--teal-dim); color: var(--teal); }
  .meta-chip.expired { border-color: var(--red); color: var(--red); }
  .meta-chip.extracted { border-color: var(--muted); color: var(--muted); font-style: italic; }
  .meta-chip.pin { border-color: var(--accent-dim); color: var(--accent); }

  .importance-bar {
    display: flex;
    gap: 3px;
    align-items: center;
  }
  .imp-dot {
    width: 8px; height: 8px;
    border-radius: 50%;
    background: var(--border);
  }
  .imp-dot.on { background: var(--accent); }

  #node-body {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 20px 24px;
    font-size: 13.5px;
    line-height: 1.75;
    white-space: pre-wrap;
    word-break: break-word;
    color: var(--text);
  }
  #node-body code {
    font-family: var(--font-mono);
    background: var(--ground);
    padding: 1px 5px;
    border-radius: 3px;
    font-size: 12px;
  }

  /* ── right panel ── */
  #panel {
    border-left: 1px solid var(--border);
    overflow-y: auto;
    padding: 16px 14px;
    display: flex;
    flex-direction: column;
    gap: 20px;
    font-size: 12px;
  }
  #panel::-webkit-scrollbar { width: 4px; }
  #panel::-webkit-scrollbar-track { background: transparent; }
  #panel::-webkit-scrollbar-thumb { background: var(--border); }

  .panel-section {}
  .panel-label {
    font-family: var(--font-mono);
    font-size: 10px;
    letter-spacing: .08em;
    text-transform: uppercase;
    color: var(--muted);
    margin-bottom: 8px;
    display: flex;
    align-items: center;
    gap: 6px;
  }
  .panel-label::after { content: ''; flex: 1; height: 1px; background: var(--border); }

  .panel-kv { display: flex; flex-direction: column; gap: 5px; }
  .panel-row { display: flex; justify-content: space-between; gap: 8px; }
  .panel-key { color: var(--muted); }
  .panel-val { font-family: var(--font-mono); font-size: 11px; color: var(--text); text-align: right; word-break: break-all; }

  .link-item {
    padding: 6px 8px;
    border-radius: var(--radius);
    border: 1px solid var(--border);
    cursor: pointer;
    transition: border-color .12s, background .12s;
    margin-bottom: 4px;
  }
  .link-item:hover { border-color: var(--teal); background: var(--teal-dim); }
  .link-rel { font-family: var(--font-mono); font-size: 10px; color: var(--teal); margin-bottom: 2px; }
  .link-path { font-family: var(--font-mono); font-size: 11px; color: var(--muted); }
  .link-title { font-size: 12px; color: var(--text); }

  /* ── overlays ── */
  #overlay {
    position: fixed; inset: 0;
    background: rgba(9,25,24,.85);
    z-index: 200;
    display: none;
    align-items: center;
    justify-content: center;
    backdrop-filter: blur(2px);
  }
  #overlay.open { display: flex; }
  #overlay-box {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 24px 28px;
    max-width: 640px;
    width: 90%;
    max-height: 80vh;
    overflow-y: auto;
  }
  #overlay-title {
    font-family: var(--font-mono);
    font-size: 14px;
    color: var(--accent);
    margin-bottom: 20px;
  }
  .health-issue {
    display: flex;
    gap: 10px;
    padding: 8px 0;
    border-bottom: 1px solid var(--border);
    align-items: flex-start;
  }
  .health-issue:last-child { border-bottom: none; }
  .health-kind {
    font-family: var(--font-mono);
    font-size: 10px;
    padding: 2px 6px;
    border-radius: 99px;
    white-space: nowrap;
    flex-shrink: 0;
  }
  .health-kind.expired { background: rgba(192,90,90,.2); color: var(--red); }
  .health-kind.empty_node { background: rgba(90,120,90,.2); color: var(--muted); }
  .health-kind.empty_category { background: rgba(90,90,120,.2); color: var(--muted); }
  .health-path { font-family: var(--font-mono); font-size: 11px; color: var(--teal); cursor: pointer; }
  .health-path:hover { color: var(--accent); }
  .stat-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 16px; }
  .stat-card {
    background: var(--ground);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 12px 14px;
  }
  .stat-num { font-family: var(--font-mono); font-size: 24px; color: var(--accent); }
  .stat-lbl { font-size: 11px; color: var(--muted); margin-top: 2px; }

  /* ── type colors ── */
  .dot-user      { background: #7ab3c5; }
  .dot-feedback  { background: #e8a84c; }
  .dot-project   { background: #5ab880; }
  .dot-reference { background: #9b7ac5; }
  .dot-note      { background: #5a7878; }
  .dot-category  { background: #3a5858; }

  /* ── knowledge graph overlay ── */
  #graph-overlay {
    position: fixed;
    top: 48px; left: 0; right: 0; bottom: 0;
    background: var(--ground);
    z-index: 150;
    display: none;
    flex-direction: column;
  }
  #graph-overlay.open { display: flex; }
  #graph-toolbar {
    display: flex;
    align-items: center;
    gap: 16px;
    padding: 10px 16px;
    border-bottom: 1px solid var(--border);
    background: var(--surface);
    font-family: var(--font-mono);
    font-size: 12px;
    color: var(--muted);
  }
  #graph-toolbar label { display: flex; align-items: center; gap: 5px; cursor: pointer; white-space: nowrap; }
  #graph-title { color: var(--accent); flex: 1; }
  #graph-canvas { flex: 1; width: 100%; cursor: grab; display: block; }
  #graph-empty {
    position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%);
    color: var(--muted); font-size: 13px; display: none;
  }
  #graph-legend {
    position: absolute;
    bottom: 12px; left: 16px;
    display: flex;
    gap: 12px;
    flex-wrap: wrap;
    max-width: calc(100% - 32px);
    font-size: 10px;
    color: var(--muted);
    pointer-events: none;
  }
  .legend-dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 4px; vertical-align: middle; }

  @media (prefers-reduced-motion: reduce) {
    *, *::before, *::after { transition: none !important; }
  }
</style>

<div id="topbar">
  <div id="logo">/ <span>memory</span></div>
  <div id="search-wrap">
    <span id="search-icon">⌕</span>
    <input id="search" type="text" placeholder="Search memories…" autocomplete="off" spellcheck="false">
    <div id="search-results"></div>
  </div>
  <button class="topbtn" onclick="showGraph()">graph</button>
  <button class="topbtn" onclick="showHealth()">health</button>
  <button class="topbtn" id="sync-btn" onclick="runSync()">sync</button>
</div>

<div id="main">
  <nav id="sidebar"><div class="tree-root" id="tree-root"></div></nav>

  <main id="content">
    <div id="empty-state">
      <span class="slash">/</span>
      <p>Select a memory node from the tree.</p>
    </div>
    <div id="node-view" style="display:none">
      <div id="node-header">
        <div id="node-breadcrumb"></div>
        <div id="node-title"></div>
        <div id="node-meta-strip"></div>
      </div>
      <div id="node-body"></div>
    </div>
  </main>

  <aside id="panel">
    <div class="panel-section" id="panel-meta" style="display:none">
      <div class="panel-label">metadata</div>
      <div class="panel-kv" id="panel-kv"></div>
    </div>
    <div class="panel-section" id="panel-links-out" style="display:none">
      <div class="panel-label">outgoing links</div>
      <div id="links-out-list"></div>
    </div>
    <div class="panel-section" id="panel-links-in" style="display:none">
      <div class="panel-label">referenced by</div>
      <div id="links-in-list"></div>
    </div>
    <div class="panel-section" id="panel-empty">
      <div class="panel-label">tree</div>
      <div id="tree-stats" style="color:var(--muted);font-size:12px;line-height:1.8"></div>
    </div>
  </aside>
</div>

<div id="overlay">
  <div id="overlay-box">
    <div id="overlay-title"></div>
    <div id="overlay-content"></div>
  </div>
</div>

<div id="graph-overlay">
  <div id="graph-toolbar">
    <span id="graph-title">/ knowledge graph</span>
    <label><input type="checkbox" id="graph-extracted"> include extracted</label>
    <button class="topbtn" onclick="closeGraph()">close ✕</button>
  </div>
  <div style="position:relative;flex:1;overflow:hidden">
    <canvas id="graph-canvas"></canvas>
    <div id="graph-empty">Keine Links im Graphen — memory_link() oder memory_infer_links() nutzen.</div>
    <div id="graph-legend">
      <span><span class="legend-dot" style="background:#7ab3c5"></span>user</span>
      <span><span class="legend-dot" style="background:#e8a84c"></span>feedback</span>
      <span><span class="legend-dot" style="background:#5ab880"></span>project</span>
      <span><span class="legend-dot" style="background:#9b7ac5"></span>reference</span>
      <span><span class="legend-dot" style="background:#5a7878"></span>note</span>
      <span style="margin-left:4px">— explicit&nbsp;&nbsp;┄┄ inferred&nbsp;&nbsp;drag = move, wheel = zoom, click = open</span>
    </div>
  </div>
</div>

<script>
// ── state ──────────────────────────────────────────────────────────────────
let allNodes = [];
let activeEl = null;

// ── bootstrap ─────────────────────────────────────────────────────────────
async function init() {
  const [tree, health] = await Promise.all([
    fetch('/api/tree').then(r => r.json()),
    fetch('/api/health').then(r => r.json()),
  ]);
  allNodes = tree;
  renderTree(tree);
  renderStats(health.stats, health.by_type);

  // close overlay on click outside
  document.getElementById('overlay').addEventListener('click', e => {
    if (e.target.id === 'overlay') closeOverlay();
  });
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') { closeOverlay(); closeGraph(); }
    if ((e.metaKey || e.ctrlKey) && e.key === 'k') {
      e.preventDefault();
      document.getElementById('search').focus();
    }
  });
  document.getElementById('graph-extracted').addEventListener('change', () => {
    if (document.getElementById('graph-overlay').classList.contains('open')) showGraph();
  });
}

// ── tree render ────────────────────────────────────────────────────────────
function buildTree(nodes) {
  const map = {};
  const roots = [];
  for (const n of nodes) {
    map[n.path] = { ...n, children: [] };
  }
  for (const n of nodes) {
    const parts = n.path.replace(/^\//, '').split('/');
    if (parts.length === 1) {
      roots.push(map[n.path]);
    } else {
      const parentPath = '/' + parts.slice(0, -1).join('/');
      if (map[parentPath]) {
        map[parentPath].children.push(map[n.path]);
      } else {
        roots.push(map[n.path]);
      }
    }
  }
  return roots;
}

function renderTree(nodes) {
  const root = document.getElementById('tree-root');
  root.innerHTML = '';
  const tree = buildTree(nodes);
  for (const node of tree) {
    root.appendChild(makeTreeNode(node, 0));
  }
}

function makeTreeNode(node, depth) {
  const wrap = document.createElement('div');
  wrap.className = 'tree-node';
  wrap.dataset.path = node.path;

  const label = document.createElement('div');
  label.className = 'tree-label';

  const toggle = document.createElement('span');
  toggle.className = 'tree-toggle' + (node.children.length ? '' : ' leaf');
  toggle.textContent = '▶';

  const dot = document.createElement('span');
  dot.className = `tree-dot dot-${node.type}`;

  const slug = document.createElement('span');
  slug.className = 'tree-slug';
  slug.textContent = node.slug;
  slug.title = node.title;

  label.append(toggle, dot, slug);
  wrap.appendChild(label);

  const children = document.createElement('div');
  children.className = 'tree-children';
  for (const child of node.children) {
    children.appendChild(makeTreeNode(child, depth + 1));
  }
  wrap.appendChild(children);

  label.addEventListener('click', e => {
    e.stopPropagation();
    if (node.children.length) {
      const open = children.classList.toggle('open');
      toggle.classList.toggle('open', open);
    }
    selectNode(node.path, label);
  });

  return wrap;
}

function selectNode(path, labelEl) {
  if (activeEl) activeEl.classList.remove('active');
  if (labelEl) { labelEl.classList.add('active'); activeEl = labelEl; }
  loadNode(path);
}

// ── node view ─────────────────────────────────────────────────────────────
async function loadNode(path) {
  const data = await fetch('/api/node?path=' + encodeURIComponent(path)).then(r => r.json());
  showNode(data);
}

function showNode(node) {
  document.getElementById('empty-state').style.display = 'none';
  document.getElementById('node-view').style.display = '';

  // breadcrumb
  const parts = node.path.replace(/^\//, '').split('/');
  const bc = document.getElementById('node-breadcrumb');
  bc.innerHTML = parts.map((p, i) => {
    const partPath = '/' + parts.slice(0, i + 1).join('/');
    return `<span class="bc-part" onclick="navigateTo('${partPath}')">${p}</span>`;
  }).join('<span class="bc-sep">/</span>');

  document.getElementById('node-title').textContent = node.title;

  // meta strip
  const strip = document.getElementById('node-meta-strip');
  const expired = node.valid_until && new Date(node.valid_until) < new Date();
  strip.innerHTML = `
    <span class="meta-chip type">${node.type}</span>
    ${node.origin === 'extracted' ? `<span class="meta-chip extracted">auto-extrahiert</span>` : ''}
    ${node.pin_triggers && node.pin_triggers.length ? `<span class="meta-chip pin">pin: ${node.pin_triggers.join(', ')}</span>` : ''}
    ${expired ? `<span class="meta-chip expired">⚠ abgelaufen</span>` : ''}
    ${node.tags && node.tags.length ? `<span class="meta-chip">${node.tags.join(', ')}</span>` : ''}
    <div class="importance-bar">${[1,2,3,4,5].map(i =>
      `<span class="imp-dot${(node.importance || 0) * 5 >= i ? ' on' : ''}"></span>`
    ).join('')}</div>
  `;

  // body
  const bodyEl = document.getElementById('node-body');
  bodyEl.textContent = node.body || '(kein Inhalt)';
  if (!node.body) bodyEl.style.color = 'var(--muted)';
  else bodyEl.style.color = '';

  // right panel — metadata
  const kv = document.getElementById('panel-kv');
  kv.innerHTML = [
    ['Pfad', node.path],
    ['Geändert', node.updated_at ? node.updated_at.slice(0, 10) : '—'],
    ['Erstellt', node.created_at ? node.created_at.slice(0, 10) : '—'],
    ['Gültig bis', node.valid_until ? node.valid_until.slice(0, 10) : '—'],
    ['Zugriffe', node.access_count ?? 0],
    ['Wichtigkeit', node.importance != null ? (node.importance * 100).toFixed(0) + '%' : '—'],
  ].map(([k, v]) => `
    <div class="panel-row">
      <span class="panel-key">${k}</span>
      <span class="panel-val">${v}</span>
    </div>
  `).join('');
  document.getElementById('panel-meta').style.display = '';
  document.getElementById('panel-empty').style.display = 'none';

  // links out
  const loList = document.getElementById('links-out-list');
  loList.innerHTML = '';
  if (node.links_out && node.links_out.length) {
    for (const l of node.links_out) {
      loList.appendChild(makeLinkItem(l.rel_type, l.target_path, l.target_title, l.note));
    }
    document.getElementById('panel-links-out').style.display = '';
  } else {
    document.getElementById('panel-links-out').style.display = 'none';
  }

  // links in
  const liList = document.getElementById('links-in-list');
  liList.innerHTML = '';
  if (node.links_in && node.links_in.length) {
    for (const l of node.links_in) {
      liList.appendChild(makeLinkItem(l.rel_type, l.source_path, l.source_title, null));
    }
    document.getElementById('panel-links-in').style.display = '';
  } else {
    document.getElementById('panel-links-in').style.display = 'none';
  }
}

function makeLinkItem(rel, path, title, note) {
  const el = document.createElement('div');
  el.className = 'link-item';
  el.innerHTML = `
    <div class="link-rel">${rel}</div>
    <div class="link-path">${path}</div>
    <div class="link-title">${title}</div>
    ${note ? `<div style="font-size:11px;color:var(--muted);margin-top:2px">${note}</div>` : ''}
  `;
  el.addEventListener('click', () => navigateTo(path));
  return el;
}

function navigateTo(path) {
  // Find and activate the tree label for this path
  const nodeEl = document.querySelector(`.tree-node[data-path="${CSS.escape(path)}"]`);
  if (nodeEl) {
    const label = nodeEl.querySelector('.tree-label');
    // expand all parents
    let parent = nodeEl.parentElement;
    while (parent) {
      if (parent.classList.contains('tree-children')) {
        parent.classList.add('open');
        const toggle = parent.previousSibling?.querySelector?.('.tree-toggle');
        if (toggle) toggle.classList.add('open');
      }
      parent = parent.parentElement;
    }
    selectNode(path, label);
    label.scrollIntoView({ block: 'nearest' });
  } else {
    loadNode(path);
  }
}

// ── search ────────────────────────────────────────────────────────────────
let searchTimer;
document.getElementById('search').addEventListener('input', e => {
  clearTimeout(searchTimer);
  const q = e.target.value.trim();
  if (!q) { closeSearch(); return; }
  searchTimer = setTimeout(() => doSearch(q), 250);
});
document.getElementById('search').addEventListener('focus', () => {
  if (document.getElementById('search-results').innerHTML) {
    document.getElementById('search-results').classList.add('open');
  }
});
document.addEventListener('click', e => {
  if (!e.target.closest('#search-wrap')) closeSearch();
});

async function doSearch(q) {
  const res = await fetch('/api/search?q=' + encodeURIComponent(q)).then(r => r.json());
  const el = document.getElementById('search-results');
  if (!res.length) {
    el.innerHTML = '<div class="sr-item" style="color:var(--muted)">Keine Ergebnisse.</div>';
    el.classList.add('open');
    return;
  }
  el.innerHTML = res.map(r => `
    <div class="sr-item" data-path="${r.path}">
      <div class="sr-path">${r.path}</div>
      <div class="sr-title">${r.title}</div>
      ${r.snippet ? `<div class="sr-snippet">${r.snippet}</div>` : ''}
    </div>
  `).join('');
  el.classList.add('open');
  el.querySelectorAll('.sr-item[data-path]').forEach(item => {
    item.addEventListener('click', () => {
      closeSearch();
      navigateTo(item.dataset.path);
    });
  });
}

function closeSearch() {
  document.getElementById('search').value = '';
  document.getElementById('search-results').classList.remove('open');
  document.getElementById('search-results').innerHTML = '';
}

// ── health overlay ────────────────────────────────────────────────────────
async function showHealth() {
  const data = await fetch('/api/health').then(r => r.json());
  document.getElementById('overlay-title').textContent = '/ health check';
  const stats = data.stats || {};
  const byType = data.by_type || [];
  const issues = data.issues || [];

  let html = `<div class="stat-grid">
    <div class="stat-card"><div class="stat-num">${stats.total || 0}</div><div class="stat-lbl">Nodes total</div></div>
    <div class="stat-card"><div class="stat-num">${stats.with_content || 0}</div><div class="stat-lbl">mit Inhalt</div></div>
    <div class="stat-card"><div class="stat-num">${issues.length}</div><div class="stat-lbl">Issues</div></div>
    <div class="stat-card"><div class="stat-num">${stats.avg_importance || '—'}</div><div class="stat-lbl">ø Wichtigkeit</div></div>
  </div>`;

  if (byType.length) {
    html += `<div class="panel-label" style="margin-bottom:12px">nach Typ</div>`;
    html += byType.map(t => `
      <div style="display:flex;justify-content:space-between;padding:4px 0;border-bottom:1px solid var(--border);font-size:12px">
        <span style="font-family:var(--font-mono);color:var(--teal)">${t.type}</span>
        <span style="color:var(--muted)">${t.c}</span>
      </div>
    `).join('');
    html += '<br>';
  }

  if (!issues.length) {
    html += `<p style="color:var(--green);font-size:13px">✓ Keine Issues gefunden.</p>`;
  } else {
    html += `<div class="panel-label">issues (${issues.length})</div>`;
    html += issues.map(i => `
      <div class="health-issue">
        <span class="health-kind ${i.kind}">${i.kind}</span>
        <div>
          <div class="health-path" onclick="closeOverlay();navigateTo('${i.path}')">${i.path}</div>
          <div style="font-size:11px;color:var(--muted)">${i.detail}</div>
        </div>
      </div>
    `).join('');
  }

  document.getElementById('overlay-content').innerHTML = html;
  document.getElementById('overlay').classList.add('open');
}

// ── sync ─────────────────────────────────────────────────────────────────
async function runSync() {
  const btn = document.getElementById('sync-btn');
  btn.textContent = 'syncing…';
  btn.disabled = true;
  try {
    const res = await fetch('/api/sync', { method: 'POST' });
    const data = await res.json();
    showMessage(data.message || 'Sync done.');
  } catch (e) {
    showMessage('Sync fehlgeschlagen: ' + e.message);
  }
  btn.textContent = 'sync';
  btn.disabled = false;
}

function showMessage(msg) {
  document.getElementById('overlay-title').textContent = '/ sync';
  document.getElementById('overlay-content').innerHTML =
    `<p style="font-size:13px;color:var(--text);line-height:1.8;white-space:pre-wrap">${msg}</p>`;
  document.getElementById('overlay').classList.add('open');
}

function closeOverlay() {
  document.getElementById('overlay').classList.remove('open');
}

// ── stats sidebar ─────────────────────────────────────────────────────────
function renderStats(stats, byType) {
  const el = document.getElementById('tree-stats');
  if (!stats) return;
  el.innerHTML = `
    <div>${stats.total || 0} nodes</div>
    <div>${stats.with_content || 0} mit Inhalt</div>
    ${(byType || []).map(t =>
      `<div><span style="font-family:var(--font-mono);color:var(--teal)">${t.type}</span> — ${t.c}</div>`
    ).join('')}
  `;
}

// ── knowledge graph view ─────────────────────────────────────────────────
const TYPE_COLOR = {
  user: '#7ab3c5', feedback: '#e8a84c', project: '#5ab880',
  reference: '#9b7ac5', note: '#5a7878', category: '#3a5858',
};
let graphState = null;
let graphAnimHandle = null;
let graphBound = false;

async function showGraph() {
  document.getElementById('graph-overlay').classList.add('open');
  const includeExtracted = document.getElementById('graph-extracted').checked;
  const data = await fetch('/api/graph?include_extracted=' + includeExtracted).then(r => r.json());
  // User may have clicked "close" while the fetch was in flight — don't start a
  // physics loop for a view that's no longer open (it would run forever, since
  // closeGraph() already ran and can't cancel an animation frame that didn't exist yet).
  if (!document.getElementById('graph-overlay').classList.contains('open')) return;
  initGraph(data);
}

function closeGraph() {
  document.getElementById('graph-overlay').classList.remove('open');
  if (graphAnimHandle) cancelAnimationFrame(graphAnimHandle);
  graphAnimHandle = null;
  graphState = null;
}

function initGraph(data) {
  const canvas = document.getElementById('graph-canvas');
  document.getElementById('graph-empty').style.display = data.edges.length ? 'none' : '';
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.parentElement.getBoundingClientRect();
  canvas.width = rect.width * dpr;
  canvas.height = rect.height * dpr;
  canvas.style.width = rect.width + 'px';
  canvas.style.height = rect.height + 'px';
  const ctx = canvas.getContext('2d');
  ctx.scale(dpr, dpr);
  const W = rect.width, H = rect.height;

  const idIndex = {};
  const nodes = data.nodes.map((n, i) => {
    idIndex[n.id] = i;
    const angle = (i / Math.max(data.nodes.length, 1)) * Math.PI * 2;
    const radius = 60 + Math.random() * 120;
    return {
      ...n,
      x: W / 2 + Math.cos(angle) * radius, y: H / 2 + Math.sin(angle) * radius,
      vx: 0, vy: 0, r: Math.min(18, 5 + Math.sqrt(n.degree || 0) * 2.5),
    };
  });
  const edges = data.edges
    .map(e => ({ ...e, a: idIndex[e.from], b: idIndex[e.to] }))
    .filter(e => e.a !== undefined && e.b !== undefined);

  if (graphAnimHandle) cancelAnimationFrame(graphAnimHandle);
  graphState = { ctx, W, H, nodes, edges, view: { x: 0, y: 0, scale: 1 } };
  if (!graphBound) { bindGraphInteraction(canvas); graphBound = true; }
  runGraphSim();
}

function runGraphSim() {
  const step = () => {
    if (!graphState) return;
    simTick(graphState.nodes, graphState.edges, graphState.W, graphState.H);
    drawGraph(graphState);
    graphAnimHandle = requestAnimationFrame(step);
  };
  graphAnimHandle = requestAnimationFrame(step);
}

function simTick(nodes, edges, W, H) {
  const REPEL = 2200, SPRING = 0.02, SPRING_LEN = 90, CENTER = 0.002, DAMP = 0.82, MAX_SPEED = 40;
  for (const n of nodes) {
    n.fx = n.fixed ? 0 : (W / 2 - n.x) * CENTER;
    n.fy = n.fixed ? 0 : (H / 2 - n.y) * CENTER;
  }
  for (let i = 0; i < nodes.length; i++) {
    for (let j = i + 1; j < nodes.length; j++) {
      const a = nodes[i], b = nodes[j];
      const dx = a.x - b.x, dy = a.y - b.y;
      const d2 = Math.max(dx * dx + dy * dy, 0.01);
      const f = REPEL / d2;
      const d = Math.sqrt(d2);
      const fx = (dx / d) * f, fy = (dy / d) * f;
      if (!a.fixed) { a.fx += fx; a.fy += fy; }
      if (!b.fixed) { b.fx -= fx; b.fy -= fy; }
    }
  }
  for (const e of edges) {
    const a = nodes[e.a], b = nodes[e.b];
    const dx = b.x - a.x, dy = b.y - a.y;
    const d = Math.sqrt(dx * dx + dy * dy) || 0.01;
    const f = (d - SPRING_LEN) * SPRING;
    const fx = (dx / d) * f, fy = (dy / d) * f;
    if (!a.fixed) { a.fx += fx; a.fy += fy; }
    if (!b.fixed) { b.fx -= fx; b.fy -= fy; }
  }
  for (const n of nodes) {
    if (n.fixed) { n.vx = 0; n.vy = 0; continue; }
    n.vx = (n.vx + n.fx) * DAMP;
    n.vy = (n.vy + n.fy) * DAMP;
    // Clamp speed — without this, two nodes spawning near-overlapping (initGraph
    // places them at random radii around the same center) get a huge one-frame
    // repulsion kick and fling off-canvas instead of settling into the layout.
    const speed = Math.sqrt(n.vx * n.vx + n.vy * n.vy);
    if (speed > MAX_SPEED) {
      n.vx = (n.vx / speed) * MAX_SPEED;
      n.vy = (n.vy / speed) * MAX_SPEED;
    }
    n.x += n.vx;
    n.y += n.vy;
  }
}

function drawGraph(gs) {
  const { ctx, W, H, nodes, edges, view } = gs;
  ctx.clearRect(0, 0, W, H);
  ctx.save();
  ctx.translate(view.x, view.y);
  ctx.scale(view.scale, view.scale);

  ctx.lineWidth = 1;
  for (const e of edges) {
    const a = nodes[e.a], b = nodes[e.b];
    ctx.beginPath();
    ctx.moveTo(a.x, a.y);
    ctx.lineTo(b.x, b.y);
    ctx.strokeStyle = e.rel_type === 'contradicts' ? 'rgba(192,90,90,.55)' : 'rgba(90,171,184,.35)';
    ctx.setLineDash(e.origin === 'inferred' ? [3, 3] : []);
    ctx.stroke();
  }
  ctx.setLineDash([]);

  for (const n of nodes) {
    ctx.beginPath();
    ctx.arc(n.x, n.y, n.r, 0, Math.PI * 2);
    ctx.fillStyle = TYPE_COLOR[n.type] || '#5a7878';
    ctx.fill();
    if (n.degree === 0) {
      ctx.strokeStyle = 'rgba(200,220,220,.3)';
      ctx.lineWidth = 1;
      ctx.stroke();
    }
    if (view.scale > 0.45) {
      ctx.fillStyle = 'rgba(200,220,220,.8)';
      ctx.font = '9px monospace';
      ctx.fillText(n.path.split('/').pop(), n.x + n.r + 3, n.y + 3);
    }
  }
  ctx.restore();
}

function bindGraphInteraction(canvas) {
  let dragging = null, panning = false, last = null, moved = false;

  function toWorld(px, py) {
    const v = graphState.view;
    return { x: (px - v.x) / v.scale, y: (py - v.y) / v.scale };
  }
  function nodeAt(px, py) {
    const w = toWorld(px, py);
    const nodes = graphState.nodes;
    for (let i = nodes.length - 1; i >= 0; i--) {
      const n = nodes[i];
      if ((n.x - w.x) ** 2 + (n.y - w.y) ** 2 <= (n.r + 3) ** 2) return n;
    }
    return null;
  }

  canvas.addEventListener('mousedown', e => {
    if (!graphState) return;
    const rect = canvas.getBoundingClientRect();
    const n = nodeAt(e.clientX - rect.left, e.clientY - rect.top);
    moved = false;
    last = { x: e.clientX, y: e.clientY };
    if (n) { dragging = n; n.fixed = true; } else { panning = true; }
  });
  canvas.addEventListener('mousemove', e => {
    if (!last || !graphState) return;
    const dx = e.clientX - last.x, dy = e.clientY - last.y;
    if (Math.abs(dx) > 2 || Math.abs(dy) > 2) moved = true;
    last = { x: e.clientX, y: e.clientY };
    const v = graphState.view;
    if (dragging) {
      dragging.x += dx / v.scale;
      dragging.y += dy / v.scale;
    } else if (panning) {
      v.x += dx;
      v.y += dy;
    }
  });
  canvas.addEventListener('mouseup', () => {
    if (dragging) {
      if (!moved) { closeGraph(); navigateTo(dragging.path); return; }
      dragging.fixed = false;
    }
    dragging = null; panning = false; last = null;
  });
  canvas.addEventListener('mouseleave', () => {
    if (dragging) dragging.fixed = false;
    dragging = null; panning = false; last = null;
  });
  canvas.addEventListener('wheel', e => {
    if (!graphState) return;
    e.preventDefault();
    const rect = canvas.getBoundingClientRect();
    const px = e.clientX - rect.left, py = e.clientY - rect.top;
    const v = graphState.view;
    const before = toWorld(px, py);
    v.scale = Math.max(0.2, Math.min(4, v.scale * (e.deltaY < 0 ? 1.1 : 0.9)));
    v.x = px - before.x * v.scale;
    v.y = py - before.y * v.scale;
  }, { passive: false });
}

init();
</script>
"""


# ── sync endpoint ─────────────────────────────────────────────────────────────

@app.post("/api/sync")
def api_sync():
    """Trigger memory sync with remote Postgres (DIARY_REMOTE_URL)."""
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

    print(f"\n  / memory browser")
    print(f"  http://localhost:{args.port}\n")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
