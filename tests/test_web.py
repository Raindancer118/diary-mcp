"""diary-web (v0.23 „Observatorium"): static assets, activity series, API smoke tests."""
from __future__ import annotations

import datetime as dt
import tomllib
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent


def _upsert(path, title="T", body="B", importance=0.5):
    import diary_server
    with patch("diary_embed.embed", return_value=None), \
         patch("diary_embed.embed_many", side_effect=lambda ts: [None] * len(ts)):
        return diary_server.memory_upsert(path=path, title=title, body=body, importance=importance)


def _backdate(path, created_days_ago, updated_days_ago=None):
    import diary_db
    with diary_db.get_db() as conn:
        conn.execute(
            "UPDATE memory_nodes SET created_at = now() - make_interval(days => %s), "
            "updated_at = now() - make_interval(days => %s) WHERE path = %s",
            (created_days_ago, created_days_ago if updated_days_ago is None else updated_days_ago, path),
        )


@pytest.fixture
def client():
    import importlib
    import diary_web
    importlib.reload(diary_web)
    return TestClient(diary_web.app)


# ── shell + assets ──────────────────────────────────────────────────────────

def test_index_serves_shell_referencing_assets(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "/assets/app.css" in r.text and "/assets/app.js" in r.text


@pytest.mark.parametrize("name,ctype", [
    ("app.css", "text/css"),
    ("app.js", "javascript"),
    ("fonts/fraunces.woff2", "font/woff2"),
    ("fonts/instrument-sans.woff2", "font/woff2"),
    ("fonts/jetbrains-mono.woff2", "font/woff2"),
])
def test_assets_are_served(client, name, ctype):
    r = client.get(f"/assets/{name}")
    assert r.status_code == 200, name
    assert ctype in r.headers["content-type"]
    assert len(r.content) > 500


def test_assets_do_not_escape_their_directory(client):
    assert client.get("/assets/../diary_web.py").status_code == 404
    assert client.get("/assets/%2e%2e/diary_web.py").status_code == 404


def test_shell_loads_nothing_from_the_internet(client):
    html = client.get("/").text
    css = client.get("/assets/app.css").text
    js = client.get("/assets/app.js").text
    for text in (html, css, js):
        text = text.replace("http://www.w3.org/2000/svg", "")  # XML namespace, not a fetch
        assert "http://" not in text and "https://" not in text


def test_security_headers(client):
    r = client.get("/")
    csp = r.headers.get("content-security-policy", "")
    assert "default-src 'self'" in csp
    assert r.headers.get("x-content-type-options") == "nosniff"
    assert r.headers.get("x-frame-options") == "DENY"


def test_assets_are_packaged():
    cfg = tomllib.loads((ROOT / "pyproject.toml").read_text())
    st = cfg["tool"]["setuptools"]
    assert "diary_web_assets" in st["packages"]
    patterns = st["package-data"]["diary_web_assets"]
    assert {"*.html", "*.css", "*.js", "fonts/*.woff2", "fonts/*.txt"} <= set(patterns)
    assert (ROOT / "diary_web_assets" / "__init__.py").exists()


# ── activity series ────────────────────────────────────────────────────────

def test_activity_series_counts_created_and_later_updates_per_day(client):
    _upsert("/projects/demo/a")
    _upsert("/projects/demo/b")
    _upsert("/projects/demo/c")
    _backdate("/projects/demo/a", 3)            # created 3 days ago, untouched since
    _backdate("/projects/demo/b", 10, 2)        # created 10 days ago, updated 2 days ago
    # c: created today

    data = client.get("/api/activity?days=14").json()
    series = data["series"]
    assert len(series) == 14
    dates = [d["date"] for d in series]
    assert dates == sorted(dates)
    assert dates[-1] == dt.date.today().isoformat()

    by_date = {d["date"]: d for d in series}
    day = lambda n: (dt.date.today() - dt.timedelta(days=n)).isoformat()
    assert by_date[day(0)]["created"] >= 1        # c (+ seeded categories may exist)
    assert by_date[day(3)]["created"] == 1
    assert by_date[day(10)]["created"] == 1
    assert by_date[day(2)]["updated"] == 1        # b's later edit
    assert by_date[day(3)]["updated"] == 0        # same-day creation is not an update
    assert data["totals"]["created"] == sum(d["created"] for d in series)


def test_activity_ignores_deleted_and_extracted(client):
    import diary_db
    _upsert("/projects/demo/gone")
    _upsert("/projects/demo/auto")
    _backdate("/projects/demo/gone", 5)
    _backdate("/projects/demo/auto", 5)
    with diary_db.get_db() as conn:
        conn.execute("UPDATE memory_nodes SET deleted_at = now() WHERE path = '/projects/demo/gone'")
        conn.execute("UPDATE memory_nodes SET origin = 'extracted' WHERE path = '/projects/demo/auto'")
    day5 = (dt.date.today() - dt.timedelta(days=5)).isoformat()
    series = {d["date"]: d for d in client.get("/api/activity?days=7").json()["series"]}
    assert series[day5]["created"] == 0


def test_activity_days_are_clamped(client):
    assert len(client.get("/api/activity?days=0").json()["series"]) == 1
    assert len(client.get("/api/activity?days=99999").json()["series"]) == 400


# ── existing endpoints keep working ────────────────────────────────────────

def test_tree_node_search_roundtrip(client):
    _upsert("/projects/demo/rsync", "Deployment per rsync", "Immer --exclude .env setzen.")
    tree = client.get("/api/tree").json()
    assert any(n["path"] == "/projects/demo/rsync" for n in tree)
    node = client.get("/api/node", params={"path": "/projects/demo/rsync"}).json()
    assert node["title"] == "Deployment per rsync"
    assert node["links_out"] == [] and node["links_in"] == []
    hits = client.get("/api/search", params={"q": "rsync"}).json()
    assert hits and hits[0]["path"] == "/projects/demo/rsync"
    assert client.get("/api/node", params={"path": "/nope"}).status_code == 404


def test_stats_and_health_endpoints(client):
    _upsert("/projects/demo/x")
    st = client.get("/api/stats?days=7").json()
    assert {"instance", "corpus", "quality", "graph", "journal", "injection"} <= set(st)
    h = client.get("/api/health").json()
    assert "issues" in h and "stats" in h
    g = client.get("/api/graph").json()
    assert "nodes" in g and "edges" in g
