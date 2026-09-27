"""
memory_stats: corpus statistics, token efficiency of the automatic injection
(from the hook event log written by memory_injection.log_event) and a
comparison with Claude Code's file-based memory (~/.claude/projects/*/memory).

Exposed as the memory_stats MCP tool and as GET /api/stats in diary-web.
Token numbers are estimates (chars / CHARS_PER_TOKEN), good for comparison,
not for billing.
"""
from __future__ import annotations

import json
import os
import re
import time
from collections import Counter
from pathlib import Path

import diary_db
import memory_injection
from diary_bootstrap import mcp

CHARS_PER_TOKEN = 3.7  # mixed German/English prose
VANILLA_INDEX_MAX_LINES = 200  # Claude Code loads the first 200 lines of MEMORY.md
_WS = re.compile(r"\s+")


def approx_tokens(chars: int | float) -> int:
    return int(round(chars / CHARS_PER_TOKEN))


def read_injection_log() -> list[dict]:
    path = memory_injection._state_dir() / "injection_log.jsonl"
    events = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                events.append(json.loads(line))
            except ValueError:
                continue
    except FileNotFoundError:
        pass
    return events


# ---------------------------------------------------------------------------
# Corpus
# ---------------------------------------------------------------------------

def _corpus_stats(conn) -> dict:
    row = conn.execute(
        "SELECT count(*) FILTER (WHERE origin = 'curated') AS curated, "
        "       count(*) FILTER (WHERE origin = 'extracted') AS extracted, "
        "       count(*) FILTER (WHERE origin = 'curated' AND embedding IS NOT NULL) AS embedded, "
        "       count(*) FILTER (WHERE pin_triggers <> '{}') AS pinned, "
        "       coalesce(sum(length(coalesce(title,'')) + length(coalesce(body,''))) "
        "                FILTER (WHERE origin = 'curated'), 0) AS chars "
        "FROM memory_nodes WHERE deleted_at IS NULL AND type <> 'category'"
    ).fetchone()
    branches = conn.execute(
        "SELECT '/' || split_part(path, '/', 2) AS branch, count(*) AS n FROM memory_nodes "
        "WHERE deleted_at IS NULL AND origin = 'curated' AND type <> 'category' "
        "GROUP BY 1 ORDER BY 2 DESC"
    ).fetchall()
    projects = conn.execute(
        "SELECT count(DISTINCT split_part(path, '/', 3)) AS n FROM memory_nodes "
        "WHERE deleted_at IS NULL AND origin = 'curated' AND path LIKE '/projects/%%/%%'"
    ).fetchone()["n"]
    links = conn.execute("SELECT count(*) AS n FROM memory_links").fetchone()["n"]
    return {
        "curated": row["curated"],
        "extracted": row["extracted"],
        "embedded": row["embedded"],
        "pinned": row["pinned"],
        "projects": projects,
        "links": links,
        "chars": row["chars"],
        "approx_tokens": approx_tokens(row["chars"]),
        "branches": {b["branch"]: b["n"] for b in branches},
    }


# ---------------------------------------------------------------------------
# Injection efficiency
# ---------------------------------------------------------------------------

def _injection_stats(days: int) -> dict:
    cutoff = time.time() - days * 86400
    events = [e for e in read_injection_log() if e.get("ts", time.time()) >= cutoff]
    starts = [e for e in events if e.get("event") == "session-start"]
    prompts = [e for e in events if e.get("event") == "prompt"]
    with_hits = [e for e in prompts if e.get("hits")]

    per_session: dict[str, int] = {}
    for e in events:
        key = e.get("session") or "?"
        per_session[key] = per_session.get(key, 0) + int(e.get("chars") or 0)
    total_chars = sum(per_session.values())

    def avg(values):
        values = list(values)
        return round(sum(values) / len(values), 1) if values else 0

    top = Counter(p for e in with_hits for p in e["hits"]).most_common(10)
    return {
        "days": days,
        "sessions": len(starts),
        "prompts": len(prompts),
        "prompts_with_hits": len(with_hits),
        "hit_rate": round(len(with_hits) / len(prompts), 3) if prompts else 0.0,
        "semantic_share": round(sum(1 for e in prompts if e.get("semantic")) / len(prompts), 3) if prompts else 0.0,
        "avg_digest_tokens": approx_tokens(avg(e.get("chars", 0) for e in starts)),
        "avg_tokens_per_hit_prompt": approx_tokens(avg(e.get("chars", 0) for e in with_hits)),
        "avg_tokens_per_session": approx_tokens(total_chars / len(per_session)) if per_session else 0,
        "total_injected_tokens": approx_tokens(total_chars),
        "avg_latency_ms": {
            "session-start": avg(e.get("latency_ms", 0) for e in starts),
            "prompt": avg(e.get("latency_ms", 0) for e in prompts),
        },
        "top_injected": [[p, n] for p, n in top],
    }


# ---------------------------------------------------------------------------
# File-based (vanilla) memory comparison
# ---------------------------------------------------------------------------

def _vanilla_root() -> Path:
    return Path(os.environ.get("DIARY_VANILLA_MEMORY_ROOT", Path.home() / ".claude"))


def _strip_frontmatter(text: str) -> str:
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            return text[end + 4:]
    return text


def _norm(text: str) -> str:
    return _WS.sub(" ", text).strip().lower()


def _vanilla_dirs() -> list[Path]:
    root = _vanilla_root()
    dirs = sorted(p for p in (root / "projects").glob("*/memory") if p.is_dir())
    if (root / "memory").is_dir():
        dirs.append(root / "memory")
    return dirs


def _project_vanilla_dir(conn, slug: str, dirs: list[Path]) -> Path | None:
    row = conn.execute(
        "SELECT config->'dirs' AS dirs FROM memory_nodes WHERE path = %s AND deleted_at IS NULL",
        (f"/projects/{slug}",),
    ).fetchone()
    registered = {d.rstrip("/").replace("/", "-") for d in (row["dirs"] or [])} if row and isinstance(row["dirs"], list) else set()
    for d in dirs:
        if d.parent.name in registered:
            return d
    suffix = "-" + slug.lower()
    matches = [d for d in dirs if d.parent.name.lower().endswith(suffix)]
    return min(matches, key=lambda d: len(d.parent.name)) if matches else None


def _vanilla_stats(conn, slug: str | None) -> dict:
    dirs = _vanilla_dirs()
    bodies = [_norm(r["body"] or "") for r in conn.execute(
        "SELECT body FROM memory_nodes WHERE deleted_at IS NULL AND origin = 'curated'"
    ).fetchall()]
    diary_text = "\n".join(bodies)

    files = chars = imported = 0
    projects_with_files = 0
    missing: list[str] = []
    for d in dirs:
        mems = [f for f in d.glob("*.md") if f.name != "MEMORY.md"]
        if mems:
            projects_with_files += 1
        for f in mems:
            text = f.read_text(encoding="utf-8", errors="replace")
            files += 1
            chars += len(text)
            probe = _norm(_strip_frontmatter(text))[:80]
            if probe and probe in diary_text:
                imported += 1
            else:
                missing.append(str(f.relative_to(_vanilla_root())))

    result = {
        "root": str(_vanilla_root()),
        "projects": projects_with_files,
        "files": files,
        "chars": chars,
        "approx_tokens": approx_tokens(chars),
        "imported": imported,
        "not_imported": files - imported,
        "not_imported_examples": missing[:10],
    }
    if slug:
        pdir = _project_vanilla_dir(conn, slug, dirs)
        info = {"slug": slug, "dir": str(pdir) if pdir else None, "files": 0,
                "approx_tokens": 0, "always_loaded_tokens": 0}
        if pdir:
            mems = [f for f in pdir.glob("*.md") if f.name != "MEMORY.md"]
            info["files"] = len(mems)
            info["approx_tokens"] = approx_tokens(sum(len(f.read_text(errors="replace")) for f in mems))
            index = pdir / "MEMORY.md"
            if index.exists():
                lines = index.read_text(errors="replace").splitlines(keepends=True)[:VANILLA_INDEX_MAX_LINES]
                info["always_loaded_tokens"] = approx_tokens(len("".join(lines)))
        result["project"] = info
    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def collect_stats(conn, days: int = 30, project_slug: str = "") -> dict:
    slug = project_slug.strip("/") or None
    corpus = _corpus_stats(conn)
    digest = memory_injection.build_session_digest(conn, slug)
    diary = {
        "project_digest_tokens": approx_tokens(len(digest)),
        "reachable_tokens": corpus["approx_tokens"],
    }
    if slug:
        row = conn.execute(
            "SELECT count(*) AS n, coalesce(sum(length(coalesce(title,'')) + length(coalesce(body,''))), 0) AS c "
            "FROM memory_nodes WHERE deleted_at IS NULL AND origin = 'curated' AND type <> 'category' "
            "AND path LIKE %s", (f"/projects/{slug}/%",),
        ).fetchone()
        diary.update(project_memories=row["n"], project_tokens=approx_tokens(row["c"]))
    return {
        "corpus": corpus,
        "injection": _injection_stats(days),
        "diary": diary,
        "vanilla": _vanilla_stats(conn, slug),
    }


def _fmt_int(n) -> str:
    return f"{int(n):,}".replace(",", ".")


def format_stats(st: dict) -> str:
    c, inj, d, v = st["corpus"], st["injection"], st["diary"], st["vanilla"]
    lines = [
        "=== diary-mcp Statistik (Tokens ≈ Zeichen/3.7) ===",
        "",
        "## Bestand",
        f"- {_fmt_int(c['curated'])} kuratierte Memories (~{_fmt_int(c['approx_tokens'])} Tokens) in "
        f"{c['projects']} Projekten, {_fmt_int(c['extracted'])} extrahierte, {c['embedded']} mit Embedding, "
        f"{c['links']} Links, {c['pinned']} Pins",
        "- Branches: " + ", ".join(f"{b} {n}" for b, n in c["branches"].items()),
        "",
        f"## Injection (letzte {inj['days']} Tage, aus dem Hook-Log)",
    ]
    if inj["sessions"] or inj["prompts"]:
        lines += [
            f"- Sessions: {inj['sessions']} · Ø Session-Digest {_fmt_int(inj['avg_digest_tokens'])} Tokens "
            f"({inj['avg_latency_ms']['session-start']} ms)",
            f"- Prompts: {inj['prompts']} · mit Treffer {inj['prompts_with_hits']} ({inj['hit_rate']:.0%}) · "
            f"Ø {_fmt_int(inj['avg_tokens_per_hit_prompt'])} Tokens pro Treffer-Prompt · "
            f"semantisch {inj['semantic_share']:.0%} · Ø {inj['avg_latency_ms']['prompt']} ms",
            f"- Ø injiziert pro Session: {_fmt_int(inj['avg_tokens_per_session'])} Tokens · "
            f"gesamt {_fmt_int(inj['total_injected_tokens'])} Tokens",
        ]
        if inj["avg_tokens_per_session"]:
            ratio = c["approx_tokens"] / inj["avg_tokens_per_session"]
            lines.append(f"- Hebel: {ratio:,.0f}× so viel Wissen erreichbar wie pro Session an Tokens injiziert wird")
        if inj["top_injected"]:
            lines.append("- Am häufigsten injiziert: " + ", ".join(f"{p} ({n}×)" for p, n in inj["top_injected"][:5]))
    else:
        lines.append("- Noch keine Hook-Events im Zeitraum (Log: ~/.cache/diary-mcp/injection_log.jsonl).")

    lines += ["", "## Vergleich zu Claude Code file-based Memory"]
    lines.append(
        f"- file-based: {_fmt_int(v['files'])} Memory-Dateien (~{_fmt_int(v['approx_tokens'])} Tokens) in "
        f"{v['projects']} Projekt-Ordnern unter {v['root']}"
    )
    if v["files"]:
        lines.append(f"- Davon im Diary vorhanden: {v['imported']} · fehlen: {v['not_imported']}"
                     + (f" (z.B. {', '.join(v['not_imported_examples'][:3])})" if v["not_imported"] else ""))
    p = v.get("project")
    if p:
        lines += [
            f"- Projekt '{p['slug']}': file-based lädt pro Session {_fmt_int(p['always_loaded_tokens'])} Tokens "
            f"(MEMORY.md-Index) und erreicht {p['files']} Dateien (~{_fmt_int(p['approx_tokens'])} Tokens) "
            f"— nur dieses Projekt, ohne Suche.",
            f"- Projekt '{p['slug']}': Diary lädt {_fmt_int(d['project_digest_tokens'])} Tokens (Digest) und erreicht "
            f"{d.get('project_memories', 0)} Projekt-Memories (~{_fmt_int(d.get('project_tokens', 0))} Tokens) plus "
            f"das gesamte Korpus (~{_fmt_int(d['reachable_tokens'])} Tokens) per Auto-Retrieval und Suche.",
        ]
    else:
        lines.append(f"- Diary: globaler Digest {_fmt_int(d['project_digest_tokens'])} Tokens, "
                     f"erreichbar ~{_fmt_int(d['reachable_tokens'])} Tokens projektübergreifend.")
    lines += [
        "- Fähigkeiten: Diary hat Hybrid-Suche (FTS+semantisch), Auto-Retrieval pro Prompt, projektübergreifenden "
        "Zugriff, Knowledge-Graph, Ablaufdaten, Sync über Maschinen; file-based hat nichts davon, ist aber "
        "ohne laufende Infrastruktur immer verfügbar.",
    ]
    return "\n".join(lines)


@mcp.tool()
def memory_stats(days: int = 30, project_slug: str = "") -> str:
    """Statistik & Token-Effizienz des Memory-Systems.

    Liefert: Bestand (Memories, Tokens, Projekte, Embeddings, Links, Pins),
    Injection-Effizienz der Hooks im Zeitraum `days` (Sessions, Prompts, Trefferquote,
    Ø injizierte Tokens, Latenz, meistinjizierte Memories) und einen Vergleich mit
    Claude Codes file-based Memory (~/.claude/projects/*/memory): Umfang, Import-
    Abdeckung und — mit `project_slug` — pro Session geladene vs. erreichbare Tokens.
    """
    with diary_db.get_db() as conn:
        st = collect_stats(conn, days=days, project_slug=project_slug)
    return format_stats(st)
