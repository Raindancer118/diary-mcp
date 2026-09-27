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
import platform
import re
import socket
import time
from collections import Counter
from pathlib import Path

import diary_db
import memory_injection
import memory_service
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
# Instance
# ---------------------------------------------------------------------------

def _package_version() -> str:
    try:
        from importlib.metadata import version
        return version("diary-mcp")
    except Exception:
        try:
            import tomllib
            return tomllib.loads((Path(__file__).parent / "pyproject.toml").read_text())["project"]["version"]
        except Exception:
            return "?"


def _redact_url(url: str | None) -> str | None:
    """postgresql://user:pw@host:port/db → host:port/db (no credentials)."""
    if not url:
        return None
    rest = url.split("://", 1)[-1]
    return rest.rsplit("@", 1)[-1]


def _running_servers() -> int | None:
    """Number of diary-mcp server processes on this machine (Linux /proc)."""
    proc = Path("/proc")
    if not proc.is_dir():
        return None
    n = 0
    for p in proc.glob("[0-9]*/cmdline"):
        try:
            cmd = p.read_bytes().replace(b"\0", b" ")
        except OSError:
            continue
        if b"bin/diary-mcp" in cmd and b"diary-mcp-" not in cmd.split(b"bin/diary-mcp", 1)[1][:1]:
            n += 1
    return n


def _instance_stats(conn) -> dict:
    info = conn.info
    pgvector = conn.execute(
        "SELECT extversion FROM pg_extension WHERE extname = 'vector'").fetchone()
    hnsw = conn.execute(
        "SELECT 1 AS ok FROM pg_indexes WHERE tablename = 'memory_nodes' AND indexdef ILIKE '%%hnsw%%'"
    ).fetchone()
    size = conn.execute("SELECT pg_database_size(current_database()) AS b").fetchone()["b"]
    last_sync = conn.execute(
        "SELECT max(value) AS v FROM diary_meta WHERE key LIKE 'last_sync:%%'").fetchone()["v"]
    identity = conn.execute(
        "SELECT display_name, relay_url, created_at FROM diary_identity LIMIT 1").fetchone()
    links = conn.execute(
        "SELECT peer_alias, peer_display_name, last_synced_at, sync_tags FROM diary_links ORDER BY established_at"
    ).fetchall()

    try:
        import diary_embed
        import diary_embed_ipc
        model = diary_embed.model_name()
        sock = diary_embed_ipc.sock_path()
        alive = sock.exists() and diary_embed_ipc._is_alive(sock)
    except Exception:
        model, alive = "?", False

    state = memory_injection._state_dir()
    log = state / "injection_log.jsonl"
    df = state / "df_cache.json"
    return {
        "version": _package_version(),
        "hostname": socket.gethostname(),
        "python": platform.python_version(),
        "db_host": info.host,
        "db_port": info.port,
        "db_name": info.dbname,
        "postgres_version": conn.execute("SHOW server_version").fetchone()["server_version"].split()[0],
        "db_size_bytes": size,
        "pgvector": pgvector is not None,
        "pgvector_version": pgvector["extversion"] if pgvector else None,
        "hnsw_index": hnsw is not None,
        "embed_model": model,
        "embed_server_alive": alive,
        "running_servers": _running_servers(),
        "remote_sync": {
            "ssh_host": os.environ.get("DIARY_REMOTE_SSH_HOST"),
            "remote": _redact_url(os.environ.get("DIARY_REMOTE_URL")),
            "last_sync": last_sync,
        },
        "federation": {
            "identity": identity["display_name"] if identity else None,
            "relay": _redact_url(identity["relay_url"]) if identity else None,
            "links": [{"alias": l["peer_alias"], "peer": l["peer_display_name"],
                       "last_synced": str(l["last_synced_at"])[:16] if l["last_synced_at"] else None,
                       "sync_tags": list(l["sync_tags"] or [])} for l in links],
        },
        "hook_log_bytes": log.stat().st_size if log.exists() else 0,
        "df_cache_age_min": round((time.time() - df.stat().st_mtime) / 60) if df.exists() else None,
    }


# ---------------------------------------------------------------------------
# Memory quality, graph, project journal
# ---------------------------------------------------------------------------

def _quality_stats(conn) -> dict:
    live = "deleted_at IS NULL AND origin = 'curated' AND type <> 'category'"
    row = conn.execute(
        f"SELECT count(*) FILTER (WHERE importance >= 0.7) AS high, "
        f"       count(*) FILTER (WHERE importance >= 0.4 AND importance < 0.7) AS mid, "
        f"       count(*) FILTER (WHERE importance < 0.4) AS low, "
        f"       count(*) FILTER (WHERE created_at > now() - interval '7 days') AS c7, "
        f"       count(*) FILTER (WHERE created_at > now() - interval '30 days') AS c30, "
        f"       count(*) FILTER (WHERE updated_at > now() - interval '7 days') AS u7, "
        f"       count(*) FILTER (WHERE updated_at > now() - interval '30 days') AS u30, "
        f"       count(*) FILTER (WHERE coalesce(access_count, 0) = 0) AS never, "
        f"       count(*) FILTER (WHERE coalesce(accessed_at, created_at) < now() - interval '180 days') AS stale, "
        f"       count(*) FILTER (WHERE valid_until IS NOT NULL AND valid_until < now()) AS expired, "
        f"       count(*) FILTER (WHERE embedding IS NULL) AS no_embedding, "
        f"       count(*) FILTER (WHERE length(coalesce(body,'')) > {memory_service.MEMORY_SOFT_MAX_CHARS}) AS oversized, "
        f"       coalesce(avg(length(coalesce(body,''))), 0) AS avg_chars "
        f"FROM memory_nodes WHERE {live}"
    ).fetchone()
    tomb = conn.execute("SELECT count(*) AS n FROM memory_nodes WHERE deleted_at IS NOT NULL").fetchone()["n"]
    expiring = conn.execute(
        "SELECT count(*) AS n FROM memory_nodes WHERE deleted_at IS NULL AND origin = 'extracted' "
        "AND created_at < now() - interval '76 days'"
    ).fetchone()["n"]
    types = conn.execute(
        f"SELECT type, count(*) AS n FROM memory_nodes WHERE {live} GROUP BY 1 ORDER BY 2 DESC").fetchall()
    most = conn.execute(
        f"SELECT path, access_count FROM memory_nodes WHERE {live} AND access_count > 0 "
        f"ORDER BY access_count DESC, path LIMIT 5").fetchall()
    largest = conn.execute(
        f"SELECT path, length(coalesce(body,'')) AS c FROM memory_nodes WHERE {live} "
        f"ORDER BY c DESC LIMIT 3").fetchall()
    top_projects = conn.execute(
        f"SELECT split_part(path, '/', 3) AS slug, count(*) AS n FROM memory_nodes "
        f"WHERE {live} AND path LIKE '/projects/%%/%%' GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT 10").fetchall()
    growth = conn.execute(
        f"SELECT to_char(date_trunc('month', created_at), 'YYYY-MM') AS m, count(*) AS n FROM memory_nodes "
        f"WHERE {live} AND created_at > date_trunc('month', now()) - interval '5 months' "
        f"GROUP BY 1 ORDER BY 1").fetchall()
    return {
        "importance": {"high": row["high"], "mid": row["mid"], "low": row["low"]},
        "types": {t["type"]: t["n"] for t in types},
        "created_7d": row["c7"], "created_30d": row["c30"],
        "updated_7d": row["u7"], "updated_30d": row["u30"],
        "never_accessed": row["never"],
        "stale_180d": row["stale"],
        "expired": row["expired"],
        "no_embedding": row["no_embedding"],
        "oversized": row["oversized"],
        "tombstones": tomb,
        "extracted_expiring_14d": expiring,
        "avg_tokens": approx_tokens(float(row["avg_chars"])),
        "most_accessed": [[r["path"], r["access_count"]] for r in most],
        "largest": [[r["path"], approx_tokens(r["c"])] for r in largest],
        "top_projects": [[r["slug"], r["n"]] for r in top_projects],
        "growth": {g["m"]: g["n"] for g in growth},
    }


def _graph_stats(conn) -> dict:
    by_type = conn.execute(
        "SELECT rel_type, count(*) AS n FROM memory_links GROUP BY 1 ORDER BY 2 DESC").fetchall()
    by_origin = conn.execute(
        "SELECT link_origin, count(*) AS n FROM memory_links GROUP BY 1").fetchall()
    orphans = conn.execute(
        "SELECT count(*) AS n FROM memory_nodes n WHERE n.deleted_at IS NULL AND n.origin = 'curated' "
        "AND n.type <> 'category' AND NOT EXISTS (SELECT 1 FROM memory_links l "
        "WHERE l.from_id = n.id OR l.to_id = n.id)").fetchone()["n"]
    types = {r["rel_type"]: r["n"] for r in by_type}
    return {
        "links": sum(types.values()),
        "by_type": types,
        "by_origin": {r["link_origin"]: r["n"] for r in by_origin},
        "contradictions": types.get("contradicts", 0),
        "orphans": orphans,
    }


def _journal_stats(conn) -> dict:
    def one(sql: str) -> int:
        return conn.execute(sql).fetchone()["n"]

    live_p = "p.deleted_at IS NULL"
    ms = conn.execute(
        f"SELECT count(*) AS total, count(*) FILTER (WHERE m.completed) AS done FROM milestones m "
        f"JOIN projects p ON p.id = m.project_id WHERE m.deleted_at IS NULL AND {live_p}").fetchone()
    tasks = conn.execute(
        f"SELECT count(*) AS total, count(*) FILTER (WHERE t.completed) AS done FROM tasks t "
        f"JOIN milestones m ON m.id = t.milestone_id JOIN projects p ON p.id = m.project_id "
        f"WHERE t.deleted_at IS NULL AND m.deleted_at IS NULL AND {live_p}").fetchone()
    today = time.strftime("%Y-%m-%d")
    rem = conn.execute(
        f"SELECT count(*) FILTER (WHERE NOT r.completed) AS open, "
        f"       count(*) FILTER (WHERE NOT r.completed AND left(r.target_date, 10) < %s) AS overdue "
        f"FROM reminders r JOIN projects p ON p.id = r.project_id WHERE r.deleted_at IS NULL AND {live_p}",
        (today,)).fetchone()
    last_log = conn.execute(
        f"SELECT max(l.timestamp) AS t FROM logs l JOIN projects p ON p.id = l.project_id "
        f"WHERE l.deleted_at IS NULL AND {live_p}").fetchone()["t"]
    return {
        "projects_active": one("SELECT count(*) AS n FROM projects WHERE deleted_at IS NULL AND NOT coalesce(archived, false)"),
        "projects_archived": one("SELECT count(*) AS n FROM projects WHERE deleted_at IS NULL AND archived"),
        "milestones_total": ms["total"], "milestones_done": ms["done"],
        "tasks_total": tasks["total"], "tasks_done": tasks["done"],
        "logs_total": one(f"SELECT count(*) AS n FROM logs l JOIN projects p ON p.id = l.project_id "
                          f"WHERE l.deleted_at IS NULL AND {live_p}"),
        "logs_30d": one(f"SELECT count(*) AS n FROM logs l JOIN projects p ON p.id = l.project_id "
                        f"WHERE l.deleted_at IS NULL AND {live_p} AND l.timestamp > now() - interval '30 days'"),
        "last_log": str(last_log)[:16] if last_log else None,
        "wiki_pages": one(f"SELECT count(*) AS n FROM wiki_pages w JOIN projects p ON p.id = w.project_id "
                          f"WHERE w.deleted_at IS NULL AND {live_p}"),
        "errors_solutions": one(f"SELECT count(*) AS n FROM errors_solutions e JOIN projects p ON p.id = e.project_id "
                                f"WHERE e.deleted_at IS NULL AND {live_p}"),
        "reminders_open": rem["open"], "reminders_overdue": rem["overdue"],
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
        "instance": _instance_stats(conn),
        "corpus": corpus,
        "quality": _quality_stats(conn),
        "graph": _graph_stats(conn),
        "journal": _journal_stats(conn),
        "injection": _injection_stats(days),
        "diary": diary,
        "vanilla": _vanilla_stats(conn, slug),
    }


def _fmt_int(n) -> str:
    return f"{int(n):,}".replace(",", ".")


def _fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


def _instance_lines(i: dict) -> list[str]:
    rs, fed = i["remote_sync"], i["federation"]
    lines = [
        "## Instanz",
        f"- diary-mcp {i['version']} auf {i['hostname']} (Python {i['python']}) · "
        f"{i['running_servers'] if i['running_servers'] is not None else '?'} laufende diary-mcp-Prozesse",
        f"- Postgres {i['postgres_version']} · DB {i['db_name']} @ {i['db_host']}:{i['db_port']} · "
        f"{_fmt_bytes(i['db_size_bytes'])} · pgvector "
        + (f"{i['pgvector_version']} ({'HNSW' if i['hnsw_index'] else 'ohne HNSW'})" if i["pgvector"] else "nein (numpy-Fallback)"),
        f"- Embeddings: {i['embed_model']} · geteilter Embed-Server "
        + ("läuft" if i["embed_server_alive"] else "läuft nicht (Prompt-Hook nur lexikalisch)"),
        "- Maschinen-Sync: " + (f"{rs['ssh_host'] or 'direkt'} → {rs['remote']}, letzter Sync {str(rs['last_sync'] or 'nie')[:16]}"
                               if rs["remote"] else "nicht konfiguriert"),
    ]
    if fed["identity"]:
        links = "; ".join(f"{l['alias']} ({l['peer']}, zuletzt {l['last_synced'] or 'nie'}, "
                          f"Auto-Tags: {', '.join(l['sync_tags']) or 'aus'})" for l in fed["links"]) or "keine Links"
        lines.append(f"- Föderation: '{fed['identity']}' @ {fed['relay']} — {links}")
    else:
        lines.append("- Föderation: keine Identity")
    lines.append(f"- Hook-Log {_fmt_bytes(i['hook_log_bytes'])} · df-Cache "
                 + (f"{i['df_cache_age_min']} min alt" if i["df_cache_age_min"] is not None else "noch nicht gebaut"))
    return lines


def _quality_lines(q: dict, g: dict) -> list[str]:
    imp = q["importance"]
    lines = [
        "## Qualität & Aktivität",
        f"- Wichtigkeit: {imp['high']} hoch (≥0.7) · {imp['mid']} mittel · {imp['low']} niedrig (<0.4) · "
        f"Ø {q['avg_tokens']} Tokens pro Memory",
        "- Typen: " + ", ".join(f"{t} {n}" for t, n in q["types"].items()),
        f"- Neu: {q['created_7d']} (7 T.) / {q['created_30d']} (30 T.) · geändert: {q['updated_7d']} / {q['updated_30d']}",
        f"- Nie abgerufen: {q['never_accessed']} · seit 180 T. ungenutzt: {q['stale_180d']} · abgelaufen: {q['expired']} · "
        f"ohne Embedding: {q['no_embedding']} · Tombstones: {q['tombstones']} · extrahierte laufen in 14 T. ab: {q['extracted_expiring_14d']}",
        f"- Zu groß (> {memory_service.MEMORY_SOFT_MAX_CHARS} Zeichen, Kandidaten zum Aufteilen): {q['oversized']}",
    ]
    if q["growth"]:
        lines.append("- Wachstum (neu/Monat): " + ", ".join(f"{m} {n}" for m, n in q["growth"].items()))
    if q["top_projects"]:
        lines.append("- Größte Projekte: " + ", ".join(f"{s} {n}" for s, n in q["top_projects"]))
    if q["most_accessed"]:
        lines.append("- Meistgenutzt: " + ", ".join(f"{p} ({n}×)" for p, n in q["most_accessed"]))
    if q["largest"]:
        lines.append("- Größte Memories: " + ", ".join(f"{p} (~{t} Tokens)" for p, t in q["largest"]))
    lines += [
        "",
        "## Graph",
        f"- {g['links']} Links (" + ", ".join(f"{t} {n}" for t, n in g["by_type"].items()) + ") · "
        + ", ".join(f"{o} {n}" for o, n in g["by_origin"].items())
        + f" · Widersprüche: {g['contradictions']} · Memories ohne Link: {g['orphans']}",
    ]
    return lines


def _journal_lines(j: dict) -> list[str]:
    return [
        "## Projekt-Diary",
        f"- Projekte: {j['projects_active']} aktiv, {j['projects_archived']} archiviert · "
        f"Meilensteine {j['milestones_done']}/{j['milestones_total']} erledigt · "
        f"Tasks {j['tasks_done']}/{j['tasks_total']} erledigt",
        f"- Logs: {j['logs_total']} gesamt, {j['logs_30d']} in 30 T., letzter {j['last_log'] or '—'} · "
        f"Wiki-Seiten {j['wiki_pages']} · Fehler/Lösungen {j['errors_solutions']} · "
        f"Reminder offen {j['reminders_open']} (überfällig {j['reminders_overdue']})",
    ]


def format_stats(st: dict) -> str:
    c, inj, d, v = st["corpus"], st["injection"], st["diary"], st["vanilla"]
    lines = [
        "=== diary-mcp Statistik (Tokens ≈ Zeichen/3.7) ===",
        "",
        *_instance_lines(st["instance"]),
        "",
        "## Bestand",
        f"- {_fmt_int(c['curated'])} kuratierte Memories (~{_fmt_int(c['approx_tokens'])} Tokens) in "
        f"{c['projects']} Projekten, {_fmt_int(c['extracted'])} extrahierte, {c['embedded']} mit Embedding, "
        f"{c['links']} Links, {c['pinned']} Pins",
        "- Branches: " + ", ".join(f"{b} {n}" for b, n in c["branches"].items()),
        "",
        "",
        *_quality_lines(st["quality"], st["graph"]),
        "",
        *_journal_lines(st["journal"]),
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
    """Statistik zu dieser diary-mcp-Instanz, dem Memory-Bestand und der Token-Effizienz.

    Liefert: Instanz (Version, Host, Postgres/DB-Größe, pgvector, Embed-Server, Sync,
    Föderation), Bestand (Memories, Tokens, Projekte, Embeddings, Links, Pins),
    Injection-Effizienz der Hooks im Zeitraum `days` (Sessions, Prompts, Trefferquote,
    Ø injizierte Tokens, Latenz, meistinjizierte Memories) und einen Vergleich mit
    Claude Codes file-based Memory (~/.claude/projects/*/memory): Umfang, Import-
    Abdeckung und — mit `project_slug` — pro Session geladene vs. erreichbare Tokens.
    Dazu Qualität/Aktivität (Wichtigkeit, Typen, ungenutzte/abgelaufene Memories,
    Wachstum), Graph (Links, Widersprüche, Waisen) und Projekt-Diary (Projekte,
    Meilensteine, Tasks, Logs, Wiki, Reminder, Fehler/Lösungen).
    """
    with diary_db.get_db() as conn:
        st = collect_stats(conn, days=days, project_slug=project_slug)
    return format_stats(st)
