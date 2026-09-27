"""Web UI autostart (v0.22.0): a starting diary-mcp server brings up diary-web
and opens it — once, not per Claude session."""
from __future__ import annotations

import pytest

import diary_web_autostart as wa


class Fake:
    def __init__(self, port_open_after_spawn=True, already_open=False):
        self.open = already_open
        self.port_open_after_spawn = port_open_after_spawn
        self.spawned = 0
        self.opened: list[str] = []

    def port_open(self, host, port):
        return self.open

    def spawn(self, host, port):
        self.spawned += 1
        if self.port_open_after_spawn:
            self.open = True

    def browser(self, url):
        self.opened.append(url)


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("DIARY_HOOK_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.delenv("DIARY_WEB_AUTOSTART", raising=False)
    monkeypatch.delenv("SSH_CONNECTION", raising=False)


def _run(f, **kw):
    return wa.ensure_web_ui(port_open=f.port_open, spawn=f.spawn, open_browser=f.browser,
                            wait_s=0.2, **kw)


def test_starts_and_opens_when_not_running():
    f = Fake()
    assert _run(f) == "started"
    assert f.spawned == 1 and f.opened == ["http://127.0.0.1:8765"]


def test_does_nothing_when_already_running():
    f = Fake(already_open=True)
    assert _run(f) == "running"
    assert f.spawned == 0 and f.opened == []


def test_no_browser_if_server_never_comes_up():
    f = Fake(port_open_after_spawn=False)
    assert _run(f) == "failed"
    assert f.opened == []


@pytest.mark.parametrize("var,value", [("DIARY_WEB_AUTOSTART", "0"), ("SSH_CONNECTION", "1.2.3.4 1 5.6.7.8 22")])
def test_disabled_by_env_or_remote(monkeypatch, var, value):
    monkeypatch.setenv(var, value)
    f = Fake()
    assert _run(f) == "disabled"
    assert f.spawned == 0


def test_headless_is_disabled(monkeypatch):
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.delenv("DISPLAY", raising=False)
    f = Fake()
    assert _run(f) == "disabled"


def test_custom_port(monkeypatch):
    monkeypatch.setenv("DIARY_WEB_PORT", "9911")
    f = Fake()
    _run(f)
    assert f.opened == ["http://127.0.0.1:9911"]


def test_port_probe_against_real_socket():
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen()
        port = s.getsockname()[1]
        assert wa._port_open("127.0.0.1", port)
    assert not wa._port_open("127.0.0.1", port)
