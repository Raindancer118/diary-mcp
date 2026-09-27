"""memory_stats (v0.19.0): token efficiency of the automatic injection,
corpus statistics and a comparison with Claude Code's file-based memory."""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest


def _upsert(path, title="T", body="B", importance=0.5):
    import diary_server
    with patch("diary_embed.embed", return_value=None), \
         patch("diary_embed.embed_many", side_effect=lambda ts: [None] * len(ts)):
        return diary_server.memory_upsert(path=path, title=title, body=body, importance=importance)


@pytest.fixture(autouse=True)
def _dirs(tmp_path, monkeypatch):
    monkeypatch.setenv("DIARY_HOOK_STATE_DIR", str(tmp_path / "state"))
    root = tmp_path / "claude"
    monkeypatch.setenv("DIARY_VANILLA_MEMORY_ROOT", str(root))
    return root


def _vanilla(root, project_dir, files: dict[str, str]):
    d = root / "projects" / project_dir.replace("/", "-") / "memory"
    d.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (d / name).write_text(text)
    return d


def test_injection_log_records_hook_events_without_content():
    import memory_injection as mi
    import memory_stats as ms
    _upsert("/projects/demo/rsync-regel", "Deployment per rsync",
            "Beim rsync auf Dorn immer --exclude .env setzen, sonst Secrets weg.", 0.9)
    for i in range(25):
        _upsert(f"/projects/demo/f{i}", f"Notiz {i}", f"Allgemeines Thema {i}.")
    with patch("memory_injection._query_vector", return_value=None):
        mi.run_hook("session-start", {"session_id": "s", "cwd": "/x/demo"})
        mi.run_hook("prompt", {"session_id": "s", "cwd": "/x/demo", "prompt": "rsync Deployment auf Dorn"})
        mi.run_hook("prompt", {"session_id": "s", "cwd": "/x/demo", "prompt": "Wetter morgen Hamburg Regen"})
    events = ms.read_injection_log()
    assert [e["event"] for e in events] == ["session-start", "prompt", "prompt"]
    assert events[1]["hits"] == ["/projects/demo/rsync-regel"] and events[1]["chars"] > 0
    assert events[2]["hits"] == [] and events[2]["chars"] == 0
    assert all("latency_ms" in e for e in events)
    raw = (mi._state_dir() / "injection_log.jsonl").read_text()
    assert "Secrets weg" not in raw  # paths and sizes only


def test_log_is_bounded(monkeypatch):
    import memory_injection as mi
    monkeypatch.setattr(mi, "LOG_MAX_BYTES", 2000)
    for i in range(200):
        mi.log_event({"event": "prompt", "chars": i, "hits": []})
    path = mi._state_dir() / "injection_log.jsonl"
    assert path.stat().st_size <= 2000
    last = json.loads(path.read_text().splitlines()[-1])
    assert last["chars"] == 199


def test_collect_stats_corpus_and_injection_numbers():
    import memory_injection as mi
    import memory_stats as ms
    import diary_db
    _upsert("/projects/demo/a", "A", "x" * 380)
    _upsert("/feedback/r", "R", "y" * 380, 0.9)
    mi.log_event({"event": "session-start", "session": "s", "chars": 800, "latency_ms": 100})
    mi.log_event({"event": "prompt", "session": "s", "chars": 400, "hits": ["/projects/demo/a"], "latency_ms": 200})
    mi.log_event({"event": "prompt", "session": "s", "chars": 0, "hits": [], "latency_ms": 100})
    with diary_db.get_db() as conn:
        st = ms.collect_stats(conn, days=30)
    assert st["corpus"]["curated"] >= 2
    assert st["corpus"]["approx_tokens"] >= 200
    inj = st["injection"]
    assert inj["sessions"] == 1 and inj["prompts"] == 2 and inj["prompts_with_hits"] == 1
    assert inj["hit_rate"] == pytest.approx(0.5)
    assert inj["avg_tokens_per_session"] == ms.approx_tokens(1200)
    assert inj["top_injected"][0] == ["/projects/demo/a", 1]


def test_vanilla_comparison_counts_files_and_import_coverage(_dirs):
    import memory_stats as ms
    import diary_db
    _vanilla(_dirs, "/home/u/proj/demo", {
        "MEMORY.md": "- [Deploy](deploy.md) — rsync mit exclude\n- [Style](style.md) — kurz\n",
        "deploy.md": "---\nname: deploy\n---\nBeim rsync auf Dorn immer --exclude .env setzen.",
        "style.md": "---\nname: style\n---\nAntworten kurz halten und direkt.",
    })
    _vanilla(_dirs, "/home/u/other", {"MEMORY.md": "- [X](x.md)\n", "x.md": "Etwas anderes."})
    _upsert("/projects/demo/deploy", "Deploy", "Beim rsync auf Dorn immer --exclude .env setzen.")
    with diary_db.get_db() as conn:
        st = ms.collect_stats(conn, days=30, project_slug="demo")
    v = st["vanilla"]
    assert v["projects"] == 2 and v["files"] == 3  # MEMORY.md indexes not counted as memories
    assert v["imported"] == 1 and v["not_imported"] == 2
    assert v["project"]["slug"] == "demo" and v["project"]["files"] == 2
    assert v["project"]["always_loaded_tokens"] == ms.approx_tokens(
        len("- [Deploy](deploy.md) — rsync mit exclude\n- [Style](style.md) — kurz\n"))
    assert st["diary"]["project_digest_tokens"] > 0


def test_vanilla_comparison_without_files(_dirs):
    import memory_stats as ms
    import diary_db
    with diary_db.get_db() as conn:
        st = ms.collect_stats(conn, days=30)
    assert st["vanilla"]["files"] == 0


def test_memory_stats_tool_renders_all_sections(_dirs):
    import diary_server
    _vanilla(_dirs, "/home/u/demo", {"MEMORY.md": "- [A](a.md)\n", "a.md": "Inhalt A"})
    _upsert("/projects/demo/n", "N", "Body")
    out = diary_server.memory_stats(project_slug="demo")
    for heading in ("Bestand", "Injection", "Vergleich", "file-based"):
        assert heading in out, heading


def test_web_stats_endpoint_returns_json(_dirs):
    from fastapi.testclient import TestClient
    import diary_web
    _upsert("/projects/demo/n", "N", "Body")
    resp = TestClient(diary_web.app).get("/api/stats", params={"days": 7, "project": "demo"})
    assert resp.status_code == 200
    data = resp.json()
    assert {"corpus", "injection", "vanilla", "diary"} <= data.keys()


# ---------------------------------------------------------------------------
# v0.20.0: instance, memory quality, graph and project-journal statistics
# ---------------------------------------------------------------------------

def _stats(**kw):
    import diary_db
    import memory_stats as ms
    with diary_db.get_db() as conn:
        return ms.collect_stats(conn, **kw)


def test_instance_section_describes_this_installation():
    inst = _stats()["instance"]
    assert inst["version"]
    assert inst["hostname"]
    assert inst["postgres_version"].split(".")[0].isdigit()
    assert inst["db_name"] == "diary_mcp_pytest"
    assert "password" not in json.dumps(inst).lower()
    assert inst["db_size_bytes"] > 0
    assert isinstance(inst["pgvector"], bool)
    assert inst["embed_model"]
    assert isinstance(inst["embed_server_alive"], bool)
    assert "federation" in inst and "remote_sync" in inst


def test_memory_quality_section():
    import diary_server
    _upsert("/projects/q/high", "H", "x" * 400, 0.9)
    _upsert("/projects/q/low", "L", "y", 0.2)
    _upsert("/user/expired", "E", "z", 0.5)
    _upsert("/projects/q/gone", "G", "g")
    diary_server.memory_delete("/projects/q/gone")
    import diary_db
    with diary_db.get_db() as conn:
        conn.execute("UPDATE memory_nodes SET valid_until = now() - interval '1 day' WHERE path = '/user/expired'")
        conn.execute("UPDATE memory_nodes SET access_count = 7 WHERE path = '/projects/q/high'")
    q = _stats()["quality"]
    assert q["importance"]["high"] >= 1 and q["importance"]["low"] >= 1
    assert q["expired"] == 1
    assert q["tombstones"] == 1
    assert q["created_7d"] >= 3
    assert q["never_accessed"] >= 1
    assert q["most_accessed"][0] == ["/projects/q/high", 7]
    assert q["largest"][0][0] == "/projects/q/high"
    assert q["top_projects"][0][0] == "q"
    assert q["types"]


def test_graph_section_counts_links_orphans_contradictions():
    import diary_server
    _upsert("/projects/g/a", "A", "a")
    _upsert("/projects/g/b", "B", "b")
    _upsert("/projects/g/lonely", "C", "c")
    diary_server.memory_link("/projects/g/a", "/projects/g/b", rel_type="contradicts")
    g = _stats()["graph"]
    assert g["links"] == 1
    assert g["by_type"] == {"contradicts": 1}
    assert g["contradictions"] == 1
    assert g["orphans"] >= 1


def test_journal_section_counts_project_diary():
    import diary_server
    diary_server.add_project("stats-journal-proj")
    diary_server.add_milestone("stats-journal-proj", "M1")
    diary_server.add_log_entry("stats-journal-proj", "log eintrag")
    diary_server.add_error_solution("stats-journal-proj", "fehler", "lösung")
    diary_server.add_reminder("stats-journal-proj", "2000-01-01", "überfällig")
    j = _stats()["journal"]
    assert j["projects_active"] >= 1
    assert j["milestones_total"] >= 1
    assert j["logs_30d"] >= 1
    assert j["errors_solutions"] >= 1
    assert j["reminders_overdue"] >= 1


def test_tool_output_has_new_sections():
    import diary_server
    _upsert("/projects/demo/n", "N", "Body")
    out = diary_server.memory_stats()
    for heading in ("## Instanz", "## Qualität", "## Graph", "## Projekt-Diary"):
        assert heading in out, heading


# ---------------------------------------------------------------------------
# Small, specific memories (Tom 2026-09-27)
# ---------------------------------------------------------------------------

def test_upsert_warns_on_oversized_curated_memory():
    import memory_service
    out = _upsert("/projects/demo/big", "Groß", "x " * memory_service.MEMORY_SOFT_MAX_CHARS)
    assert "aufteilen" in out
    assert "aufteilen" not in _upsert("/projects/demo/small", "Klein", "rsync nur mit --exclude .env.")


def test_upsert_docstring_states_style_rule():
    import memory_service
    doc = memory_service.memory_upsert.__doc__
    assert "klein" in doc and "Füllwörter" in doc


def test_quality_counts_oversized_memories():
    import memory_service
    _upsert("/projects/demo/big", "Groß", "y" * (memory_service.MEMORY_SOFT_MAX_CHARS + 1))
    _upsert("/projects/demo/small", "Klein", "kurz")
    q = _stats()["quality"]
    assert q["oversized"] == 1
