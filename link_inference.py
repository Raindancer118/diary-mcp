"""
Confidence-scored automatic linking (v0.24.0).

Every candidate pair of curated memories gets a confidence in [0, 1]:

- An explicit reference in the text (a memory path or ``[[slug]]``) → MENTION_CONFIDENCE.
- Near-identical content (cosine ≥ DUPLICATE_COSINE) → at least DUPLICATE_CONFIDENCE.
- Otherwise a small logistic model over six signals: hub-corrected embedding
  similarity (CSLS), mutual nearest-neighbour rank, shared rare terms (idf),
  same project, same folder and shared graph neighbours (Adamic-Adar).

The model is fitted on the *deliberate* links only (link_origin = 'explicit':
set by hand or approved suggestions) with non-negative weights, so more
similarity can never lower the confidence and automatic links never train the
next round (no self-reinforcement). Until an instance has MIN_POSITIVES such
links, DEFAULT_WEIGHTS apply (fitted on Tom's corpus, 2026-09-27: 166 explicit
links, cross-validated AUC 0.905).

    confidence ≥ AUTO_CONFIDENCE     → inferred link, created automatically
    SUGGEST_CONFIDENCE … AUTO        → link_suggestions (status 'pending')
    below                            → nothing

The review list is only ever processed through memory_link_suggestions /
memory_link_suggestions_decide, on the user's explicit request. Neither the
batch nor write-time linking touches pending, rejected or approved entries;
a rejected pair is never linked or suggested again.
"""
from __future__ import annotations

import json
import math
import re
import uuid
from collections import Counter
from dataclasses import dataclass

import numpy as np

import diary_db
import memory_injection
from diary_bootstrap import mcp

AUTO_CONFIDENCE = 0.7
SUGGEST_CONFIDENCE = 0.35
MENTION_CONFIDENCE = 0.95
DUPLICATE_COSINE = 0.9
DUPLICATE_CONFIDENCE = 0.9
MIN_POSITIVES = 40

K_SEMANTIC = 15      # semantic neighbours per node that become candidates
K_LEXICAL = 10       # lexical neighbours per node that become candidates
K_HUB = 10           # neighbourhood size for the CSLS hub correction
LEX_MAX_POSTINGS = 80  # terms in more documents than this don't propose candidates
MAX_AUTO_PER_RUN = 1000  # safety net against a bug flooding the graph

FEATURES = ("semantic", "mutual_rank", "terms", "project", "folder", "neighbours")
DEFAULT_WEIGHTS = {"semantic": 0.588, "mutual_rank": 0.75, "terms": 9.212,
                   "project": 1.382, "folder": 0.397, "neighbours": 4.28}
DEFAULT_BIAS = -5.96
MODEL_META_KEY = "link_model"

_PATH_RE = re.compile(r"/(?:projects|user|feedback|references|notes|links)(?:/[\w.\-]+)+")
_WIKI_RE = re.compile(r"\[\[([\w.\-/]+)\]\]")
# evidence lists only word-like stems, not paths or version fragments
_READABLE = re.compile(r"^[a-zäöüß][a-zäöüß0-9\-]{4,}$")

_NODES_SQL = """
    SELECT id, path, updated_at, body FROM memory_nodes
     WHERE deleted_at IS NULL AND origin = 'curated' AND type <> 'category'
     ORDER BY path
"""
# Embedding + lexemes only depend on a row's content: computed once per
# (id, updated_at) and kept for the life of the process, so a write-time pass
# re-reads only the memory that was just saved (full load ≈ 600 ms otherwise).
_CONTENT_SQL = """
    SELECT id, embedding,
           (SELECT coalesce(array_agg(l), '{}')
              FROM unnest(tsvector_to_array(to_tsvector('german',
                   coalesce(title, '') || ' ' || coalesce(body, '')))) l
             WHERE length(l) > 2) AS lx
      FROM memory_nodes WHERE id = ANY(%s)
"""
_content_cache: dict[tuple[str, object], tuple[object, np.ndarray | None, frozenset]] = {}


def _content(conn, rows) -> list[tuple[np.ndarray | None, frozenset]]:
    db = diary_db.get_database_url()
    stale = [r["id"] for r in rows
             if (hit := _content_cache.get((db, r["id"]))) is None or hit[0] != r["updated_at"]]
    if stale:
        stamp = {r["id"]: r["updated_at"] for r in rows}
        for r in conn.execute(_CONTENT_SQL, (stale,)).fetchall():
            vec = None
            if r["embedding"] is not None:
                v = np.asarray(r["embedding"], dtype=np.float32)
                norm = float(np.linalg.norm(v))
                vec = v / norm if norm else None
            lx = frozenset(x for x in r["lx"] if x not in memory_injection._FILLER)
            _content_cache[(db, r["id"])] = (stamp[r["id"]], vec, lx)
    return [_content_cache[(db, r["id"])][1:] for r in rows]


# ── corpus ─────────────────────────────────────────────────────────────────

class Corpus:
    """Everything the scorer needs about the curated memories. Per-row
    neighbour ranks, idf weights and postings are computed on demand: a
    write-time pass touches one row and its ~40 candidates, the nightly
    pass calls prepare_full() once."""

    def __init__(self, ids, paths, has_vec, sim, docs):
        self.ids, self.paths, self.has_vec, self.sim, self.docs = ids, paths, has_vec, sim, docs
        self.n = len(ids)
        self.mentions: dict[tuple[int, int], int] = {}   # pair → mentioning node
        self.links: dict[tuple[int, int], list[str]] = {}  # pair → link origins
        self.explicit_adj: list[set[int]] = [set() for _ in range(self.n)]
        self.df = Counter()
        for d in docs:
            self.df.update(d)
        k_hub = max(1, min(K_HUB, self.n - 1))
        self.hub = np.zeros(self.n, dtype=np.float32)
        if self.n > 1:
            top = -np.partition(-sim, k_hub - 1, axis=1)[:, :k_hub]
            valid = top > -1.0
            cnt = valid.sum(1)
            self.hub = (np.where(valid, top, 0).sum(1) / np.maximum(cnt, 1)).astype(np.float32)
            self.hub[(cnt == 0) | ~has_vec] = 0.0
        self._terms: dict[int, dict[str, float]] = {}
        self._norm: dict[int, float] = {}
        self._rank: np.ndarray | None = None
        self._order: np.ndarray | None = None
        self._postings: dict[str, list[int]] | None = None

    def prepare_full(self) -> None:
        if self.n and self._rank is None:
            self._order = np.argsort(-self.sim, axis=1, kind="stable")
            self._rank = np.empty_like(self._order)
            self._rank[np.arange(self.n)[:, None], self._order] = np.arange(1, self.n + 1)
            self._postings = {}
            for k, d in enumerate(self.docs):
                for lx in d:
                    self._postings.setdefault(lx, []).append(k)

    def top(self, i: int, k: int) -> np.ndarray:
        if self._order is not None:
            return self._order[i, :k]
        row = -self.sim[i]
        idx = np.argpartition(row, k - 1)[:k] if k < self.n else np.arange(self.n)
        return idx[np.argsort(row[idx], kind="stable")]

    def rank(self, i: int, j: int) -> int:
        """1-based position of j among i's neighbours by similarity."""
        if self._rank is not None:
            return int(self._rank[i, j])
        return 1 + int((self.sim[i] > self.sim[i, j]).sum())

    def having_in_top(self, f: int, k: int) -> np.ndarray:
        """Rows whose k nearest neighbours include f."""
        if self._rank is not None:
            return np.nonzero(self._rank[:, f] <= k)[0]
        return np.nonzero((self.sim > self.sim[:, f:f + 1]).sum(1) < k)[0]

    def postings(self, lx: str) -> list[int]:
        if self._postings is not None:
            return self._postings[lx]
        return [k for k, d in enumerate(self.docs) if lx in d]

    def terms(self, k: int) -> dict[str, float]:
        t = self._terms.get(k)
        if t is None:
            t = self._terms[k] = {lx: memory_injection._idf(self.n, self.df[lx]) for lx in self.docs[k]}
        return t

    def norm(self, k: int) -> float:
        v = self._norm.get(k)
        if v is None:
            v = self._norm[k] = math.sqrt(sum(w * w for w in self.terms(k).values())) or 1.0
        return v


def _pair(i: int, j: int) -> tuple[int, int]:
    return (i, j) if i < j else (j, i)


def _project(path: str) -> str | None:
    parts = path.split("/")
    return parts[2] if len(parts) > 3 and parts[1] == "projects" else None


def _folder(path: str) -> str:
    return path.rsplit("/", 1)[0]


def load_corpus(conn) -> Corpus:
    rows = conn.execute(_NODES_SQL).fetchall()
    n = len(rows)
    index = {r["id"]: k for k, r in enumerate(rows)}
    paths = [r["path"] for r in rows]
    content = _content(conn, rows)

    has_vec = np.array([v is not None for v, _ in content], dtype=bool)
    dim = next((len(v) for v, _ in content if v is not None), 1)
    emb = np.zeros((n, dim), dtype=np.float32)
    if has_vec.any():
        emb[has_vec] = np.stack([v for v, _ in content if v is not None])
    sim = emb @ emb.T if n else np.zeros((0, 0), dtype=np.float32)
    sim[~has_vec, :] = -1.0
    sim[:, ~has_vec] = -1.0
    if n:
        np.fill_diagonal(sim, -1.0)
    c = Corpus([r["id"] for r in rows], paths, has_vec, sim, [lx for _, lx in content])

    by_path = {p: k for k, p in enumerate(paths)}
    by_slug: dict[str, list[int]] = {}
    for k, p in enumerate(paths):
        by_slug.setdefault(p.rsplit("/", 1)[1], []).append(k)
    for k, r in enumerate(rows):
        body = r["body"] or ""
        targets = {by_path[p] for p in _PATH_RE.findall(body) if p in by_path}
        for slug in _WIKI_RE.findall(body):
            if slug.startswith("/"):
                if slug in by_path:
                    targets.add(by_path[slug])
                continue
            hits = by_slug.get(slug.rsplit("/", 1)[-1], [])
            near = [h for h in hits if _folder(paths[h]) == _folder(paths[k])]
            if near or len(hits) == 1:  # ambiguous slugs elsewhere are skipped, not guessed
                targets.add((near or hits)[0])
        for t in targets - {k}:
            c.mentions.setdefault(_pair(k, t), k)

    for link in conn.execute("SELECT from_id, to_id, link_origin FROM memory_links").fetchall():
        a, b = index.get(link["from_id"]), index.get(link["to_id"])
        if a is None or b is None or a == b:
            continue
        c.links.setdefault(_pair(a, b), []).append(link["link_origin"])
        if link["link_origin"] == "explicit":
            c.explicit_adj[a].add(b)
            c.explicit_adj[b].add(a)
    return c


# ── candidates and features ────────────────────────────────────────────────

def candidates(c: Corpus, focus: list[int] | None = None, include_inferred: bool = True) -> set[tuple[int, int]]:
    """Pairs worth scoring: semantic and lexical neighbours, two hops in the
    deliberate graph, textual mentions and (for re-scoring) existing inferred links."""
    if focus is None:
        c.prepare_full()
    nodes = range(c.n) if focus is None else focus
    focus_set = None if focus is None else set(focus)
    out: set[tuple[int, int]] = set()

    def add(i, j):
        if i != j:
            out.add(_pair(int(i), int(j)))

    k_sem = min(K_SEMANTIC, max(c.n - 1, 0))
    for i in nodes:
        if c.has_vec[i] and k_sem:
            for j in c.top(i, k_sem):
                if c.has_vec[j]:
                    add(i, j)
            if focus_set is not None:
                for j in c.having_in_top(i, k_sem):
                    if c.has_vec[j]:
                        add(i, j)
        scores: dict[int, float] = {}
        for lx, w in c.terms(i).items():
            if c.df[lx] > LEX_MAX_POSTINGS:
                continue
            for j in c.postings(lx):
                if j != i:
                    scores[j] = scores.get(j, 0.0) + w * w
        for j, _ in sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))[:K_LEXICAL]:
            add(i, j)
        for z in c.explicit_adj[i]:
            for j in c.explicit_adj[z]:
                add(i, j)
    inferred = [p for p, o in c.links.items() if "inferred" in o] if include_inferred else []
    for (a, b) in list(c.mentions) + inferred:
        if focus_set is None or a in focus_set or b in focus_set:
            out.add((a, b))
    return out


def features(c: Corpus, i: int, j: int) -> np.ndarray:
    if c.has_vec[i] and c.has_vec[j]:
        semantic = 2 * float(c.sim[i, j]) - float(c.hub[i]) - float(c.hub[j])
        mutual_rank = 1.0 / math.sqrt(c.rank(i, j) * c.rank(j, i))
    else:
        semantic = mutual_rank = 0.0
    ti, tj = c.terms(i), c.terms(j)
    small, big = (ti, tj) if len(ti) <= len(tj) else (tj, ti)
    terms = sum(w * w for lx, w in small.items() if lx in big) / (c.norm(i) * c.norm(j))
    pi, pj = c.paths[i], c.paths[j]
    project = float(_project(pi) is not None and _project(pi) == _project(pj))
    folder = float(_folder(pi) == _folder(pj))
    common = c.explicit_adj[i] & c.explicit_adj[j]
    adamic_adar = sum(1.0 / math.log(1 + len(c.explicit_adj[z])) for z in common)
    return np.array([semantic, mutual_rank, terms, project, folder, math.log1p(adamic_adar)])


# ── model ──────────────────────────────────────────────────────────────────

@dataclass
class Model:
    weights: dict[str, float]
    bias: float
    source: str      # 'default' | 'trained'
    positives: int

    def confidence(self, x: np.ndarray) -> float:
        z = self.bias + sum(self.weights[f] * float(v) for f, v in zip(FEATURES, x))
        return 1.0 / (1.0 + math.exp(-max(-40.0, min(40.0, z))))


def _default_model(positives: int = 0) -> Model:
    return Model(dict(DEFAULT_WEIGHTS), DEFAULT_BIAS, "default", positives)


def fit(c: Corpus) -> Model:
    """Non-negative L2 logistic regression; positives = deliberate links,
    negatives = unlinked candidate pairs. Mentions are excluded — they are
    handled by a rule and would otherwise read as negatives."""
    pos = [p for p, o in c.links.items() if "explicit" in o and p not in c.mentions]
    if len(pos) < MIN_POSITIVES:
        return _default_model(len(pos))
    # Labels and pool depend on deliberate links only — excluding already
    # auto-linked pairs here would shift the model after every run.
    neg = [p for p in candidates(c, include_inferred=False)
           if "explicit" not in c.links.get(p, ()) and p not in c.mentions]
    if not neg:
        return _default_model(len(pos))
    X = np.array([features(c, i, j) for i, j in pos + neg])
    y = np.r_[np.ones(len(pos)), np.zeros(len(neg))]
    mu, sd = X.mean(0), X.std(0) + 1e-9
    Z = (X - mu) / sd
    w, b = np.zeros(Z.shape[1]), -4.0
    l2, lr = 1.0 / len(y), 10.0
    for _ in range(3000):
        p = 1.0 / (1.0 + np.exp(-(Z @ w + b)))
        w = np.maximum(0.0, w - lr * (Z.T @ (p - y) / len(y) + l2 * w))
        b -= lr * float((p - y).mean())
    raw = w / sd
    return Model({f: round(float(v), 4) for f, v in zip(FEATURES, raw)},
                 round(float(b - (w * mu / sd).sum()), 4), "trained", len(pos))


def _stored_model(conn) -> Model | None:
    row = conn.execute("SELECT value FROM diary_meta WHERE key = %s", (MODEL_META_KEY,)).fetchone()
    if not row:
        return None
    try:
        d = json.loads(row["value"])
        return Model({f: float(d["weights"][f]) for f in FEATURES}, float(d["bias"]),
                     d.get("source", "trained"), int(d.get("positives", 0)))
    except (ValueError, KeyError, TypeError):
        return None


def _store_model(conn, m: Model) -> None:
    conn.execute(
        "INSERT INTO diary_meta (key, value, updated_at) VALUES (%s, %s, now()) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
        (MODEL_META_KEY, json.dumps({"weights": m.weights, "bias": m.bias,
                                      "source": m.source, "positives": m.positives})),
    )


# ── scoring ────────────────────────────────────────────────────────────────

@dataclass
class Scored:
    i: int
    j: int
    confidence: float
    evidence: str
    source: int      # node the link points away from (the mentioning one, if any)


def _evidence(c: Corpus, i: int, j: int, x: np.ndarray, mention_by: int | None) -> list[str]:
    reasons = []
    if mention_by is not None:
        reasons.append(f"erwähnt in {c.paths[mention_by]}")
    cos = float(c.sim[i, j])
    if c.has_vec[i] and c.has_vec[j]:
        if cos >= DUPLICATE_COSINE:
            reasons.append(f"nahezu gleicher Inhalt (cos {cos:.2f})")
        elif cos >= 0.5 and max(c.rank(i, j), c.rank(j, i)) <= 5:
            reasons.append(f"semantisch nah (cos {cos:.2f}, gegenseitig unter den Top 5)")
    if x[3]:
        reasons.append(f"gleiches Projekt ({_project(c.paths[i])})")
    elif x[4]:
        reasons.append("gleicher Ordner")
    common = len(c.explicit_adj[i] & c.explicit_adj[j])
    if common:
        reasons.append(f"{common} gemeinsame{'r' if common == 1 else ''} Nachbar{'n' if common > 1 else ''}")
    if x[2] >= 0.05:
        ti, tj = c.terms(i), c.terms(j)
        shared = sorted((lx for lx in ti if lx in tj and _READABLE.match(lx)), key=lambda lx: (-ti[lx], lx))[:3]
        if shared:
            reasons.append("gemeinsame Begriffe: " + ", ".join(shared))
    return reasons


def score(c: Corpus, model: Model, pairs) -> list[Scored]:
    out = []
    for i, j in pairs:
        x = features(c, i, j)
        conf = model.confidence(x)
        mention_by = c.mentions.get((i, j))
        if mention_by is not None:
            conf = max(conf, MENTION_CONFIDENCE)
        if c.has_vec[i] and c.has_vec[j] and c.sim[i, j] >= DUPLICATE_COSINE:
            conf = max(conf, DUPLICATE_CONFIDENCE)
        source = mention_by if mention_by is not None else i
        out.append(Scored(i, j, round(conf, 4), "; ".join(_evidence(c, i, j, x, mention_by)) or "Modell",
                          source))
    out.sort(key=lambda s: -s.confidence)
    return out


# ── applying scores ────────────────────────────────────────────────────────

def _canonical(a, b):
    return (a, b) if str(a) < str(b) else (b, a)


def _apply(conn, c: Corpus, scored: list[Scored], *, dry_run: bool, auto_threshold: float,
           max_auto_non_mention: int | None = None) -> dict:
    report = {"auto": 0, "suggested": 0, "backfilled": 0, "auto_pairs": []}
    known = {}
    for r in conn.execute("SELECT id, from_id, to_id, status FROM link_suggestions").fetchall():
        known[_canonical(r["from_id"], r["to_id"])] = (r["id"], r["status"])
    auto_non_mention = 0
    for s in scored:
        a, b = c.ids[s.i], c.ids[s.j]
        pair = (s.i, s.j)
        state = known.get(_canonical(a, b))
        if pair in c.links:
            if "inferred" in c.links[pair] and not dry_run:
                cur = conn.execute(
                    "UPDATE memory_links SET confidence = %s, evidence = %s, updated_at = now() "
                    "WHERE link_origin = 'inferred' AND ((from_id = %s AND to_id = %s) OR (from_id = %s AND to_id = %s)) "
                    # confidence is REAL: compare at the stored precision, or every run rewrites every row
                    "AND (confidence IS NULL OR abs(confidence - %s) > 1e-4 OR evidence IS DISTINCT FROM %s)",
                    (s.confidence, s.evidence, a, b, b, a, s.confidence, s.evidence))
                report["backfilled"] += cur.rowcount
            elif "inferred" in c.links[pair]:
                report["backfilled"] += 1
            continue
        if state and state[1] in ("rejected", "approved"):
            continue
        if s.confidence >= auto_threshold:
            is_mention = pair in c.mentions
            if not is_mention and max_auto_non_mention is not None and auto_non_mention >= max_auto_non_mention:
                continue
            if report["auto"] >= MAX_AUTO_PER_RUN:
                break
            src, dst = (a, b) if s.source == s.i else (b, a)
            if not dry_run:
                conn.execute(
                    "INSERT INTO memory_links (from_id, to_id, rel_type, link_origin, confidence, evidence) "
                    "VALUES (%s, %s, 'related', 'inferred', %s, %s) ON CONFLICT (from_id, to_id, rel_type) DO NOTHING",
                    (src, dst, s.confidence, s.evidence))
                if state:
                    conn.execute("UPDATE link_suggestions SET status = 'auto', decided_at = now(), "
                                 "updated_at = now() WHERE id = %s", (state[0],))
            c.links.setdefault(pair, []).append("inferred")
            report["auto"] += 1
            auto_non_mention += 0 if is_mention else 1
            report["auto_pairs"].append(pair)
        elif s.confidence >= SUGGEST_CONFIDENCE:
            if state:  # pending: keep it current, it was already counted when first suggested
                if not dry_run:
                    conn.execute("UPDATE link_suggestions SET confidence = %s, evidence = %s, updated_at = now() "
                                 "WHERE id = %s AND status = 'pending' "
                                 "AND (abs(confidence - %s) > 1e-4 OR evidence IS DISTINCT FROM %s)",
                                 (s.confidence, s.evidence, state[0], s.confidence, s.evidence))
                continue
            if not dry_run:
                lo, hi = _canonical(a, b)
                conn.execute(
                    "INSERT INTO link_suggestions (from_id, to_id, confidence, evidence) VALUES (%s, %s, %s, %s) "
                    "ON CONFLICT (from_id, to_id) DO NOTHING", (lo, hi, s.confidence, s.evidence))
            report["suggested"] += 1
    return report


# ── entry points ───────────────────────────────────────────────────────────

def run(dry_run: bool = False) -> dict:
    """Full pass (nightly cron): refit the model, score every candidate pair,
    link high confidence, queue medium confidence, re-score inferred links."""
    with diary_db.get_db() as conn:
        c = load_corpus(conn)
        model = fit(c)
        if not dry_run:
            _store_model(conn, model)
        pairs = candidates(c)
        scored = score(c, model, pairs)
        report = _apply(conn, c, scored, dry_run=dry_run, auto_threshold=AUTO_CONFIDENCE)
    report.pop("auto_pairs")
    report.update(model=model.source, positives=model.positives, weights=model.weights,
                  bias=model.bias, nodes=c.n, candidates=len(pairs), dry_run=dry_run)
    return report


def link_node(conn, node_id, auto_threshold: float | None = None, max_auto: int | None = None) -> list[str]:
    """Write-time linking for one freshly saved memory, inside the caller's
    transaction. Returns the paths it was linked to automatically."""
    c = load_corpus(conn)
    try:
        k = c.ids.index(node_id)
    except ValueError:
        return []  # extracted, category or deleted
    model = _stored_model(conn) or _default_model()
    scored = score(c, model, candidates(c, focus=[k]))
    report = _apply(conn, c, scored, dry_run=False,
                    auto_threshold=AUTO_CONFIDENCE if auto_threshold is None else auto_threshold,
                    max_auto_non_mention=max_auto)
    return [c.paths[j if i == k else i] for i, j in report["auto_pairs"]]


def format_report(r: dict) -> str:
    mode = "Trockenlauf" if r.get("dry_run") else "Lauf"
    model = (f"Modell trainiert auf {r['positives']} bewussten Links" if r["model"] == "trained"
             else f"Standard-Gewichte (erst {r['positives']} von {MIN_POSITIVES} bewussten Links)")
    return (f"Link-Inferenz ({mode}): {r['auto']} automatisch verlinkt, {r['suggested']} neu vorgemerkt, "
            f"{r['backfilled']} bestehende auto-Links neu bewertet. {r['nodes']} Memories, "
            f"{r['candidates']} Kandidatenpaare. {model}.")


# ── review list ────────────────────────────────────────────────────────────
# Processed by the user in diary-web, or by Claude — but only on explicit request.

REL_TYPES = ("related", "supports", "contradicts", "requires", "derived_from")


def list_pending(limit: int = 20, min_confidence: float = 0.0) -> tuple[int, list[dict]]:
    """(total pending, highest-confidence suggestions with both memories)."""
    limit = max(1, min(int(limit), 500))
    with diary_db.get_db() as conn:
        rows = conn.execute(
            "SELECT s.id, s.confidence, s.evidence, s.created_at, "
            "a.path AS a_path, a.title AS a_title, a.type AS a_type, a.body AS a_body, "
            "b.path AS b_path, b.title AS b_title, b.type AS b_type, b.body AS b_body "
            "FROM link_suggestions s JOIN memory_nodes a ON a.id = s.from_id JOIN memory_nodes b ON b.id = s.to_id "
            "WHERE s.status = 'pending' AND s.confidence >= %s AND a.deleted_at IS NULL AND b.deleted_at IS NULL "
            "ORDER BY s.confidence DESC, s.created_at LIMIT %s", (min_confidence, limit)).fetchall()
        total = conn.execute(
            "SELECT count(*) AS n FROM link_suggestions s JOIN memory_nodes a ON a.id = s.from_id "
            "JOIN memory_nodes b ON b.id = s.to_id "
            "WHERE s.status = 'pending' AND a.deleted_at IS NULL AND b.deleted_at IS NULL").fetchone()["n"]

    def side(r, k):
        return {"path": r[f"{k}_path"], "title": r[f"{k}_title"], "type": r[f"{k}_type"],
                "hook": memory_injection.node_hook(r[f"{k}_body"])}
    return total, [{"id": str(r["id"]), "confidence": round(float(r["confidence"]), 4),
                    "evidence": r["evidence"] or "", "a": side(r, "a"), "b": side(r, "b")} for r in rows]


def decide(ids, decision: str, rel_type: str = "related", note: str = "") -> tuple[int, int]:
    """Approve (→ deliberate link) or reject pending suggestions. Returns (done, not found).
    Raises ValueError for an unknown decision or rel_type."""
    decision = decision.strip().lower()
    if decision not in ("approve", "reject"):
        raise ValueError("decision muss 'approve' oder 'reject' sein")
    if rel_type not in REL_TYPES:
        raise ValueError(f"rel_type muss einer von {', '.join(REL_TYPES)} sein")
    wanted, missing = [], 0
    for raw in ids:
        try:
            wanted.append(uuid.UUID(str(raw).strip()))
        except ValueError:
            missing += 1
    done = 0
    with diary_db.get_db() as conn:
        for sid in wanted:
            row = conn.execute("SELECT * FROM link_suggestions WHERE id = %s AND status = 'pending'",
                               (sid,)).fetchone()
            if not row:
                missing += 1
                continue
            if decision == "approve":
                conn.execute(
                    "INSERT INTO memory_links (from_id, to_id, rel_type, note, link_origin, confidence, evidence) "
                    "VALUES (%s, %s, %s, %s, 'explicit', %s, %s) ON CONFLICT (from_id, to_id, rel_type) DO NOTHING",
                    (row["from_id"], row["to_id"], rel_type,
                     note or f"bestätigter Vorschlag (Konfidenz {row['confidence']:.2f})",
                     row["confidence"], row["evidence"]))
            conn.execute("UPDATE link_suggestions SET status = %s, decided_at = now(), updated_at = now() "
                         "WHERE id = %s", ("approved" if decision == "approve" else "rejected", sid))
            done += 1
    return done, missing


@mcp.tool()
def memory_link_suggestions(limit: int = 20, min_confidence: float = 0.0) -> str:
    """Listet offene Verknüpfungs-Vorschläge mittlerer Konfidenz (höchste zuerst).

    NUR verwenden, wenn der User ausdrücklich darum bittet, die Vorschlagsliste
    anzusehen oder abzuarbeiten. Nie proaktiv, nie nebenbei, nie als
    Aufräum-Schritt anderer Aufgaben. Freigeben/Ablehnen über
    memory_link_suggestions_decide — ebenfalls nur auf ausdrückliche Anfrage.
    (Der User kann die Liste auch selbst in diary-web unter „Vorschläge" bearbeiten.)
    """
    total, items = list_pending(min(int(limit), 200), min_confidence)
    if not items:
        return "Keine offenen Verknüpfungs-Vorschläge."
    lines = [f"{total} offene Vorschläge (zeige {len(items)}):"]
    for it in items:
        lines.append(f"\n[{it['id']}] Konfidenz {it['confidence']:.2f}\n"
                     f"  {it['a']['path']} — {it['a']['title']}\n  {it['b']['path']} — {it['b']['title']}\n"
                     f"  Grund: {it['evidence']}")
    lines.append("\nEntscheiden: memory_link_suggestions_decide(ids='id1,id2', decision='approve'|'reject')")
    return "\n".join(lines)


@mcp.tool()
def memory_link_suggestions_decide(ids: str, decision: str, rel_type: str = "related", note: str = "") -> str:
    """Gibt Verknüpfungs-Vorschläge frei oder lehnt sie ab.

    NUR verwenden, wenn der User ausdrücklich darum bittet, bestimmte
    Vorschläge freizugeben oder abzulehnen (oder die Liste gemeinsam mit ihm
    durchzugehen). Nie eigenständig entscheiden.

    ids:      kommagetrennte Vorschlags-IDs aus memory_link_suggestions
    decision: 'approve' (legt einen bewussten Link an) oder 'reject'
              (das Paar wird nie wieder vorgeschlagen oder automatisch verlinkt)
    rel_type: related | supports | contradicts | requires | derived_from
    """
    try:
        done, missing = decide(ids.split(","), decision, rel_type, note)
    except ValueError as exc:
        return f"Fehler: {exc}."
    verb = "freigegeben" if decision.strip().lower() == "approve" else "abgelehnt"
    return f"{done} Vorschläge {verb}." + (f" {missing} nicht gefunden oder bereits entschieden." if missing else "")
