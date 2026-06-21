"""
DiaryDBHandler — Python logging handler that writes records directly into the diary Postgres DB.
Use this to hook backend apps into the diary without going through HTTP.
"""
import logging
from datetime import datetime

import psycopg
from psycopg.rows import dict_row

from diary_db import get_database_url


class DiaryDBHandler(logging.Handler):
    def __init__(self, project_name: str):
        super().__init__()
        self.project_name = project_name
        self.project_id: int | None = None
        self._connect()

    def _connect(self) -> None:
        try:
            with psycopg.connect(get_database_url(), row_factory=dict_row) as conn:
                row = conn.execute(
                    "SELECT id FROM projects WHERE name = %s", (self.project_name,)
                ).fetchone()
                if row:
                    self.project_id = row["id"]
                else:
                    row = conn.execute(
                        "INSERT INTO projects (name, status) VALUES (%s, %s) RETURNING id",
                        (self.project_name, "Auto-created by DiaryDBHandler"),
                    ).fetchone()
                    self.project_id = row["id"]
                    conn.execute(
                        "INSERT INTO logs (project_id, author, entry, level, worker) "
                        "VALUES (%s, %s, %s, %s, %s)",
                        (self.project_id, "System", "Projekt automatisch durch Logger erstellt.", "INFO", "System"),
                    )
                    conn.commit()
        except Exception as exc:
            print(f"DiaryDBHandler: failed to connect: {exc}")

    def emit(self, record: logging.LogRecord) -> None:
        if not self.project_id:
            return
        try:
            msg = self.format(record)
            with psycopg.connect(get_database_url(), row_factory=dict_row) as conn:
                conn.execute(
                    "INSERT INTO logs (project_id, author, entry, level, worker) VALUES (%s, %s, %s, %s, %s)",
                    (self.project_id, f"AppLogger [{record.levelname}]", msg, record.levelname, record.name),
                )
                if record.levelno >= logging.ERROR:
                    conn.execute(
                        "INSERT INTO errors_solutions (project_id, error_msg, solution_msg) VALUES (%s, %s, %s)",
                        (self.project_id, msg, "TODO: Lösung fehlt noch."),
                    )
                conn.commit()
        except Exception:
            self.handleError(record)


def get_diary_logger(
    project_name: str,
    level: int = logging.INFO,
) -> logging.Logger:
    """Return a logger that writes records directly into the diary Postgres database."""
    logger = logging.getLogger(f"DiaryLogger_{project_name}")
    logger.setLevel(level)
    if not logger.handlers:
        handler = DiaryDBHandler(project_name)
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
    return logger
