"""
Local IPC for sharing one loaded embedding model across multiple diary-mcp
processes.

Problem: every diary-mcp process (one per running Claude Code session) loads
its own copy of the fastembed ONNX model (~470MB) on first use. With several
sessions open at once, that's N copies of the same model in RAM for no
reason — they're all embedding into the identical vector space.

Fix: processes race to bind a well-known Unix domain socket. Whoever wins
becomes the embedding server for the machine (loads the model once, serves
embed requests from everyone else). Everyone who loses connects as a client
instead of loading their own copy. If the server process exits, the socket
becomes stale; the next process to need it detects that and takes over —
no supervisor, no separate daemon to manage, self-healing by construction.

Wire protocol: one request per connection, 8-byte big-endian length prefix
followed by a JSON payload, both ways. Simple and sufficient at this scale
(the payloads are a handful of memory texts, not a high-throughput stream).
"""
from __future__ import annotations

import json
import logging
import os
import socket
import tempfile
import threading
from pathlib import Path

_log = logging.getLogger(__name__)


def sock_path() -> Path:
    override = os.environ.get("DIARY_EMBED_SOCK")
    if override:
        return Path(override)
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        # Per-user, mode 0700 by systemd convention — already private.
        base = Path(xdg)
    else:
        # tempfile.gettempdir() (/tmp) is world-writable — a plain socket
        # there lets another local user pre-create the path, squat on the
        # name, or read memory content sent as embed requests. Use a
        # dedicated per-uid subdirectory instead so only we can create or
        # see files in it.
        base = Path(tempfile.gettempdir()) / f"diary-mcp-{os.getuid()}"
    _ensure_private_dir(base)
    return base / "diary-mcp-embed.sock"


def _ensure_private_dir(path: Path) -> None:
    """Create path as a directory only this user can read/write/enter, and
    refuse to trust it if it already exists owned by someone else."""
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    st = path.stat()
    if st.st_uid != os.getuid():
        raise PermissionError(f"refusing to use {path}: owned by uid {st.st_uid}, not us")
    os.chmod(path, 0o700)


def _send_message(conn: socket.socket, payload: bytes) -> None:
    conn.sendall(len(payload).to_bytes(8, "big") + payload)


def _recv_exact(conn: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("peer closed connection")
        buf += chunk
    return buf


def _recv_message(conn: socket.socket) -> bytes:
    length = int.from_bytes(_recv_exact(conn, 8), "big")
    return _recv_exact(conn, length)


def _owned_by_us(path: Path) -> bool:
    """False if the socket file exists but belongs to another local user —
    never connect to or trust such a socket (it could be an impersonation
    attempt reading memory content sent as embed requests)."""
    try:
        return path.stat().st_uid == os.getuid()
    except FileNotFoundError:
        return False


def _is_alive(path: Path) -> bool:
    """True if something is actually listening on path (vs. a stale socket
    file left behind by a process that died without cleaning up)."""
    if not _owned_by_us(path):
        return False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            s.connect(str(path))
            return True
    except OSError:
        return False


class EmbedServer:
    """Owns the shared socket and serves embed requests using compute_fn."""

    def __init__(self, compute_fn):
        self._compute_fn = compute_fn
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._path: Path | None = None

    def try_start(self) -> bool:
        """Attempt to become the embedding server. Returns False if another
        live process already owns the socket (caller should become a client
        instead)."""
        path = sock_path()
        path.parent.mkdir(parents=True, exist_ok=True)

        # A pre-existing socket file we don't own is never ours to reclaim —
        # could be another local user's file (impersonation/hijack attempt).
        # Refuse outright rather than trying to unlink or bind over it.
        if path.exists() and not _owned_by_us(path):
            _log.warning(
                "Refusing embed IPC socket at %s: owned by another user", path
            )
            return False

        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            old_umask = os.umask(0o177)  # belt-and-suspenders: bind creates the file 0600
            try:
                sock.bind(str(path))
            finally:
                os.umask(old_umask)
        except OSError:
            if _is_alive(path):
                sock.close()
                return False
            # Stale socket file from a process that died uncleanly — reclaim it.
            try:
                path.unlink()
            except (FileNotFoundError, PermissionError):
                pass
            try:
                old_umask = os.umask(0o177)
                try:
                    sock.bind(str(path))
                finally:
                    os.umask(old_umask)
            except OSError as exc:
                _log.warning("Could not bind embed IPC socket at %s: %s", path, exc)
                sock.close()
                return False

        os.chmod(path, 0o600)
        sock.listen(32)
        self._sock = sock
        self._path = path
        self._thread = threading.Thread(
            target=self._accept_loop, daemon=True, name="embed-ipc-server"
        )
        self._thread.start()
        return True

    def _accept_loop(self) -> None:
        while True:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return  # socket closed (stop() called)
            threading.Thread(
                target=self._handle, args=(conn,), daemon=True
            ).start()

    def _handle(self, conn: socket.socket) -> None:
        with conn:
            try:
                data = _recv_message(conn)
            except ConnectionError:
                # Connect-then-disconnect with no data — this is _is_alive()'s
                # liveness probe (or a client that vanished before sending
                # anything), not a real request. Not worth logging.
                return
            try:
                req = json.loads(data)
                texts = req.get("texts", [])
                vectors = self._compute_fn(texts)
                _send_message(conn, json.dumps({"vectors": vectors}).encode())
            except Exception as exc:  # noqa: BLE001 — never let a bad request kill the server
                _log.warning("embed IPC request failed: %s", exc)
                try:
                    _send_message(conn, json.dumps({"error": str(exc)}).encode())
                except OSError:
                    pass

    def stop(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        if self._path is not None:
            try:
                self._path.unlink()
            except FileNotFoundError:
                pass


def request_remote(texts: list[str], timeout: float = 30.0) -> list | None:
    """Ask the shared embedding server (if any) to embed texts. Returns None
    on any failure — caller should fall back to loading its own model."""
    path = sock_path()
    if path.exists() and not _owned_by_us(path):
        # Never send memory content to a socket owned by another local user.
        _log.warning("Refusing to use embed IPC socket at %s: owned by another user", path)
        return None
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(str(path))
            _send_message(s, json.dumps({"texts": texts}).encode())
            resp = json.loads(_recv_message(s))
            if "error" in resp:
                _log.warning("embed IPC server reported error: %s", resp["error"])
                return None
            return resp.get("vectors")
    except Exception as exc:  # noqa: BLE001
        _log.debug("embed IPC client request failed, falling back to local: %s", exc)
        return None
