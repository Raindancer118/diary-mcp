import json
import logging
import subprocess
import urllib.error
import urllib.request
from datetime import datetime, timedelta

from mcp.server.fastmcp import FastMCP

import psycopg
from psycopg.rows import dict_row

import diary_embed
from diary_config import load_config, save_config
from diary_db import (
    _BACKFILL_PARENT_SQL,
    apply_log_retention,
    get_db,
    get_project_id,
    get_remote_db,
    get_remote_ssh_host,
    get_remote_url,
    init_db,
    remote_db_url,
)

_log = logging.getLogger(__name__)
mcp = FastMCP("Diary")
init_db()


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
    with get_db() as conn:
        pid = get_project_id(conn, project_name)
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
    with get_db() as conn:
        pid = get_project_id(conn, project_name)
        if not pid:
            return "Projekt nicht gefunden."
        conn.execute("UPDATE projects SET config = %s WHERE id = %s", (config_json_string, pid))
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

        with get_db() as conn:
            for item in data:
                proj = item.get("project", "Unbekannt")
                level = item.get("level", "INFO").upper()
                worker = item.get("worker", "App")
                msg = item.get("message", "")
                ts = item.get("timestamp", _now())

                pid = get_project_id(conn, proj)
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

                apply_log_retention(conn, pid)
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

    with get_db() as conn:
        cond, params = like_clause(["name", "status"])
        for row in conn.execute(f"SELECT name FROM projects WHERE {cond}", params).fetchall():
            results.append(f"- [Projekt] {row['name']}")

        cond, params = like_clause(["entry", "worker"])
        for row in conn.execute(
            f"SELECT p.name, l.entry, l.worker FROM logs l JOIN projects p ON l.project_id = p.id WHERE {cond}",
            params,
        ).fetchall():
            results.append(f"- [Log in '{row['name']}' / {row['worker']}] {row['entry'][:80]}...")

        cond, params = like_clause(["error_msg", "solution_msg"])
        for row in conn.execute(
            f"SELECT p.name, e.error_msg FROM errors_solutions e JOIN projects p ON e.project_id = p.id WHERE {cond}",
            params,
        ).fetchall():
            results.append(f"- [Error/Solution in '{row['name']}'] {row['error_msg'][:80]}...")

        cond, params = like_clause(["title", "content"])
        for row in conn.execute(
            f"SELECT p.name, w.title FROM wiki_pages w JOIN projects p ON w.project_id = p.id WHERE {cond}",
            params,
        ).fetchall():
            results.append(f"- [Wiki in '{row['name']}'] Seite: {row['title']}")

        cond, params = like_clause(["t.title"])
        for row in conn.execute(
            "SELECT p.name, m.title AS m_title, t.title AS t_title "
            "FROM tasks t JOIN milestones m ON t.milestone_id = m.id JOIN projects p ON m.project_id = p.id "
            f"WHERE {cond}",
            params,
        ).fetchall():
            results.append(f"- [Aufgabe in '{row['name']}' -> '{row['m_title']}'] {row['t_title']}")

    return "\n".join(results) if len(results) > 1 else "Nichts gefunden."


@mcp.tool()
def filter_logs(project_name: str, level: str = None, worker: str = None, limit: int = 50) -> str:
    """Filtert Logs eines Projekts gezielt nach Loglevel (z.B. ERROR) oder Worker/Subprozess."""
    with get_db() as conn:
        pid = get_project_id(conn, project_name)
        if not pid:
            return "Projekt nicht gefunden."

        sql = "SELECT * FROM logs WHERE project_id = %s"
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

    with get_db() as conn:
        sql = "SELECT id, name, archived, updated_at FROM projects"
        if not include_archived:
            sql += " WHERE archived = FALSE"
        projects = conn.execute(sql).fetchall()

        if not projects:
            return "Keine Projekte gefunden."

        result = ["Aktuelle Projekte im Diary:"]
        for p in projects:
            milestones = conn.execute(
                "SELECT completed FROM milestones WHERE project_id = %s", (p["id"],)
            ).fetchall()
            comp = 0.0
            if milestones:
                comp_count = sum(1 for m in milestones if m["completed"])
                comp = round((comp_count / len(milestones)) * 100, 1)

            reminders = conn.execute(
                "SELECT target_date, completed FROM reminders WHERE project_id = %s", (p["id"],)
            ).fetchall()
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

    with get_db() as conn:
        p = conn.execute("SELECT * FROM projects WHERE name = %s", (project_name,)).fetchone()
        if not p:
            return f"Projekt '{project_name}' nicht gefunden."

        pid = p["id"]
        apply_log_retention(conn, pid)

        milestones = conn.execute("SELECT * FROM milestones WHERE project_id = %s", (pid,)).fetchall()
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
            "SELECT COUNT(*) AS c FROM errors_solutions WHERE project_id = %s", (pid,)
        ).fetchone()["c"]
        if err_count > 0:
            res.append(f"Bekannte Fehler & Lösungen: {err_count} (Nutze get_errors_solutions zum Abrufen)\n")

        reminders = conn.execute("SELECT * FROM reminders WHERE project_id = %s", (pid,)).fetchall()
        if reminders:
            res.append("Wiedervorlagen:")
            for r in reminders:
                mark = "[x]" if r["completed"] else "[ ]"
                due = " ⚠️ [FÄLLIG!]" if not r["completed"] and _is_reminder_due(r["target_date"]) else ""
                res.append(f"  R{r['id']}: {mark} {r['target_date']} - {r['note']}{due}")

        res.append("\nMeilensteine & Aufgaben:")
        if not milestones:
            res.append("  (Keine)")
        for m in milestones:
            mark = "[x]" if m["completed"] else "[ ]"
            comp_date = f" (am {m['completed_at']})" if m["completed"] and m["completed_at"] else ""
            res.append(f"  M{m['id']}: {mark} {m['title']}{comp_date}")
            for t in conn.execute("SELECT * FROM tasks WHERE milestone_id = %s", (m["id"],)).fetchall():
                t_mark = "[x]" if t["completed"] else "[ ]"
                res.append(f"      T{t['id']}: {t_mark} {t['title']}")

        res.append(f"\nLetzte Log-Einträge (Max {limit}):")
        logs = conn.execute(
            "SELECT * FROM logs WHERE project_id = %s ORDER BY timestamp DESC LIMIT %s", (pid, limit)
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
    with get_db() as conn:
        if get_project_id(conn, project_name):
            return "Projekt existiert bereits."
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
    with get_db() as conn:
        pid = get_project_id(conn, project_name)
        if not pid:
            return "Nicht gefunden."
        conn.execute("DELETE FROM projects WHERE id = %s", (pid,))
    return "Projekt gelöscht."


@mcp.tool()
def archive_project(project_name: str, archive: bool = True) -> str:
    with get_db() as conn:
        pid = get_project_id(conn, project_name)
        if not pid:
            return "Nicht gefunden."
        conn.execute("UPDATE projects SET archived = %s, updated_at = %s WHERE id = %s", (archive, _now(), pid))
    return "Archiv-Status aktualisiert."


@mcp.tool()
def update_status(project_name: str, status_text: str) -> str:
    with get_db() as conn:
        pid = get_project_id(conn, project_name)
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
    with get_db() as conn:
        pid = get_project_id(conn, project_name)
        if not pid:
            return "Nicht gefunden."
        conn.execute(
            "INSERT INTO logs (project_id, timestamp, author, entry, level, worker) VALUES (%s, %s, %s, %s, %s, %s)",
            (pid, now, author, log_text, level.upper(), worker),
        )
        conn.execute("UPDATE projects SET updated_at = %s WHERE id = %s", (now, pid))
        apply_log_retention(conn, pid)
    return "Log hinzugefügt."


@mcp.tool()
def edit_log_entry(log_id: int, new_text: str) -> str:
    with get_db() as conn:
        conn.execute("UPDATE logs SET entry = %s WHERE id = %s", (new_text, log_id))
    return f"Log L{log_id} aktualisiert."


@mcp.tool()
def delete_log_entry(log_id: int) -> str:
    with get_db() as conn:
        conn.execute("DELETE FROM logs WHERE id = %s", (log_id,))
    return f"Log L{log_id} gelöscht."


# =====================================================================
# ERROR & SOLUTION TOOLS
# =====================================================================

@mcp.tool()
def add_error_solution(project_name: str, error_msg: str, solution_msg: str) -> str:
    """Protokolliert einen aufgetretenen Fehler und dessen genaue Lösung."""
    with get_db() as conn:
        pid = get_project_id(conn, project_name)
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
    with get_db() as conn:
        pid = get_project_id(conn, project_name)
        if not pid:
            return "Projekt nicht gefunden."
        rows = conn.execute(
            "SELECT id, error_msg, solution_msg, created_at FROM errors_solutions "
            "WHERE project_id = %s ORDER BY created_at DESC",
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
    with get_db() as conn:
        pid = get_project_id(conn, project_name)
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
    with get_db() as conn:
        pid = get_project_id(conn, project_name)
        if not pid:
            return "Projekt nicht gefunden."
        comp_at = _now() if completed else None
        conn.execute(
            "UPDATE milestones SET completed = %s, completed_at = %s WHERE id = %s AND project_id = %s",
            (completed, comp_at, milestone_id, pid),
        )
        conn.execute("UPDATE projects SET updated_at = now() WHERE id = %s", (pid,))
    return "Meilenstein aktualisiert."


@mcp.tool()
def edit_milestone(project_name: str, milestone_id: int, new_title: str) -> str:
    with get_db() as conn:
        pid = get_project_id(conn, project_name)
        if not pid:
            return "Projekt nicht gefunden."
        conn.execute(
            "UPDATE milestones SET title = %s WHERE id = %s AND project_id = %s",
            (new_title, milestone_id, pid),
        )
    return "Umbenannt."


@mcp.tool()
def delete_milestone(project_name: str, milestone_id: int) -> str:
    with get_db() as conn:
        pid = get_project_id(conn, project_name)
        if not pid:
            return "Projekt nicht gefunden."
        conn.execute("DELETE FROM milestones WHERE id = %s AND project_id = %s", (milestone_id, pid))
    return "Gelöscht."


@mcp.tool()
def get_active_tasks(project_name: str) -> str:
    with get_db() as conn:
        pid = get_project_id(conn, project_name)
        if not pid:
            return "Nicht gefunden."
        tasks = conn.execute(
            "SELECT m.title AS m_title, t.id, t.title FROM tasks t "
            "JOIN milestones m ON t.milestone_id = m.id "
            "WHERE m.project_id = %s AND t.completed = FALSE ORDER BY m.id",
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
    with get_db() as conn:
        pid = get_project_id(conn, project_name)
        if not pid:
            return "Projekt nicht gefunden."
        row = conn.execute(
            "INSERT INTO tasks (milestone_id, title) VALUES (%s, %s) RETURNING id",
            (milestone_id, title),
        ).fetchone()
    return f"Task T{row['id']} hinzugefügt."


@mcp.tool()
def toggle_task(project_name: str, milestone_id: int, task_id: int, completed: bool) -> str:
    with get_db() as conn:
        conn.execute(
            "UPDATE tasks SET completed = %s WHERE id = %s AND milestone_id = %s",
            (completed, task_id, milestone_id),
        )
    return "Task aktualisiert."


@mcp.tool()
def edit_task(project_name: str, milestone_id: int, task_id: int, new_title: str) -> str:
    with get_db() as conn:
        conn.execute(
            "UPDATE tasks SET title = %s WHERE id = %s AND milestone_id = %s",
            (new_title, task_id, milestone_id),
        )
    return "Task umbenannt."


@mcp.tool()
def delete_task(project_name: str, milestone_id: int, task_id: int) -> str:
    with get_db() as conn:
        conn.execute("DELETE FROM tasks WHERE id = %s AND milestone_id = %s", (task_id, milestone_id))
    return "Task gelöscht."


# =====================================================================
# REMINDER TOOLS
# =====================================================================

@mcp.tool()
def add_reminder(project_name: str, target_date: str, note: str) -> str:
    with get_db() as conn:
        pid = get_project_id(conn, project_name)
        if not pid:
            return "Projekt nicht gefunden."
        row = conn.execute(
            "INSERT INTO reminders (project_id, target_date, note) VALUES (%s, %s, %s) RETURNING id",
            (pid, target_date, note),
        ).fetchone()
    return f"Wiedervorlage R{row['id']} erstellt."


@mcp.tool()
def edit_reminder(reminder_id: int, new_date: str, new_note: str) -> str:
    with get_db() as conn:
        conn.execute(
            "UPDATE reminders SET target_date = %s, note = %s WHERE id = %s",
            (new_date, new_note, reminder_id),
        )
    return f"Reminder R{reminder_id} erfolgreich bearbeitet."


@mcp.tool()
def snooze_reminder(reminder_id: int, add_days: int) -> str:
    fmt = load_config()["date_format"]
    with get_db() as conn:
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
        conn.execute("UPDATE reminders SET target_date = %s WHERE id = %s", (new_date, reminder_id))
    return f"Reminder R{reminder_id} um {add_days} Tage auf {new_date} aufgeschoben ('snooze')."


@mcp.tool()
def toggle_reminder(reminder_id: int, completed: bool) -> str:
    with get_db() as conn:
        conn.execute("UPDATE reminders SET completed = %s WHERE id = %s", (completed, reminder_id))
    return "Reminder aktualisiert."


@mcp.tool()
def delete_reminder(reminder_id: int) -> str:
    with get_db() as conn:
        conn.execute("DELETE FROM reminders WHERE id = %s", (reminder_id,))
    return "Reminder gelöscht."


# =====================================================================
# WIKI TOOLS
# =====================================================================

@mcp.tool()
def add_wiki_page(project_name: str, title: str, content: str) -> str:
    with get_db() as conn:
        pid = get_project_id(conn, project_name)
        if not pid:
            return "Projekt nicht gefunden."
        row = conn.execute(
            "INSERT INTO wiki_pages (project_id, title, content) VALUES (%s, %s, %s) RETURNING id",
            (pid, title, content),
        ).fetchone()
    return f"Wiki-Seite W{row['id']} '{title}' angelegt."


@mcp.tool()
def edit_wiki_page(project_name: str, page_id: int, new_content: str) -> str:
    with get_db() as conn:
        pid = get_project_id(conn, project_name)
        if not pid:
            return "Projekt nicht gefunden."
        conn.execute(
            "UPDATE wiki_pages SET content = %s, updated_at = now() WHERE id = %s AND project_id = %s",
            (new_content, page_id, pid),
        )
    return "Wiki-Seite aktualisiert."


@mcp.tool()
def get_wiki_pages(project_name: str) -> str:
    with get_db() as conn:
        pid = get_project_id(conn, project_name)
        if not pid:
            return "Projekt nicht gefunden."
        pages = conn.execute(
            "SELECT id, title, updated_at FROM wiki_pages WHERE project_id = %s", (pid,)
        ).fetchall()

    if not pages:
        return "Das Wiki ist noch leer."
    res = [f"Wiki-Seiten für '{project_name}':"]
    for p in pages:
        res.append(f"  W{p['id']}: {p['title']} (Zuletzt geändert: {p['updated_at']})")
    return "\n".join(res)


@mcp.tool()
def get_wiki_page(project_name: str, page_id: int) -> str:
    with get_db() as conn:
        pid = get_project_id(conn, project_name)
        if not pid:
            return "Projekt nicht gefunden."
        page = conn.execute(
            "SELECT title, content, updated_at FROM wiki_pages WHERE id = %s AND project_id = %s",
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

    with get_db() as conn:
        pid = get_project_id(conn, project_name)
        if not pid:
            return "Projekt nicht gefunden."

        conditions = ["(title ILIKE %s OR content ILIKE %s)" for _ in words]
        params: list = [pid] + [val for w in words for val in (f"%{w}%", f"%{w}%")]
        sql = (
            "SELECT id, title, substr(content, 1, 100) AS snippet FROM wiki_pages "
            f"WHERE project_id = %s AND {' AND '.join(conditions)}"
        )
        pages = conn.execute(sql, params).fetchall()

    if not pages:
        return f"Keine Ergebnisse für '{search_query}' im Wiki gefunden."
    res = [f"Wiki-Suchergebnisse für '{search_query}':"]
    for p in pages:
        snippet = p["snippet"].replace("\n", " ") + "..."
        res.append(f"  - W{p['id']} | Titel: {p['title']}\n      Preview: {snippet}")
    return "\n".join(res)


# =====================================================================
# MEMORY TREE TOOLS
# =====================================================================

def _ensure_memory_parent(conn, path: str):
    """Ensures all parent nodes exist for the given path; returns immediate parent id."""
    parts = path.strip("/").split("/")
    if len(parts) <= 1:
        return None

    parent_path = "/" + "/".join(parts[:-1])
    row = conn.execute("SELECT id FROM memory_nodes WHERE path = %s", (parent_path,)).fetchone()
    if row:
        return row["id"]

    grandparent_id = _ensure_memory_parent(conn, parent_path)
    parent_slug = parts[-2]
    row = conn.execute(
        """INSERT INTO memory_nodes (parent_id, path, slug, type, title)
           VALUES (%s, %s, %s, 'category', %s)
           ON CONFLICT (path) DO UPDATE SET updated_at = now()
           RETURNING id""",
        (grandparent_id, parent_path, parent_slug, parent_slug.capitalize()),
    ).fetchone()
    return row["id"]


@mcp.tool()
def memory_context() -> str:
    """Session-Start-Snapshot: liefert den kompletten Memory-Tree als Übersicht + kürzlich geänderte Nodes mit vollem Inhalt."""
    with get_db() as conn:
        nodes = conn.execute(
            "SELECT path, type, title, updated_at FROM memory_nodes "
            "WHERE origin = 'curated' ORDER BY path"
        ).fetchall()
        recent_cutoff = datetime.now() - timedelta(days=14)
        recent = conn.execute(
            "SELECT path, title, body, updated_at FROM memory_nodes "
            "WHERE origin = 'curated' AND body IS NOT NULL AND body != '' AND updated_at > %s "
            "ORDER BY updated_at DESC",
            (recent_cutoff,),
        ).fetchall()
        extracted_count = conn.execute(
            "SELECT COUNT(*) AS c FROM memory_nodes WHERE origin = 'extracted'"
        ).fetchone()["c"]

    lines = ["=== Claude Memory Context ===\n", "MEMORY TREE:"]
    for node in nodes:
        depth = node["path"].count("/") - 1
        indent = "  " * max(0, depth)
        updated = str(node["updated_at"])[:10]
        lines.append(f"{indent}[{node['type']}] {node['path']} — {node['title']} ({updated})")

    if recent:
        lines.append("\n\nRECENTLY UPDATED (last 14 days):")
        for node in recent:
            lines.append(f"\n--- {node['path']} ---")
            lines.append(f"Titel: {node['title']}")
            lines.append(f"Geändert: {str(node['updated_at'])[:10]}")
            lines.append(node["body"] or "")
            lines.append("---")

    if extracted_count:
        lines.append(
            f"\n\n(+ {extracted_count} auto-extrahierte Memories aus Chat-Transkripten — "
            f"standardmäßig NICHT geladen/durchsucht, da kostspieliger. "
            f"Bei Bedarf gezielt via memory_search(query, include_extracted=True).)"
        )

    return "\n".join(lines)


@mcp.tool()
def memory_tree(path: str = "/", include_extracted: bool = False) -> str:
    """Gibt den Memory-Tree ab einem bestimmten Pfad aus (default: Wurzel).

    include_extracted=False (Default) blendet die automatisch aus Transkripten
    extrahierten Memories aus, damit der Baum kompakt und hochwertig bleibt.
    """
    origin_clause = "" if include_extracted else "AND origin = 'curated'"
    with get_db() as conn:
        if path == "/":
            nodes = conn.execute(
                f"SELECT path, type, title, updated_at FROM memory_nodes "
                f"WHERE TRUE {origin_clause} ORDER BY path"
            ).fetchall()
        else:
            nodes = conn.execute(
                f"SELECT path, type, title, updated_at FROM memory_nodes "
                f"WHERE (path = %s OR path LIKE %s) {origin_clause} ORDER BY path",
                (path, f"{path}/%"),
            ).fetchall()

    if not nodes:
        return f"Kein Memory-Node unter '{path}' gefunden."

    lines = [f"Memory Tree: {path}"]
    for node in nodes:
        depth = node["path"].count("/") - 1
        indent = "  " * max(0, depth)
        updated = str(node["updated_at"])[:10]
        lines.append(f"{indent}[{node['type']}] {node['path']} — {node['title']} ({updated})")
    return "\n".join(lines)


@mcp.tool()
def memory_get(path: str) -> str:
    """Gibt den vollen Inhalt eines Memory-Nodes zurück und trackt den Zugriff."""
    with get_db() as conn:
        node = conn.execute("SELECT * FROM memory_nodes WHERE path = %s", (path,)).fetchone()
        if not node:
            return f"Kein Memory-Node unter '{path}' gefunden."

        # Track access (OpenMemory-inspired composite scoring input)
        conn.execute(
            "UPDATE memory_nodes SET access_count = access_count + 1, accessed_at = now() WHERE path = %s",
            (path,),
        )

        # Fetch outgoing links
        links = conn.execute(
            """SELECT ml.rel_type, ml.note, mn.path AS target_path, mn.title AS target_title
               FROM memory_links ml
               JOIN memory_nodes mn ON ml.to_id = mn.id
               WHERE ml.from_id = %s""",
            (node["id"],),
        ).fetchall()

        # Warn if expired
        expired_note = ""
        if node.get("valid_until") and node["valid_until"] < datetime.now():
            expired_note = f"\n⚠️  ABGELAUFEN seit {str(node['valid_until'])[:10]}"

    lines = [
        f"=== Memory: {node['title']} ==={expired_note}",
        f"Pfad:       {node['path']}",
        f"Typ:        {node['type']}",
        f"Wichtigkeit: {node['importance']:.1f} | Zugriffe: {node['access_count']}"
        f"{' | AUTO-INJECT' if node.get('auto_inject') else ''}",
        f"Tags:       {', '.join(node['tags']) if node['tags'] else '—'}",
        f"Gültig bis: {str(node['valid_until'])[:10] if node['valid_until'] else '—'}",
        f"Erstellt:   {str(node['created_at'])[:10]} | Geändert: {str(node['updated_at'])[:10]}",
        "",
        node["body"] or "(kein Inhalt)",
    ]
    if links:
        lines.append("\nVerknüpfungen:")
        for lnk in links:
            note = f" — {lnk['note']}" if lnk.get("note") else ""
            lines.append(f"  [{lnk['rel_type']}] {lnk['target_path']} ({lnk['target_title']}){note}")
    return "\n".join(lines)


_pgvector_cache: dict[str, bool] = {}


def _pgvector_ready(conn) -> bool:
    """Cache whether the connected DB has pgvector (per database URL)."""
    from diary_db import get_database_url, has_pgvector
    url = get_database_url()
    if url not in _pgvector_cache:
        _pgvector_cache[url] = has_pgvector(conn)
    return _pgvector_cache[url]


def _refresh_vector(conn, path: str, embedding) -> None:
    """Populate the indexed embedding_v column from the REAL[] embedding (pgvector only)."""
    if embedding is None or not _pgvector_ready(conn):
        return
    conn.execute(
        "UPDATE memory_nodes SET embedding_v = embedding::vector WHERE path = %s",
        (path,),
    )


@mcp.tool()
def memory_upsert(
    path: str,
    title: str,
    body: str,
    type: str = "note",
    tags: str = "",
    importance: float = 0.5,
    valid_until: str = "",
    auto_inject: bool = False,
    origin: str = "curated",
) -> str:
    """Erstellt oder aktualisiert einen Memory-Node.

    path:        z.B. '/feedback/commit-style' oder '/projects/eduvault4/status'
    type:        user | feedback | project | reference | note | category
    tags:        kommagetrennte Tags, optional
    importance:  0.0–1.0 Wichtigkeitsscore (default 0.5)
    valid_until: ISO-Datum bis wann die Info gültig ist, z.B. '2026-07-15' (optional)
    auto_inject: wenn True, wird dieses Memory beim Laden des zugehörigen Projekts
                 automatisch in den Kontext injiziert (siehe memory_project_context).
    origin:      'curated' (Default — bewusst gespeichert, Standard-Suche) oder
                 'extracted' (automatisch aus Transkript geerntet, nur auf Anfrage).
                 Für extrahierte Memories besser memory_save_extracted() nutzen.
    """
    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []
    valid_until_val = valid_until.strip() if valid_until else None
    embedding = diary_embed.embed(f"{title}\n{body}")  # None if model unavailable

    with get_db() as conn:
        parent_id = _ensure_memory_parent(conn, path)
        slug = path.strip("/").split("/")[-1]

        existing = conn.execute("SELECT id FROM memory_nodes WHERE path = %s", (path,)).fetchone()
        if existing:
            conn.execute(
                "UPDATE memory_nodes SET title=%s, body=%s, type=%s, tags=%s, "
                "importance=%s, valid_until=%s, auto_inject=%s, origin=%s, embedding=%s, "
                "updated_at=now() WHERE path=%s",
                (title, body, type, tag_list, importance, valid_until_val, auto_inject, origin, embedding, path),
            )
            _refresh_vector(conn, path, embedding)
            return f"Memory '{path}' aktualisiert."
        else:
            conn.execute(
                "INSERT INTO memory_nodes (parent_id, path, slug, type, title, body, tags, importance, valid_until, auto_inject, origin, embedding) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (parent_id, path, slug, type, title, body, tag_list, importance, valid_until_val, auto_inject, origin, embedding),
            )
            _refresh_vector(conn, path, embedding)
            return f"Memory '{path}' erstellt."


@mcp.tool()
def memory_save_extracted(path: str, title: str, body: str, type: str = "note",
                          tags: str = "", importance: float = 0.3) -> str:
    """Speichert ein automatisch aus einem Chat-Transkript extrahiertes Memory (origin='extracted').

    Diese Memories landen in der ZWEITEN Stufe: sie werden standardmäßig NICHT
    in memory_context/memory_tree/memory_search geladen, weil sie zahlreich und
    roh sind. Sie sind nur über memory_search(query, include_extracted=True)
    auffindbar. Gedacht für eine Extraktions-Pipeline (z.B. SessionEnd-Hook),
    nicht für bewusst kuratiertes Wissen — dafür memory_upsert() verwenden.

    Standard-importance ist bewusst niedrig (0.3), da unkuratiert.
    """
    return memory_upsert(path=path, title=title, body=body, type=type, tags=tags,
                         importance=importance, origin="extracted")


@mcp.tool()
def memory_promote(path: str, importance: float = 0.7) -> str:
    """Befördert ein auto-extrahiertes Memory zu einem kuratierten (origin='curated').

    Nutze dies, wenn du beim Durchsuchen der extrahierten Memories eines findest,
    das dauerhaft wertvoll ist und künftig standardmäßig verfügbar sein soll.
    """
    with get_db() as conn:
        result = conn.execute(
            "UPDATE memory_nodes SET origin='curated', importance=%s, updated_at=now() "
            "WHERE path=%s RETURNING origin",
            (importance, path),
        ).fetchone()
        if not result:
            return f"Node '{path}' nicht gefunden."
    return f"'{path}' zu kuratiertem Memory befördert (importance {importance:.1f})."


@mcp.tool()
def memory_delete(path: str) -> str:
    """Löscht einen Memory-Node und alle seine Kinder (Unterknoten)."""
    with get_db() as conn:
        count_row = conn.execute(
            "SELECT COUNT(*) AS c FROM memory_nodes WHERE path = %s OR path LIKE %s",
            (path, f"{path}/%"),
        ).fetchone()
        count = count_row["c"]
        if count == 0:
            return f"Kein Node unter '{path}' gefunden."
        conn.execute(
            "DELETE FROM memory_nodes WHERE path = %s OR path LIKE %s",
            (path, f"{path}/%"),
        )
    return f"{count} Memory-Node(s) unter '{path}' gelöscht."


@mcp.tool()
def memory_search(query: str, include_extracted: bool = False) -> str:
    """Sucht im Memory-Tree via PostgreSQL Full-Text-Search (fällt auf ILIKE zurück).

    ZWEI-STUFEN-MODELL — wichtig:
      • Standard (include_extracted=False): durchsucht NUR die kuratierten Memories,
        die Claude bewusst gespeichert hat. Hochwertig, kompakt, günstig.
      • include_extracted=True: durchsucht ZUSÄTZLICH die automatisch aus Chat-
        Transkripten extrahierten Memories. Diese sind zahlreich, roh und
        kostspieliger im Kontext — nur bewusst einschalten, z.B. wenn die
        kuratierte Suche nichts Brauchbares liefert oder du gezielt nach einem
        Detail aus einem früheren Gespräch suchst.

    Die Standardsuche zeigt am Ende an, wie viele Treffer es zusätzlich in den
    auto-extrahierten Memories gäbe, damit du entscheiden kannst, ob sich das
    teurere Durchsuchen lohnt.
    """
    origin_clause = "" if include_extracted else "AND origin = 'curated'"
    with get_db() as conn:
        fts_results = conn.execute(
            f"""SELECT path, title, type, origin,
                      ts_headline('german', coalesce(body,''), plainto_tsquery('german', %s),
                                  'MaxWords=25,MinWords=10,StartSel=«,StopSel=»') AS snippet
               FROM memory_nodes
               WHERE to_tsvector('german', coalesce(title,'') || ' ' || coalesce(body,''))
                     @@ plainto_tsquery('german', %s) {origin_clause}
               ORDER BY ts_rank(
                   to_tsvector('german', coalesce(title,'') || ' ' || coalesce(body,'')),
                   plainto_tsquery('german', %s)
               ) DESC LIMIT 20""",
            (query, query, query),
        ).fetchall()

        if fts_results:
            results, mode = fts_results, "FTS"
        else:
            results = conn.execute(
                f"""SELECT path, title, type, origin, substr(coalesce(body,''), 1, 200) AS snippet
                   FROM memory_nodes WHERE (title ILIKE %s OR body ILIKE %s) {origin_clause} LIMIT 20""",
                (f"%{query}%", f"%{query}%"),
            ).fetchall()
            mode = "LIKE"

        # Count extra hits in the extracted tier (only relevant in default mode)
        extracted_hits = 0
        if not include_extracted:
            extracted_hits = conn.execute(
                """SELECT COUNT(*) AS c FROM memory_nodes
                   WHERE origin = 'extracted'
                     AND (to_tsvector('german', coalesce(title,'') || ' ' || coalesce(body,''))
                          @@ plainto_tsquery('german', %s)
                          OR title ILIKE %s OR body ILIKE %s)""",
                (query, f"%{query}%", f"%{query}%"),
            ).fetchone()["c"]

    scope = "alle Tiers" if include_extracted else "kuratiert"
    if not results and not extracted_hits:
        return f"Keine Memory-Ergebnisse für '{query}' ({scope})."

    lines = [f"Memory-Suchergebnisse [{mode}, {scope}] für '{query}':"]
    for r in results:
        tag = "" if r.get("origin", "curated") == "curated" else " ⟨auto-extrahiert⟩"
        lines.append(f"\n[{r['type']}] {r['path']} — {r['title']}{tag}")
        if r.get("snippet"):
            lines.append(f"  {r['snippet']}")
    if extracted_hits:
        lines.append(
            f"\n— Hinweis: {extracted_hits} weitere Treffer in auto-extrahierten Memories "
            f"(kostspieliger). Mit memory_search(query, include_extracted=True) durchsuchbar."
        )
    return "\n".join(lines)


@mcp.tool()
def memory_search_semantic(query: str, top_k: int = 8, include_extracted: bool = False) -> str:
    """Semantische Suche im Memory-Tree über Embeddings (sprachübergreifend).

    Im Gegensatz zu memory_search (lexikalisch, Stichwort-basiert) findet diese
    Suche Memories nach BEDEUTUNG — auch wenn andere Wörter oder eine andere
    Sprache verwendet werden (z.B. deutsche Anfrage findet englischen Inhalt).
    Nutze sie, wenn du ein Konzept suchst und die genauen Begriffe nicht kennst.

    Bei vielen Memories nutzt sie automatisch den pgvector-HNSW-Index (schnell);
    sonst einen numpy-Fallback. include_extracted=True bezieht die auto-extrahierten
    Memories mit ein (kostspieliger).
    """
    qvec = diary_embed.embed(query)
    if qvec is None:
        return ("Semantische Suche nicht verfügbar (Embedding-Modell konnte nicht geladen "
                "werden). Nutze stattdessen memory_search(query).")

    qlit = "[" + ",".join(repr(float(x)) for x in qvec) + "]"  # pgvector text format
    origin_clause = "" if include_extracted else "AND origin = 'curated'"
    with get_db() as conn:
        if _pgvector_ready(conn):
            rows = conn.execute(
                f"""SELECT path, title, type, origin,
                           1 - (embedding_v <=> %s::vector) AS score,
                           substr(coalesce(body,''), 1, 160) AS snippet
                    FROM memory_nodes
                    WHERE embedding_v IS NOT NULL {origin_clause}
                    ORDER BY embedding_v <=> %s::vector LIMIT %s""",
                (qlit, qlit, top_k),
            ).fetchall()
            backend = "pgvector/HNSW"
        else:
            candidates = conn.execute(
                f"SELECT path, title, type, origin, embedding, "
                f"substr(coalesce(body,''),1,160) AS snippet "
                f"FROM memory_nodes WHERE embedding IS NOT NULL {origin_clause}"
            ).fetchall()
            scored = []
            for c in candidates:
                s = diary_embed.cosine(qvec, c["embedding"])
                scored.append({**c, "score": s})
            scored.sort(key=lambda r: r["score"], reverse=True)
            rows = scored[:top_k]
            backend = "numpy"

    if not rows:
        return f"Keine semantischen Treffer für '{query}'. (Tipp: memory_reembed_all falls Embeddings fehlen.)"

    lines = [f"Semantische Suche [{backend}] für '{query}':"]
    for r in rows:
        tag = "" if r.get("origin", "curated") == "curated" else " ⟨auto-extrahiert⟩"
        lines.append(f"\n[{r['type']}] {r['path']} — {r['title']}{tag}  (Score {r['score']:.2f})")
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

    with get_db() as conn:
        cond = "WHERE embedding IS NULL" if only_missing else ""
        rows = conn.execute(
            f"SELECT path, title, body FROM memory_nodes {cond}"
        ).fetchall()
        if not rows:
            return "Nichts zu embedden — alle Memories haben bereits Embeddings."

        texts = [f"{r['title']}\n{r['body'] or ''}" for r in rows]
        vecs = diary_embed.embed_many(texts)

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


_SYNC_COLS = ("id::text, path, slug, type, title, body, tags, importance, "
              "valid_until, auto_inject, origin, embedding, created_at, updated_at")
_SYNC_INSERT = """INSERT INTO memory_nodes
       (path, slug, type, title, body, tags, importance, valid_until, auto_inject, origin, embedding, created_at, updated_at)
       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
       ON CONFLICT (path) DO UPDATE SET
           type=EXCLUDED.type, title=EXCLUDED.title, body=EXCLUDED.body,
           tags=EXCLUDED.tags, importance=EXCLUDED.importance,
           valid_until=EXCLUDED.valid_until, auto_inject=EXCLUDED.auto_inject,
           origin=EXCLUDED.origin, embedding=EXCLUDED.embedding, updated_at=EXCLUDED.updated_at"""


def _sync_row(n: dict) -> tuple:
    return (n["path"], n["slug"], n["type"], n["title"], n["body"], n["tags"],
            n["importance"], n["valid_until"], n["auto_inject"], n["origin"],
            n["embedding"], n["created_at"], n["updated_at"])


@mcp.tool()
def memory_sync() -> str:
    """Bidirektionaler Sync des Memory-Trees mit der Remote-Postgres-Instanz.

    Last-write-wins: der Node mit dem neueren updated_at gewinnt. Öffnet bei Bedarf
    selbstständig einen ephemeren SSH-Tunnel (wenn DIARY_REMOTE_SSH_HOST gesetzt ist)
    und baut ihn nach dem Sync wieder ab.

    Voraussetzung: DIARY_REMOTE_URL (DB-Endpunkt wie er auf dem Remote-Host sichtbar ist)
    und optional DIARY_REMOTE_SSH_HOST (SSH-Alias, z.B. 'dorn').
    """
    if not get_remote_url():
        return (
            "DIARY_REMOTE_URL ist nicht gesetzt.\n"
            "Beispiel: export DIARY_REMOTE_URL='postgresql://claude:pw@127.0.0.1:54320/diary_mcp'\n"
            "Für SSH-Tunnel zusätzlich: export DIARY_REMOTE_SSH_HOST='dorn'"
        )

    def depth(path: str) -> int:
        return path.count("/")

    try:
        with get_db() as local_conn:
            local_nodes = local_conn.execute(
                f"SELECT {_SYNC_COLS} FROM memory_nodes ORDER BY path"
            ).fetchall()
        local_by_path = {n["path"]: n for n in local_nodes}

        with remote_db_url() as rurl:
            remote_conn = psycopg.connect(rurl, row_factory=dict_row)
            try:
                _ensure_remote_schema(remote_conn)
                remote_nodes = remote_conn.execute(
                    f"SELECT {_SYNC_COLS} FROM memory_nodes ORDER BY path"
                ).fetchall()
                remote_by_path = {n["path"]: n for n in remote_nodes}

                # Push local → remote (parents before children)
                pushed = 0
                for path in sorted(local_by_path, key=depth):
                    local_n = local_by_path[path]
                    remote_n = remote_by_path.get(path)
                    if not remote_n or local_n["updated_at"] > remote_n["updated_at"]:
                        remote_conn.execute(_SYNC_INSERT, _sync_row(local_n))
                        pushed += 1
                remote_conn.commit()
            finally:
                remote_conn.close()

        # Pull remote → local (parents before children)
        pulled = 0
        with get_db() as local_conn:
            for path in sorted(remote_by_path, key=depth):
                remote_n = remote_by_path[path]
                local_n = local_by_path.get(path)
                if not local_n or remote_n["updated_at"] > local_n["updated_at"]:
                    local_conn.execute(_SYNC_INSERT, _sync_row(remote_n))
                    pulled += 1

        # Re-link parent_id on both sides + refresh vector index for newly pulled rows
        with get_db() as local_conn:
            local_conn.execute(_BACKFILL_PARENT_SQL)
            if _pgvector_ready(local_conn):
                local_conn.execute(
                    "UPDATE memory_nodes SET embedding_v = embedding::vector "
                    "WHERE embedding IS NOT NULL AND embedding_v IS NULL"
                )
        with remote_db_url() as rurl:
            rc = psycopg.connect(rurl)
            try:
                rc.execute(_BACKFILL_PARENT_SQL)
                rc.commit()
            finally:
                rc.close()

        tunnel_note = f" (via SSH-Tunnel {get_remote_ssh_host()})" if get_remote_ssh_host() else ""
        return f"Sync abgeschlossen{tunnel_note}. Lokal→Remote: {pushed} gepusht. Remote→Lokal: {pulled} gepullt."
    except Exception as exc:
        return f"Sync fehlgeschlagen: {exc}"


@mcp.tool()
def memory_link(from_path: str, to_path: str, rel_type: str = "related", note: str = "") -> str:
    """Erstellt eine Verknüpfung zwischen zwei Memory-Nodes (Knowledge Graph).

    rel_type: related | supports | contradicts | requires | derived_from
    """
    valid_types = {"related", "supports", "contradicts", "requires", "derived_from"}
    if rel_type not in valid_types:
        return f"Ungültiger rel_type '{rel_type}'. Erlaubt: {', '.join(sorted(valid_types))}"
    with get_db() as conn:
        from_node = conn.execute("SELECT id FROM memory_nodes WHERE path = %s", (from_path,)).fetchone()
        to_node = conn.execute("SELECT id FROM memory_nodes WHERE path = %s", (to_path,)).fetchone()
        if not from_node:
            return f"Quell-Node '{from_path}' nicht gefunden."
        if not to_node:
            return f"Ziel-Node '{to_path}' nicht gefunden."
        note_val = note.strip() or None
        conn.execute(
            """INSERT INTO memory_links (from_id, to_id, rel_type, note)
               VALUES (%s, %s, %s, %s)
               ON CONFLICT (from_id, to_id, rel_type) DO UPDATE SET note = EXCLUDED.note""",
            (from_node["id"], to_node["id"], rel_type, note_val),
        )
    return f"Link '{from_path}' --[{rel_type}]--> '{to_path}' gespeichert."


@mcp.tool()
def memory_get_links(path: str) -> str:
    """Gibt alle eingehenden und ausgehenden Verknüpfungen eines Memory-Nodes zurück."""
    with get_db() as conn:
        node = conn.execute("SELECT id, title FROM memory_nodes WHERE path = %s", (path,)).fetchone()
        if not node:
            return f"Node '{path}' nicht gefunden."
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
    lines = [f"Links für '{path}' ({node['title']}):"]
    if links_out:
        lines.append("\nAusgehend:")
        for l in links_out:
            note = f" — {l['note']}" if l.get("note") else ""
            lines.append(f"  [{l['rel_type']}] → {l['target_path']} ({l['target_title']}){note}")
    if links_in:
        lines.append("\nEingehend:")
        for l in links_in:
            lines.append(f"  [{l['rel_type']}] ← {l['source_path']} ({l['source_title']})")
    if not links_out and not links_in:
        lines.append("  (Keine Verknüpfungen)")
    return "\n".join(lines)


@mcp.tool()
def memory_health() -> str:
    """Health-Check des Memory-Trees: abgelaufene Nodes, leere Kategorien, widersprüchliche Links."""
    issues: list[str] = []
    with get_db() as conn:
        expired = conn.execute(
            "SELECT path, title, valid_until FROM memory_nodes "
            "WHERE valid_until IS NOT NULL AND valid_until < now()"
        ).fetchall()
        for e in expired:
            issues.append(f"[ABGELAUFEN] {e['path']} — {e['title']} (seit {str(e['valid_until'])[:10]})")

        orphan_cats = conn.execute(
            "SELECT m.path, m.title FROM memory_nodes m WHERE m.type = 'category' "
            "AND NOT EXISTS (SELECT 1 FROM memory_nodes c WHERE c.parent_id = m.id)"
        ).fetchall()
        for o in orphan_cats:
            issues.append(f"[LEERE KATEGORIE] {o['path']} — {o['title']}")

        empty_nodes = conn.execute(
            "SELECT n.path, n.title FROM memory_nodes n WHERE n.type != 'category' "
            "AND (n.body IS NULL OR n.body = '') "
            "AND NOT EXISTS (SELECT 1 FROM memory_nodes c WHERE c.parent_id = n.id)"
        ).fetchall()
        for n in empty_nodes:
            issues.append(f"[KEIN INHALT] {n['path']} — {n['title']}")

        contradictions = conn.execute(
            "SELECT n1.path AS p1, n2.path AS p2 FROM memory_links ml "
            "JOIN memory_nodes n1 ON ml.from_id = n1.id "
            "JOIN memory_nodes n2 ON ml.to_id = n2.id "
            "WHERE ml.rel_type = 'contradicts'"
        ).fetchall()
        for c in contradictions:
            issues.append(f"[WIDERSPRUCH] {c['p1']} ↔ {c['p2']}")

        stats = conn.execute(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN body IS NOT NULL AND body != '' THEN 1 ELSE 0 END) AS with_content, "
            "ROUND(AVG(importance)::numeric, 2) AS avg_importance FROM memory_nodes"
        ).fetchone()

    summary = [
        "=== Memory Health Check ===",
        f"Nodes gesamt: {stats['total']} | Mit Inhalt: {stats['with_content']} | ø Wichtigkeit: {stats['avg_importance']}",
        f"Issues gefunden: {len(issues)}",
    ]
    if issues:
        summary.append("\nDetails:")
        summary.extend(f"  {i}" for i in issues)
    else:
        summary.append("\n✓ Keine Issues gefunden.")
    return "\n".join(summary)


@mcp.tool()
def memory_set_importance(path: str, importance: float) -> str:
    """Setzt den Wichtigkeitsscore eines Memory-Nodes manuell (0.0–1.0)."""
    if not 0.0 <= importance <= 1.0:
        return "Fehler: importance muss zwischen 0.0 und 1.0 liegen."
    with get_db() as conn:
        result = conn.execute(
            "UPDATE memory_nodes SET importance = %s, updated_at = now() WHERE path = %s RETURNING path",
            (importance, path),
        ).fetchone()
        if not result:
            return f"Node '{path}' nicht gefunden."
    return f"Wichtigkeit von '{path}' auf {importance:.2f} gesetzt."


@mcp.tool()
def memory_set_auto_inject(path: str, enabled: bool = True) -> str:
    """Markiert ein Memory als Auto-Inject (oder hebt die Markierung auf).

    Auto-Inject-Memories unter /projects/<slug>/... werden beim Laden des Projekts
    automatisch in Claudes Kontext geladen (via memory_project_context bzw. SessionStart-Hook).
    """
    with get_db() as conn:
        result = conn.execute(
            "UPDATE memory_nodes SET auto_inject = %s, updated_at = now() WHERE path = %s RETURNING path",
            (enabled, path),
        ).fetchone()
        if not result:
            return f"Node '{path}' nicht gefunden."
    state = "aktiviert" if enabled else "deaktiviert"
    return f"Auto-Inject für '{path}' {state}."


@mcp.tool()
def memory_project_context(project_slug: str, only_auto_inject: bool = True) -> str:
    """Liefert den Memory-Kontext für ein Projekt — gedacht zum automatischen Injizieren beim Projektstart.

    project_slug:     z.B. 'eduvault4' (ohne /projects/-Präfix) — wird auf /projects/<slug>/... gematcht.
    only_auto_inject: wenn True (default), nur als auto_inject markierte Memories; sonst alle.

    Gibt die vollständigen Inhalte zurück, sodass Claude sie direkt verwenden kann.
    Globale /user- und /feedback-Auto-Inject-Memories werden immer mitgeliefert.
    """
    base = f"/projects/{project_slug.strip('/')}"
    with get_db() as conn:
        if only_auto_inject:
            rows = conn.execute(
                "SELECT path, type, title, body, importance FROM memory_nodes "
                "WHERE auto_inject AND (path = %s OR path LIKE %s "
                "  OR ((path LIKE '/user/%%' OR path LIKE '/feedback/%%'))) "
                "ORDER BY (path LIKE %s) DESC, importance DESC, path",
                (base, f"{base}/%", f"{base}%"),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT path, type, title, body, importance FROM memory_nodes "
                "WHERE (path = %s OR path LIKE %s) AND type != 'category' "
                "ORDER BY importance DESC, path",
                (base, f"{base}/%"),
            ).fetchall()

    if not rows:
        scope = "Auto-Inject-" if only_auto_inject else ""
        return f"Keine {scope}Memories für Projekt '{project_slug}' gefunden."

    lines = [f"=== Auto-Inject Memory-Kontext: {project_slug} ===",
             f"({len(rows)} Memories automatisch geladen)\n"]
    for r in rows:
        lines.append(f"--- [{r['type']}] {r['path']} — {r['title']} (Wichtigkeit {r['importance']:.1f}) ---")
        lines.append(r["body"] or "(kein Inhalt)")
        lines.append("")
    return "\n".join(lines)


def _ensure_remote_schema(conn) -> None:
    """Creates / upgrades the memory_nodes table on the remote if needed."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memory_nodes (
            id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            parent_id UUID REFERENCES memory_nodes(id) ON DELETE CASCADE,
            path      TEXT NOT NULL UNIQUE,
            slug      TEXT NOT NULL,
            type      TEXT NOT NULL DEFAULT 'note',
            title     TEXT NOT NULL,
            body      TEXT,
            tags      TEXT[] DEFAULT '{}',
            importance   REAL DEFAULT 0.5,
            access_count INTEGER DEFAULT 0,
            accessed_at  TIMESTAMPTZ,
            valid_until  TIMESTAMPTZ,
            auto_inject  BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMPTZ DEFAULT now(),
            updated_at TIMESTAMPTZ DEFAULT now()
        )
    """)
    conn.execute("ALTER TABLE memory_nodes ADD COLUMN IF NOT EXISTS importance REAL DEFAULT 0.5")
    conn.execute("ALTER TABLE memory_nodes ADD COLUMN IF NOT EXISTS valid_until TIMESTAMPTZ")
    conn.execute("ALTER TABLE memory_nodes ADD COLUMN IF NOT EXISTS auto_inject BOOLEAN DEFAULT FALSE")
    conn.execute("ALTER TABLE memory_nodes ADD COLUMN IF NOT EXISTS origin TEXT NOT NULL DEFAULT 'curated'")
    # Remove the legacy updated_at trigger if present — it corrupts last-write-wins sync.
    conn.execute("DROP TRIGGER IF EXISTS memory_nodes_updated_at ON memory_nodes")


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
