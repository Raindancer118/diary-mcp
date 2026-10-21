"""Usage feedback (v0.30.0): at session end the hook checks, per injected
memory, whether Claude's reply actually drew on it. Memories injected again
and again without ever being used stop being injected — retrieval feedback
reshaping memory, as in REALM-style reconsolidation, in its simplest form."""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest


def _upsert(path, title="Title", body="Body", importance=0.8, embedding=None):
    import diary_server
    with patch("diary_embed.embed", return_value=embedding), \
         patch("diary_embed.embed_many", side_effect=lambda ts: [embedding] * len(ts)):
        return diary_server.memory_upsert(path=path, title=title, body=body, importance=importance)


def _conn():
    import diary_db
    return diary_db.get_db()


@pytest.fixture(autouse=True)
def _state_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("DIARY_HOOK_STATE_DIR", str(tmp_path / "state"))


def _user(text, ts="2026-10-21T10:00:00Z"):
    return {"type": "user", "timestamp": ts, "message": {"role": "user", "content": text}}


def _inject(paths, ts="2026-10-21T10:00:01Z"):
    body = "# diary-mcp: passende Memories zu diesem Prompt (automatisch)\n" + "\n".join(
        f"## T ⟨{p}⟩\nbody" for p in paths)
    return {"type": "attachment", "timestamp": ts,
            "attachment": {"type": "hook_additional_context", "content": [body]}}


def _assistant(text="", tool=None, ts="2026-10-21T10:00:05Z"):
    content = []
    if text:
        content.append({"type": "text", "text": text})
    if tool:
        content.append({"type": "tool_use", "name": tool[0], "input": tool[1]})
    return {"type": "assistant", "timestamp": ts, "message": {"role": "assistant", "content": content}}


def _tool_result(ts="2026-10-21T10:00:06Z"):
    return {"type": "user", "timestamp": ts,
            "message": {"role": "user", "content": [{"type": "tool_result", "content": "ok"}]}}


def _write(tmp_path, rows):
    f = tmp_path / "t.jsonl"
    f.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n")
    return f


def _seed():
    _upsert("/projects/demo/rsync-regel", "Deployment per rsync",
            "Beim rsync auf Dorn immer --exclude .env setzen, sonst Produktionssecrets weg. "
            "Wrapper deploy-dorn.sh nutzt Hetzner Storagebox als Zwischenstation.")
    for i in range(10):
        _upsert(f"/projects/demo/f{i}", f"Notiz {i}", f"Allgemeine Notiz Nummer {i} über Projekte.")


def test_transcript_parsing_pairs_injection_with_prompt_and_reply(tmp_path):
    import memory_injection as mi
    f = _write(tmp_path, [
        _user("Deploy das bitte auf Dorn"),
        _inject(["/projects/demo/rsync-regel"]),
        _assistant("Mache ich.", tool=("Bash", {"command": "rsync -a --exclude .env ./ dorn:/srv"})),
        _tool_result(),
        _assistant("Fertig, Produktionssecrets blieben unangetastet."),
        _user("danke", ts="2026-10-21T10:01:00Z"),
        _assistant("Gern.", ts="2026-10-21T10:01:02Z"),
    ])
    [inj] = mi.injections_in_transcript(f)
    assert inj["paths"] == ["/projects/demo/rsync-regel"]
    assert inj["prompt"] == "Deploy das bitte auf Dorn"
    assert "--exclude .env" in inj["reply"] and "Produktionssecrets" in inj["reply"]
    assert "Gern." not in inj["reply"]


def test_reply_drawing_on_memory_counts_as_used_and_unrelated_does_not(tmp_path):
    import memory_injection as mi
    _seed()
    with _conn() as conn:
        n = len(mi._corpus_lexemes(conn))
        used = mi.usage_rank(conn, "/projects/demo/rsync-regel", "Deploy das bitte auf Dorn",
                             "rsync mit --exclude .env über den Wrapper deploy-dorn.sh, "
                             "Produktionssecrets bleiben, Hetzner Storagebox als Zwischenstation.")
        unused = mi.usage_rank(conn, "/projects/demo/rsync-regel", "Deploy das bitte auf Dorn",
                               "Ich habe das Build-Skript angepasst und die Tests laufen grün.")
        generic = mi.usage_rank(conn, "/projects/demo/rsync-regel", "Deploy das bitte auf Dorn",
                                "Allgemeine Notiz Nummer drei über Projekte, und rsync.")
        mentioned = mi.usage_rank(conn, "/projects/demo/rsync-regel", "x",
                                  'memory_get("/projects/demo/rsync-regel")')
    assert mi.is_used(used, n) and not mi.is_used(unused, n) and not mi.is_used(generic, n)
    assert mentioned == 0.0


def test_session_end_hook_records_feedback(tmp_path):
    import memory_injection as mi
    _seed()
    f = _write(tmp_path, [
        _user("Deploy das bitte auf Dorn"),
        _inject(["/projects/demo/rsync-regel", "/projects/demo/f1"]),
        _assistant("rsync mit --exclude .env über deploy-dorn.sh, Produktionssecrets sicher, "
                   "Hetzner Storagebox Zwischenstation."),
    ])
    mi.run_hook("session-end", {"session_id": "s1", "transcript_path": str(f), "cwd": "/x/demo"})
    with _conn() as conn:
        rows = {r["path"]: r["used"] for r in conn.execute(
            "SELECT path, used FROM injection_feedback WHERE session_id = 's1'").fetchall()}
    assert rows == {"/projects/demo/rsync-regel": True, "/projects/demo/f1": False}
    # idempotent: a second run (e.g. resumed session) doesn't double-count
    mi.run_hook("session-end", {"session_id": "s1", "transcript_path": str(f), "cwd": "/x/demo"})
    with _conn() as conn:
        assert conn.execute("SELECT count(*) AS n FROM injection_feedback").fetchone()["n"] == 2


def test_repeatedly_unused_memory_is_no_longer_a_candidate():
    import memory_injection as mi
    _seed()
    with _conn() as conn:
        for i in range(mi.USE_MIN_SAMPLES):
            conn.execute("INSERT INTO injection_feedback (session_id, path, injected_at, used, score) "
                         "VALUES (%s, '/projects/demo/rsync-regel', now() - %s::interval, false, 0)",
                         (f"s{i}", f"{i} hours"))
        cands = mi.score_candidates(conn, "Deployment auf Dorn per rsync", "demo")
    assert "/projects/demo/rsync-regel" not in [c["path"] for c in cands]


def test_behaviour_rules_are_never_suppressed():
    import memory_injection as mi
    _upsert("/feedback/deploy-rule", "Deployment per rsync", "rsync auf Dorn immer mit exclude .env")
    with _conn() as conn:
        for i in range(mi.USE_MIN_SAMPLES):
            conn.execute("INSERT INTO injection_feedback (session_id, path, injected_at, used, score) "
                         "VALUES (%s, '/feedback/deploy-rule', now(), false, 1)", (f"s{i}",))
        assert "/feedback/deploy-rule" not in mi._never_used(conn)


def test_one_use_keeps_memory_eligible():
    import memory_injection as mi
    _seed()
    with _conn() as conn:
        for i in range(mi.USE_MIN_SAMPLES):
            conn.execute("INSERT INTO injection_feedback (session_id, path, injected_at, used, score) "
                         "VALUES (%s, '/projects/demo/rsync-regel', now() - %s::interval, %s, 0)",
                         (f"s{i}", f"{i} hours", i == 0))
        cands = mi.score_candidates(conn, "Deployment auf Dorn per rsync", "demo")
    assert "/projects/demo/rsync-regel" in [c["path"] for c in cands]


def test_stats_report_live_usage_rate(tmp_path):
    import memory_stats
    _seed()
    with _conn() as conn:
        conn.execute("INSERT INTO injection_feedback (session_id, path, injected_at, used, score) VALUES "
                     "('a', '/projects/demo/rsync-regel', now(), true, 20), "
                     "('b', '/projects/demo/rsync-regel', now(), false, 0)")
        st = memory_stats.collect_stats(conn)
    assert st["injection"]["feedback"] == {"judged": 2, "used": 1, "use_rate": 0.5, "suppressed": 0}
    assert "genutzt" in memory_stats.format_stats(st)
