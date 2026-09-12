"""
Hybrid memory search: FTS (Postgres tsvector) + semantic (embeddings), fused via
Reciprocal Rank Fusion, re-ranked by importance/recency. Plus the extracted-tier
"tripwire" safety net and reembedding.

Split out of the former diary_server.py monolith (v0.10.0).
"""
from datetime import datetime

import diary_db
import diary_embed
from diary_bootstrap import mcp
from memory_service import _contradiction_warnings, _pgvector_ready

# Extracted-tier tripwire: cosine similarity above which an auto-extracted memory
# is surfaced as a "see also" hint even though include_extracted=False. High bar
# on purpose — this is a recall safety net for near-duplicate content, not a
# backdoor into the full extracted tier (that's what include_extracted=True is for).
TRIPWIRE_SIMILARITY_THRESHOLD = 0.85
TRIPWIRE_MAX_HITS = 3


def _apply_ranking(rows: list[dict], sim_key: str = "sim") -> list[dict]:
    """Blendet Importance und Recency als Tiebreaker in den Similarity-Score ein.

    Formel:
        final_score = similarity * (0.5 + 0.5 * importance)
                      + 1e-6 * recency_days_ago_inv

    Erklärung:
      • Similarity (FTS-rank oder Cosine) wird mit einem Faktor (0.5–1.0) skaliert,
        der linear von importance abhängt. importance=0 → Faktor 0.5 (halbiert nur),
        importance=1 → Faktor 1.0 (unveränderter Score). Similarity dominiert stets.
      • Recency-Term: 1/(1+days_since_update) — winzig (1e-6 * max ~1), dient nur
        als stabiler Tiebreaker bei gleichen final_scores, nicht als Rangsignal.
    """
    now = datetime.now()
    result = []
    for r in rows:
        sim = float(r.get(sim_key, 0.0))
        importance = float(r.get("importance", 0.5) or 0.5)
        updated_at = r.get("updated_at")
        if updated_at:
            try:
                if isinstance(updated_at, str):
                    updated_at = datetime.fromisoformat(updated_at)
                days_ago = max(0.0, (now - updated_at.replace(tzinfo=None)).total_seconds() / 86400)
            except Exception:
                days_ago = 365.0
        else:
            days_ago = 365.0
        recency = 1.0 / (1.0 + days_ago)
        final = sim * (0.5 + 0.5 * importance) + 1e-6 * recency
        result.append({**r, "final_score": final})
    result.sort(key=lambda x: x["final_score"], reverse=True)
    return result


def _rrf_fuse(
    fts_paths: list[str],
    vec_paths: list[str],
    k: int = 60,
) -> list[str]:
    """Reciprocal Rank Fusion der FTS- und Vektor-Ranglisten.

    Formel: RRF_score(d) = 1/(k + rank_fts(d)) + 1/(k + rank_vec(d))
    k=60 (Standard nach Cormack et al. 2009). Fehlende Einträge zählen als
    rank = len(Liste)+1 (schlechtester möglicher Rang).
    """
    fts_rank = {p: i + 1 for i, p in enumerate(fts_paths)}
    vec_rank = {p: i + 1 for i, p in enumerate(vec_paths)}
    all_paths = set(fts_paths) | set(vec_paths)
    fts_missing = len(fts_paths) + 1
    vec_missing = len(vec_paths) + 1
    scores = {
        p: 1.0 / (k + fts_rank.get(p, fts_missing)) + 1.0 / (k + vec_rank.get(p, vec_missing))
        for p in all_paths
    }
    return sorted(all_paths, key=lambda p: scores[p], reverse=True)


def _extracted_tripwire_hits(conn, qvec, expiry_clause: str) -> list[dict]:
    """Cosine-search the extracted tier for near-duplicates of the query, regardless
    of the FTS/hybrid results above. See TRIPWIRE_SIMILARITY_THRESHOLD docstring."""
    if qvec is None:
        return []
    if _pgvector_ready(conn):
        qlit = "[" + ",".join(repr(float(x)) for x in qvec) + "]"
        rows = conn.execute(
            f"""SELECT path, title, 1 - (embedding_v <=> %s::vector) AS sim,
                       substr(coalesce(body,''), 1, 160) AS snippet
                FROM memory_nodes
                WHERE origin = 'extracted' AND embedding_v IS NOT NULL AND deleted_at IS NULL {expiry_clause}
                ORDER BY embedding_v <=> %s::vector LIMIT 20""",
            (qlit, qlit),
        ).fetchall()
    else:
        candidates = conn.execute(
            f"""SELECT path, title, embedding, substr(coalesce(body,''), 1, 160) AS snippet
                FROM memory_nodes
                WHERE origin = 'extracted' AND embedding IS NOT NULL AND deleted_at IS NULL {expiry_clause}"""
        ).fetchall()
        rows = [{**c, "sim": diary_embed.cosine(qvec, c["embedding"])} for c in candidates]
    hits = [r for r in rows if r["sim"] >= TRIPWIRE_SIMILARITY_THRESHOLD]
    hits.sort(key=lambda r: r["sim"], reverse=True)
    return hits[:TRIPWIRE_MAX_HITS]


def _hybrid_retrieve(
    conn,
    query: str,
    qvec,
    include_extracted: bool,
    include_expired: bool,
    limit: int,
) -> tuple[list[dict], str]:
    """Core hybrid retrieval (FTS + semantic, RRF-fused, importance/recency-ranked).

    Split out of memory_search so memory_recall can reuse the exact same
    retrieval+ranking behavior instead of re-implementing it — the only
    difference between the two tools is what memory_search does with the
    hits (tripwire/extracted-count formatting) vs. memory_recall (1-hop
    graph expansion). Returns (ranked_rows, mode_label). `conn` must NOT
    have qvec computed inside its transaction (see memory_search's own
    comment on why embed() must run before `with get_db()`).
    """
    origin_clause = "" if include_extracted else "AND origin = 'curated'"
    expiry_clause = "" if include_expired else "AND (valid_until IS NULL OR valid_until >= now())"

    fts_rows = conn.execute(
        f"""SELECT id, path, title, type, origin, importance, updated_at,
                  ts_rank(
                      to_tsvector('german', coalesce(title,'') || ' ' || coalesce(body,'')),
                      plainto_tsquery('german', %s)
                  ) AS sim,
                  ts_headline('german', coalesce(body,''), plainto_tsquery('german', %s),
                              'MaxWords=25,MinWords=10,StartSel=«,StopSel=»') AS snippet
           FROM memory_nodes
           WHERE deleted_at IS NULL {expiry_clause}
             AND to_tsvector('german', coalesce(title,'') || ' ' || coalesce(body,''))
                 @@ plainto_tsquery('german', %s) {origin_clause}
           ORDER BY sim DESC LIMIT 30""",
        (query, query, query),
    ).fetchall()

    vec_rows: list[dict] = []
    if qvec is not None and _pgvector_ready(conn):
        qlit = "[" + ",".join(repr(float(x)) for x in qvec) + "]"
        vec_rows = conn.execute(
            f"""SELECT id, path, title, type, origin, importance, updated_at,
                       1 - (embedding_v <=> %s::vector) AS sim,
                       substr(coalesce(body,''), 1, 200) AS snippet
                FROM memory_nodes
                WHERE embedding_v IS NOT NULL AND deleted_at IS NULL {expiry_clause}
                {origin_clause}
                ORDER BY embedding_v <=> %s::vector LIMIT 30""",
            (qlit, qlit),
        ).fetchall()
    elif qvec is not None:
        candidates = conn.execute(
            f"""SELECT id, path, title, type, origin, importance, updated_at, embedding,
                       substr(coalesce(body,''), 1, 200) AS snippet
                FROM memory_nodes
                WHERE embedding IS NOT NULL AND deleted_at IS NULL {expiry_clause}
                {origin_clause}"""
        ).fetchall()
        scored = []
        for c in candidates:
            s = diary_embed.cosine(qvec, c["embedding"])
            scored.append({**c, "sim": s})
        scored.sort(key=lambda r: r["sim"], reverse=True)
        vec_rows = scored[:30]

    fts_by_path = {r["path"]: r for r in fts_rows}
    vec_by_path = {r["path"]: r for r in vec_rows}

    if fts_rows and vec_rows:
        fused_paths = _rrf_fuse(
            [r["path"] for r in fts_rows],
            [r["path"] for r in vec_rows],
        )
        # Merge metadata: prefer FTS row (has snippet from ts_headline), fall back to vec
        all_meta = {**vec_by_path, **fts_by_path}
        merged = [all_meta[p] for p in fused_paths if p in all_meta]
        for i, row in enumerate(merged):
            if "sim" not in row or row.get("sim") is None:
                row = dict(row)
                merged[i] = {**row, "sim": 1.0 / (i + 1)}
        mode = "Hybrid/RRF"
        results = merged
    elif fts_rows:
        mode = "FTS"
        results = list(fts_rows)
    elif vec_rows:
        mode = "Semantisch"
        results = list(vec_rows)
    else:
        results = conn.execute(
            f"""SELECT id, path, title, type, origin, importance, updated_at,
                       0.1 AS sim,
                       substr(coalesce(body,''), 1, 200) AS snippet
               FROM memory_nodes WHERE deleted_at IS NULL {expiry_clause}
                 AND (title ILIKE %s OR body ILIKE %s) {origin_clause} LIMIT 30""",
            (f"%{query}%", f"%{query}%"),
        ).fetchall()
        mode = "LIKE"

    results = _apply_ranking(results, sim_key="sim")[:limit]
    return results, mode


@mcp.tool()
def memory_search(
    query: str,
    include_extracted: bool = False,
    include_expired: bool = False,
) -> str:
    """Hybride Suche im Memory-Tree: FTS + semantische Suche, fusioniert via RRF.

    ZWEI-STUFEN-MODELL — wichtig:
      • Standard (include_extracted=False): durchsucht NUR die kuratierten Memories,
        die Claude bewusst gespeichert hat. Hochwertig, kompakt, günstig.
      • include_extracted=True: durchsucht ZUSÄTZLICH die automatisch aus Chat-
        Transkripten extrahierten Memories. Diese sind zahlreich, roh und
        kostspieliger im Kontext — nur bewusst einschalten, z.B. wenn die
        kuratierte Suche nichts Brauchbares liefert oder du gezielt nach einem
        Detail aus einem früheren Gespräch suchst.

    TRIPWIRE: Auch im Default-Modus wird die extrahierte Ebene auf FAST IDENTISCHE
    Treffer geprüft (Cosine-Similarity >= 0.85) und getrennt als "Sicherheitsnetz"
    angezeigt — verhindert, dass ein nahezu perfekter Treffer aus einer früheren
    Session komplett unsichtbar bleibt, ohne die Standardsuche mit rohem Tier-2-
    Material zu fluten.

    HYBRID-SEARCH: Kombiniert lexikalische FTS-Suche (PostgreSQL tsvector) mit
    semantischer Vektorsuche (Embeddings). Wenn das Embedding-Modell nicht
    verfügbar ist, wird automatisch auf FTS-only zurückgefallen.
    Fusion: Reciprocal Rank Fusion (RRF, k=60).

    RANKING: final_score = similarity * (0.5 + 0.5 * importance)
                           + 1e-6 * recency_tiebreaker
    Recency-Term = 1/(1+days_since_update) — winziger Tiebreaker, dominiert nie.

    ABLAUFENDE MEMORIES: include_expired=False (Standard) — Memories mit
    vergangenem valid_until werden ausgeblendet. include_expired=True zeigt sie.

    WIDERSPRÜCHE: Treffer mit einem 'contradicts'-Link auf einen anderen Node
    werden inline markiert, statt nur bei einem manuellen memory_health() sichtbar
    zu sein.
    """
    expiry_clause = "" if include_expired else "AND (valid_until IS NULL OR valid_until >= now())"
    # Embed BEFORE opening the DB connection: this can block for a long time on
    # first use (heavy import + ONNX load, or a stalled model download), and
    # doing it while a transaction is open would hold locks on memory_nodes for
    # that whole time — starving any concurrent DDL (e.g. another process's
    # init_db() migration) until it finishes. Verified live: a memory_search
    # call stuck on embed() left a 46-minute "idle in transaction" backend that
    # blocked a fresh diary-mcp process's ALTER TABLE indefinitely.
    qvec = diary_embed.embed(query)

    with diary_db.get_db() as conn:
        results, mode = _hybrid_retrieve(conn, query, qvec, include_extracted, include_expired, limit=20)

        # Just-in-time contradiction surfacing for whatever made it into `results`.
        contradiction_map = _contradiction_warnings(conn, [r["id"] for r in results])

        # Extracted-tier tripwire (see _extracted_tripwire_hits docstring) — only
        # relevant in default mode, since include_extracted=True already searches it.
        tripwire_hits = [] if include_extracted else _extracted_tripwire_hits(conn, qvec, expiry_clause)

        # Count extra hits in the extracted tier (only relevant in default mode)
        extracted_hits = 0
        if not include_extracted:
            extracted_hits = conn.execute(
                f"""SELECT COUNT(*) AS c FROM memory_nodes
                   WHERE origin = 'extracted' AND deleted_at IS NULL {expiry_clause}
                     AND (to_tsvector('german', coalesce(title,'') || ' ' || coalesce(body,''))
                          @@ plainto_tsquery('german', %s)
                          OR title ILIKE %s OR body ILIKE %s)""",
                (query, f"%{query}%", f"%{query}%"),
            ).fetchone()["c"]

    scope = "alle Tiers" if include_extracted else "kuratiert"
    expired_note = "" if include_expired else ", ohne abgelaufene"
    if not results and not extracted_hits and not tripwire_hits:
        return f"Keine Memory-Ergebnisse für '{query}' ({scope}{expired_note})."

    lines = [f"Memory-Suchergebnisse [{mode}, {scope}{expired_note}] für '{query}':"]
    for r in results:
        tag = "" if r.get("origin", "curated") == "curated" else " ⟨auto-extrahiert⟩"
        score_str = f"  Score {r['final_score']:.4f}"
        lines.append(f"\n[{r['type']}] {r['path']} — {r['title']}{tag}{score_str}")
        if r.get("snippet"):
            lines.append(f"  {r['snippet']}")
        for other_path, other_title in contradiction_map.get(r["id"], []):
            lines.append(f"  ⚠ WIDERSPRUCH: widerspricht {other_path} ({other_title})")
    if tripwire_hits:
        lines.append(
            f"\n— Sicherheitsnetz (auto-extrahiert, Ähnlichkeit ≥{TRIPWIRE_SIMILARITY_THRESHOLD}) —"
        )
        for h in tripwire_hits:
            lines.append(f"  [{h['sim']:.3f}] {h['path']} — {h['title']}")
            if h.get("snippet"):
                lines.append(f"    {h['snippet']}")
    if extracted_hits:
        lines.append(
            f"\n— Hinweis: {extracted_hits} weitere Treffer in auto-extrahierten Memories "
            f"(kostspieliger). Mit memory_search(query, include_extracted=True) durchsuchbar."
        )
    return "\n".join(lines)


@mcp.tool()
def memory_recall(query: str, top_k: int = 5, include_extracted: bool = False) -> str:
    """Ein-Schritt-Kontextabruf für agentische Nutzung: Hybrid-Suche + 1-Hop-
    Graph-Nachbarn + Widerspruchs-Warnungen in EINEM Tool-Call.

    Für einen Agenten mitten in einer Aufgabe ist "gib mir relevanten Kontext
    zu X" der Normalfall — bisher brauchte das memory_search() gefolgt von
    manuellen memory_get_links()-Calls pro Treffer, oder den admin-only
    memory_query_graph() (nur semantisch, nicht auf dem Haupt-Server). Dieses
    Tool kombiniert beides: dieselbe Hybrid-Suche wie memory_search (FTS +
    semantisch, RRF-fusioniert, importance/recency-geranked), aber direkt
    auf top_k verdichtet und um die direkten Graph-Nachbarn jedes Treffers
    erweitert — damit taucht z.B. auch ein per memory_link() explizit
    verknüpfter, aber lexikalisch/semantisch nicht treffender Kontext auf.

    top_k klein halten (Default 5) — das ist bewusst kompakter als
    memory_search (Default 20 Treffer), für den Fall wo Kontext-Budget zählt.
    Abgelaufene Memories (valid_until) sind immer ausgeblendet — bei aktivem
    Bedarf für abgelaufene/erweiterte Suche stattdessen memory_search nutzen.
    """
    qvec = diary_embed.embed(query)  # embed BEFORE opening the connection — see memory_search
    with diary_db.get_db() as conn:
        results, mode = _hybrid_retrieve(
            conn, query, qvec, include_extracted, include_expired=False, limit=top_k
        )
        if not results:
            return f"Keine Treffer für '{query}'."

        hit_ids = [r["id"] for r in results]
        contradiction_map = _contradiction_warnings(conn, hit_ids)
        neighbors = conn.execute(
            "SELECT ml.from_id, ml.to_id, ml.rel_type, "
            "mn1.path AS from_path, mn1.title AS from_title, "
            "mn2.path AS to_path, mn2.title AS to_title "
            "FROM memory_links ml "
            "JOIN memory_nodes mn1 ON ml.from_id = mn1.id "
            "JOIN memory_nodes mn2 ON ml.to_id = mn2.id "
            "WHERE (ml.from_id = ANY(%s) OR ml.to_id = ANY(%s)) "
            "AND mn1.deleted_at IS NULL AND mn2.deleted_at IS NULL",
            (hit_ids, hit_ids),
        ).fetchall()

    scope = "alle Tiers" if include_extracted else "kuratiert"
    lines = [f"Recall [{mode}, {scope}] für '{query}':"]
    for r in results:
        tag = "" if r.get("origin", "curated") == "curated" else " ⟨auto-extrahiert⟩"
        lines.append(f"\n● [{r['type']}] {r['path']} — {r['title']}{tag}  (Score {r['final_score']:.4f})")
        if r.get("snippet"):
            lines.append(f"  {r['snippet']}")
        for other_path, other_title in contradiction_map.get(r["id"], []):
            lines.append(f"  ⚠ WIDERSPRUCH: widerspricht {other_path} ({other_title})")
        own_neighbors = [n for n in neighbors if n["from_id"] == r["id"] or n["to_id"] == r["id"]]
        for n in own_neighbors:
            if n["from_id"] == r["id"]:
                lines.append(f"    --{n['rel_type']}--> {n['to_path']} ({n['to_title']})")
            else:
                lines.append(f"    <--{n['rel_type']}-- {n['from_path']} ({n['from_title']})")
    return "\n".join(lines)


@mcp.tool()
def memory_search_semantic(
    query: str,
    top_k: int = 8,
    include_extracted: bool = False,
    include_expired: bool = False,
) -> str:
    """Semantische Suche im Memory-Tree über Embeddings (sprachübergreifend).

    Im Gegensatz zu memory_search (lexikalisch, Stichwort-basiert) findet diese
    Suche Memories nach BEDEUTUNG — auch wenn andere Wörter oder eine andere
    Sprache verwendet werden (z.B. deutsche Anfrage findet englischen Inhalt).
    Nutze sie, wenn du ein Konzept suchst und die genauen Begriffe nicht kennst.

    Bei vielen Memories nutzt sie automatisch den pgvector-HNSW-Index (schnell);
    sonst einen numpy-Fallback. include_extracted=True bezieht die auto-extrahierten
    Memories mit ein (kostspieliger).

    RANKING: final_score = cosine_similarity * (0.5 + 0.5 * importance)
                           + 1e-6 * recency_tiebreaker
    Recency-Term = 1/(1+days_since_update) — winziger Tiebreaker, dominiert nie.

    ABLAUFENDE MEMORIES: include_expired=False (Standard) — Memories mit
    vergangenem valid_until werden ausgeblendet. include_expired=True zeigt sie.
    """
    qvec = diary_embed.embed(query)
    if qvec is None:
        return ("Semantische Suche nicht verfügbar (Embedding-Modell konnte nicht geladen "
                "werden). Nutze stattdessen memory_search(query).")

    qlit = "[" + ",".join(repr(float(x)) for x in qvec) + "]"  # pgvector text format
    origin_clause = "" if include_extracted else "AND origin = 'curated'"
    expiry_clause = "" if include_expired else "AND (valid_until IS NULL OR valid_until >= now())"
    # Fetch more candidates so ranking can re-sort before limiting to top_k
    fetch_k = max(top_k * 3, 30)
    with diary_db.get_db() as conn:
        if _pgvector_ready(conn):
            rows = conn.execute(
                f"""SELECT path, title, type, origin, importance, updated_at,
                           1 - (embedding_v <=> %s::vector) AS sim,
                           substr(coalesce(body,''), 1, 160) AS snippet
                    FROM memory_nodes
                    WHERE embedding_v IS NOT NULL AND deleted_at IS NULL {expiry_clause} {origin_clause}
                    ORDER BY embedding_v <=> %s::vector LIMIT %s""",
                (qlit, qlit, fetch_k),
            ).fetchall()
            backend = "pgvector/HNSW"
        else:
            candidates = conn.execute(
                f"""SELECT path, title, type, origin, importance, updated_at, embedding,
                           substr(coalesce(body,''),1,160) AS snippet
                    FROM memory_nodes
                    WHERE embedding IS NOT NULL AND deleted_at IS NULL {expiry_clause} {origin_clause}"""
            ).fetchall()
            scored = []
            for c in candidates:
                s = diary_embed.cosine(qvec, c["embedding"])
                scored.append({**c, "sim": s})
            scored.sort(key=lambda r: r["sim"], reverse=True)
            rows = scored[:fetch_k]
            backend = "numpy"

    if not rows:
        expired_note = "" if include_expired else " (ohne abgelaufene)"
        return (f"Keine semantischen Treffer für '{query}'{expired_note}. "
                f"(Tipp: memory_reembed_all falls Embeddings fehlen.)")

    ranked = _apply_ranking(list(rows), sim_key="sim")[:top_k]
    expired_note = "" if include_expired else ", ohne abgelaufene"
    lines = [f"Semantische Suche [{backend}{expired_note}] für '{query}':"]
    for r in ranked:
        tag = "" if r.get("origin", "curated") == "curated" else " ⟨auto-extrahiert⟩"
        lines.append(
            f"\n[{r['type']}] {r['path']} — {r['title']}{tag}"
            f"  (sim {r.get('sim', 0):.3f}, final {r['final_score']:.4f})"
        )
        if r.get("snippet"):
            lines.append(f"  {r['snippet']}")
    return "\n".join(lines)


@mcp.tool()
def memory_reembed_all(only_missing: bool = True) -> str:
    """(Re-)berechnet Embeddings für Memories und füllt den Vektor-Index.

    only_missing=True (Default): nur Nodes ohne Embedding. False: alle neu berechnen
    (z.B. nach Modellwechsel). Nötig nach dem ersten Aktivieren der semantischen Suche
    oder nach dem Import bestehender Memories.
    """
    if not diary_embed.is_available():
        return "Embedding-Modell nicht verfügbar — fastembed/Modell konnte nicht geladen werden."

    cond = "WHERE deleted_at IS NULL AND embedding IS NULL" if only_missing else "WHERE deleted_at IS NULL"
    with diary_db.get_db() as conn:
        rows = conn.execute(
            f"SELECT path, title, body FROM memory_nodes {cond}"
        ).fetchall()
    if not rows:
        return "Nichts zu embedden — alle Memories haben bereits Embeddings."

    # Batch-embed with no transaction open — this can take a while for many
    # nodes, and doing it inside a `with get_db()` block would hold locks on
    # memory_nodes for the whole run (see memory_search's fix for why that's
    # dangerous: it can block other processes' schema migrations indefinitely).
    texts = [f"{r['title']}\n{r['body'] or ''}" for r in rows]
    vecs = diary_embed.embed_many(texts)

    with diary_db.get_db() as conn:
        done = 0
        pgv = _pgvector_ready(conn)
        for r, v in zip(rows, vecs):
            if v is None:
                continue
            conn.execute("UPDATE memory_nodes SET embedding = %s WHERE path = %s", (v, r["path"]))
            if pgv:
                conn.execute(
                    "UPDATE memory_nodes SET embedding_v = embedding::vector WHERE path = %s",
                    (r["path"],),
                )
            done += 1

    backend = "pgvector-Index aktualisiert" if pgv else "numpy-Fallback (kein pgvector)"
    return f"{done} Memories (re-)embedded. {backend}."
