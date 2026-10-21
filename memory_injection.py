"""
Automatic context injection — the part that makes diary knowledge reach the
model without it having to remember a tool call.

Three consumers share this module:
  • SessionStart hook  → build_session_hint(): project pins + one line on
    what exists and how to fetch it. Nothing else is preloaded.
  • UserPromptSubmit hook → retrieve_for_prompt(): at most one memory, gated
    by a logistic model over lexical coverage, cosine similarity (via the
    already-running shared embedding server, diary_embed_ipc — the hook never
    loads a model itself) and how clearly the memory leads both signals.
    Fitted and measured with `diary-eval` on labelled real prompts.
  • memory_project_context / memory_context tools → build_session_digest():
    the token-budgeted project index Claude pulls on demand.

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
from dataclasses import dataclass, replace
from pathlib import Path

HOOK_MAX_CHARS = 120
DIGEST_BUDGET_CHARS = 8000
PINNED_BODY_MAX_CHARS = 2500
GLOBAL_MIN_IMPORTANCE = 0.7
GLOBAL_MAX_LINES = 20

# Per-prompt injection is deliberately conservative (Tom 2026-09-27): nothing
# is better than a wrong memory — Claude can always search itself. One hit:
# a second one cost precision on the gold set (94 % → 92 %) for no recall.
PROMPT_MAX_HITS = 1
PROMPT_SNIPPET_CHARS = 600
PROMPT_MIN_LEXEMES = 2
MIN_CONFIDENCE = 0.9
MIN_IMPORTANCE = 0.5
LEX_MIN_MATCHES = 2
LEX_MIN_SCORE = 7.0  # summed idf of matched terms: two generic words never qualify

# Stemmed conversational filler that Postgres' german stopword list keeps.
# Memories quoting the user contain the same filler, which made it look
# relevant (live false positive 2026-09-27).
_FILLER = frozenset("""
    dass halt gern gerne mocht mochtest moglich moglichkeit kann kannst konnt irgendwi
    irgendwas bitt mal eigent eigentlich einfach schon wirklich eben gerad genau nochmal
    sowas etwa ding mach macht soll sollt sollst wurd wurde wurden gibt geht bess best
    besteht jetzt dann denn also doch noch immer ganz bisschen klar okay hallo danke
    brauch brauchst woll wollt will hatt hast habe gut neu
""".split())
# Confidence from cosine similarity of the multilingual MiniLM model, linear
# between these points: 0.50 → 0, 0.80 → 1 (0.77 ≈ 90 %). A heuristic
# calibration, not a probability.
SEM_SIM_ZERO = 0.50
SEM_SIM_FULL = 0.80
SEM_TIMEOUT_S = 1.0
DF_CACHE_TTL_S = 6 * 3600
# Usage feedback: a reply "used" an injected memory if it names its path or
# if, among all memories, this one ranks in the top USE_TOP_SHARE for overlap
# with the reply (idf mass of shared terms the prompt didn't contain, divided
# by √ of the memory's own mass). Raw overlap is useless: long replies share
# terms with nearly everything (checked on Tom's transcripts 2026-10-21).
# Fact memories judged USE_MIN_SAMPLES times without one use stop being
# injected; behaviour rules (/feedback, /user) shape conduct, not wording, so
# they are judged but never suppressed.
USE_TOP_SHARE = 0.01
USE_MIN_SAMPLES = 3
USE_SUPPRESSIBLE = ("/projects/", "/references/")
LOG_MAX_BYTES = 2_000_000

# Pins live only inside projects (/projects/<slug>/...) and every injected pin
# block carries this instruction, so pins get pruned instead of accumulating.
PIN_INSTRUCTION = ("Pins nur für wirklich relevante, dauerhaft nötige Projekt-Infos. "
                   "Bringt ein Pin keinen Mehrwert (veraltet, redundant, selten gebraucht), "
                   "ihn per memory_unpin(path) aussortieren.")


def is_pinnable(path: str) -> bool:
    parts = path.strip("/").split("/")
    return len(parts) >= 3 and parts[0] == "projects" and all(parts)

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
    pinned = []
    if base:
        pinned = conn.execute(
            f"SELECT path, type, title, body FROM memory_nodes "
            f"WHERE {_LIVE} AND pin_triggers && %s::text[] AND path LIKE %s "
            f"ORDER BY importance DESC, path",
            (triggers, f"{base}/%"),
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
        add(f"\n## Gepinnt\n({PIN_INSTRUCTION})")
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


def build_session_hint(conn, slug: str | None, trigger: str = "start") -> str:
    """What the SessionStart hook injects: project pins in full plus one line
    saying what exists and how to fetch it. The project index itself is not
    preloaded (Tom 2026-09-27) — many sessions never need it; Claude pulls it
    via memory_project_context / memory_recall when it does."""
    base = _project_base(slug)
    if not base:
        return ""
    triggers = ["start", "compact"] if trigger == "compact" else ["start"]
    pinned = conn.execute(
        f"SELECT path, title, body FROM memory_nodes WHERE {_LIVE} AND pin_triggers && %s::text[] "
        f"AND path LIKE %s ORDER BY importance DESC, path",
        (triggers, f"{base}/%"),
    ).fetchall()
    row = conn.execute(
        f"SELECT count(*) AS n, max(updated_at) AS last FROM memory_nodes "
        f"WHERE {_LIVE} AND {_NOT_AUTO} AND type <> 'category' AND path LIKE %s",
        (f"{base}/%",),
    ).fetchone()
    if not pinned and not row["n"]:
        return ""
    lines = [f"diary-mcp: Projekt '{slug}' hat {row['n']} Memories "
             f"(zuletzt geändert {str(row['last'])[:10]}). Bei Bedarf selbst laden: "
             f"memory_project_context('{slug}') für den Index, memory_recall(query) für gezielte Suche."]
    if pinned:
        lines.append(f"\n## Gepinnt\n({PIN_INSTRUCTION})")
        for r in pinned:
            body = (r["body"] or "").strip()
            if len(body) > PINNED_BODY_MAX_CHARS:
                body = body[:PINNED_BODY_MAX_CHARS].rstrip() + f"\n… [gekürzt → memory_get(\"{r['path']}\")]"
            lines.append(f"### {r['title']} ⟨{r['path']}⟩\n{body}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Per-prompt retrieval
# ---------------------------------------------------------------------------

def _prompt_lexemes(conn, prompt: str) -> list[str]:
    row = conn.execute(
        "SELECT coalesce(array_agg(l), '{}') AS lx "
        "FROM unnest(tsvector_to_array(to_tsvector('german', %s))) l WHERE length(l) > 2",
        (prompt[:4000],),
    ).fetchone()
    return [lx for lx in row["lx"] if lx not in _FILLER]


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


# ---------------------------------------------------------------------------
# Learned gate. Raw similarity is a poor absolute signal (on Tom's labelled
# prompts, right and wrong top hits overlap almost completely), but the right
# memory is usually the clear #1 of both signals. A logistic model over these
# relative features is fitted offline by `diary-eval fit`; DEFAULT_MODEL is
# that fit, a written model file overrides it.
# ---------------------------------------------------------------------------

FEATURE_NAMES = ("sim", "lex", "sem_margin", "lex_margin", "sem_top", "lex_top",
                 "both_top", "importance", "in_project")


@dataclass(frozen=True)
class InjectionModel:
    weights: dict
    min_prob: float = 0.5
    trained_on: int = 0
    fitted_at: str = ""

    def prob(self, features) -> float:
        z = self.weights.get("bias", 0.0) + sum(
            self.weights.get(n, 0.0) * x for n, x in zip(FEATURE_NAMES, features))
        return 1 / (1 + math.exp(-max(-60.0, min(60.0, z))))


DEFAULT_MODEL = InjectionModel(weights={"bias": -4.91, "sim": 4.14, "lex": 3.19, "sem_margin": 2.46,
                                        "lex_margin": 0.3, "sem_top": 1.68, "lex_top": 1.94,
                                        "both_top": 0.87, "importance": -0.29, "in_project": 2.19},
                               min_prob=0.5, trained_on=169, fitted_at="2026-10-21")


def _model_path() -> Path:
    env = os.environ.get("DIARY_INJECTION_MODEL")
    if env:
        return Path(env)
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "diary-mcp" / "injection_model.json"


def load_model() -> InjectionModel:
    """Model file if present and well-formed, else DEFAULT_MODEL. Never raises."""
    try:
        data = json.loads(_model_path().read_text())
        weights = {k: float(v) for k, v in data["weights"].items()}
        if set(weights) != set(FEATURE_NAMES) | {"bias"}:
            return DEFAULT_MODEL
        return InjectionModel(weights=weights, min_prob=float(data.get("min_prob", DEFAULT_MODEL.min_prob)),
                              trained_on=int(data.get("trained_on", 0)), fitted_at=str(data.get("fitted_at", "")))
    except Exception:
        return DEFAULT_MODEL


def save_model(model: InjectionModel) -> Path:
    path = _model_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"weights": model.weights, "min_prob": model.min_prob,
                               "trained_on": model.trained_on, "fitted_at": model.fitted_at}, indent=2))
    tmp.replace(path)
    return path


def candidate_features(cands: list[dict]) -> list[list[float]]:
    """Per candidate, in FEATURE_NAMES order. Margins are relative to the
    best other candidate of the same signal."""
    sims = sorted((c["sim"] for c in cands if c.get("sim") is not None), reverse=True)
    lexs = sorted((c["lex_conf"] for c in cands if c.get("lex_conf", 0) > 0), reverse=True)
    out = []
    for c in cands:
        s = c["sim"] if c.get("sim") is not None else 0.0
        l = c.get("lex_conf", 0.0)
        sem_top = c.get("sim") is not None and s == sims[0]
        lex_top = l > 0 and l == lexs[0]
        if sims:
            other = (sims[1] if len(sims) > 1 else 0.0) if sem_top else sims[0]
            sem_margin = s - other
        else:
            sem_margin = 0.0
        if l > 0:
            other = (lexs[1] if len(lexs) > 1 else 0.0) if lex_top else lexs[0]
            lex_margin = l - other
        else:
            lex_margin = 0.0
        out.append([s, l, sem_margin, lex_margin, float(sem_top), float(lex_top),
                    float(sem_top and lex_top), float(c.get("importance", 0.5)),
                    float(bool(c.get("in_project")))])
    return out


@dataclass(frozen=True)
class RetrievalParams:
    """Every knob of the per-prompt gate; defaults are the live settings.
    mode 'learned' gates on the model probability (min_prob None → the
    model's own threshold); 'threshold' is the pre-0.28 absolute gate."""
    max_hits: int = PROMPT_MAX_HITS
    min_confidence: float = MIN_CONFIDENCE
    min_importance: float = MIN_IMPORTANCE
    lex_min_matches: int = LEX_MIN_MATCHES
    lex_min_score: float = LEX_MIN_SCORE
    sem_sim_zero: float = SEM_SIM_ZERO
    sem_sim_full: float = SEM_SIM_FULL
    mode: str = "learned"
    min_prob: float | None = None


DEFAULT_PARAMS = RetrievalParams()


def _lexical_hits(conn, lexemes: list[str], base: str | None, exclude: set[str],
                  params: RetrievalParams = DEFAULT_PARAMS) -> list[dict]:
    tsq = " | ".join("'" + lx.replace("'", "''") + "'" for lx in lexemes)
    rows = conn.execute(
        f"SELECT path, title, body, importance, provenance, "
        f"tsvector_to_array(to_tsvector('german', coalesce(title,''))) AS tlx, "
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
    # Terms no memory contains still count: a prompt about "rsync + Kubernetes"
    # is not answered by a memory that only knows rsync.
    query_mass = sum(_idf(n, df.get(lx, 0)) for lx in qset) or 1.0
    hits = []
    for r in rows:
        if r["path"] in exclude:
            continue
        matched = qset & set(r["dlx"])
        if len(matched) < params.lex_min_matches:
            continue
        matched_mass = sum(_idf(n, df.get(lx, 1)) for lx in matched)
        if matched_mass < params.lex_min_score:
            continue
        # Confidence: the memory must cover the prompt's (idf-weighted) content
        # AND be about it — long logs mention everything somewhere, so the
        # title has to carry a good part of the query too.
        title_mass = sum(_idf(n, df.get(lx, 1)) for lx in qset & set(r["tlx"]))
        confidence = min(1.0, matched_mass / query_mass, 0.5 + title_mass / query_mass)
        hits.append({"path": r["path"], "title": r["title"], "body": r["body"], "provenance": r["provenance"],
                     "importance": float(r["importance"] or 0.5), "lex_conf": confidence})
    return hits


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
            f"SELECT path, title, body, importance, provenance, 1 - (embedding_v <=> %s::vector) AS sim FROM memory_nodes "
            f"WHERE {_LIVE} AND {_NOT_AUTO} AND embedding_v IS NOT NULL AND type <> 'category' "
            f"ORDER BY embedding_v <=> %s::vector LIMIT 10",
            (vec, vec),
        ).fetchall()
    else:
        import diary_embed
        cand = conn.execute(
            f"SELECT path, title, body, importance, provenance, embedding FROM memory_nodes "
            f"WHERE {_LIVE} AND {_NOT_AUTO} AND embedding IS NOT NULL AND type <> 'category'"
        ).fetchall()
        rows = sorted(({**r, "sim": diary_embed.cosine(qvec, r["embedding"])} for r in cand),
                      key=lambda r: -r["sim"])[:10]
    return [{"path": r["path"], "title": r["title"], "body": r["body"], "provenance": r["provenance"],
             "importance": float(r["importance"] or 0.5), "sim": float(r["sim"])}
            for r in rows if r["path"] not in exclude]


def _never_used(conn) -> set[str]:
    """Paths injected USE_MIN_SAMPLES+ times without any judged use."""
    try:
        with conn.transaction():  # savepoint: a missing table must not abort the caller's transaction
            rows = conn.execute(
                "SELECT path FROM injection_feedback GROUP BY path "
                "HAVING count(*) >= %s AND NOT bool_or(used)", (USE_MIN_SAMPLES,)).fetchall()
        return {r["path"] for r in rows if r["path"].startswith(USE_SUPPRESSIBLE)}
    except Exception:
        return set()


def score_candidates(conn, prompt: str, slug: str | None, qvec: list[float] | None = None,
                     exclude: set[str] | None = None,
                     params: RetrievalParams = DEFAULT_PARAMS) -> list[dict]:
    """Every memory either signal found, with the raw signals (lex_conf, sim)
    kept apart so select_hits() can re-gate them under other parameters."""
    exclude = set(exclude or ()) | _never_used(conn)
    lexemes = _prompt_lexemes(conn, prompt)
    if len(lexemes) < PROMPT_MIN_LEXEMES and qvec is None:
        return []
    base = _project_base(slug)
    lex = (_lexical_hits(conn, lexemes, base, exclude, params)
           if len(lexemes) >= PROMPT_MIN_LEXEMES else [])
    sem = _semantic_hits(conn, qvec, exclude) if qvec else []
    merged: dict[str, dict] = {}
    for h in lex + sem:
        c = merged.setdefault(h["path"], {"path": h["path"], "title": h["title"], "body": h["body"],
                                          "provenance": h.get("provenance"),
                                          "importance": h["importance"], "lex_conf": 0.0, "sim": None,
                                          "in_project": bool(base) and (h["path"] == base
                                                                        or h["path"].startswith(base + "/"))})
        if "lex_conf" in h:
            c["lex_conf"] = h["lex_conf"]
        if "sim" in h:
            c["sim"] = h["sim"]
    return list(merged.values())


def candidate_confidence(c: dict, params: RetrievalParams = DEFAULT_PARAMS) -> float:
    sem = 0.0
    if c.get("sim") is not None:
        span = params.sem_sim_full - params.sem_sim_zero
        sem = max(0.0, min(1.0, (c["sim"] - params.sem_sim_zero) / span)) if span > 0 else 0.0
    # Independent evidence from both signals: 1 - (1 - a)(1 - b).
    return 1 - (1 - c.get("lex_conf", 0.0)) * (1 - sem)


def rank_candidates(cands: list[dict], params: RetrievalParams = DEFAULT_PARAMS,
                    model: InjectionModel | None = None) -> list[dict]:
    if params.mode == "learned":
        model = model or load_model()
        confs = [model.prob(f) for f in candidate_features(cands)]
    else:
        confs = [candidate_confidence(c, params) for c in cands]
    ranked = [{**c, "confidence": conf} for c, conf in zip(cands, confs)]
    ranked.sort(key=lambda h: (-h["confidence"], -h["importance"], h["path"]))
    return ranked


def gate_threshold(params: RetrievalParams = DEFAULT_PARAMS, model: InjectionModel | None = None) -> float:
    if params.mode != "learned":
        return params.min_confidence
    return params.min_prob if params.min_prob is not None else (model or load_model()).min_prob


def select_hits(cands: list[dict], params: RetrievalParams = DEFAULT_PARAMS,
                model: InjectionModel | None = None) -> list[dict]:
    if params.mode == "learned":
        model = model or load_model()
    threshold = gate_threshold(params, model)
    return [h for h in rank_candidates(cands, params, model)
            if h["confidence"] >= threshold
            and h["importance"] >= params.min_importance][:params.max_hits]


def retrieve_for_prompt(conn, prompt: str, slug: str | None, qvec: list[float] | None = None,
                        exclude: set[str] | None = None, limit: int | None = None,
                        params: RetrievalParams = DEFAULT_PARAMS) -> list[dict]:
    if limit is not None:
        params = replace(params, max_hits=limit)
    return select_hits(score_candidates(conn, prompt, slug, qvec, exclude, params), params)


def _query_vector(prompt: str) -> tuple[list[float] | None, str]:
    """Embedding via the already-running shared server only — never loads the
    model in the short-lived hook process (that would stall every prompt).
    Returns (vector, reason); reason says why the vector is missing."""
    try:
        import diary_embed_ipc as ipc
        path = ipc.sock_path()
        if not path.exists():
            return None, "no_socket"
        vecs = ipc.request_remote([prompt[:2000]], timeout=SEM_TIMEOUT_S)
        return (vecs[0], "ok") if vecs else (None, "empty")
    except TimeoutError:
        return None, "timeout"
    except Exception as e:
        return None, type(e).__name__


_PROVENANCE_NOTE = {"user": " (von Tom gesagt)", "claude": " (von Claude erschlossen)"}


def format_prompt_hits(hits: list[dict]) -> str:
    lines = ["# diary-mcp: passende Memories zu diesem Prompt (automatisch)"]
    for h in hits:
        body = (h["body"] or "").strip()
        if len(body) > PROMPT_SNIPPET_CHARS:
            body = body[:PROMPT_SNIPPET_CHARS].rstrip() + f" … [→ memory_get(\"{h['path']}\")]"
        origin = _PROVENANCE_NOTE.get(h.get("provenance"), "")
        lines.append(f"## {h['title']} ⟨{h['path']}⟩{origin}\n{body}")
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
# Usage feedback (SessionEnd)
# ---------------------------------------------------------------------------

_INJECT_MARK = "# diary-mcp: passende Memories"
_INJECT_PATH = re.compile(r"⟨(/[^⟩]+)⟩")


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    parts = []
    for c in content or []:
        if not isinstance(c, dict):
            continue
        if c.get("type") == "text":
            parts.append(c.get("text", ""))
        elif c.get("type") == "tool_use":
            parts.append(json.dumps(c.get("input", {}), ensure_ascii=False))
    return "\n".join(parts)


def _is_prompt(row: dict) -> bool:
    if row.get("type") != "user" or row.get("isMeta"):
        return False
    content = (row.get("message") or {}).get("content")
    if isinstance(content, str):
        return True
    return bool(content) and all(isinstance(c, dict) and c.get("type") == "text" for c in content)


def injections_in_transcript(path) -> list[dict]:
    """Each diary injection with the prompt it answered and everything Claude
    wrote (text + tool inputs) until the next real prompt."""
    out, current, last_prompt = [], None, ""
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if _is_prompt(row):
            current = None
            last_prompt = _text_of((row.get("message") or {}).get("content"))
            continue
        att = row.get("attachment") or {}
        if row.get("type") == "attachment" and att.get("type") == "hook_additional_context":
            text = "\n".join(att.get("content") or []) if isinstance(att.get("content"), list) else str(att.get("content"))
            if _INJECT_MARK in text:
                current = {"paths": _INJECT_PATH.findall(text), "prompt": last_prompt,
                           "reply": "", "ts": row.get("timestamp")}
                out.append(current)
            continue
        if current is not None and row.get("type") == "assistant":
            current["reply"] += _text_of((row.get("message") or {}).get("content")) + "\n"
    return [o for o in out if o["paths"] and o["ts"]]


def _lexeme_set(conn, text: str) -> set[str]:
    row = conn.execute(
        "SELECT coalesce(array_agg(l), '{}') AS lx FROM unnest(tsvector_to_array(to_tsvector('german', %s))) l "
        "WHERE length(l) > 2", (text[:20000],)).fetchone()
    return set(row["lx"]) - _FILLER


def _corpus_lexemes(conn) -> dict[str, set[str]]:
    return {r["path"]: set(r["lx"]) - _FILLER for r in conn.execute(
        f"SELECT path, tsvector_to_array({_DOC}) AS lx FROM memory_nodes WHERE {_LIVE}").fetchall()}


def usage_rank(conn, path: str, prompt: str, reply: str,
               corpus: dict[str, set[str]] | None = None) -> float:
    """Share of memories that overlap the reply at least as much as `path`
    (0.0 = named outright). Lower = more clearly used."""
    if path in reply:
        return 0.0
    corpus = corpus if corpus is not None else _corpus_lexemes(conn)
    if path not in corpus:
        return 1.0
    n, df = _doc_freqs(conn)
    picked = _lexeme_set(conn, reply) - _lexeme_set(conn, prompt)

    def norm(lexemes: set[str]) -> float:
        own = sum(_idf(n, df.get(lx, 1)) for lx in lexemes)
        return sum(_idf(n, df.get(lx, 1)) for lx in lexemes & picked) / math.sqrt(own or 1.0)

    target = norm(corpus[path])
    if target <= 0:
        return 1.0
    return sum(1 for lx in corpus.values() if norm(lx) >= target) / len(corpus)


def is_used(rank: float, corpus_size: int) -> bool:
    return rank * corpus_size <= max(1, round(USE_TOP_SHARE * corpus_size))


def record_feedback(conn, session_id: str, transcript_path) -> dict:
    judged = used = 0
    injections = injections_in_transcript(transcript_path)
    corpus = _corpus_lexemes(conn) if injections else {}
    for inj in injections:
        for path in inj["paths"]:
            rank = usage_rank(conn, path, inj["prompt"], inj["reply"], corpus)
            ok = is_used(rank, len(corpus))
            cur = conn.execute(
                "INSERT INTO injection_feedback (session_id, path, injected_at, used, score) "
                "VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                (session_id, path, inj["ts"], ok, rank))
            if cur.rowcount:
                judged += 1
                used += ok
    return {"judged": judged, "used": used}


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


def log_event(event: dict) -> None:
    """Append one hook event (paths and sizes only, never content) to the
    injection log that memory_stats reads. Bounded: keeps the newer half."""
    try:
        path = _state_dir() / "injection_log.jsonl"
        line = json.dumps({"ts": round(time.time(), 3), **event}, ensure_ascii=False) + "\n"
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
        if path.stat().st_size > LOG_MAX_BYTES:
            lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
            keep, size = [], 0
            for ln in reversed(lines):
                if size + len(ln.encode()) > LOG_MAX_BYTES // 2:
                    break
                keep.append(ln)
                size += len(ln.encode())
            tmp = path.with_suffix(".tmp")
            tmp.write_text("".join(reversed(keep)), encoding="utf-8")
            tmp.replace(path)
    except Exception:
        pass


def _miss_info(cands: list[dict]) -> dict:
    """Why nothing was injected — path and score of the best candidate only,
    so thresholds can be tuned from the log without storing prompt content."""
    if not cands:
        return {"reason": "no_candidates"}
    best = rank_candidates(cands)[0]
    reason = "below_confidence" if best["confidence"] < gate_threshold() else "low_importance"
    return {"reason": reason, "best": {"path": best["path"], "conf": round(best["confidence"], 3),
                                       "imp": best["importance"]}}


def _hook_output(event_name: str, context: str) -> str:
    return json.dumps({"hookSpecificOutput": {"hookEventName": event_name,
                                              "additionalContext": context}})


def run_hook(event: str, payload: dict) -> str:
    started = time.monotonic()
    record: dict = {"event": event, "session": payload.get("session_id")}
    out = _run_hook(event, payload, record)
    if "slug" in record:  # reached the DB; skipped/failed runs aren't injection events
        record["latency_ms"] = round((time.monotonic() - started) * 1000)
        log_event(record)
    return out


def _run_hook(event: str, payload: dict, record: dict) -> str:
    try:
        import psycopg
        from psycopg.rows import dict_row
        cwd = payload.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
        session_id = payload.get("session_id")
        if event == "session-start":
            with psycopg.connect(_database_url(), row_factory=dict_row, connect_timeout=3) as conn:
                slug = slug_from_cwd(conn, cwd)
                trigger = "compact" if payload.get("source") == "compact" else "start"
                hint = build_session_hint(conn, slug, trigger=trigger)
            # Fresh or summarized context → earlier per-prompt injections are gone.
            reset_session(session_id)
            record.update(slug=slug, source=payload.get("source") or "startup", chars=len(hint))
            return _hook_output("SessionStart", hint) if hint else ""
        if event == "session-end":
            transcript = payload.get("transcript_path")
            if not transcript or not session_id:
                return ""
            with psycopg.connect(_database_url(), row_factory=dict_row, connect_timeout=3) as conn:
                slug = slug_from_cwd(conn, cwd)
                record.update(slug=slug, **record_feedback(conn, session_id, transcript))
            return ""
        if event == "prompt":
            prompt = (payload.get("prompt") or "").strip()
            if not prompt or prompt.startswith("/") and " " not in prompt:
                return ""
            qvec, sem_reason = _query_vector(prompt)
            injected = load_injected(session_id)
            with psycopg.connect(_database_url(), row_factory=dict_row, connect_timeout=2) as conn:
                slug = slug_from_cwd(conn, cwd)
                cands = score_candidates(conn, prompt, slug, qvec=qvec, exclude=injected)
                hits = select_hits(cands)
                record.update(slug=slug, semantic=qvec is not None, hits=[h["path"] for h in hits], chars=0)
                if sem_reason != "ok":
                    record["semantic_reason"] = sem_reason
                if not hits:
                    record.update(_miss_info(cands))
                    return ""
                conn.execute(
                    "UPDATE memory_nodes SET access_count = access_count + 1, accessed_at = now() "
                    "WHERE path = ANY(%s)", ([h["path"] for h in hits],))
            remember_injected(session_id, injected | {h["path"] for h in hits})
            text = format_prompt_hits(hits)
            record["chars"] = len(text)
            return _hook_output("UserPromptSubmit", text)
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
