"""
Bring up the diary-web UI when a diary-mcp server starts, and open it in the
browser — only if it isn't running yet, so a new Claude session doesn't open
another tab. The web server is started detached and outlives the MCP process.

Skipped without a graphical session, over SSH (e.g. the Dorn instance) and with
DIARY_WEB_AUTOSTART=0. Port: DIARY_WEB_PORT (default 8765).
"""
from __future__ import annotations

import fcntl
import os
import shutil
import socket
import subprocess
import sys
import time
import webbrowser

import memory_injection

HOST = "127.0.0.1"


def _port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.3):
            return True
    except OSError:
        return False


def _spawn(host: str, port: int) -> None:
    exe = shutil.which("diary-web")
    cmd = [exe] if exe else [sys.executable, "-c", "import diary_web; diary_web.main()"]
    log = open(memory_injection._state_dir() / "diary-web.log", "ab")
    subprocess.Popen(cmd + ["--host", host, "--port", str(port)], stdin=subprocess.DEVNULL,
                     stdout=log, stderr=log, start_new_session=True, env=os.environ.copy())


def _enabled() -> bool:
    if os.environ.get("DIARY_WEB_AUTOSTART", "1").lower() in ("0", "false", "no", "off"):
        return False
    if os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"):
        return False
    return bool(os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY"))


def ensure_web_ui(port_open=_port_open, spawn=_spawn, open_browser=webbrowser.open,
                  wait_s: float = 8.0) -> str:
    """Returns 'disabled' | 'running' | 'started' | 'failed'."""
    if not _enabled():
        return "disabled"
    port = int(os.environ.get("DIARY_WEB_PORT", "8765"))
    # Several Claude sessions can start at once; the lock makes exactly one of
    # them spawn + open, the others then see the port open.
    lock_path = memory_injection._state_dir() / "web-autostart.lock"
    with open(lock_path, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if port_open(HOST, port):
            return "running"
        spawn(HOST, port)
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            if port_open(HOST, port):
                open_browser(f"http://{HOST}:{port}")
                return "started"
            time.sleep(0.1)
    return "failed"


def start_in_background() -> None:
    import threading

    def run():
        try:
            ensure_web_ui()
        except Exception:
            pass  # the UI is a convenience; never affect the MCP server

    threading.Thread(target=run, daemon=True, name="web-autostart").start()
