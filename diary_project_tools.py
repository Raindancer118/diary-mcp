"""
Classic project-diary tools: projects, logs, errors/solutions, milestones,
tasks, reminders, wiki pages, plus global config and cross-project search.

Split out of the former diary_server.py monolith (v0.10.0) — this module owns
everything that predates the memory tree (v0.4+). The memory tree itself lives
in memory_service.py / search_engine.py / graph_core.py / sync_manager.py.
"""
import json
import logging
import subprocess
import urllib.error
import urllib.request
from datetime import datetime, timedelta

import diary_db
from diary_bootstrap import mcp
from diary_config import load_config, save_config

_log = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now().strftime(load_config()["date_format"])


def _is_reminder_due(date_str: str) -> bool:
    fmt = load_config()["date_format"]
    try:
        target = (
            datetime.strptime(date_str, "%Y-%m-%d")
            if len(date_str) == 10
            else datetime.strptime(date_str, fmt)
        )
        return datetime.now() >= target
    except ValueError:
        return False


def _create_github_issue(repo: str, title: str, body: str) -> None:
    try:
        result = subprocess.run(
            ["gh", "issue", "create", "-R", repo, "-t", title, "-b", body],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            _log.warning("gh issue create failed for %s: %s", repo, result.stderr.strip())
    except FileNotFoundError:
        _log.warning("gh CLI not found, skipping GitHub issue creation")
    except subprocess.TimeoutExpired:
        _log.warning("gh issue create timed out for %s", repo)


def _maybe_create_gh_issue(conn, project_id: int, title: str, body: str) -> None:
    row = conn.execute("SELECT config FROM projects WHERE id = %s", (project_id,)).fetchone()
    if not row or not row["config"]:
        return
    try:
        cfg = row["config"]  # JSONB → already a dict
        if cfg.get("auto_gh_issues") and cfg.get("gh_repo"):
            _create_github_issue(cfg["gh_repo"], title, body)
    except (KeyError, TypeError):
        pass


# =====================================================================
# GLOBAL / CONFIG / SYNC TOOLS
# =====================================================================

@mcp.tool()
def get_global_config() -> str:
    return json.dumps(load_config(), indent=2, ensure_ascii=False)


@mcp.tool()
def update_global_config(config_json_string: str) -> str:
    try:
        save_config(json.loads(config_json_string))
        return "Globale Konfiguration aktualisiert."
    except json.JSONDecodeError as e:
        return f"Fehler: Ungültiges JSON — {e}"


@mcp.tool()
def get_project_config(project_name: str) -> str:
    with diary_db.get_db() as conn:
        pid = diary_db.get_project_id(conn, project_name)
        if not pid:
            return "Projekt nicht gefunden."
        row = conn.execute("SELECT config FROM projects WHERE id = %s", (pid,)).fetchone()
        return json.dumps(row["config"], indent=2, ensure_ascii=False) if row and row["config"] else "{}"


@mcp.tool()
def update_project_config(project_name: str, config_json_string: str) -> str:
    try:
        json.loads(config_json_string)
    except json.JSONDecodeError:
        return "Fehler: Ungültiges JSON Format."
    with diary_db.get_db() as conn:
        pid = diary_db.get_project_id(conn, project_name)
        if not pid:
            return "Projekt nicht gefunden."
        conn.execute(
            "UPDATE projects SET config = %s, updated_at = now() WHERE id = %s",
            (config_json_string, pid),
        )
    return "Projekt-Config erfolgreich aktualisiert."


@mcp.tool()
def sync_remote_logs() -> str:
    """Holt alle neuen Logs von den Remote-Servern ab, trägt sie lokal ein und feuert ggf. GitHub Issues."""
    cfg = load_config()
    urls = cfg.get("remote_logger_urls", [])
    if not urls:
        return "Keine 'remote_logger_urls' in der globalen Config hinterlegt."

    synced_count = 0
    errors: list[str] = []

    for url in urls:
        try:
            with urllib.request.urlopen(
                urllib.request.Request(url, method="GET"), timeout=5
            ) as response:
                data = json.loads(response.read().decode())
        except urllib.error.URLError as exc:
            errors.append(f"{url}: {exc}")
            continue
        except (json.JSONDecodeError, ValueError) as exc:
            errors.append(f"{url}: ungültige Antwort — {exc}")
            continue

        with diary_db.get_db() as conn:
            for item in data:
                proj = item.get("project", "Unbekannt")
                level = item.get("level", "INFO").upper()
                worker = item.get("worker", "App")
                msg = item.get("message", "")
                ts = item.get("timestamp", _now())

                pid = diary_db.get_project_id(conn, proj)
                if not pid:
                    row = conn.execute(
                        "INSERT INTO projects (name, status) VALUES (%s, %s) RETURNING id",
                        (proj, "Auto-created by Remote Sync"),
                    ).fetchone()
                    pid = row["id"]

                conn.execute(
                    "INSERT INTO logs (project_id, timestamp, author, entry, level, worker) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    (pid, ts, f"AppLogger [{level}]", msg, level, worker),
                )

                if level in ("ERROR", "CRITICAL", "FATAL", "EXCEPTION"):
                    conn.execute(
                        "INSERT INTO errors_solutions (project_id, error_msg, solution_msg) "
                        "VALUES (%s, %s, %s)",
                        (pid, msg, "TODO: Lösung fehlt noch."),
                    )
                    _maybe_create_gh_issue(
                        conn, pid,
                        f"[{worker}] {level} in {proj}",
                        f"Ein neuer Fehler wurde von Worker '{worker}' geworfen:\n\n**Level:** {level}\n**Log:**\n{msg}",
                    )

                diary_db.apply_log_retention(conn, pid)
                synced_count += 1

    summary = f"Sync abgeschlossen. {synced_count} neue Logs integriert."
    if errors:
        summary += "\nFehler:\n" + "\n".join(f"  - {e}" for e in errors)
    return summary


@mcp.tool()
def search_global(query: str) -> str:
    """SUPER-SEARCH: Sucht projektübergreifend in Projekten, Logs, Aufgaben, Meilensteinen, Wikis und Fehlerberichten."""
    words = query.split()
    if not words:
        return "Kein Suchbegriff angegeben."

    def like_clause(columns: list[str]) -> tuple[str, list]:
        parts: list[str] = []
        params: list[str] = []
        for w in words:
            col_conds = " OR ".join(f"{col} ILIKE %s" for col in columns)
            parts.append(f"({col_conds})")
            params.extend(f"%{w}%" for _ in columns)
        return " AND ".join(parts), params

    results = [f"Globale Suchergebnisse für '{query}':"]

    with diary_db.get_db() as conn:
        cond, params = like_clause(["name", "status"])
        for row in conn.execute(
            f"SELECT name FROM projects WHERE deleted_at IS NULL AND ({cond})", params
        ).fetchall():
            results.append(f"- [Projekt] {row['name']}")

        cond, params = like_clause(["entry", "worker"])
        for row in conn.execute(
            "SELECT p.name, l.entry, l.worker FROM logs l JOIN projects p ON l.project_id = p.id "
            f"WHERE l.deleted_at IS NULL AND p.deleted_at IS NULL AND ({cond})",
            params,
        ).fetchall():
            results.append(f"- [Log in '{row['name']}' / {row['worker']}] {row['entry'][:80]}...")

        cond, params = like_clause(["error_msg", "solution_msg"])
        for row in conn.execute(
            "SELECT p.name, e.error_msg FROM errors_solutions e JOIN projects p ON e.project_id = p.id "
            f"WHERE e.deleted_at IS NULL AND p.deleted_at IS NULL AND ({cond})",
            params,
        ).fetchall():
            results.append(f"- [Error/Solution in '{row['name']}'] {row['error_msg'][:80]}...")

        cond, params = like_clause(["title", "content"])
        for row in conn.execute(
            "SELECT p.name, w.title FROM wiki_pages w JOIN projects p ON w.project_id = p.id "
            f"WHERE w.deleted_at IS NULL AND p.deleted_at IS NULL AND ({cond})",
            params,
        ).fetchall():
            results.append(f"- [Wiki in '{row['name']}'] Seite: {row['title']}")

        cond, params = like_clause(["t.title"])
        for row in conn.execute(
            "SELECT p.name, m.title AS m_title, t.title AS t_title "
            "FROM tasks t JOIN milestones m ON t.milestone_id = m.id JOIN projects p ON m.project_id = p.id "
            f"WHERE t.deleted_at IS NULL AND m.deleted_at IS NULL AND p.deleted_at IS NULL AND ({cond})",
            params,
        ).fetchall():
            results.append(f"- [Aufgabe in '{row['name']}' -> '{row['m_title']}'] {row['t_title']}")

    return "\n".join(results) if len(results) > 1 else "Nichts gefunden."


@mcp.tool()
def filter_logs(project_name: str, level: str = None, worker: str = None, limit: int = 50) -> str:
    """Filtert Logs eines Projekts gezielt nach Loglevel (z.B. ERROR) oder Worker/Subprozess."""
    with diary_db.get_db() as conn:
        pid = diary_db.get_project_id(conn, project_name)
        if not pid:
            return "Projekt nicht gefunden."

        sql = "SELECT * FROM logs WHERE project_id = %s AND deleted_at IS NULL"
        params: list = [pid]
        if level:
            sql += " AND level = %s"
            params.append(level.upper())
        if worker:
            sql += " AND worker ILIKE %s"
            params.append(f"%{worker}%")
        sql += " ORDER BY timestamp DESC LIMIT %s"
        params.append(limit)

        logs = conn.execute(sql, params).fetchall()

    if not logs:
        return "Keine passenden Logs gefunden."
    lines = [f"Gefilterte Logs für '{project_name}':"]
    for entry in logs:
        lines.append(
            f"  L{entry['id']} [{entry['timestamp']}] [{entry['level']}] {entry['worker']}: {entry['entry']}"
        )
    return "\n".join(lines)


# =====================================================================
# PROJECT TOOLS
# =====================================================================

@mcp.tool()
def get_projects(include_archived: bool = None) -> str:
    cfg = load_config()
    if include_archived is None:
        include_archived = cfg.get("show_archived_by_default", False)

    with diary_db.get_db() as conn:
        sql = "SELECT id, name, archived, updated_at FROM projects WHERE deleted_at IS NULL"
        if not include_archived:
            sql += " AND archived = FALSE"
        projects = conn.execute(sql).fetchall()

        if not projects:
            return "Keine Projekte gefunden."

        # Batch fetch milestones
        project_ids = [p["id"] for p in projects]
        milestones_all = conn.execute(
            "SELECT project_id, completed FROM milestones WHERE project_id = ANY(%s) AND deleted_at IS NULL",
            (project_ids,),
        ).fetchall()

        milestones_by_project = {}
        for m in milestones_all:
            milestones_by_project.setdefault(m["project_id"], []).append(m)

        # Batch fetch reminders
        reminders_all = conn.execute(
            "SELECT project_id, target_date, completed FROM reminders WHERE project_id = ANY(%s) AND deleted_at IS NULL",
            (project_ids,),
        ).fetchall()

        reminders_by_project = {}
        for r in reminders_all:
            reminders_by_project.setdefault(r["project_id"], []).append(r)

        result = ["Aktuelle Projekte im Diary:"]
        for p in projects:
            pid = p["id"]
            milestones = milestones_by_project.get(pid, [])
            comp = 0.0
            if milestones:
                comp_count = sum(1 for m in milestones if m["completed"])
                comp = round((comp_count / len(milestones)) * 100, 1)

            reminders = reminders_by_project.get(pid, [])
            due_count = sum(
                1 for r in reminders if not r["completed"] and _is_reminder_due(r["target_date"])
            )

            archived_tag = " [ARCHIVIERT]" if p["archived"] else ""
            warning_tag = f" ⚠️ [ACHTUNG: {due_count} fällig!]" if due_count > 0 else ""
            result.append(
                f"- {p['name']}{archived_tag}: {comp}% abgeschlossen (Zuletzt: {p['updated_at']}){warning_tag}"
            )

    return "\n".join(result)


@mcp.tool()
def get_project(project_name: str) -> str:
    cfg = load_config()
    limit = cfg.get("default_log_limit", 20)

    with diary_db.get_db() as conn:
        p = conn.execute(
            "SELECT * FROM projects WHERE name = %s AND deleted_at IS NULL", (project_name,)
        ).fetchone()
        if not p:
            return f"Projekt '{project_name}' nicht gefunden."

        pid = p["id"]
        diary_db.apply_log_retention(conn, pid)

        milestones = conn.execute(
            "SELECT * FROM milestones WHERE project_id = %s AND deleted_at IS NULL", (pid,)
        ).fetchall()
        comp = 0.0
        if milestones:
            comp_count = sum(1 for m in milestones if m["completed"])
            comp = round((comp_count / len(milestones)) * 100, 1)

        archived_str = " [ARCHIVIERT]" if p["archived"] else ""
        res = [f"=== Projekt: {p['name']}{archived_str} ==="]
        res.append(f"Erstellt am: {p['created_at']}")
        res.append(f"Fortschritt: {comp}%")
        res.append(f"\nAktueller Status/Fokus:\n{p['status']}\n")

        err_count = conn.execute(
            "SELECT COUNT(*) AS c FROM errors_solutions WHERE project_id = %s AND deleted_at IS NULL", (pid,)
        ).fetchone()["c"]
        if err_count > 0:
            res.append(f"Bekannte Fehler & Lösungen: {err_count} (Nutze get_errors_solutions zum Abrufen)\n")

        reminders = conn.execute(
            "SELECT * FROM reminders WHERE project_id = %s AND deleted_at IS NULL", (pid,)
        ).fetchall()
        if reminders:
            res.append("Wiedervorlagen:")
            for r in reminders:
                mark = "[x]" if r["completed"] else "[ ]"
                due = " ⚠️ [FÄLLIG!]" if not r["completed"] and _is_reminder_due(r["target_date"]) else ""
                res.append(f"  R{r['id']}: {mark} {r['target_date']} - {r['note']}{due}")

        res.append("\nMeilensteine & Aufgaben:")
        if not milestones:
            res.append("  (Keine)")
        else:
            milestone_ids = [m["id"] for m in milestones]
            tasks_all = conn.execute(
                "SELECT * FROM tasks WHERE milestone_id = ANY(%s) AND deleted_at IS NULL",
                (milestone_ids,),
            ).fetchall()
            tasks_by_m = {}
            for t in tasks_all:
                tasks_by_m.setdefault(t["milestone_id"], []).append(t)

        for m in milestones:
            mark = "[x]" if m["completed"] else "[ ]"
            comp_date = f" (am {m['completed_at']})" if m["completed"] and m["completed_at"] else ""
            res.append(f"  M{m['id']}: {mark} {m['title']}{comp_date}")
            for t in tasks_by_m.get(m["id"], []):
                t_mark = "[x]" if t["completed"] else "[ ]"
                res.append(f"      T{t['id']}: {t_mark} {t['title']}")

        res.append(f"\nLetzte Log-Einträge (Max {limit}):")
        logs = conn.execute(
            "SELECT * FROM logs WHERE project_id = %s AND deleted_at IS NULL "
            "ORDER BY timestamp DESC LIMIT %s",
            (pid, limit),
        ).fetchall()
        if not logs:
            res.append("  (Keine)")
        for entry in reversed(logs):
            res.append(
                f"  L{entry['id']} [{entry['timestamp']}] [{entry['level']}] {entry['worker']}: {entry['entry']}"
            )

    return "\n".join(res)


@mcp.tool()
def add_project(project_name: str, initial_status: str = "") -> str:
    now = _now()
    author = load_config().get("default_author", "System")
    with diary_db.get_db() as conn:
        if diary_db.get_project_id(conn, project_name):
            return "Projekt existiert bereits."
        # A tombstoned project keeps its row (name stays UNIQUE) — revive it instead
        # of inserting, same rule as memory_upsert reviving a deleted memory_nodes path.
        tombstoned = conn.execute(
            "SELECT id FROM projects WHERE name = %s AND deleted_at IS NOT NULL", (project_name,)
        ).fetchone()
        if tombstoned:
            pid = tombstoned["id"]
            conn.execute(
                "UPDATE projects SET status = %s, archived = FALSE, deleted_at = NULL, "
                "updated_at = now() WHERE id = %s",
                (initial_status, pid),
            )
        else:
            row = conn.execute(
                "INSERT INTO projects (name, status) VALUES (%s, %s) RETURNING id",
                (project_name, initial_status),
            ).fetchone()
            pid = row["id"]
        conn.execute(
            "INSERT INTO logs (project_id, timestamp, author, entry, level, worker) VALUES (%s, %s, %s, %s, %s, %s)",
            (pid, now, author, "Projekt erstellt.", "INFO", "System"),
        )
    return f"Projekt '{project_name}' angelegt."


@mcp.tool()
def delete_project(project_name: str) -> str:
    """Löscht ein Projekt (Tombstone) inkl. aller Kind-Datensätze (Milestones, Tasks,
    Logs, Reminders, Wiki-Seiten, Errors/Solutions), damit die Löschung per
    memory_sync_diary() zur Remote propagiert statt sie durch DB-CASCADE zu verlieren."""
    with diary_db.get_db() as conn:
        pid = diary_db.get_project_id(conn, project_name)
        if not pid:
            return "Nicht gefunden."
        conn.execute(
            "UPDATE tasks SET deleted_at = now(), updated_at = now() WHERE deleted_at IS NULL AND milestone_id IN "
            "(SELECT id FROM milestones WHERE project_id = %s)",
            (pid,),
        )
        conn.execute(
            "UPDATE milestones SET deleted_at = now(), updated_at = now() "
            "WHERE project_id = %s AND deleted_at IS NULL",
            (pid,),
        )
        conn.execute(
            "UPDATE logs SET deleted_at = now(), updated_at = now() "
            "WHERE project_id = %s AND deleted_at IS NULL",
            (pid,),
        )
        conn.execute(
            "UPDATE reminders SET deleted_at = now(), updated_at = now() "
            "WHERE project_id = %s AND deleted_at IS NULL",
            (pid,),
        )
        conn.execute(
            "UPDATE wiki_pages SET deleted_at = now(), updated_at = now() "
            "WHERE project_id = %s AND deleted_at IS NULL",
            (pid,),
        )
        conn.execute(
            "UPDATE errors_solutions SET deleted_at = now(), updated_at = now() "
            "WHERE project_id = %s AND deleted_at IS NULL",
            (pid,),
        )
        conn.execute("UPDATE projects SET deleted_at = now(), updated_at = now() WHERE id = %s", (pid,))
    return "Projekt gelöscht."


@mcp.tool()
def archive_project(project_name: str, archive: bool = True) -> str:
    with diary_db.get_db() as conn:
        pid = diary_db.get_project_id(conn, project_name)
        if not pid:
            return "Nicht gefunden."
        conn.execute("UPDATE projects SET archived = %s, updated_at = %s WHERE id = %s", (archive, _now(), pid))
    return "Archiv-Status aktualisiert."


@mcp.tool()
def update_status(project_name: str, status_text: str) -> str:
    with diary_db.get_db() as conn:
        pid = diary_db.get_project_id(conn, project_name)
        if not pid:
            return "Nicht gefunden."
        conn.execute("UPDATE projects SET status = %s, updated_at = %s WHERE id = %s", (status_text, _now(), pid))
    return "Status aktualisiert."


# =====================================================================
# LOG TOOLS
# =====================================================================

@mcp.tool()
def add_log_entry(project_name: str, log_text: str, level: str = "INFO", worker: str = "System") -> str:
    """Fügt einen manuellen Log-Eintrag zum Projekt hinzu."""
    now = _now()
    author = load_config().get("default_author", "System")
    with diary_db.get_db() as conn:
        pid = diary_db.get_project_id(conn, project_name)
        if not pid:
            return "Nicht gefunden."
        conn.execute(
            "INSERT INTO logs (project_id, timestamp, author, entry, level, worker) VALUES (%s, %s, %s, %s, %s, %s)",
            (pid, now, author, log_text, level.upper(), worker),
        )
        conn.execute("UPDATE projects SET updated_at = %s WHERE id = %s", (now, pid))
        diary_db.apply_log_retention(conn, pid)
    return "Log hinzugefügt."


@mcp.tool()
def edit_log_entry(log_id: int, new_text: str) -> str:
    with diary_db.get_db() as conn:
        conn.execute(
            "UPDATE logs SET entry = %s, updated_at = now() WHERE id = %s", (new_text, log_id)
        )
    return f"Log L{log_id} aktualisiert."


@mcp.tool()
def delete_log_entry(log_id: int) -> str:
    with diary_db.get_db() as conn:
        conn.execute(
            "UPDATE logs SET deleted_at = now(), updated_at = now() WHERE id = %s", (log_id,)
        )
    return f"Log L{log_id} gelöscht."


# =====================================================================
# ERROR & SOLUTION TOOLS
# =====================================================================

@mcp.tool()
def add_error_solution(project_name: str, error_msg: str, solution_msg: str) -> str:
    """Protokolliert einen aufgetretenen Fehler und dessen genaue Lösung."""
    with diary_db.get_db() as conn:
        pid = diary_db.get_project_id(conn, project_name)
        if not pid:
            return "Projekt nicht gefunden."
        conn.execute(
            "INSERT INTO errors_solutions (project_id, error_msg, solution_msg) VALUES (%s, %s, %s)",
            (pid, error_msg, solution_msg),
        )
        _maybe_create_gh_issue(
            conn, pid,
            f"[Diary-Erfassung] {error_msg[:50]}...",
            f"Ein neuer Fehler wurde im Diary erfasst.\n\n**Fehler:**\n{error_msg}\n\n**Aktuelle Lösung/Notiz:**\n{solution_msg}",
        )
    return "Fehler & Lösung erfolgreich dokumentiert."


@mcp.tool()
def get_errors_solutions(project_name: str) -> str:
    with diary_db.get_db() as conn:
        pid = diary_db.get_project_id(conn, project_name)
        if not pid:
            return "Projekt nicht gefunden."
        rows = conn.execute(
            "SELECT id, error_msg, solution_msg, created_at FROM errors_solutions "
            "WHERE project_id = %s AND deleted_at IS NULL ORDER BY created_at DESC",
            (pid,),
        ).fetchall()

    if not rows:
        return "Bisher keine Errors/Solutions dokumentiert."
    res = [f"Errors & Solutions für '{project_name}':"]
    for r in rows:
        res.append(f"\n[ID: {r['id']} | {r['created_at']}]")
        res.append(f"❌ Fehler: {r['error_msg']}")
        res.append(f"✅ Lösung: {r['solution_msg']}")
    return "\n".join(res)


# =====================================================================
# MILESTONE & TASK TOOLS
# =====================================================================

@mcp.tool()
def add_milestone(project_name: str, title: str) -> str:
    with diary_db.get_db() as conn:
        pid = diary_db.get_project_id(conn, project_name)
        if not pid:
            return "Nicht gefunden."
        row = conn.execute(
            "INSERT INTO milestones (project_id, title) VALUES (%s, %s) RETURNING id",
            (pid, title),
        ).fetchone()
        conn.execute("UPDATE projects SET updated_at = now() WHERE id = %s", (pid,))
    return f"Meilenstein M{row['id']} hinzugefügt."


@mcp.tool()
def toggle_milestone(project_name: str, milestone_id: int, completed: bool) -> str:
    with diary_db.get_db() as conn:
        pid = diary_db.get_project_id(conn, project_name)
        if not pid:
            return "Projekt nicht gefunden."
        comp_at = _now() if completed else None
        conn.execute(
            "UPDATE milestones SET completed = %s, completed_at = %s, updated_at = now() "
            "WHERE id = %s AND project_id = %s",
            (completed, comp_at, milestone_id, pid),
        )
        conn.execute("UPDATE projects SET updated_at = now() WHERE id = %s", (pid,))
    return "Meilenstein aktualisiert."


@mcp.tool()
def edit_milestone(project_name: str, milestone_id: int, new_title: str) -> str:
    with diary_db.get_db() as conn:
        pid = diary_db.get_project_id(conn, project_name)
        if not pid:
            return "Projekt nicht gefunden."
        conn.execute(
            "UPDATE milestones SET title = %s, updated_at = now() WHERE id = %s AND project_id = %s",
            (new_title, milestone_id, pid),
        )
    return "Umbenannt."


@mcp.tool()
def delete_milestone(project_name: str, milestone_id: int) -> str:
    """Löscht einen Meilenstein (Tombstone) inkl. seiner Tasks."""
    with diary_db.get_db() as conn:
        pid = diary_db.get_project_id(conn, project_name)
        if not pid:
            return "Projekt nicht gefunden."
        conn.execute(
            "UPDATE tasks SET deleted_at = now(), updated_at = now() "
            "WHERE milestone_id = %s AND deleted_at IS NULL",
            (milestone_id,),
        )
        conn.execute(
            "UPDATE milestones SET deleted_at = now(), updated_at = now() "
            "WHERE id = %s AND project_id = %s",
            (milestone_id, pid),
        )
    return "Gelöscht."


@mcp.tool()
def get_active_tasks(project_name: str) -> str:
    with diary_db.get_db() as conn:
        pid = diary_db.get_project_id(conn, project_name)
        if not pid:
            return "Nicht gefunden."
        tasks = conn.execute(
            "SELECT m.title AS m_title, t.id, t.title FROM tasks t "
            "JOIN milestones m ON t.milestone_id = m.id "
            "WHERE m.project_id = %s AND t.completed = FALSE "
            "AND t.deleted_at IS NULL AND m.deleted_at IS NULL ORDER BY m.id",
            (pid,),
        ).fetchall()

    if not tasks:
        return f"Alle Aufgaben in '{project_name}' sind erledigt!"
    res = [f"Aktuell offene Aufgaben für '{project_name}':"]
    current_m = None
    for t in tasks:
        if t["m_title"] != current_m:
            current_m = t["m_title"]
            res.append(f"\n[{current_m}]")
        res.append(f"  - T{t['id']}: {t['title']}")
    return "\n".join(res)


@mcp.tool()
def add_task(project_name: str, milestone_id: int, title: str) -> str:
    with diary_db.get_db() as conn:
        pid = diary_db.get_project_id(conn, project_name)
        if not pid:
            return "Projekt nicht gefunden."
        row = conn.execute(
            "INSERT INTO tasks (milestone_id, title) VALUES (%s, %s) RETURNING id",
            (milestone_id, title),
        ).fetchone()
    return f"Task T{row['id']} hinzugefügt."


@mcp.tool()
def toggle_task(project_name: str, milestone_id: int, task_id: int, completed: bool) -> str:
    with diary_db.get_db() as conn:
        conn.execute(
            "UPDATE tasks SET completed = %s, updated_at = now() WHERE id = %s AND milestone_id = %s",
            (completed, task_id, milestone_id),
        )
    return "Task aktualisiert."


@mcp.tool()
def edit_task(project_name: str, milestone_id: int, task_id: int, new_title: str) -> str:
    with diary_db.get_db() as conn:
        conn.execute(
            "UPDATE tasks SET title = %s, updated_at = now() WHERE id = %s AND milestone_id = %s",
            (new_title, task_id, milestone_id),
        )
    return "Task umbenannt."


@mcp.tool()
def delete_task(project_name: str, milestone_id: int, task_id: int) -> str:
    with diary_db.get_db() as conn:
        conn.execute(
            "UPDATE tasks SET deleted_at = now(), updated_at = now() "
            "WHERE id = %s AND milestone_id = %s",
            (task_id, milestone_id),
        )
    return "Task gelöscht."


# =====================================================================
# REMINDER TOOLS
# =====================================================================

@mcp.tool()
def add_reminder(project_name: str, target_date: str, note: str) -> str:
    with diary_db.get_db() as conn:
        pid = diary_db.get_project_id(conn, project_name)
        if not pid:
            return "Projekt nicht gefunden."
        row = conn.execute(
            "INSERT INTO reminders (project_id, target_date, note) VALUES (%s, %s, %s) RETURNING id",
            (pid, target_date, note),
        ).fetchone()
    return f"Wiedervorlage R{row['id']} erstellt."


@mcp.tool()
def edit_reminder(reminder_id: int, new_date: str, new_note: str) -> str:
    with diary_db.get_db() as conn:
        conn.execute(
            "UPDATE reminders SET target_date = %s, note = %s, updated_at = now() WHERE id = %s",
            (new_date, new_note, reminder_id),
        )
    return f"Reminder R{reminder_id} erfolgreich bearbeitet."


@mcp.tool()
def snooze_reminder(reminder_id: int, add_days: int) -> str:
    fmt = load_config()["date_format"]
    with diary_db.get_db() as conn:
        row = conn.execute("SELECT target_date FROM reminders WHERE id = %s", (reminder_id,)).fetchone()
        if not row:
            return "Reminder nicht gefunden."
        date_str = row["target_date"]
        try:
            if len(date_str) == 10:
                dt = datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=add_days)
                new_date = dt.strftime("%Y-%m-%d")
            else:
                dt = datetime.strptime(date_str, fmt) + timedelta(days=add_days)
                new_date = dt.strftime(fmt)
        except ValueError:
            return f"Konnte das Datum '{date_str}' nicht parsen."
        conn.execute(
            "UPDATE reminders SET target_date = %s, updated_at = now() WHERE id = %s",
            (new_date, reminder_id),
        )
    return f"Reminder R{reminder_id} um {add_days} Tage auf {new_date} aufgeschoben ('snooze')."


@mcp.tool()
def toggle_reminder(reminder_id: int, completed: bool) -> str:
    with diary_db.get_db() as conn:
        conn.execute(
            "UPDATE reminders SET completed = %s, updated_at = now() WHERE id = %s",
            (completed, reminder_id),
        )
    return "Reminder aktualisiert."


@mcp.tool()
def delete_reminder(reminder_id: int) -> str:
    with diary_db.get_db() as conn:
        conn.execute(
            "UPDATE reminders SET deleted_at = now(), updated_at = now() WHERE id = %s", (reminder_id,)
        )
    return "Reminder gelöscht."


# =====================================================================
# WIKI TOOLS
# =====================================================================

@mcp.tool()
def add_wiki_page(project_name: str, title: str, content: str) -> str:
    with diary_db.get_db() as conn:
        pid = diary_db.get_project_id(conn, project_name)
        if not pid:
            return "Projekt nicht gefunden."
        row = conn.execute(
            "INSERT INTO wiki_pages (project_id, title, content) VALUES (%s, %s, %s) RETURNING id",
            (pid, title, content),
        ).fetchone()
    return f"Wiki-Seite W{row['id']} '{title}' angelegt."


@mcp.tool()
def edit_wiki_page(project_name: str, page_id: int, new_content: str) -> str:
    with diary_db.get_db() as conn:
        pid = diary_db.get_project_id(conn, project_name)
        if not pid:
            return "Projekt nicht gefunden."
        conn.execute(
            "UPDATE wiki_pages SET content = %s, updated_at = now() "
            "WHERE id = %s AND project_id = %s AND deleted_at IS NULL",
            (new_content, page_id, pid),
        )
    return "Wiki-Seite aktualisiert."


@mcp.tool()
def get_wiki_pages(project_name: str) -> str:
    with diary_db.get_db() as conn:
        pid = diary_db.get_project_id(conn, project_name)
        if not pid:
            return "Projekt nicht gefunden."
        pages = conn.execute(
            "SELECT id, title, updated_at FROM wiki_pages WHERE project_id = %s AND deleted_at IS NULL",
            (pid,),
        ).fetchall()

    if not pages:
        return "Das Wiki ist noch leer."
    res = [f"Wiki-Seiten für '{project_name}':"]
    for p in pages:
        res.append(f"  W{p['id']}: {p['title']} (Zuletzt geändert: {p['updated_at']})")
    return "\n".join(res)


@mcp.tool()
def get_wiki_page(project_name: str, page_id: int) -> str:
    with diary_db.get_db() as conn:
        pid = diary_db.get_project_id(conn, project_name)
        if not pid:
            return "Projekt nicht gefunden."
        page = conn.execute(
            "SELECT title, content, updated_at FROM wiki_pages "
            "WHERE id = %s AND project_id = %s AND deleted_at IS NULL",
            (page_id, pid),
        ).fetchone()

    if not page:
        return "Wiki-Seite nicht gefunden."
    return f"=== WIKI: {page['title']} ===\nLetztes Update: {page['updated_at']}\n\n{page['content']}"


@mcp.tool()
def search_wiki(project_name: str, search_query: str) -> str:
    words = search_query.split()
    if not words:
        return "Kein Suchbegriff angegeben."

    with diary_db.get_db() as conn:
        pid = diary_db.get_project_id(conn, project_name)
        if not pid:
            return "Projekt nicht gefunden."

        conditions = ["(title ILIKE %s OR content ILIKE %s)" for _ in words]
        params: list = [pid] + [val for w in words for val in (f"%{w}%", f"%{w}%")]
        sql = (
            "SELECT id, title, substr(content, 1, 100) AS snippet FROM wiki_pages "
            f"WHERE project_id = %s AND deleted_at IS NULL AND {' AND '.join(conditions)}"
        )
        pages = conn.execute(sql, params).fetchall()

    if not pages:
        return f"Keine Ergebnisse für '{search_query}' im Wiki gefunden."
    res = [f"Wiki-Suchergebnisse für '{search_query}':"]
    for p in pages:
        snippet = p["snippet"].replace("\n", " ") + "..."
        res.append(f"  - W{p['id']} | Titel: {p['title']}\n      Preview: {snippet}")
    return "\n".join(res)
