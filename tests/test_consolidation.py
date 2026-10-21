"""Reviewable consolidation ("dreaming", v0.29.0): a nightly pass proposes
merges, supersessions, deletions of long-expired memories, absolute dates and
archiving of stale ones — and changes nothing until Tom decides. Mirrors
Anthropic's Managed-Agents dreaming (input untouched, output reviewed), minus
Auto Dream's silent rewrites."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest


def _upsert(path, title="Title", body="Body", importance=0.5, embedding=None, valid_until=""):
    import diary_server
    with patch("diary_embed.embed", return_value=embedding), \
         patch("diary_embed.embed_many", side_effect=lambda ts: [embedding] * len(ts)):
        return diary_server.memory_upsert(path=path, title=title, body=body,
                                          importance=importance, valid_until=valid_until)


def _conn():
    import diary_db
    return diary_db.get_db()


def _node(path):
    with _conn() as conn:
        return conn.execute("SELECT * FROM memory_nodes WHERE path = %s", (path,)).fetchone()


def _age(path, days):
    with _conn() as conn:
        conn.execute("UPDATE memory_nodes SET created_at = created_at - %s::interval, "
                     "updated_at = updated_at - %s::interval, accessed_at = NULL WHERE path = %s",
                     (f"{days} days", f"{days} days", path))


V = [1.0] + [0.0] * 383
V_NEAR = [0.999, 0.0447] + [0.0] * 382   # cosine ≈ 0.999
V_OTHER = [0.0, 1.0] + [0.0] * 382


def _proposals(kind=None):
    import consolidation
    items = consolidation.list_pending(limit=100)[1]
    return [p for p in items if kind is None or p["kind"] == kind]


def test_duplicate_pair_is_proposed_with_newer_or_more_important_kept():
    import consolidation
    _upsert("/projects/demo/deploy-a", "Deploy auf Dorn", "rsync mit exclude .env", 0.6, V)
    _upsert("/projects/demo/deploy-b", "Deploy auf Dorn (Kopie)", "rsync mit exclude .env, kurz", 0.9, V_NEAR)
    _upsert("/projects/demo/other", "Anderes Thema", "nichts", 0.5, V_OTHER)
    counts = consolidation.propose()
    assert counts["duplicate"] == 1
    [p] = _proposals("duplicate")
    assert p["detail"]["keep"] == "/projects/demo/deploy-b"
    assert p["detail"]["drop"] == "/projects/demo/deploy-a"
    assert p["detail"]["similarity"] > 0.99


def test_propose_is_idempotent_and_rejections_stick():
    import consolidation
    _upsert("/projects/demo/a", "A", "x", 0.5, V)
    _upsert("/projects/demo/b", "B", "x", 0.5, V_NEAR)
    consolidation.propose()
    assert consolidation.propose()["duplicate"] == 0
    [p] = _proposals("duplicate")
    assert consolidation.decide([p["id"]], "reject") == (1, 0)
    consolidation.propose()
    assert _proposals("duplicate") == []


def test_long_expired_memory_is_proposed_for_deletion():
    import consolidation
    past = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    _upsert("/projects/demo/old-status", "Status alt", "veraltet", 0.5, V_OTHER, valid_until=past)
    consolidation.propose()
    [p] = _proposals("expired")
    assert p["paths"] == ["/projects/demo/old-status"]


def test_relative_dates_proposed_only_with_reliable_anchor():
    import consolidation
    _upsert("/projects/demo/log", "Log", "Gestern deployed, heute getestet. `heute` im Code bleibt.", 0.5, V_OTHER)
    _age("/projects/demo/log", 3)
    _upsert("/projects/demo/edited", "Edited", "heute passiert", 0.5, [0.0, 0.0, 1.0] + [0.0] * 381)
    with _conn() as conn:  # created long ago, edited now → no reliable anchor
        conn.execute("UPDATE memory_nodes SET created_at = now() - interval '40 days' WHERE path = %s",
                     ("/projects/demo/edited",))
    consolidation.propose()
    props = _proposals("relative_date")
    assert [p["paths"] for p in props] == [["/projects/demo/log"]]
    anchor = _node("/projects/demo/log")["created_at"].astimezone(consolidation.TZ).date()
    reps = props[0]["detail"]["replacements"]
    assert ["Gestern", (anchor - timedelta(days=1)).isoformat()] in reps
    assert ["heute", anchor.isoformat()] in reps
    assert len(reps) == 2  # the inline-code `heute` is left alone


def test_stale_low_importance_memory_is_proposed_for_archiving():
    import consolidation
    _upsert("/projects/demo/trivia", "Trivia", "unwichtig", 0.3, V_OTHER)
    _age("/projects/demo/trivia", 400)
    consolidation.propose()
    [p] = _proposals("stale")
    assert p["paths"] == ["/projects/demo/trivia"]


def test_apply_merge_folds_and_tombstones():
    import consolidation
    _upsert("/projects/demo/a", "A", "alpha", 0.6, V)
    _upsert("/projects/demo/b", "B", "beta", 0.9, V_NEAR)
    consolidation.propose()
    [p] = _proposals("duplicate")
    with patch("diary_embed.embed", return_value=V):
        assert consolidation.decide([p["id"]], "merge") == (1, 0)
    assert _node("/projects/demo/a")["deleted_at"] is not None
    assert "alpha" in _node("/projects/demo/b")["body"]
    done = consolidation.history(limit=5)
    assert done[0]["status"] == "applied" and "merge" in done[0]["result"]


def test_apply_supersede_expires_older_and_links():
    import consolidation
    import graph_core
    _upsert("/projects/demo/a", "Stand 1", "alt", 0.6, V)
    _upsert("/projects/demo/b", "Stand 2", "neu", 0.9, V_NEAR)
    consolidation.propose()
    [p] = _proposals("duplicate")
    assert consolidation.decide([p["id"]], "supersede") == (1, 0)
    old = _node("/projects/demo/a")
    assert old["deleted_at"] is None and old["valid_until"] is not None
    assert old["valid_until"] <= datetime.now(timezone.utc)
    links = graph_core.memory_get_links("/projects/demo/b")
    assert "supersedes" in links and "/projects/demo/a" in links


def test_apply_relative_date_rewrites_body():
    import consolidation
    _upsert("/projects/demo/log", "Log", "Gestern deployed.", 0.5, V_OTHER)
    _age("/projects/demo/log", 2)
    consolidation.propose()
    [p] = _proposals("relative_date")
    with patch("diary_embed.embed", return_value=V_OTHER):
        assert consolidation.decide([p["id"]], "apply") == (1, 0)
    assert "Gestern" not in _node("/projects/demo/log")["body"]


def test_apply_stale_archives_reversibly_and_expired_deletes_softly():
    import consolidation
    _upsert("/projects/demo/trivia", "Trivia", "unwichtig", 0.3, V_OTHER)
    _age("/projects/demo/trivia", 400)
    past = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    _upsert("/projects/demo/old", "Alt", "weg", 0.5, V, valid_until=past)
    consolidation.propose()
    ids = [p["id"] for p in _proposals()]
    assert consolidation.decide(ids, "apply") == (2, 0)
    trivia = _node("/projects/demo/trivia")
    assert trivia["deleted_at"] is None and trivia["valid_until"] is not None
    assert _node("/projects/demo/old")["deleted_at"] is not None


def test_changed_memory_makes_proposal_outdated_instead_of_applying():
    import consolidation
    _upsert("/projects/demo/a", "A", "alpha", 0.6, V)
    _upsert("/projects/demo/b", "B", "beta", 0.9, V_NEAR)
    consolidation.propose()
    [p] = _proposals("duplicate")
    _upsert("/projects/demo/a", "A", "alpha, inzwischen erweitert", 0.6, V)
    assert consolidation.decide([p["id"]], "merge") == (0, 1)
    assert _node("/projects/demo/a")["deleted_at"] is None
    assert consolidation.history(limit=1)[0]["status"] == "outdated"


def test_invalid_decision_for_kind_is_rejected():
    import consolidation
    _upsert("/projects/demo/trivia", "Trivia", "unwichtig", 0.3, V_OTHER)
    _age("/projects/demo/trivia", 400)
    consolidation.propose()
    [p] = _proposals("stale")
    with pytest.raises(ValueError):
        consolidation.decide([p["id"]], "merge")


def test_mcp_tools_render_and_decide():
    import consolidation
    _upsert("/projects/demo/a", "A", "alpha", 0.6, V)
    _upsert("/projects/demo/b", "B", "beta", 0.9, V_NEAR)
    assert "1 neue" in consolidation.memory_consolidation_run()
    text = consolidation.memory_consolidation_proposals()
    assert "/projects/demo/a" in text and "merge" in text and "supersede" in text
    pid = _proposals()[0]["id"]
    assert "abgelehnt" in consolidation.memory_consolidation_decide(pid, "reject")


def test_cron_only_proposes(monkeypatch):
    import consolidation
    from scripts import consolidation_cron
    called = []
    monkeypatch.setattr(consolidation, "propose", lambda: called.append("propose") or {"duplicate": 0})
    monkeypatch.setattr(consolidation, "decide", lambda *a, **k: called.append("decide"))
    consolidation_cron.main()
    assert called == ["propose"]


def test_web_api_lists_and_decides():
    from fastapi.testclient import TestClient
    import consolidation
    import diary_web
    _upsert("/projects/demo/a", "A", "alpha", 0.6, V)
    _upsert("/projects/demo/b", "B", "beta", 0.9, V_NEAR)
    consolidation.propose()
    client = TestClient(diary_web.app)
    data = client.get("/api/consolidation").json()
    assert data["pending"] == 1 and data["items"][0]["kind"] == "duplicate"
    pid = data["items"][0]["id"]
    blocked = client.post("/api/consolidation/decide", content=json.dumps({"ids": [pid], "decision": "reject"}),
                          headers={"content-type": "application/json", "origin": "http://evil.example"})
    assert blocked.status_code == 403
    ok = client.post("/api/consolidation/decide", json={"ids": [pid], "decision": "reject"},
                     headers={"origin": "http://testserver"})
    assert ok.json() == {"done": 1, "skipped": 0}
