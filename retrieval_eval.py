"""
Offline evaluation of the per-prompt injection gate (memory_injection).

A gold set is JSONL, one prompt per line:
    {"prompt": "...", "slug": "eduvault4", "expected": ["/projects/..."], "acceptable": [...]}
`expected` empty → a negative: nothing should be injected. `acceptable` lists
paths that are fine to inject but not required (no false positive, no recall).

Candidates are scored once per prompt (DB + embedding); every parameter set is
then evaluated in memory, so a full grid sweep costs seconds.

CLI (`diary-eval`):
    diary-eval [--gold F]                    metrics for the live parameters
    diary-eval --sweep [--precision-target]  grid sweep + recommendation
    diary-eval --failures                    misses and false positives
    diary-eval mine [--limit N]              real prompts from Claude Code transcripts
    diary-eval pool PROMPTS.jsonl            loosely-gated candidates, for labelling
"""
from __future__ import annotations

import argparse
import itertools
import json
import os
import re
import sys
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path

import memory_injection as mi

DEFAULT_GRID: dict[str, list] = {
    "min_confidence": [0.9, 0.8, 0.7, 0.6, 0.5, 0.4],
    "min_importance": [0.5, 0.3],
    "sem_sim_zero": [0.4, 0.5],
    "sem_sim_full": [0.7, 0.8],
    "max_hits": [1, 2, 3],
}


def default_gold_path() -> Path:
    env = os.environ.get("DIARY_EVAL_GOLD")
    if env:
        return Path(env)
    base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "diary-mcp" / "eval" / "gold.jsonl"


@dataclass(frozen=True)
class GoldItem:
    prompt: str
    slug: str | None
    expected: tuple[str, ...]
    acceptable: tuple[str, ...]
    note: str = ""


def load_gold(path: Path | str) -> list[GoldItem]:
    items = []
    for i, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("//"):
            continue
        try:
            row = json.loads(line)
            prompt = row["prompt"]
            if not isinstance(prompt, str) or not prompt.strip():
                raise ValueError("empty prompt")
            items.append(GoldItem(prompt=prompt, slug=row.get("slug"),
                                  expected=tuple(row.get("expected") or ()),
                                  acceptable=tuple(row.get("acceptable") or ()),
                                  note=row.get("note", "")))
        except (KeyError, ValueError, TypeError) as e:
            raise ValueError(f"{path}: line {i}: {e}") from e
    return items


def collect_candidates(conn, gold: list[GoldItem], embed=None,
                       vectors: list | None = None) -> list[list[dict]]:
    """Raw candidates per gold item. Pass `vectors` (precomputed) to keep model
    inference out of an open DB transaction."""
    if vectors is None:
        vectors = [embed(g.prompt) if embed else None for g in gold]
    return [mi.score_candidates(conn, g.prompt, g.slug, qvec=v) for g, v in zip(gold, vectors)]


@dataclass
class EvalResult:
    positives: int = 0
    negatives: int = 0
    found: int = 0           # positives with ≥ 1 expected path injected
    injected: int = 0        # injected memories in total
    correct: int = 0         # injected memories that were expected or acceptable
    prompts_injected: int = 0
    negative_fp: int = 0     # negatives that got something unacceptable injected

    @property
    def recall(self) -> float:
        return self.found / self.positives if self.positives else 0.0

    @property
    def precision(self) -> float:
        return self.correct / self.injected if self.injected else 1.0

    @property
    def negative_fp_rate(self) -> float:
        return self.negative_fp / self.negatives if self.negatives else 0.0

    @property
    def injection_rate(self) -> float:
        total = self.positives + self.negatives
        return self.prompts_injected / total if total else 0.0


def evaluate(gold: list[GoldItem], cands: list[list[dict]], params: mi.RetrievalParams) -> EvalResult:
    res = EvalResult()
    for g, c in zip(gold, cands):
        hits = [h["path"] for h in mi.select_hits(c, params)]
        ok = set(g.expected) | set(g.acceptable)
        res.injected += len(hits)
        res.correct += sum(1 for h in hits if h in ok)
        res.prompts_injected += bool(hits)
        if g.expected:
            res.positives += 1
            res.found += any(h in g.expected for h in hits)
        else:
            res.negatives += 1
            res.negative_fp += any(h not in ok for h in hits)
    return res


@dataclass
class SweepRow:
    params: mi.RetrievalParams
    result: EvalResult


def sweep(gold, cands, grid: dict[str, list] | None = None,
          base: mi.RetrievalParams = mi.DEFAULT_PARAMS) -> list[SweepRow]:
    grid = grid or DEFAULT_GRID
    names = list(grid)
    rows = []
    for combo in itertools.product(*(grid[n] for n in names)):
        params = replace(base, **dict(zip(names, combo)))
        if params.sem_sim_full <= params.sem_sim_zero:
            continue
        rows.append(SweepRow(params, evaluate(gold, cands, params)))
    return rows


def recommend(rows: list[SweepRow], precision_target: float = 0.9) -> SweepRow | None:
    """Highest recall among parameter sets that keep precision ≥ target;
    ties → higher precision, fewer false positives on negatives, stricter gate."""
    ok = [r for r in rows if r.result.precision >= precision_target and r.result.injected]
    if not ok:
        return None
    return max(ok, key=lambda r: (r.result.recall, r.result.precision,
                                  -r.result.negative_fp_rate, r.params.min_confidence,
                                  -r.params.max_hits))


def format_result(name: str, r: EvalResult) -> str:
    return (f"{name}: recall {r.recall:.0%} ({r.found}/{r.positives}) · precision {r.precision:.0%} "
            f"({r.correct}/{r.injected}) · FP on negatives {r.negative_fp_rate:.0%} "
            f"({r.negative_fp}/{r.negatives}) · injected on {r.injection_rate:.0%} of prompts")


def format_params(p: mi.RetrievalParams) -> str:
    d = asdict(p)
    return ", ".join(f"{k}={d[k]}" for k in ("min_confidence", "min_importance", "max_hits",
                                             "sem_sim_zero", "sem_sim_full"))


def format_failures(gold, cands, params: mi.RetrievalParams, top: int = 3) -> str:
    out = []
    for g, c in zip(gold, cands):
        ranked = mi.rank_candidates(c, params)
        hits = [h["path"] for h in mi.select_hits(c, params)]
        ok = set(g.expected) | set(g.acceptable)
        missed = g.expected and not any(h in g.expected for h in hits)
        wrong = [h for h in hits if h not in ok]
        if not missed and not wrong:
            continue
        prompt = g.prompt if len(g.prompt) <= 90 else g.prompt[:89] + "…"
        out.append(f"{'MISS' if missed else 'FP  '} [{g.slug or '-'}] {prompt}")
        if missed:
            conf = {r["path"]: r["confidence"] for r in ranked}
            for e in g.expected:
                out.append(f"     expected {e}  conf={conf[e]:.2f}" if e in conf
                           else f"     expected {e}  (not a candidate)")
        for w in wrong:
            out.append(f"     wrongly injected {w}")
        for r in ranked[:top]:
            out.append(f"     cand {r['confidence']:.2f} imp={r['importance']:.1f} {r['path']}")
    return "\n".join(out) or "no failures"


# ---------------------------------------------------------------------------
# Gold-set building helpers
# ---------------------------------------------------------------------------

_SKIP_PROMPT = re.compile(r"^\s*(<|\[Request interrupted|Caveat:|/[a-z])")


def mine_prompts(projects_dir: Path, limit: int = 300, min_chars: int = 12) -> list[dict]:
    """Real user prompts from Claude Code transcripts, newest sessions first,
    deduplicated. Only typed text — tool results and command wrappers skipped."""
    files = sorted(projects_dir.glob("*/*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    seen, out = set(), []
    for f in files:
        try:
            lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("type") != "user" or row.get("isMeta") or row.get("isSidechain"):
                continue
            content = (row.get("message") or {}).get("content")
            if isinstance(content, list):
                texts = [c.get("text", "") for c in content
                         if isinstance(c, dict) and c.get("type") == "text"]
                if len(texts) != len(content):  # contains tool results/images
                    continue
                content = "\n".join(texts)
            if not isinstance(content, str):
                continue
            text = content.strip()
            if len(text) < min_chars or _SKIP_PROMPT.match(text) or text in seen:
                continue
            seen.add(text)
            out.append({"prompt": text[:2000], "cwd": row.get("cwd", "")})
            if len(out) >= limit:
                return out
    return out


POOL_PARAMS = mi.RetrievalParams(min_confidence=0.0, min_importance=0.0, max_hits=6,
                                 sem_sim_zero=0.3, sem_sim_full=0.7)


def candidate_pool(conn, gold: list[GoldItem], vectors: list) -> list[dict]:
    cands = collect_candidates(conn, gold, vectors=vectors)
    return [{"prompt": g.prompt, "slug": g.slug,
             "candidates": [{"path": h["path"], "title": h["title"], "conf": round(h["confidence"], 2),
                             "imp": h["importance"]} for h in mi.select_hits(c, POOL_PARAMS)]}
            for g, c in zip(gold, cands)]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _embed_all(prompts: list[str]) -> list:
    try:
        import diary_embed
        if not diary_embed.is_available():
            print("warning: embedding model unavailable — lexical signal only", file=sys.stderr)
            return [None] * len(prompts)
        return list(diary_embed.embed_many(prompts))
    except Exception as e:
        print(f"warning: embedding failed ({e}) — lexical signal only", file=sys.stderr)
        return [None] * len(prompts)


def _connect():
    import psycopg
    from psycopg.rows import dict_row
    return psycopg.connect(mi._database_url(), row_factory=dict_row)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="diary-eval", description=__doc__.split("\n\n")[0])
    ap.add_argument("command", nargs="?", choices=["eval", "mine", "pool"], default="eval")
    ap.add_argument("file", nargs="?", help="prompts JSONL for `pool`")
    ap.add_argument("--gold", type=Path, default=None)
    ap.add_argument("--sweep", action="store_true")
    ap.add_argument("--precision-target", type=float, default=0.9)
    ap.add_argument("--failures", action="store_true")
    ap.add_argument("--no-semantic", action="store_true")
    ap.add_argument("--limit", type=int, default=300)
    args = ap.parse_args(argv)

    if args.command == "mine":
        with _connect() as conn:
            for row in mine_prompts(Path.home() / ".claude" / "projects", limit=args.limit):
                row["slug"] = mi.slug_from_cwd(conn, row.pop("cwd") or "/")
                print(json.dumps(row, ensure_ascii=False))
        return 0

    if args.command == "pool":
        if not args.file:
            ap.error("pool needs a prompts JSONL file")
        rows = [json.loads(l) for l in Path(args.file).read_text().splitlines() if l.strip()]
        gold = [GoldItem(r["prompt"], r.get("slug"), (), ()) for r in rows]
        vectors = [None] * len(gold) if args.no_semantic else _embed_all([g.prompt for g in gold])
        with _connect() as conn:
            for row in candidate_pool(conn, gold, vectors):
                print(json.dumps(row, ensure_ascii=False))
        return 0

    gold_path = args.gold or default_gold_path()
    if not gold_path.exists():
        print(f"no gold set at {gold_path} (set --gold or DIARY_EVAL_GOLD)", file=sys.stderr)
        return 2
    gold = load_gold(gold_path)
    vectors = [None] * len(gold) if args.no_semantic else _embed_all([g.prompt for g in gold])
    with _connect() as conn:
        cands = collect_candidates(conn, gold, vectors=vectors)
    print(f"gold set: {gold_path} · {len(gold)} prompts")
    print(format_result("live", evaluate(gold, cands, mi.DEFAULT_PARAMS)))
    chosen = mi.DEFAULT_PARAMS
    if args.sweep:
        rows = sweep(gold, cands)
        best = recommend(rows, args.precision_target)
        if best is None:
            print(f"no parameter set reaches precision ≥ {args.precision_target:.0%}")
        else:
            chosen = best.params
            print(format_result("best ", best.result))
            print(f"       {format_params(best.params)}")
        frontier: dict[float, SweepRow] = {}
        for r in rows:
            key = round(r.result.precision, 2)
            if r.result.injected and (key not in frontier or r.result.recall > frontier[key].result.recall):
                frontier[key] = r
        print("precision/recall frontier:")
        for key in sorted(frontier, reverse=True)[:12]:
            r = frontier[key]
            print(f"  P {r.result.precision:.0%}  R {r.result.recall:.0%}  FP-neg "
                  f"{r.result.negative_fp_rate:.0%}  ← {format_params(r.params)}")
    if args.failures:
        print(format_failures(gold, cands, chosen))
    return 0


if __name__ == "__main__":
    sys.exit(main())
