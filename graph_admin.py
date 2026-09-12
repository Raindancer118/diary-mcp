"""
Knowledge-graph INTROSPECTION/MAINTENANCE tools (graphify-inspired): explain,
path, stats, infer, report, query. Registered on the separate diary-admin-mcp
server (diary_admin_server.py / admin_mcp), NOT on the main diary-mcp server.

Why split out (2026-08 architecture review): with 515 memory nodes and a 44%
orphan rate, these 6 tools were mostly unused by Claude during normal work —
they're audit/maintenance operations a human asks for explicitly ("show me
clusters", "clean up the graph"), not something an agent reaches for mid-task.
Keeping them on the main server's tool surface only adds tool-selection noise
for every ordinary session. memory_link/memory_get_links (actually creating and
reading links — a normal, frequent operation) stayed on the main server; see
graph_core.py.

These are still plain importable functions (not hidden), so the existing pytest
suite keeps calling them directly and diary_server.py re-exports them for
backward-compatible direct access — only their MCP *tool* registration moved.
To use them from Claude, add the `diary-admin-mcp` entry point to .mcp.json
(README.md) for the duration of an audit, not as a standing tool.
"""
import diary_embed
from diary_admin_bootstrap import admin_mcp
import diary_db
from memory_service import _pgvector_ready
from search_engine import _apply_ranking


def _escape_like(s: str) -> str:
    """Escape %, _ and \\ so a path is matched literally as a LIKE prefix, not as a
    wildcard pattern (a path containing '_' or '%' — e.g. '/projects/my_app' — would
    otherwise silently match unrelated sibling paths)."""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _fetch_link_graph(conn, scope_path: str = "", include_extracted: bool = False):
    """Lädt alle (nicht-tombstonten) Nodes + Links, optional unter scope_path gefiltert.

    Returns (nodes: {id: row}, links: [row]) — row dicts enthalten path/title/type/
    importance für Nodes bzw. from_id/to_id/rel_type/origin für Links.
    """
    origin_clause = "" if include_extracted else "AND origin = 'curated'"
    scope_clause = ""
    params: list = []
    if scope_path:
        scope_clause = "AND (path = %s OR path LIKE %s)"
        params = [scope_path, f"{_escape_like(scope_path)}/%"]
    node_rows = conn.execute(
        f"SELECT id, path, title, type, importance, accessed_at FROM memory_nodes "
        f"WHERE deleted_at IS NULL {origin_clause} {scope_clause}",
        params,
    ).fetchall()
    nodes = {r["id"]: r for r in node_rows}
    if not nodes:
        return nodes, []
    link_rows = conn.execute(
        "SELECT from_id, to_id, rel_type, link_origin AS origin FROM memory_links "
        "WHERE from_id = ANY(%s) AND to_id = ANY(%s)",
        (list(nodes.keys()), list(nodes.keys())),
    ).fetchall()
    return nodes, link_rows


def _connected_components(nodes: dict, links: list) -> list[list]:
    """Connected components (ungerichtet) über den Link-Graphen — 'Cluster' ohne Leiden/igraph."""
    adj: dict = {nid: set() for nid in nodes}
    for l in links:
        if l["from_id"] in adj and l["to_id"] in adj:
            adj[l["from_id"]].add(l["to_id"])
            adj[l["to_id"]].add(l["from_id"])
    seen: set = set()
    components = []
    for start in nodes:
        if start in seen:
            continue
        stack = [start]
        comp = []
        seen.add(start)
        while stack:
            cur = stack.pop()
            comp.append(cur)
            for nb in adj[cur]:
                if nb not in seen:
                    seen.add(nb)
                    stack.append(nb)
        components.append(comp)
    return components


@admin_mcp.tool()
def memory_infer_links(scope_path: str = "", threshold: float = 0.82, max_new: int = 20) -> str:
    """Schlägt automatisch Links zwischen semantisch ähnlichen Memories vor (graphify-Stil: INFERRED-Edges).

    Vergleicht paarweise Embeddings (Cosine-Similarity, brute-force) aller kuratierten
    Nodes unter scope_path (leer = ganzer Baum) und legt für Paare oberhalb von
    `threshold` einen Link rel_type='related', origin='inferred' an — NUR wenn
    zwischen den beiden Nodes noch kein Link (in irgendeine Richtung/rel_type) existiert.
    Bestehende Links (egal ob explicit oder inferred) werden nie verändert oder dupliziert.
    max_new begrenzt die pro Aufruf neu angelegten Links (Sicherheitsnetz gegen Graph-Spam).
    """
    if not 0.0 < threshold <= 1.0:
        return "Fehler: threshold muss zwischen 0.0 (exklusiv) und 1.0 liegen."
    with diary_db.get_db() as conn:
        rows = conn.execute(
            f"""SELECT id, path, title, embedding FROM memory_nodes
                WHERE deleted_at IS NULL AND origin = 'curated' AND embedding IS NOT NULL
                {"AND (path = %s OR path LIKE %s)" if scope_path else ""}""",
            ([scope_path, f"{_escape_like(scope_path)}/%"] if scope_path else []),
        ).fetchall()
        if len(rows) < 2:
            return "Zu wenige embedded Nodes im angegebenen Bereich (mind. 2 nötig; memory_reembed_all prüfen)."

        existing = conn.execute(
            "SELECT from_id, to_id FROM memory_links WHERE from_id = ANY(%s) AND to_id = ANY(%s)",
            ([r["id"] for r in rows], [r["id"] for r in rows]),
        ).fetchall()
        linked_pairs = {frozenset((e["from_id"], e["to_id"])) for e in existing}

        # Normalize every embedding once up front so the O(n^2) inner loop is a
        # plain dot product instead of a full cosine (which would recompute each
        # vector's norm on every one of its n-1 comparisons).
        unit_vecs = [diary_embed.normalize(r["embedding"]) for r in rows]

        candidates = []
        for i in range(len(rows)):
            for j in range(i + 1, len(rows)):
                a, b = rows[i], rows[j]
                if frozenset((a["id"], b["id"])) in linked_pairs:
                    continue
                sim = diary_embed.dot(unit_vecs[i], unit_vecs[j])
                if sim >= threshold:
                    candidates.append((sim, a, b))
        candidates.sort(key=lambda c: c[0], reverse=True)
        candidates = candidates[:max_new]

        created = []
        for sim, a, b in candidates:
            # RETURNING id is NULL/absent when ON CONFLICT DO NOTHING actually no-ops
            # (e.g. a concurrent memory_infer_links call inserted this exact pair first) —
            # only report pairs that were genuinely inserted just now, not races we lost.
            inserted = conn.execute(
                """INSERT INTO memory_links (from_id, to_id, rel_type, link_origin)
                   VALUES (%s, %s, 'related', 'inferred')
                   ON CONFLICT (from_id, to_id, rel_type) DO NOTHING
                   RETURNING id""",
                (a["id"], b["id"]),
            ).fetchone()
            if inserted:
                created.append(f"  [INFERRED {sim:.3f}] {a['path']} ↔ {b['path']}")

    if not created:
        return f"Keine neuen inferierten Links gefunden (threshold={threshold}, {len(rows)} Nodes verglichen)."
    return (f"{len(created)} inferierte Link(s) angelegt (threshold={threshold}, "
            f"{len(rows)} Nodes verglichen):\n" + "\n".join(created))


@admin_mcp.tool()
def memory_explain(path: str) -> str:
    """Erklärt einen Memory-Node: Degree, alle Verknüpfungen (explicit/inferred) — wie graphify's `explain`."""
    with diary_db.get_db() as conn:
        node = conn.execute(
            "SELECT id, title, type, importance FROM memory_nodes WHERE path = %s AND deleted_at IS NULL",
            (path,),
        ).fetchone()
        if not node:
            return f"Node '{path}' nicht gefunden."
        out_rows = conn.execute(
            "SELECT ml.rel_type, ml.link_origin AS origin, mn.path, mn.title FROM memory_links ml "
            "JOIN memory_nodes mn ON ml.to_id = mn.id WHERE ml.from_id = %s",
            (node["id"],),
        ).fetchall()
        in_rows = conn.execute(
            "SELECT ml.rel_type, ml.link_origin AS origin, mn.path, mn.title FROM memory_links ml "
            "JOIN memory_nodes mn ON ml.from_id = mn.id WHERE ml.to_id = %s",
            (node["id"],),
        ).fetchall()

    degree = len(out_rows) + len(in_rows)
    lines = [
        f"Node: {path}",
        f"  Titel:       {node['title']}",
        f"  Typ:         {node['type']}",
        f"  Wichtigkeit: {node['importance']}",
        f"  Degree:      {degree} ({len(out_rows)} ausgehend, {len(in_rows)} eingehend)",
        "",
        f"Verknüpfungen ({degree}):",
    ]
    for r in out_rows:
        tag = "EXPLICIT" if r["origin"] == "explicit" else "INFERRED"
        lines.append(f"  --{r['rel_type']}--> {r['path']} ({r['title']}) [{tag}]")
    for r in in_rows:
        tag = "EXPLICIT" if r["origin"] == "explicit" else "INFERRED"
        lines.append(f"  <--{r['rel_type']}-- {r['path']} ({r['title']}) [{tag}]")
    if degree == 0:
        lines.append("  (keine — Waisen-Node im Knowledge-Graph)")
    return "\n".join(lines)


@admin_mcp.tool()
def memory_path(from_path: str, to_path: str, max_hops: int = 6) -> str:
    """Findet den kürzesten Pfad zwischen zwei Memory-Nodes im Knowledge-Graph (BFS, ungerichtet) — wie graphify's `path`."""
    with diary_db.get_db() as conn:
        endpoints = conn.execute(
            "SELECT path, id FROM memory_nodes WHERE path = ANY(%s) AND deleted_at IS NULL",
            ([from_path, to_path],),
        ).fetchall()
        by_path = {e["path"]: e["id"] for e in endpoints}
        if from_path not in by_path:
            return f"Node '{from_path}' nicht gefunden."
        if to_path not in by_path:
            return f"Node '{to_path}' nicht gefunden."
        from_id, to_id = by_path[from_path], by_path[to_path]
        if from_id == to_id:
            return f"'{from_path}' ist derselbe Node."

        link_rows = conn.execute(
            "SELECT ml.from_id, ml.to_id, ml.rel_type, mn1.path AS from_path, mn2.path AS to_path "
            "FROM memory_links ml "
            "JOIN memory_nodes mn1 ON ml.from_id = mn1.id "
            "JOIN memory_nodes mn2 ON ml.to_id = mn2.id "
            "WHERE mn1.deleted_at IS NULL AND mn2.deleted_at IS NULL"
        ).fetchall()

    # adj[node_id] entries carry the link's TRUE stored direction ("fwd"/"rev" relative
    # to that node) plus the original from_path/to_path — so the printout always shows
    # the relationship the way it was actually created, never flipped by whichever
    # direction the BFS happened to traverse the edge in.
    adj: dict = {}
    for l in link_rows:
        adj.setdefault(l["from_id"], []).append((l["to_id"], l["rel_type"], "fwd", l["from_path"], l["to_path"]))
        adj.setdefault(l["to_id"], []).append((l["from_id"], l["rel_type"], "rev", l["from_path"], l["to_path"]))

    # BFS mit Pfad-Rekonstruktion (parent + verwendete Kante pro besuchtem Node)
    start, goal = from_id, to_id
    parent: dict = {start: None}
    edge_used: dict = {}
    queue = [(start, 0)]
    qi = 0
    found = False
    while qi < len(queue):
        cur, hops = queue[qi]
        qi += 1
        if cur == goal:
            found = True
            break
        if hops >= max_hops:
            continue
        for nb, rel_type, direction, stored_from, stored_to in adj.get(cur, []):
            if nb not in parent:
                parent[nb] = cur
                edge_used[nb] = (rel_type, direction, stored_from, stored_to)
                queue.append((nb, hops + 1))

    if not found:
        return f"Kein Pfad zwischen '{from_path}' und '{to_path}' innerhalb von {max_hops} Hops gefunden."

    # Pfad von goal zurück zu start rekonstruieren
    chain = []
    node = goal
    while node != start:
        chain.append(edge_used[node])
        node = parent[node]
    chain.reverse()

    lines = [f"Kürzester Pfad ({len(chain)} Hop{'s' if len(chain) != 1 else ''}):"]
    for rel_type, direction, stored_from, stored_to in chain:
        if direction == "fwd":
            lines.append(f"  {stored_from} --{rel_type}--> {stored_to}")
        else:
            lines.append(f"  {stored_to} <--{rel_type}-- {stored_from}")
    return "\n".join(lines)


@admin_mcp.tool()
def memory_graph_stats(top_n: int = 10, scope_path: str = "") -> str:
    """Graph-Statistiken über den Memory-Knowledge-Graph: 'God Nodes', Waisen, Link-Verteilung (graphify-Stil)."""
    with diary_db.get_db() as conn:
        nodes, links = _fetch_link_graph(conn, scope_path=scope_path, include_extracted=False)

    if not nodes:
        return "Keine Nodes im angegebenen Bereich."

    degree: dict = {nid: 0 for nid in nodes}
    rel_counts: dict = {}
    origin_counts = {"explicit": 0, "inferred": 0}
    for l in links:
        if l["from_id"] in degree:
            degree[l["from_id"]] += 1
        if l["to_id"] in degree:
            degree[l["to_id"]] += 1
        rel_counts[l["rel_type"]] = rel_counts.get(l["rel_type"], 0) + 1
        origin_counts[l["origin"]] = origin_counts.get(l["origin"], 0) + 1

    god_nodes = sorted(degree.items(), key=lambda kv: kv[1], reverse=True)[:top_n]
    orphans = [nid for nid, d in degree.items() if d == 0 and nodes[nid]["type"] != "category"]

    lines = [
        "=== Memory Graph Stats ===",
        f"Nodes: {len(nodes)} | Links: {len(links)} "
        f"(explicit: {origin_counts.get('explicit', 0)}, inferred: {origin_counts.get('inferred', 0)})",
        "",
        f"God Nodes (Top {top_n} nach Degree):",
    ]
    for nid, d in god_nodes:
        if d == 0:
            break
        n = nodes[nid]
        lines.append(f"  [{d:2d}] {n['path']} — {n['title']} ({n['type']})")

    lines.append("")
    lines.append(f"Waisen ohne Verknüpfung ({len(orphans)}):")
    for nid in orphans[:30]:
        n = nodes[nid]
        lines.append(f"  {n['path']} — {n['title']}")
    if len(orphans) > 30:
        lines.append(f"  … und {len(orphans) - 30} weitere")

    if rel_counts:
        lines.append("")
        lines.append("Link-Typen:")
        for rt, c in sorted(rel_counts.items(), key=lambda kv: kv[1], reverse=True):
            lines.append(f"  {rt}: {c}")

    return "\n".join(lines)


@admin_mcp.tool()
def memory_query_graph(question: str, top_k: int = 5) -> str:
    """Beantwortet eine Frage mit einem fokussierten Teilgraphen statt Einzeltreffern (wie graphify's `query`).

    Führt memory_search_semantic aus und erweitert jeden Treffer um seine direkten
    (1-Hop) Verknüpfungen im Knowledge-Graph — liefert so nicht nur "was passt",
    sondern auch "was damit zusammenhängt".
    """
    qvec = diary_embed.embed(question)
    if qvec is None:
        return ("Semantische Suche nicht verfügbar (Embedding-Modell konnte nicht geladen "
                "werden). Nutze stattdessen memory_search(query).")

    qlit = "[" + ",".join(repr(float(x)) for x in qvec) + "]"
    # Over-fetch then re-rank, same pattern as memory_search_semantic: raw cosine
    # order alone would ignore importance/recency, so blend those in via
    # _apply_ranking before slicing down to top_k.
    fetch_k = max(top_k * 3, 15)
    with diary_db.get_db() as conn:
        if _pgvector_ready(conn):
            candidates_rows = conn.execute(
                """SELECT id, path, title, type, importance, updated_at,
                          1 - (embedding_v <=> %s::vector) AS sim
                   FROM memory_nodes
                   WHERE embedding_v IS NOT NULL AND deleted_at IS NULL AND origin = 'curated'
                   ORDER BY embedding_v <=> %s::vector LIMIT %s""",
                (qlit, qlit, fetch_k),
            ).fetchall()
        else:
            candidates = conn.execute(
                """SELECT id, path, title, type, importance, updated_at, embedding FROM memory_nodes
                   WHERE embedding IS NOT NULL AND deleted_at IS NULL AND origin = 'curated'"""
            ).fetchall()
            scored = [{**c, "sim": diary_embed.cosine(qvec, c["embedding"])} for c in candidates]
            scored.sort(key=lambda r: r["sim"], reverse=True)
            candidates_rows = scored[:fetch_k]

        if not candidates_rows:
            return f"Keine Treffer für '{question}'."
        hits = _apply_ranking(list(candidates_rows), sim_key="sim")[:top_k]

        hit_ids = [h["id"] for h in hits]
        expansion = conn.execute(
            "SELECT ml.from_id, ml.to_id, ml.rel_type, ml.link_origin AS origin, "
            "mn1.path AS from_path, mn1.title AS from_title, "
            "mn2.path AS to_path, mn2.title AS to_title "
            "FROM memory_links ml "
            "JOIN memory_nodes mn1 ON ml.from_id = mn1.id "
            "JOIN memory_nodes mn2 ON ml.to_id = mn2.id "
            "WHERE (ml.from_id = ANY(%s) OR ml.to_id = ANY(%s)) "
            "AND mn1.deleted_at IS NULL AND mn2.deleted_at IS NULL",
            (hit_ids, hit_ids),
        ).fetchall()

    lines = [f"Teilgraph für '{question}' ({len(hits)} Kern-Treffer + 1-Hop-Nachbarn):"]
    for h in hits:
        lines.append(f"\n● [{h['type']}] {h['path']} — {h['title']} (sim {h.get('sim', 0):.3f})")
        neighbors = [e for e in expansion if e["from_id"] == h["id"] or e["to_id"] == h["id"]]
        for e in neighbors:
            tag = "EXPLICIT" if e["origin"] == "explicit" else "INFERRED"
            if e["from_id"] == h["id"]:
                lines.append(f"    --{e['rel_type']}--> {e['to_path']} ({e['to_title']}) [{tag}]")
            else:
                lines.append(f"    <--{e['rel_type']}-- {e['from_path']} ({e['from_title']}) [{tag}]")
        if not neighbors:
            lines.append("    (keine Verknüpfungen)")
    return "\n".join(lines)


@admin_mcp.tool()
def memory_report(scope_path: str = "") -> str:
    """Erzeugt einen Markdown-Report über den Memory-Knowledge-Graph (analog graphify's GRAPH_REPORT.md).

    Enthält: Übersicht, Kernkonzepte (God Nodes), Cluster (Connected Components des
    Link-Graphen — bewusst ohne Leiden/igraph-Abhängigkeit, für diese Skala reichen
    einfache Zusammenhangskomponenten), Waisen und Widersprüche.
    scope_path grenzt auf einen Teilbaum ein (z.B. '/projects/diary-mcp'); leer = ganzer Baum.
    """
    with diary_db.get_db() as conn:
        nodes, links = _fetch_link_graph(conn, scope_path=scope_path, include_extracted=False)
        contradictions = [l for l in links if l["rel_type"] == "contradicts"]

    if not nodes:
        return f"# Memory Graph Report\n\nKeine Nodes unter '{scope_path or '/'}'."

    degree: dict = {nid: 0 for nid in nodes}
    for l in links:
        degree[l["from_id"]] = degree.get(l["from_id"], 0) + 1
        degree[l["to_id"]] = degree.get(l["to_id"], 0) + 1

    components = [c for c in _connected_components(nodes, links) if len(c) >= 2]
    components.sort(key=len, reverse=True)
    orphans = [nid for nid, d in degree.items() if d == 0 and nodes[nid]["type"] != "category"]
    god_nodes = sorted(degree.items(), key=lambda kv: kv[1], reverse=True)[:8]

    md = [f"# Memory Graph Report — {scope_path or '/'}", ""]
    md.append(f"**{len(nodes)}** Nodes, **{len(links)}** Links, **{len(components)}** Cluster, "
               f"**{len(orphans)}** Waisen.")
    md.append("")
    md.append("## Kernkonzepte (God Nodes)")
    for nid, d in god_nodes:
        if d == 0:
            break
        n = nodes[nid]
        md.append(f"- **{n['path']}** ({n['title']}) — Degree {d}")
    md.append("")
    md.append(f"## Cluster ({len(components)})")
    for i, comp in enumerate(components[:15], 1):
        titles = ", ".join(nodes[nid]["title"] for nid in comp[:8])
        more = f" (+{len(comp) - 8} weitere)" if len(comp) > 8 else ""
        md.append(f"{i}. {titles}{more}")
    if not components:
        md.append("(keine — noch keine Nodes verknüpft; memory_infer_links ausprobieren?)")
    md.append("")
    md.append(f"## Waisen ({len(orphans)})")
    for nid in orphans[:20]:
        md.append(f"- {nodes[nid]['path']} — {nodes[nid]['title']}")
    if len(orphans) > 20:
        md.append(f"- … und {len(orphans) - 20} weitere")
    if contradictions:
        md.append("")
        md.append(f"## ⚠ Widersprüche ({len(contradictions)})")
        for l in contradictions:
            a, b = nodes.get(l["from_id"]), nodes.get(l["to_id"])
            if a and b:
                md.append(f"- {a['path']} ↔ {b['path']}")
    return "\n".join(md)


@admin_mcp.tool()
def memory_consolidate_report(
    scope_path: str = "",
    dup_threshold: float = 0.93,
    stale_days: int = 180,
    max_pairs: int = 20,
) -> str:
    """Findet Konsolidierungs-Kandidaten im kuratierten Memory-Tree: Near-Duplicate-
    Paare (Merge-Kandidaten) und veraltete, kaum genutzte Nodes (Archivierungs-
    Kandidaten). REIN LESEND — löscht/mergt nichts von selbst.

    Motivation: memory_infer_links findet ähnliche Paare, aber verknüpft sie nur
    (rel_type='related'); es gibt keine Möglichkeit, echte Near-Duplikate als
    Merge-Kandidaten zu erkennen, und für den kuratierten Tier existiert (anders
    als für 'extracted' mit seinem TTL) kein Verfall-Mechanismus — die kuratierte
    Ebene wächst bei langfristigem Agentic Use unbegrenzt weiter, was Suchqualität
    und -kosten schleichend verschlechtert.

    NEAR-DUPLICATES: paarweiser Cosine-Vergleich (brute-force, wie memory_infer_links)
    aller kuratierten, embedded Nodes unter scope_path (leer = ganzer Baum).
    dup_threshold ist bewusst höher als memory_infer_links' Default (0.82) — hier
    geht es um "praktisch dasselbe", nicht "thematisch verwandt". Ergebnis ist ein
    Vorschlag für memory_merge(keep_path, merge_path), keine automatische Aktion.

    STALE CANDIDATES: kuratierte Nodes mit importance <= 0.4, ohne aktives Pinning
    (pin_triggers leer — gepinnte Nodes sind bewusst dauerhaft wichtig und werden
    nie als veraltet vorgeschlagen), deren letzte Aktivität (accessed_at, ersatzweise
    updated_at) älter als stale_days ist. Vorschlag: importance senken, aktualisieren
    oder memory_delete.

    max_pairs begrenzt beide Listen (Report-Länge, kein Sicherheitsmechanismus).
    """
    if not 0.0 < dup_threshold <= 1.0:
        return "Fehler: dup_threshold muss zwischen 0.0 (exklusiv) und 1.0 liegen."

    scope_clause = "AND (path = %s OR path LIKE %s)" if scope_path else ""
    scope_params = [scope_path, f"{_escape_like(scope_path)}/%"] if scope_path else []

    with diary_db.get_db() as conn:
        embedded = conn.execute(
            f"""SELECT id, path, title, embedding FROM memory_nodes
                WHERE deleted_at IS NULL AND origin = 'curated' AND embedding IS NOT NULL
                {scope_clause}""",
            scope_params,
        ).fetchall()

        dup_pairs = []
        if len(embedded) >= 2:
            unit_vecs = [diary_embed.normalize(r["embedding"]) for r in embedded]
            for i in range(len(embedded)):
                for j in range(i + 1, len(embedded)):
                    sim = diary_embed.dot(unit_vecs[i], unit_vecs[j])
                    if sim >= dup_threshold:
                        dup_pairs.append((sim, embedded[i], embedded[j]))
            dup_pairs.sort(key=lambda p: p[0], reverse=True)
            dup_pairs = dup_pairs[:max_pairs]

        stale = conn.execute(
            f"""SELECT path, title, importance, COALESCE(accessed_at, updated_at) AS last_touch
                FROM memory_nodes
                WHERE deleted_at IS NULL AND origin = 'curated' AND importance <= 0.4
                  AND pin_triggers = '{{}}'
                  AND COALESCE(accessed_at, updated_at) < now() - %s::interval
                  {scope_clause}
                ORDER BY last_touch ASC LIMIT %s""",
            [f"{int(stale_days)} days", *scope_params, max_pairs] if scope_path
            else [f"{int(stale_days)} days", max_pairs],
        ).fetchall()

    lines = [f"Konsolidierungs-Report — {scope_path or '/'} "
             f"(dup_threshold={dup_threshold}, stale_days={stale_days}):"]

    lines.append(f"\n## Near-Duplicates ({len(dup_pairs)})")
    if dup_pairs:
        for sim, a, b in dup_pairs:
            lines.append(f"  [{sim:.3f}] {a['path']} ({a['title']}) ↔ {b['path']} ({b['title']})"
                         f"  — z.B. memory_merge('{a['path']}', '{b['path']}')")
    else:
        reason = "zu wenige embedded Nodes" if len(embedded) < 2 else "keine über dup_threshold"
        lines.append(f"  (keine — {reason})")

    lines.append("\n## Stale Candidates ({}, importance<=0.4, ungepinnt, >{}d inaktiv)".format(
        len(stale), stale_days))
    if stale:
        for r in stale:
            lines.append(f"  {r['path']} ({r['title']}) — importance {r['importance']}, "
                         f"zuletzt aktiv {r['last_touch']}")
    else:
        lines.append("  (keine)")

    return "\n".join(lines)
