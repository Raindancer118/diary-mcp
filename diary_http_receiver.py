"""
Diary HTTP Queue Server — runs on remote hosts.

Apps push log entries via POST /log.
The local MCP server polls via GET /sync and drains the queue atomically.
"""

import json
import logging
import sqlite3
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from diary_config import DATA_DIR

QUEUE_DB = DATA_DIR / "diary_queue.db"

_log = logging.getLogger(__name__)


def _get_queue_db() -> sqlite3.Connection:
    conn = sqlite3.connect(QUEUE_DB, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS queue (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            project   TEXT    NOT NULL,
            level     TEXT    NOT NULL DEFAULT 'INFO',
            worker    TEXT    NOT NULL DEFAULT 'App',
            message   TEXT    NOT NULL,
            timestamp TEXT    NOT NULL
        )
    """)
    conn.commit()
    return conn


class DiaryQueueHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        if self.path != "/log":
            self._send(404, {"error": "Not found"})
            return

        content_length = int(self.headers.get("Content-Length", 0))
        if content_length == 0:
            self._send(400, {"error": "Empty body"})
            return

        try:
            data = json.loads(self.rfile.read(content_length))
        except (json.JSONDecodeError, ValueError) as exc:
            self._send(400, {"error": f"Invalid JSON: {exc}"})
            return

        project = data.get("project", "").strip()
        message = data.get("message", "").strip()
        if not project or not message:
            self._send(400, {"error": "Missing required fields: project, message"})
            return

        level = data.get("level", "INFO").upper()
        worker = data.get("worker", "App")
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        try:
            conn = _get_queue_db()
            conn.execute(
                "INSERT INTO queue (project, level, worker, message, timestamp) VALUES (?, ?, ?, ?, ?)",
                (project, level, worker, message, timestamp),
            )
            conn.commit()
            conn.close()
            self._send(200, {"status": "queued"})
        except sqlite3.Error as exc:
            _log.error("Queue insert failed: %s", exc)
            self._send(500, {"error": str(exc)})

    def do_GET(self) -> None:
        if self.path != "/sync":
            self._send(404, {"error": "Not found"})
            return

        try:
            conn = _get_queue_db()
            rows = conn.execute("SELECT * FROM queue ORDER BY id").fetchall()
            result = [dict(r) for r in rows]
            conn.execute("DELETE FROM queue")
            conn.commit()
            conn.close()
            self._send(200, result)
        except sqlite3.Error as exc:
            _log.error("Queue drain failed: %s", exc)
            self._send(500, {"error": str(exc)})

    def _send(self, status: int, body) -> None:
        payload = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args) -> None:
        _log.debug(format, *args)


def run(port: int = 8111) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    _get_queue_db()
    server = HTTPServer(("", port), DiaryQueueHandler)
    _log.info("Diary Remote Queue listening on port %d", port)
    _log.info("Push logs via: POST http://localhost:%d/log", port)
    _log.info("MCP syncs via: GET  http://localhost:%d/sync", port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        _log.info("Shutting down.")


if __name__ == "__main__":
    run()
