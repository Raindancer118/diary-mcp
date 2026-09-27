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
    return TestClient(diary_web.app, base_url="http://127.0.0.1:8765")


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


# ── link suggestions (v0.25.0) ─────────────────────────────────────────────

ORIGIN = {"Origin": "http://127.0.0.1:8765"}


def _suggest_pair(monkeypatch):
    """Two near-duplicates with AUTO raised above the duplicate rule → one pending suggestion."""
    import random
    import link_inference
    monkeypatch.setattr(link_inference, "AUTO_CONFIDENCE", 0.99)
    rnd = random.Random(5)
    v = [rnd.gauss(0, 1) for _ in range(384)]
    import diary_server
    for path, vec in (("/projects/a/alpha", v), ("/projects/b/beta", [x * 1.001 for x in v])):
        with patch("diary_embed.embed", return_value=vec):
            diary_server.memory_upsert(path=path, title=path.rsplit("/", 1)[1].title(),
                                       body=f"Erste Zeile von {path}.\nMehr Text.")


def test_suggestions_endpoint_lists_pending_with_both_sides(client, monkeypatch):
    _suggest_pair(monkeypatch)
    data = client.get("/api/suggestions").json()
    assert data["pending"] == 1
    s = data["items"][0]
    assert {s["a"]["path"], s["b"]["path"]} == {"/projects/a/alpha", "/projects/b/beta"}
    assert s["a"]["hook"].startswith("Erste Zeile")
    assert 0.35 <= s["confidence"] < 0.99 and s["evidence"]


def test_approve_via_web_creates_explicit_link(client, monkeypatch):
    _suggest_pair(monkeypatch)
    sid = client.get("/api/suggestions").json()["items"][0]["id"]
    r = client.post("/api/suggestions/decide", json={"ids": [sid], "decision": "approve",
                                                      "rel_type": "supports"}, headers=ORIGIN)
    assert r.status_code == 200 and r.json()["done"] == 1
    node = client.get("/api/node", params={"path": "/projects/a/alpha"}).json()
    rels = [l["rel_type"] for l in node["links_out"]] + [l["rel_type"] for l in node["links_in"]]
    assert "supports" in rels
    assert client.get("/api/suggestions").json()["pending"] == 0


def test_reject_via_web(client, monkeypatch):
    _suggest_pair(monkeypatch)
    sid = client.get("/api/suggestions").json()["items"][0]["id"]
    r = client.post("/api/suggestions/decide", json={"ids": [sid], "decision": "reject"}, headers=ORIGIN)
    assert r.json()["done"] == 1
    assert client.get("/api/suggestions").json()["pending"] == 0
    node = client.get("/api/node", params={"path": "/projects/a/alpha"}).json()
    assert not node["links_out"] and not node["links_in"]


def test_decide_rejects_bad_input(client, monkeypatch):
    _suggest_pair(monkeypatch)
    sid = client.get("/api/suggestions").json()["items"][0]["id"]
    assert client.post("/api/suggestions/decide", json={"ids": [sid], "decision": "maybe"},
                       headers=ORIGIN).status_code == 400
    assert client.post("/api/suggestions/decide", json={"ids": [sid], "decision": "approve",
                                                         "rel_type": "hates"}, headers=ORIGIN).status_code == 400


@pytest.mark.parametrize("path,body", [
    ("/api/suggestions/decide", {"ids": [], "decision": "reject"}),
    ("/api/sync", {}),
])
def test_write_endpoints_block_cross_site_requests(client, path, body):
    # another website in the same browser: foreign Origin, or a form post (no JSON)
    assert client.post(path, json=body, headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.post(path, content="ids=x", headers={**ORIGIN, "Content-Type": "application/x-www-form-urlencoded"}).status_code == 415


# ── manual auto-connect runs (v0.26.0) ─────────────────────────────────────

def test_auto_connect_preview_then_run(client):
    import diary_server
    with patch("memory_service.AUTO_LINK_THRESHOLD", 1.1):
        for path, body in (("/projects/demo/w1", "siehe /projects/demo/w2"), ("/projects/demo/w2", "Ziel")):
            with patch("diary_embed.embed", return_value=None):
                diary_server.memory_upsert(path=path, title=path, body=body)
    preview = client.post("/api/links/auto", json={"dry_run": True}, headers=ORIGIN).json()
    assert preview["auto"] >= 1 and preview["dry_run"] is True and preview["examples"]
    assert not client.get("/api/node", params={"path": "/projects/demo/w1"}).json()["links_out"]
    done = client.post("/api/links/auto", json={"dry_run": False}, headers=ORIGIN).json()
    assert done["auto"] >= 1 and done["dry_run"] is False
    assert client.get("/api/node", params={"path": "/projects/demo/w1"}).json()["links_out"]
    info = client.get("/api/links/auto").json()
    assert info["last_run"]["trigger"] == "web"
    assert info["thresholds"]["auto"] > info["thresholds"]["suggest"]


def test_auto_connect_is_csrf_protected(client):
    assert client.post("/api/links/auto", json={"dry_run": True},
                       headers={"Origin": "https://evil.example"}).status_code == 403


def test_auto_connect_busy_is_409(client):
    import link_inference
    with patch("link_inference.run", return_value={"busy": True}):
        assert client.post("/api/links/auto", json={"dry_run": False}, headers=ORIGIN).status_code == 409


def test_graph_edges_carry_creation_time(client):
    import diary_server
    _upsert("/projects/demo/e1")
    _upsert("/projects/demo/e2")
    diary_server.memory_link("/projects/demo/e1", "/projects/demo/e2", "supports")
    edges = client.get("/api/graph").json()["edges"]
    e = next(x for x in edges if x["rel_type"] == "supports")
    assert e["created_at"] and e["created_at"][:4].isdigit()
