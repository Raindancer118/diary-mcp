"""
Automatic context injection — the part that makes diary knowledge reach the
model without it having to remember a tool call.

Three consumers share this module:
  • SessionStart hook  → build_session_digest(): a token-budgeted index of the
    current project (path — title — one-line hook, ranked) + global rules +
    full bodies of pinned nodes. The diary equivalent of Claude Code's
    always-loaded MEMORY.md, but ranked, scoped and budgeted.
  • UserPromptSubmit hook → retrieve_for_prompt(): per-turn retrieval.
    Lexical part is an OR query over the prompt's lexemes, scored BM25-style
    with corpus document frequencies (so "projekt" alone never triggers), plus
    an optional semantic part when the shared embedding server
    (diary_embed_ipc) is already running — never loads a model in the hook.
  • memory_context / memory_project_context tools (budgeted output).

Hooks run as `diary-hook <event>` (console script → main()). Every error path
returns empty output: a hook must never block or break a session.
"""
from __future__ import annotations

import json
import math
import os
import re
import sys
import time
from pathlib import Path

HOOK_MAX_CHARS = 120
DIGEST_BUDGET_CHARS = 8000
PINNED_BODY_MAX_CHARS = 2500
GLOBAL_MIN_IMPORTANCE = 0.7
GLOBAL_MAX_LINES = 20

PROMPT_MAX_HITS = 3
PROMPT_SNIPPET_CHARS = 600
PROMPT_MIN_LEXEMES = 2
LEX_MIN_MATCHES = 2
LEX_MIN_SCORE = 7.0
LEX_RELATIVE_CUTOFF = 0.5  # drop hits scoring below this share of the best one
SEM_MIN_SIM = 0.55
SEM_TIMEOUT_S = 1.0
DF_CACHE_TTL_S = 6 * 3600

_DOC = "to_tsvector('german', coalesce(title,'') || ' ' || coalesce(body,''))"
_LIVE = ("deleted_at IS NULL AND origin = 'curated' "
         "AND (valid_until IS NULL OR valid_until >= now())")
_NOT_AUTO = "path NOT LIKE '%%/auto/%%'"


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _database_url() -> str:
    return os.environ.get("DIARY_DATABASE_URL", "postgresql://localhost/diary_mcp")


def _state_dir() -> Path:
    override = os.environ.get("DIARY_HOOK_STATE_DIR")
    if override:
        base = Path(override)
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "diary-mcp"
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    return base


_MD_NOISE = re.compile(r"\*\*|__|[*`>]")
# Sentence end: two letters before the punctuation (so "z.B." and "4." don't
# count), then whitespace + uppercase or end of line.
_SENTENCE = re.compile(r"(.+?[A-Za-zÄÖÜäöüß)\]\"'][A-Za-zÄÖÜäöüß)\]\"'][.!?])(?=\s+[A-ZÄÖÜ\[(\"„]|\s*$)")


def node_hook(body: str | None, max_chars: int = HOOK_MAX_CHARS) -> str:
    """First meaningful sentence of a body, markdown stripped, capped."""
    for line in (body or "").splitlines():
        if line.lstrip().startswith("#"):  # headings mostly repeat the title
            continue
        line = _MD_NOISE.sub("", line).strip(" -•\t")
        if len(line) < 3 or line.startswith("---"):
            continue
        m = _SENTENCE.match(line)
        text = m.group(1) if m else line
        if len(text) > max_chars:
            text = text[: max_chars - 1].rstrip() + "…"
        return text
    return ""


def _project_base(slug: str | None) -> str | None:
    return f"/projects/{slug.strip('/')}" if slug else None


def _rank_score(r: dict) -> float:
    imp = float(r.get("importance") or 0.5)
    access = min(int(r.get("access_count") or 0), 20) / 40
    recency = 0.0
    upd = r.get("updated_at")
    if upd is not None:
        days = (time.time() - upd.timestamp()) / 86400
        recency = 0.2 if days < 30 else 0.0
    return imp + access + recency


def _project_centroid(conn, base: str) -> list[float] | None:
    """Mean of the project's stored embeddings — lets global rules be ranked by
    relevance to this project without loading an embedding model."""
    rows = conn.execute(
        f"SELECT embedding FROM memory_nodes WHERE {_LIVE} AND {_NOT_AUTO} "
        f"AND embedding IS NOT NULL AND (path = %s OR path LIKE %s)",
        (base, f"{base}/%"),
    ).fetchall()
    vecs = [r["embedding"] for r in rows if r["embedding"]]
    if not vecs:
        return None
    dim = len(vecs[0])
    vecs = [v for v in vecs if len(v) == dim]
    return [sum(v[i] for v in vecs) / len(vecs) for i in range(dim)]


def _global_score(r: dict, centroid: list[float] | None) -> float:
    score = float(r["importance"] or 0.5) + min(int(r["access_count"] or 0), 20) / 100
    if centroid is not None and r.get("embedding"):
        import diary_embed
        score += 0.8 * diary_embed.cosine(centroid, r["embedding"])
    return score


# ---------------------------------------------------------------------------
# Session digest
# ---------------------------------------------------------------------------

def build_session_digest(conn, slug: str | None, trigger: str = "start",
                         budget_chars: int = DIGEST_BUDGET_CHARS) -> str:
    base = _project_base(slug)
    triggers = ["start", "compact"] if trigger == "compact" else ["start"]
    scope = "(path LIKE '/user/%%' OR path LIKE '/feedback/%%'"
    params: list = []
    if base:
        scope += " OR path = %s OR path LIKE %s"
        params += [base, f"{base}/%"]
    scope += ")"

    pinned = conn.execute(
        f"SELECT path, type, title, body FROM memory_nodes "
        f"WHERE {_LIVE} AND pin_triggers && %s::text[] AND {scope} "
        f"ORDER BY importance DESC, path",
        [triggers, *params],
    ).fetchall()
    pinned_paths = {r["path"] for r in pinned}

    project_rows = []
    if base:
        project_rows = conn.execute(
            f"SELECT path, title, body, importance, access_count, updated_at FROM memory_nodes "
            f"WHERE {_LIVE} AND {_NOT_AUTO} AND type <> 'category' AND (path = %s OR path LIKE %s)",
            (base, f"{base}/%"),
        ).fetchall()
        project_rows = sorted((r for r in project_rows if r["path"] not in pinned_paths),
                              key=lambda r: (-_rank_score(r), r["path"]))

    global_rows = conn.execute(
        f"SELECT path, title, body, importance, access_count, embedding FROM memory_nodes "
        f"WHERE {_LIVE} AND type <> 'category' AND importance >= %s "
        f"AND (path LIKE '/user/%%' OR path LIKE '/feedback/%%')",
        (GLOBAL_MIN_IMPORTANCE,),
    ).fetchall()
    centroid = _project_centroid(conn, base) if base else None
    global_rows = sorted((r for r in global_rows if r["path"] not in pinned_paths),
                         key=lambda r: (-_global_score(r, centroid), r["path"]))[:GLOBAL_MAX_LINES]

    out: list[str] = []
    used = 0

    def add(line: str) -> bool:
        nonlocal used
        if used + len(line) + 1 > budget_chars:
            return False
        out.append(line)
        used += len(line) + 1
        return True

    label = slug or "—"
    add(f"# diary-mcp Memory — Projekt '{label}' (automatisch geladen)")

    if pinned:
        add("\n## Gepinnt")
        for r in pinned:
            body = (r["body"] or "").strip()
            if len(body) > PINNED_BODY_MAX_CHARS:
                body = body[:PINNED_BODY_MAX_CHARS].rstrip() + f"\n… [gekürzt → memory_get(\"{r['path']}\")]"
            if not add(f"### {r['title']} ⟨{r['path']}⟩\n{body}"):
                break

    # Project index gets the larger share; global rules the remainder.
    omitted_project = 0
    if project_rows:
        add(f"\n## Projekt-Index ({len(project_rows)} Memories, wichtigste zuerst)")
        project_cap = used + int((budget_chars - used) * 0.7)
        for i, r in enumerate(project_rows):
            line = f"- {r['path'][len(base):] or '/'} — {r['title']}"
            hook = node_hook(r["body"])
            if hook and hook != r["title"]:
                line += f": {hook}"
            if used + len(line) + 1 > project_cap or not add(line):
                omitted_project = len(project_rows) - i
                break

    omitted_global = 0
    if global_rows:
        add("\n## Globale Regeln & Präferenzen")
        for i, r in enumerate(global_rows):
            hook = node_hook(r["body"])
            line = f"- {r['title']}" + (f": {hook}" if hook and hook != r["title"] else "") + f" ⟨{r['path']}⟩"
            if not add(line):
                omitted_global = len(global_rows) - i
                break

    footer = []
    if omitted_project or omitted_global:
        footer.append(f"(+ {omitted_project + omitted_global} weitere nicht gezeigt — Budget.)")
    if base and project_rows:
        footer.append(f"Pfade im Index relativ zu {base}.")
    footer.append("Volltext: memory_get(path) · Suche: memory_recall(query) · "
                  "Neues/Geändertes sofort per memory_upsert sichern.")
    out.append("\n" + "\n".join(footer))
    return "\n".join(out).strip()


# ---------------------------------------------------------------------------
# Per-prompt retrieval
# ---------------------------------------------------------------------------

def _prompt_lexemes(conn, prompt: str) -> list[str]:
    row = conn.execute(
        "SELECT coalesce(array_agg(l), '{}') AS lx "
        "FROM unnest(tsvector_to_array(to_tsvector('german', %s))) l WHERE length(l) > 2",
        (prompt[:4000],),
    ).fetchone()
    return list(row["lx"])


def _doc_freqs(conn) -> tuple[int, dict[str, int]]:
    """Corpus document frequencies, cached on disk: ts_stat over the whole
    curated corpus costs ~200 ms — too slow for every prompt."""
    cache = _state_dir() / "df_cache.json"
    try:
        data = json.loads(cache.read_text())
        if (time.time() - data["built"] < DF_CACHE_TTL_S
                and data.get("db") == _database_url()):
            return data["n"], data["df"]
    except Exception:
        pass
    n = conn.execute(f"SELECT count(*) AS c FROM memory_nodes WHERE {_LIVE}").fetchone()["c"]
    rows = conn.execute(
        f"SELECT word, ndoc FROM ts_stat($q$SELECT {_DOC} FROM memory_nodes WHERE "
        f"deleted_at IS NULL AND origin = 'curated'$q$)"
    ).fetchall()
    df = {r["word"]: r["ndoc"] for r in rows}
    try:
        tmp = cache.with_suffix(".tmp")
        tmp.write_text(json.dumps({"built": time.time(), "db": _database_url(), "n": n, "df": df}))
        tmp.replace(cache)
    except Exception:
        pass
    return n, df


def invalidate_df_cache() -> None:
    try:
        (_state_dir() / "df_cache.json").unlink()
    except Exception:
        pass


def _idf(n: int, df: int) -> float:
    return math.log(1 + (n - df + 0.5) / (df + 0.5))


def _lexical_hits(conn, lexemes: list[str], base: str | None, exclude: set[str]) -> list[dict]:
    tsq = " | ".join("'" + lx.replace("'", "''") + "'" for lx in lexemes)
    rows = conn.execute(
        f"SELECT path, title, body, importance, tsvector_to_array(to_tsvector('german', coalesce(title,''))) AS tlx, "
        f"tsvector_to_array({_DOC}) AS dlx FROM memory_nodes "
        f"WHERE {_LIVE} AND {_NOT_AUTO} AND type <> 'category' "
        f"AND {_DOC} @@ to_tsquery('simple', %s) "
        f"ORDER BY ts_rank_cd({_DOC}, to_tsquery('simple', %s), 32) DESC LIMIT 60",
        (tsq, tsq),
    ).fetchall()
    if not rows:
        return []
    n, df = _doc_freqs(conn)
    n = max(n, len(rows))
    qset = set(lexemes)
    hits = []
    for r in rows:
        if r["path"] in exclude:
            continue
        matched = qset & set(r["dlx"])
        if len(matched) < LEX_MIN_MATCHES:
            continue
        title_matched = qset & set(r["tlx"])
        score = sum(_idf(n, df.get(lx, 1)) for lx in matched)
        score += 0.5 * sum(_idf(n, df.get(lx, 1)) for lx in title_matched)
        if score < LEX_MIN_SCORE:
            continue
        if base and (r["path"] == base or r["path"].startswith(base + "/")):
            score *= 1.3
        score *= 0.5 + 0.5 * float(r["importance"] or 0.5)
        hits.append({"path": r["path"], "title": r["title"], "body": r["body"], "score": score})
    hits.sort(key=lambda h: -h["score"])
    return _relative_cutoff(hits)


def _relative_cutoff(hits: list[dict]) -> list[dict]:
    """Expects hits sorted by score desc."""
    if not hits:
        return hits
    floor = hits[0]["score"] * LEX_RELATIVE_CUTOFF
    return [h for h in hits if h["score"] >= floor]


def _has_pgvector(conn) -> bool:
    row = conn.execute(
        "SELECT 1 AS ok FROM information_schema.columns "
        "WHERE table_name = 'memory_nodes' AND column_name = 'embedding_v'"
    ).fetchone()
    return row is not None


def _semantic_hits(conn, qvec: list[float], exclude: set[str]) -> list[dict]:
    if _has_pgvector(conn):
        vec = "[" + ",".join(f"{x:.6f}" for x in qvec) + "]"
        rows = conn.execute(
            f"SELECT path, title, body, 1 - (embedding_v <=> %s::vector) AS sim FROM memory_nodes "
            f"WHERE {_LIVE} AND {_NOT_AUTO} AND embedding_v IS NOT NULL AND type <> 'category' "
            f"ORDER BY embedding_v <=> %s::vector LIMIT 10",
            (vec, vec),
        ).fetchall()
    else:
        import diary_embed
        cand = conn.execute(
            f"SELECT path, title, body, embedding FROM memory_nodes "
            f"WHERE {_LIVE} AND {_NOT_AUTO} AND embedding IS NOT NULL AND type <> 'category'"
        ).fetchall()
        rows = sorted(({**r, "sim": diary_embed.cosine(qvec, r["embedding"])} for r in cand),
                      key=lambda r: -r["sim"])[:10]
    return [{"path": r["path"], "title": r["title"], "body": r["body"], "score": float(r["sim"])}
            for r in rows if r["sim"] >= SEM_MIN_SIM and r["path"] not in exclude]


def retrieve_for_prompt(conn, prompt: str, slug: str | None, qvec: list[float] | None = None,
                        exclude: set[str] | None = None, limit: int = PROMPT_MAX_HITS) -> list[dict]:
    exclude = exclude or set()
    lexemes = _prompt_lexemes(conn, prompt)
    if len(lexemes) < PROMPT_MIN_LEXEMES and qvec is None:
        return []
    base = _project_base(slug)
    lex = _lexical_hits(conn, lexemes, base, exclude) if len(lexemes) >= PROMPT_MIN_LEXEMES else []
    sem = _semantic_hits(conn, qvec, exclude) if qvec else []
    if not sem:
        return lex[:limit]
    # Reciprocal rank fusion — scores of both lists live on different scales.
    fused: dict[str, float] = {}
    by_path: dict[str, dict] = {}
    for hits in (lex, sem):
        for rank, h in enumerate(hits):
            fused[h["path"]] = fused.get(h["path"], 0.0) + 1.0 / (60 + rank)
            by_path.setdefault(h["path"], h)
    order = sorted(fused, key=lambda p: -fused[p])
    return [by_path[p] for p in order[:limit]]


def _query_vector(prompt: str) -> list[float] | None:
    """Embedding via the already-running shared server only — never loads the
    model in the short-lived hook process (that would stall every prompt)."""
    try:
        import diary_embed_ipc as ipc
        path = ipc.sock_path()
        if not path.exists():
            return None
        vecs = ipc.request_remote([prompt[:2000]], timeout=SEM_TIMEOUT_S)
        return vecs[0] if vecs else None
    except Exception:
        return None


def format_prompt_hits(hits: list[dict]) -> str:
    lines = ["# diary-mcp: passende Memories zu diesem Prompt (automatisch)"]
    for h in hits:
        body = (h["body"] or "").strip()
        if len(body) > PROMPT_SNIPPET_CHARS:
            body = body[:PROMPT_SNIPPET_CHARS].rstrip() + f" … [→ memory_get(\"{h['path']}\")]"
        lines.append(f"## {h['title']} ⟨{h['path']}⟩\n{body}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Per-session dedupe state
# ---------------------------------------------------------------------------

def _session_file(session_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", session_id)[:100] or "default"
    return _state_dir() / "sessions" / f"{safe}.json"


def load_injected(session_id: str | None) -> set[str]:
    if not session_id:
        return set()
    try:
        return set(json.loads(_session_file(session_id).read_text()))
    except Exception:
        return set()


def remember_injected(session_id: str | None, paths: set[str]) -> None:
    if not session_id:
        return
    f = _session_file(session_id)
    f.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    f.write_text(json.dumps(sorted(paths)))
    _gc_sessions(f.parent)


def _gc_sessions(directory: Path, max_age_s: int = 7 * 86400) -> None:
    cutoff = time.time() - max_age_s
    for p in directory.glob("*.json"):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
        except OSError:
            pass


def reset_session(session_id: str | None) -> None:
    if session_id:
        try:
            _session_file(session_id).unlink()
        except FileNotFoundError:
            pass


# ---------------------------------------------------------------------------
# Hook entry point
# ---------------------------------------------------------------------------

def slug_from_cwd(conn, cwd: str) -> str:
    """Project slug for cwd: registered dirs (exact, then longest prefix) →
    basename fallback. Same rules as scripts/_slug_resolve.py."""
    cwd = cwd.rstrip("/")
    rows = conn.execute(
        "SELECT slug, config->'dirs' AS dirs FROM memory_nodes "
        "WHERE path LIKE '/projects/%%' AND path NOT LIKE '/projects/%%/%%' "
        "AND deleted_at IS NULL AND config ? 'dirs'"
    ).fetchall()
    best, best_len = None, -1
    for r in rows:
        for d in r["dirs"] if isinstance(r["dirs"], list) else []:
            if not isinstance(d, str):
                continue
            d = d.rstrip("/")
            if cwd == d:
                return r["slug"]
            if cwd.startswith(d + "/") and len(d) > best_len:
                best, best_len = r["slug"], len(d)
    if best:
        return best
    base = os.path.basename(cwd)
    return re.sub(r"[^a-z0-9]+", "-", base.lower()).strip("-") or "misc"


def _hook_output(event_name: str, context: str) -> str:
    return json.dumps({"hookSpecificOutput": {"hookEventName": event_name,
                                              "additionalContext": context}})


def run_hook(event: str, payload: dict) -> str:
    try:
        import psycopg
        from psycopg.rows import dict_row
        cwd = payload.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
        session_id = payload.get("session_id")
        if event == "session-start":
            with psycopg.connect(_database_url(), row_factory=dict_row, connect_timeout=3) as conn:
                slug = slug_from_cwd(conn, cwd)
                trigger = "compact" if payload.get("source") == "compact" else "start"
                digest = build_session_digest(conn, slug, trigger=trigger)
            # Fresh or summarized context → earlier per-prompt injections are gone.
            reset_session(session_id)
            return _hook_output("SessionStart", digest)
        if event == "prompt":
            prompt = (payload.get("prompt") or "").strip()
            if not prompt or prompt.startswith("/") and " " not in prompt:
                return ""
            qvec = _query_vector(prompt)
            injected = load_injected(session_id)
            with psycopg.connect(_database_url(), row_factory=dict_row, connect_timeout=2) as conn:
                slug = slug_from_cwd(conn, cwd)
                hits = retrieve_for_prompt(conn, prompt, slug, qvec=qvec, exclude=injected)
                if not hits:
                    return ""
                conn.execute(
                    "UPDATE memory_nodes SET access_count = access_count + 1, accessed_at = now() "
                    "WHERE path = ANY(%s)", ([h["path"] for h in hits],))
            remember_injected(session_id, injected | {h["path"] for h in hits})
            return _hook_output("UserPromptSubmit", format_prompt_hits(hits))
    except Exception:
        return ""
    return ""


def main() -> None:
    event = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        payload = json.load(sys.stdin)
    except Exception:
        payload = {}
    out = run_hook(event, payload)
    if out:
        print(out)


if __name__ == "__main__":
    main()
