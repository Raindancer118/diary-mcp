"""
Tests for the shared-embedding IPC layer (diary_embed_ipc.py) and its
integration into diary_embed.py.

Goal: when multiple diary-mcp processes run on the same machine, only ONE of
them should load the ~470MB fastembed model into RAM. The first process to
start elects itself as the embedding server over a Unix domain socket; every
other process becomes a client and routes embed calls through it.

These tests never load the real ONNX model (slow, heavy) — they inject a fake
compute function / fake model instead, isolated per-test via a unique socket
path so tests don't collide with each other or with a real running daemon.
"""
from __future__ import annotations

import importlib
import os
import socket
import tempfile
import threading
import time
import uuid
from pathlib import Path

import pytest

import diary_embed_ipc as ipc


def _unique_sock_path() -> Path:
    return Path(tempfile.gettempdir()) / f"diary-mcp-embed-test-{uuid.uuid4().hex}.sock"


@pytest.fixture
def sock_path(monkeypatch):
    path = _unique_sock_path()
    monkeypatch.setenv("DIARY_EMBED_SOCK", str(path))
    yield path
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def test_first_process_becomes_server(sock_path):
    server = ipc.EmbedServer(lambda texts: [[1.0, 2.0] for _ in texts])
    assert server.try_start() is True
    server.stop()


def test_second_process_becomes_client(sock_path):
    server1 = ipc.EmbedServer(lambda texts: [[1.0] for _ in texts])
    assert server1.try_start() is True
    server2 = ipc.EmbedServer(lambda texts: [[2.0] for _ in texts])
    assert server2.try_start() is False  # socket already owned by server1
    server1.stop()


def test_client_roundtrip_through_server(sock_path):
    def compute(texts):
        return [[float(len(t))] for t in texts]

    server = ipc.EmbedServer(compute)
    assert server.try_start() is True
    try:
        vectors = ipc.request_remote(["hi", "hello there"], timeout=5.0)
        assert vectors == [[2.0], [11.0]]
    finally:
        server.stop()


def test_socket_created_with_restrictive_permissions(sock_path):
    server = ipc.EmbedServer(lambda texts: [[1.0] for _ in texts])
    assert server.try_start() is True
    try:
        mode = sock_path.stat().st_mode & 0o777
        assert mode == 0o600
    finally:
        server.stop()


def test_default_sock_path_uses_private_per_uid_dir(monkeypatch):
    monkeypatch.delenv("DIARY_EMBED_SOCK", raising=False)
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    path = ipc.sock_path()
    assert f"-{os.getuid()}" in path.parent.name
    assert (path.parent.stat().st_mode & 0o777) == 0o700
    assert path.parent.stat().st_uid == os.getuid()


def test_refuses_foreign_owned_socket_file(sock_path, monkeypatch):
    # Simulate a socket file that exists but isn't ours by making
    # _owned_by_us report False, regardless of the real filesystem owner
    # (we can't actually chown to another uid without root in a test).
    real_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    real_sock.bind(str(sock_path))
    real_sock.listen(1)
    try:
        monkeypatch.setattr(ipc, "_owned_by_us", lambda p: False)
        server = ipc.EmbedServer(lambda texts: [[1.0] for _ in texts])
        assert server.try_start() is False  # must not reclaim/bind over it

        vectors = ipc.request_remote(["x"], timeout=1.0)
        assert vectors is None  # must not send data to it either
    finally:
        real_sock.close()


def test_liveness_probe_does_not_disrupt_server(sock_path, caplog):
    # _is_alive() connects and disconnects without sending anything (that's
    # exactly what election does when it finds an existing socket file).
    # This must not be logged as a request failure, and the server must stay
    # usable for real requests afterwards.
    server = ipc.EmbedServer(lambda texts: [[5.0] for _ in texts])
    assert server.try_start() is True
    try:
        assert ipc._is_alive(sock_path) is True
        assert ipc.request_remote(["still works"], timeout=5.0) == [[5.0]]
        assert "embed IPC request failed" not in caplog.text
    finally:
        server.stop()


def test_client_falls_back_when_no_server(sock_path):
    # No server ever started on this socket path.
    vectors = ipc.request_remote(["anything"], timeout=1.0)
    assert vectors is None


def test_stale_socket_is_reclaimed(sock_path):
    # Simulate a crashed prior server: a socket file exists but nothing is
    # listening on it. A new server must detect this and take over rather
    # than permanently ceding the role.
    dead = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    dead.bind(str(sock_path))
    dead.close()  # file remains on disk, but no listener behind it
    assert sock_path.exists()

    server = ipc.EmbedServer(lambda texts: [[9.0] for _ in texts])
    assert server.try_start() is True
    try:
        vectors = ipc.request_remote(["x"], timeout=5.0)
        assert vectors == [[9.0]]
    finally:
        server.stop()


def test_concurrent_requests_are_handled(sock_path):
    def compute(texts):
        time.sleep(0.05)
        return [[float(i)] for i in range(len(texts))]

    server = ipc.EmbedServer(compute)
    assert server.try_start() is True
    try:
        results = {}

        def worker(idx):
            results[idx] = ipc.request_remote(["a", "b", "c"], timeout=5.0)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        assert len(results) == 5
        for r in results.values():
            assert r == [[0.0], [1.0], [2.0]]
    finally:
        server.stop()


class TestDiaryEmbedIntegration:
    """diary_embed.py should route through the IPC layer transparently."""

    @pytest.fixture(autouse=True)
    def _reload_module(self, sock_path, monkeypatch):
        # Force a clean module state per test: role election and model
        # loading are cached at module scope in diary_embed.py.
        monkeypatch.delenv("DIARY_EMBED_NO_IPC", raising=False)
        global diary_embed
        import diary_embed as _diary_embed
        importlib.reload(_diary_embed)
        diary_embed = _diary_embed
        yield
        importlib.reload(diary_embed)  # leave a clean module for the next test

    def test_server_role_computes_locally(self, monkeypatch):
        monkeypatch.setattr(
            diary_embed, "_compute_local", lambda texts: [[1.0] for _ in texts]
        )
        vectors = diary_embed.embed_many(["a", "b"])
        assert vectors == [[1.0], [1.0]]
        assert diary_embed._role == "server"

    def test_client_role_uses_remote_server(self, monkeypatch, sock_path):
        # A separate "process" (here: a raw EmbedServer) owns the socket, so
        # diary_embed must elect itself as client.
        remote = ipc.EmbedServer(lambda texts: [[42.0] for _ in texts])
        assert remote.try_start() is True
        try:
            vectors = diary_embed.embed_many(["x", "y"])
            assert vectors == [[42.0], [42.0]]
            assert diary_embed._role == "client"
        finally:
            remote.stop()

    def test_client_falls_back_to_local_if_daemon_dies(self, monkeypatch, sock_path):
        remote = ipc.EmbedServer(lambda texts: [[1.0] for _ in texts])
        assert remote.try_start() is True
        # Force client role, then kill the daemon before the real call.
        diary_embed._ensure_role()
        assert diary_embed._role == "client"
        remote.stop()

        monkeypatch.setattr(
            diary_embed, "_compute_local", lambda texts: [[7.0] for _ in texts]
        )
        vectors = diary_embed.embed_many(["z"])
        assert vectors == [[7.0]]

    def test_no_ipc_env_var_skips_election(self, monkeypatch, sock_path):
        monkeypatch.setenv("DIARY_EMBED_NO_IPC", "1")
        monkeypatch.setattr(
            diary_embed, "_compute_local", lambda texts: [[3.0] for _ in texts]
        )
        vectors = diary_embed.embed_many(["a"])
        assert vectors == [[3.0]]
        assert diary_embed._role == "standalone"

    def test_warmup_does_not_load_model_when_client(self, monkeypatch, sock_path):
        remote = ipc.EmbedServer(lambda texts: [[1.0] for _ in texts])
        assert remote.try_start() is True
        try:
            load_calls = []
            monkeypatch.setattr(
                diary_embed, "_get_model", lambda: load_calls.append(1) or None
            )
            diary_embed.warmup()
            assert diary_embed._role == "client"
            assert load_calls == []  # client must never load its own copy
        finally:
            remote.stop()

    def test_warmup_loads_model_when_server(self, monkeypatch, sock_path):
        load_calls = []
        monkeypatch.setattr(
            diary_embed, "_get_model", lambda: load_calls.append(1) or None
        )
        diary_embed.warmup()
        assert diary_embed._role == "server"
        assert load_calls == [1]
