"""Confidence-scored automatic linking (v0.24.0): high confidence links
automatically, medium confidence lands in a review list that is only processed
on explicit request."""
from __future__ import annotations

import os
import random
from unittest.mock import patch

import psycopg
import pytest
from psycopg.rows import dict_row


def _conn():
    import diary_db
    return psycopg.connect(diary_db.get_database_url(), row_factory=dict_row, autocommit=True)


def _vec(seed: int) -> list[float]:
    rnd = random.Random(seed)
    return [rnd.gauss(0, 1) for _ in range(384)]


def _put(path, body="Inhalt", title=None, vec=None, origin="curated", tags=""):
    import diary_server
    with patch("diary_embed.embed", return_value=vec if vec is not None else _vec(hash(path) % 10_000)):
        return diary_server.memory_upsert(path=path, title=title or path.rsplit("/", 1)[1],
                                          body=body, origin=origin, tags=tags)


def _link_row(a, b):
    with _conn() as c:
        return c.execute(
            "SELECT ml.* FROM memory_links ml JOIN memory_nodes x ON x.id = ml.from_id "
            "JOIN memory_nodes y ON y.id = ml.to_id "
            "WHERE (x.path = %s AND y.path = %s) OR (x.path = %s AND y.path = %s)",
            (a, b, b, a)).fetchone()


def _suggestions(status=None):
    with _conn() as c:
        sql = ("SELECT s.*, x.path AS a, y.path AS b FROM link_suggestions s "
               "JOIN memory_nodes x ON x.id = s.from_id JOIN memory_nodes y ON y.id = s.to_id")
        if status:
            return c.execute(sql + " WHERE s.status = %s", (status,)).fetchall()
        return c.execute(sql).fetchall()


def _pair(rows, a, b):
    return [r for r in rows if {r["a"], r["b"]} == {a, b}]


# ── rules: explicit mentions and near-duplicates ──────────────────────────

def test_path_mention_is_linked_automatically_with_confidence_and_evidence():
    _put("/projects/demo/deploy", "Deploy per CI.")
    _put("/projects/demo/runbook", "Vorher /projects/demo/deploy lesen, dann starten.")
    row = _link_row("/projects/demo/runbook", "/projects/demo/deploy")
    assert row is not None
    assert row["link_origin"] == "inferred"
    assert row["confidence"] >= 0.9
    assert "erwähnt" in row["evidence"]


def test_wiki_mention_resolves_by_slug():
    _put("/projects/demo/auth-session-model", "Sessions laufen 15 min.")
    _put("/projects/demo/login-flow", "Details siehe [[auth-session-model]].")
    assert _link_row("/projects/demo/login-flow", "/projects/demo/auth-session-model") is not None


def test_near_duplicate_content_is_linked_automatically():
    v = _vec(1)
    _put("/projects/a/one", "x", vec=v)
    _put("/projects/b/two", "y", vec=[x * 1.001 for x in v])
    row = _link_row("/projects/a/one", "/projects/b/two")
    assert row is not None and row["confidence"] >= 0.9


def test_unrelated_memories_are_neither_linked_nor_suggested():
    import link_inference
    _put("/projects/a/eins", "Kochrezept Linsensuppe")
    _put("/projects/b/zwei", "Kubernetes Ingress Zertifikate")
    link_inference.run()
    assert _link_row("/projects/a/eins", "/projects/b/zwei") is None
    assert not _pair(_suggestions(), "/projects/a/eins", "/projects/b/zwei")


# ── medium confidence: review list, never processed on its own ────────────

@pytest.fixture
def medium(monkeypatch):
    """Make the near-duplicate rule (0.9) land between the two thresholds."""
    import link_inference
    monkeypatch.setattr(link_inference, "AUTO_CONFIDENCE", 0.99)
    v = _vec(7)
    _put("/projects/a/alpha", "a", vec=v)
    _put("/projects/b/beta", "b", vec=[x * 1.001 for x in v])
    return "/projects/a/alpha", "/projects/b/beta"


def test_medium_confidence_goes_to_review_list_not_to_links(medium):
    a, b = medium
    assert _link_row(a, b) is None
    rows = _pair(_suggestions("pending"), a, b)
    assert len(rows) == 1 and 0.35 <= rows[0]["confidence"] < 0.99


def test_review_list_is_never_processed_by_the_batch(medium):
    import link_inference
    a, b = medium
    link_inference.run()
    link_inference.run()
    assert _link_row(a, b) is None
    assert len(_pair(_suggestions("pending"), a, b)) == 1


def test_approve_creates_an_explicit_link(medium):
    import link_inference
    a, b = medium
    sid = _pair(_suggestions("pending"), a, b)[0]["id"]
    out = link_inference.memory_link_suggestions_decide(str(sid), "approve")
    assert "1" in out
    row = _link_row(a, b)
    assert row["link_origin"] == "explicit"
    assert row["confidence"] is not None
    assert _pair(_suggestions("approved"), a, b)


def test_reject_is_remembered_even_if_confidence_later_rises(medium, monkeypatch):
    import link_inference
    a, b = medium
    sid = _pair(_suggestions("pending"), a, b)[0]["id"]
    link_inference.memory_link_suggestions_decide(str(sid), "reject")
    monkeypatch.setattr(link_inference, "AUTO_CONFIDENCE", 0.7)
    link_inference.run()
    assert _link_row(a, b) is None
    assert [r["status"] for r in _pair(_suggestions(), a, b)] == ["rejected"]


def test_decide_validates_input(medium):
    import link_inference
    assert "decision" in link_inference.memory_link_suggestions_decide("x", "maybe").lower()
    out = link_inference.memory_link_suggestions_decide("00000000-0000-0000-0000-000000000000", "approve")
    assert "0" in out or "nicht gefunden" in out.lower()


def test_listing_shows_confidence_and_evidence(medium):
    import link_inference
    out = link_inference.memory_link_suggestions()
    assert "/projects/a/alpha" in out and "/projects/b/beta" in out
    assert "0." in out  # confidence printed


def test_listing_empty_message():
    import link_inference
    assert "keine" in link_inference.memory_link_suggestions().lower()


def test_review_tools_demand_an_explicit_request():
    import link_inference
    for fn in (link_inference.memory_link_suggestions, link_inference.memory_link_suggestions_decide):
        assert "ausdrücklich" in fn.__doc__


# ── batch run ────────────────────────────────────────────────────────────

def test_run_is_idempotent():
    import link_inference
    _put("/projects/demo/x", "siehe /projects/demo/y")
    _put("/projects/demo/y", "Ziel")
    first = link_inference.run()
    second = link_inference.run()
    assert second["auto"] == 0 and second["suggested"] == 0
    assert first["auto"] + second["auto"] >= 0


def test_batch_catches_mentions_written_before_the_target_existed():
    import link_inference
    _put("/projects/demo/early", "verweist auf /projects/demo/later")
    _put("/projects/demo/later", "kommt später", vec=_vec(99))
    link_inference.run()
    assert _link_row("/projects/demo/early", "/projects/demo/later") is not None


def test_dry_run_writes_nothing():
    import link_inference
    with patch("memory_service.AUTO_LINK_THRESHOLD", 1.1):  # no write-time linking during setup
        _put("/projects/demo/p", "siehe /projects/demo/q")
        _put("/projects/demo/q", "Ziel")
    report = link_inference.run(dry_run=True)
    assert report["auto"] >= 1
    assert _link_row("/projects/demo/p", "/projects/demo/q") is None
    assert not _suggestions()


def test_explicit_links_untouched_and_inferred_links_backfilled():
    import diary_server, link_inference
    with patch("memory_service.AUTO_LINK_THRESHOLD", 1.1):  # keep write-time linking out of the setup
        _put("/projects/demo/m", "m")
        _put("/projects/demo/n", "n")
        _put("/projects/demo/o", "o")
    diary_server.memory_link("/projects/demo/m", "/projects/demo/n", "supports", "von Hand")
    with _conn() as c:
        c.execute("INSERT INTO memory_links (from_id, to_id, rel_type, link_origin) "
                  "SELECT a.id, b.id, 'related', 'inferred' FROM memory_nodes a, memory_nodes b "
                  "WHERE a.path = '/projects/demo/m' AND b.path = '/projects/demo/o'")
    link_inference.run()
    manual = _link_row("/projects/demo/m", "/projects/demo/n")
    assert manual["rel_type"] == "supports" and manual["note"] == "von Hand"
    assert manual["confidence"] is None
    legacy = _link_row("/projects/demo/m", "/projects/demo/o")
    assert legacy is not None and legacy["confidence"] is not None  # scored, not deleted


def test_extracted_and_deleted_memories_are_ignored():
    import diary_server, link_inference
    _put("/projects/demo/gone", "weg")
    diary_server.memory_delete("/projects/demo/gone")
    _put("/projects/demo/auto", "extrahiert", origin="extracted")
    _put("/projects/demo/live", "siehe /projects/demo/auto und /projects/demo/gone")
    link_inference.run()
    assert _link_row("/projects/demo/live", "/projects/demo/auto") is None
    assert _link_row("/projects/demo/live", "/projects/demo/gone") is None


# ── model ────────────────────────────────────────────────────────────────

def test_model_falls_back_to_defaults_with_few_explicit_links():
    import link_inference
    _put("/projects/demo/a", "a")
    assert link_inference.run()["model"] == "default"


def test_model_trains_on_explicit_links_only():
    import diary_server, link_inference
    for p in range(12):
        for k in range(5):
            _put(f"/projects/p{p}/n{k}", f"Notiz {k} zu Projekt {p}")
    for p in range(12):
        for k in range(4):
            diary_server.memory_link(f"/projects/p{p}/n{k}", f"/projects/p{p}/n{k + 1}")
    report = link_inference.run(dry_run=True)
    assert report["model"] == "trained"
    assert report["positives"] == 48
    assert report["weights"]["project"] > 0
    assert all(w >= 0 for w in report["weights"].values())


def test_trained_runs_are_stable():
    """Auto-links must not feed back into training: a second full run with a
    trained model changes nothing, not even the stored confidences."""
    import diary_server, link_inference
    for p in range(12):
        for k in range(5):
            _put(f"/projects/p{p}/n{k}", f"Notiz {k} zu Projekt {p} mit Thema t{p}{k % 2}")
    for p in range(12):
        for k in range(4):
            diary_server.memory_link(f"/projects/p{p}/n{k}", f"/projects/p{p}/n{k + 1}")
    _put("/projects/p0/extra", "siehe /projects/p1/n0 und /projects/p2/n0")
    first = link_inference.run()
    assert first["model"] == "trained"
    second = link_inference.run()
    assert (second["auto"], second["suggested"], second["backfilled"]) == (0, 0, 0)
    assert second["weights"] == first["weights"]


def test_evidence_order_does_not_depend_on_hash_seed():
    """Equally rare shared terms are listed alphabetically; set iteration order
    changes per process (PYTHONHASHSEED) and made every nightly run rewrite links."""
    _put("/projects/demo/t1", "zebrafisch apfelbaum mondrakete", vec=_vec(11))
    _put("/projects/demo/t2", "mondrakete zebrafisch apfelbaum", vec=_vec(12))
    row = _link_row("/projects/demo/t1", "/projects/demo/t2") or \
        _pair(_suggestions(), "/projects/demo/t1", "/projects/demo/t2")[0]
    assert "gemeinsame Begriffe: apfelbaum, mondrake" in row["evidence"]


# ── plumbing ─────────────────────────────────────────────────────────────

def test_link_confidence_is_synced(test_databases):
    import diary_server
    _put("/projects/demo/s1", "siehe /projects/demo/s2")
    _put("/projects/demo/s2", "Ziel")
    # the mention is resolved by the batch (s2 did not exist when s1 was written)
    import link_inference
    link_inference.run()
    diary_server.memory_sync()
    with psycopg.connect(os.environ["DIARY_REMOTE_URL"], row_factory=dict_row) as r:
        row = r.execute(
            "SELECT ml.confidence, ml.evidence FROM memory_links ml "
            "JOIN memory_nodes a ON a.id = ml.from_id JOIN memory_nodes b ON b.id = ml.to_id "
            "WHERE a.path IN ('/projects/demo/s1', '/projects/demo/s2') "
            "AND b.path IN ('/projects/demo/s1', '/projects/demo/s2')"
        ).fetchone()
    assert row is not None and row["confidence"] >= 0.9 and "erwähnt" in row["evidence"]


def test_cron_script_uses_confidence_linking():
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "scripts" / "link_inference_cron.py").read_text()
    assert "link_inference.run(" in src


def test_stats_report_pending_suggestions(medium):
    import diary_db, memory_stats
    with diary_db.get_db() as conn:
        g = memory_stats.collect_stats(conn)["graph"]
    assert g["suggestions_pending"] == 1
    assert "auto_links" in g


# ── manual runs (v0.26.0) ────────────────────────────────────────────────

def test_run_records_last_run_but_dry_run_does_not():
    import link_inference
    _put("/projects/demo/lr", "Ziel")
    link_inference.run(dry_run=True, trigger="web")
    assert link_inference.last_run() is None
    link_inference.run(trigger="web")
    last = link_inference.last_run()
    assert last["trigger"] == "web" and "auto" in last
    import re
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d", last["at"]), last["at"]  # JS Date.parse needs ±HH:MM


def test_dry_run_can_preview_examples():
    import link_inference
    with patch("memory_service.AUTO_LINK_THRESHOLD", 1.1):
        _put("/projects/demo/pv1", "siehe /projects/demo/pv2")
        _put("/projects/demo/pv2", "Ziel")
    report = link_inference.run(dry_run=True, examples=5)
    ex = report["examples"]
    assert any(e["action"] == "auto" and {e["a"], e["b"]} == {"/projects/demo/pv1", "/projects/demo/pv2"}
               and e["confidence"] >= 0.9 and e["evidence"] for e in ex)


def test_concurrent_run_reports_busy_instead_of_waiting():
    import link_inference
    with _conn() as other:
        other.execute("BEGIN")
        other.execute("SELECT pg_advisory_xact_lock(%s)", (link_inference.RUN_LOCK_KEY,))
        report = link_inference.run()
        other.execute("ROLLBACK")
    assert report["busy"] is True
    assert link_inference.run()["busy"] is False


# ── project spine (v0.26.0) ──────────────────────────────────────────────

def _project_links(slug):
    with _conn() as c:
        return c.execute(
            "SELECT a.path AS a, b.path AS b, ml.link_origin, ml.note, ml.confidence FROM memory_links ml "
            "JOIN memory_nodes a ON a.id = ml.from_id JOIN memory_nodes b ON b.id = ml.to_id "
            "WHERE a.path LIKE %s AND b.path LIKE %s", (f"/projects/{slug}/%", f"/projects/{slug}/%")).fetchall()


def _connected(paths, links):
    parent = {p: p for p in paths}
    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    for l in links:
        parent[find(l["a"])] = find(l["b"])
    return len({find(p) for p in paths}) == 1


def _spine_setup(n, slug="sp"):
    paths = [f"/projects/{slug}/n{k}" for k in range(n)]
    with patch("memory_service.AUTO_LINK_THRESHOLD", 1.1):
        for k, p in enumerate(paths):
            _put(p, " ".join(f"wort{k}x{w}" for w in range(4)), vec=_vec(500 + k))  # no shared terms
    return paths


def test_spine_connects_a_project_with_n_minus_1_links():
    import link_inference
    paths = _spine_setup(5)
    report = link_inference.run()
    links = _project_links("sp")
    assert report["spine"] == 4 and len(links) == 4
    assert _connected(paths, links)
    assert all(l["link_origin"] == "inferred" and l["note"] == "Projekt-Rückgrat" for l in links)
    assert all(l["confidence"] is not None for l in links)


def test_spine_counts_existing_links_and_is_idempotent():
    import diary_server, link_inference
    paths = _spine_setup(4)
    diary_server.memory_link(paths[0], paths[1])
    assert link_inference.run()["spine"] == 2
    assert link_inference.run()["spine"] == 0
    assert _connected(paths, _project_links("sp"))


def test_spine_never_uses_rejected_pairs(monkeypatch):
    import link_inference
    paths = _spine_setup(2)
    with _conn() as c:
        c.execute("INSERT INTO link_suggestions (from_id, to_id, confidence, status) "
                  "SELECT a.id, b.id, 0.5, 'rejected' FROM memory_nodes a, memory_nodes b "
                  "WHERE a.path = %s AND b.path = %s", (paths[0], paths[1]))
    assert link_inference.run()["spine"] == 0
    assert not _project_links("sp")


def test_spine_only_inside_projects():
    import link_inference
    with patch("memory_service.AUTO_LINK_THRESHOLD", 1.1):
        _put("/feedback/f1", "Regel eins", vec=_vec(601))
        _put("/feedback/f2", "Regel zwei", vec=_vec(602))
        _put("/projects/one/x", "x", vec=_vec(603))
        _put("/projects/two/y", "y", vec=_vec(604))
    assert link_inference.run()["spine"] == 0


def test_spine_dry_run_writes_nothing():
    import link_inference
    _spine_setup(3)
    assert link_inference.run(dry_run=True)["spine"] == 2
    assert not _project_links("sp")


def test_new_project_memory_gets_its_nearest_sibling_at_write_time():
    paths = _spine_setup(3)
    v = _vec(500)  # same direction as n0 → n0 is the nearest sibling
    _put("/projects/sp/new", "Frischer Eintrag", vec=[x + 0.3 * y for x, y in zip(v, _vec(777))])
    links = [l for l in _project_links("sp") if "/projects/sp/new" in (l["a"], l["b"])]
    assert len(links) == 1
    assert {links[0]["a"], links[0]["b"]} == {"/projects/sp/new", "/projects/sp/n0"}
