"""Automatic context injection (v0.18.0): session-start digest, per-prompt
retrieval and the hook entry point.

Motivation: the SessionStart hook only ever injected explicitly pinned nodes
(live: 0 pinned → nothing injected), and the per-prompt FTS hook used
plainto_tsquery (AND over every word) → a natural-language prompt practically
never matched. Net effect: diary knowledge reached the model only when it
remembered to call a tool, strictly worse than Claude Code's always-loaded
MEMORY.md index.
"""
from __future__ import annotations

import ast
import json
import tomllib
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _upsert(path, title="Title", body="Body", importance=0.5, valid_until="", embedding=None):
    import diary_server
    with patch("diary_embed.embed", return_value=embedding), \
         patch("diary_embed.embed_many", side_effect=lambda ts: [embedding] * len(ts)):
        return diary_server.memory_upsert(path=path, title=title, body=body,
                                          importance=importance, valid_until=valid_until)


def _conn():
    import diary_db
    return diary_db.get_db()


@pytest.fixture(autouse=True)
def _state_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("DIARY_HOOK_STATE_DIR", str(tmp_path / "state"))


# ---------------------------------------------------------------------------
# Packaging: a module missing from py-modules installs fine but crashes the
# installed server at import time (diary_embed_ipc in 0.17.0).
# ---------------------------------------------------------------------------

def test_every_imported_local_module_is_packaged():
    cfg = tomllib.loads((ROOT / "pyproject.toml").read_text())
    packaged = set(cfg["tool"]["setuptools"]["py-modules"])
    local = {p.stem for p in ROOT.glob("*.py")}
    missing = set()
    for mod in packaged:
        tree = ast.parse((ROOT / f"{mod}.py").read_text())
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                names = [node.module.split(".")[0]]
            missing |= {n for n in names if n in local and n not in packaged}
    assert not missing, f"imported but not in py-modules: {missing}"


def test_hook_entry_point_declared():
    cfg = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert cfg["project"]["scripts"]["diary-hook"] == "memory_injection:main"


# ---------------------------------------------------------------------------
# One-line hooks (the equivalent of MEMORY.md's "— hook" part)
# ---------------------------------------------------------------------------

def test_node_hook_takes_first_meaningful_line_and_truncates():
    import memory_injection as mi
    body = "# Heading\n\n**Wichtig:** Deploy nur per CI. Zweiter Satz hier.\nmehr"
    assert mi.node_hook(body) == "Wichtig: Deploy nur per CI."
    long = "x" * 500
    assert len(mi.node_hook(long)) <= mi.HOOK_MAX_CHARS
    assert mi.node_hook("") == ""
    assert mi.node_hook("Nutze memory_recall(query) statt memory_search.") == \
        "Nutze memory_recall(query) statt memory_search."
    assert mi.node_hook("Gilt z.B. für Tools. Rest.") == "Gilt z.B. für Tools."
    assert mi.node_hook("Klausuren Herbst / 4. Semester sind fix. Mehr.") == \
        "Klausuren Herbst / 4. Semester sind fix."


def test_digest_ranks_global_rules_by_relevance_to_project():
    import memory_injection as mi
    a = [1.0] + [0.0] * 383
    b = [0.0, 1.0] + [0.0] * 382
    _upsert("/projects/demo/arch", "Architektur", "Plugin-Architektur.", 0.8, embedding=a)
    _upsert("/feedback/relevant-rule", "Relevante Regel", "Passt zum Projekt.", 0.75, embedding=a)
    _upsert("/feedback/offtopic-rule", "Fremde Regel", "Anderes Thema.", 0.8, embedding=b)
    with _conn() as conn:
        out = mi.build_session_digest(conn, "demo")
    assert out.index("Relevante Regel") < out.index("Fremde Regel")


# ---------------------------------------------------------------------------
# Session digest
# ---------------------------------------------------------------------------

def test_digest_lists_project_nodes_and_global_rules_without_noise():
    import diary_server
    import memory_injection as mi
    _upsert("/projects/demo/architecture", "Architektur", "Hexagonal, Postgres als Quelle.", 0.9)
    _upsert("/projects/demo/auto/abc-raw-prompt", "raw prompt", "bitte mach", 0.3)
    _upsert("/projects/demo/expired", "Alt", "abgelaufen", 0.9, valid_until="2020-01-01")
    _upsert("/projects/demo/gone", "Weg", "gelöscht", 0.9)
    diary_server.memory_delete("/projects/demo/gone")
    diary_server.memory_save_extracted("/projects/demo/extracted-x", "Ext", "roh")
    _upsert("/projects/other/secret", "Fremdprojekt", "gehört nicht hierher", 0.9)
    _upsert("/feedback/commit-style", "Commit-Stil", "Nie Claude erwähnen.", 0.9)
    _upsert("/feedback/minor-pref", "Kleinkram", "egal", 0.2)

    with _conn() as conn:
        out = mi.build_session_digest(conn, "demo")

    assert "- /architecture — Architektur" in out and "Hexagonal, Postgres als Quelle." in out
    assert "Commit-Stil" in out
    for noise in ("auto/abc-raw-prompt", "/projects/demo/expired", "/projects/demo/gone",
                  "extracted-x", "Fremdprojekt", "Kleinkram"):
        assert noise not in out, noise
    assert "memory_get" in out


def test_digest_includes_pinned_bodies_in_full():
    import diary_server
    import memory_injection as mi
    body = "Zeile eins.\n" + "Detail " * 60
    _upsert("/projects/demo/critical", "Kritisch", body, 0.5)
    diary_server.memory_pin("/projects/demo/critical", on_start=True, on_compact=True)
    with _conn() as conn:
        out = mi.build_session_digest(conn, "demo")
        out_compact = mi.build_session_digest(conn, "demo", trigger="compact")
    assert body.strip() in out
    assert body.strip() in out_compact


def test_digest_respects_budget_and_reports_omitted():
    import memory_injection as mi
    for i in range(150):
        _upsert(f"/projects/big/node-{i:03d}", f"Knoten {i}", f"Inhalt Nummer {i} " + "blah " * 30,
                importance=0.5 + (i % 5) / 10)
    with _conn() as conn:
        out = mi.build_session_digest(conn, "big", budget_chars=3000)
    assert len(out) <= 3000 + 400  # footer slack
    assert "weitere" in out
    # Highest-importance nodes win the budget.
    assert "- /node-004 " in out


def test_digest_empty_project_still_gives_global_rules():
    import memory_injection as mi
    _upsert("/feedback/rule", "Regel", "Immer testen.", 0.9)
    with _conn() as conn:
        out = mi.build_session_digest(conn, "nonexistent")
    assert "Regel" in out


# ---------------------------------------------------------------------------
# Per-prompt retrieval
# ---------------------------------------------------------------------------

def _seed_retrieval_corpus():
    _upsert("/projects/demo/rsync-regel", "Deployment per rsync",
            "Beim rsync auf Dorn immer --exclude .env setzen, sonst Produktionssecrets weg.", 0.9)
    for i in range(12):
        _upsert(f"/projects/demo/filler-{i}", f"Projekt Notiz {i}",
                f"Allgemeine Projekt Notiz über das Projekt Nummer {i}.", 0.5)


def test_retrieval_matches_natural_language_prompt_with_or_semantics():
    import memory_injection as mi
    _seed_retrieval_corpus()
    with _conn() as conn:
        hits = mi.retrieve_for_prompt(
            conn, "Kannst du bitte das Deployment auf Dorn machen, per rsync wie immer?", "demo")
    assert hits and hits[0]["path"] == "/projects/demo/rsync-regel"


def test_relative_cutoff_drops_hits_far_weaker_than_the_best():
    import memory_injection as mi
    hits = [{"path": "a", "score": 20.0}, {"path": "b", "score": 11.0}, {"path": "c", "score": 7.5}]
    assert [h["path"] for h in mi._relative_cutoff(hits)] == ["a", "b"]
    assert mi._relative_cutoff([]) == []


def test_retrieval_ignores_matches_on_common_words_only():
    import memory_injection as mi
    _seed_retrieval_corpus()
    with _conn() as conn:
        hits = mi.retrieve_for_prompt(conn, "Was steht in dem Projekt eigentlich so drin?", "demo")
    assert all(not h["path"].startswith("/projects/demo/filler") for h in hits)


def test_retrieval_skips_trivial_prompts_and_excluded_paths():
    import memory_injection as mi
    _seed_retrieval_corpus()
    with _conn() as conn:
        assert mi.retrieve_for_prompt(conn, "ja", "demo") == []
        hits = mi.retrieve_for_prompt(conn, "rsync Deployment auf Dorn", "demo",
                                      exclude={"/projects/demo/rsync-regel"})
    assert all(h["path"] != "/projects/demo/rsync-regel" for h in hits)


def test_retrieval_never_returns_extracted():
    import diary_server
    import memory_injection as mi
    diary_server.memory_save_extracted("/projects/demo/ext-kubernetes", "Kubernetes Cluster",
                                       "Kubernetes Cluster Helm Chart Ingress")
    with _conn() as conn:
        hits = mi.retrieve_for_prompt(conn, "Kubernetes Cluster Helm Chart Ingress", "demo")
    assert hits == []


def test_retrieval_uses_semantic_vector_without_lexical_overlap():
    import memory_injection as mi
    vec = [0.0] * 384
    vec[0] = 1.0
    _upsert("/projects/demo/semantic-only", "Fahrzeugpflege", "Winterreifen im Oktober wechseln.",
            0.7, embedding=vec)
    with _conn() as conn:
        hits = mi.retrieve_for_prompt(conn, "Wann sollte ich die Pneus tauschen lassen?", "demo",
                                      qvec=vec)
    assert [h["path"] for h in hits] == ["/projects/demo/semantic-only"]


# ---------------------------------------------------------------------------
# Hook entry point + per-session dedupe
# ---------------------------------------------------------------------------

def test_prompt_hook_injects_once_per_session():
    import memory_injection as mi
    _seed_retrieval_corpus()
    payload = {"session_id": "s1", "cwd": "/somewhere/demo",
               "prompt": "Mach das Deployment auf Dorn per rsync"}
    with patch("memory_injection._query_vector", return_value=None):
        first = mi.run_hook("prompt", payload)
        second = mi.run_hook("prompt", payload)
    ctx = json.loads(first)["hookSpecificOutput"]["additionalContext"]
    assert "rsync-regel" in ctx
    assert second == ""


def test_session_start_hook_outputs_digest_and_compact_resets_dedupe():
    import memory_injection as mi
    _seed_retrieval_corpus()
    prompt = {"session_id": "s2", "cwd": "/x/demo", "prompt": "rsync Deployment Dorn"}
    with patch("memory_injection._query_vector", return_value=None):
        assert mi.run_hook("prompt", prompt)
        assert mi.run_hook("prompt", prompt) == ""
        out = mi.run_hook("session-start", {"session_id": "s2", "cwd": "/x/demo", "source": "compact"})
        assert "rsync-regel" in json.loads(out)["hookSpecificOutput"]["additionalContext"]
        assert mi.run_hook("prompt", prompt)  # context was summarized → may inject again


def test_hook_fails_silent_on_db_error(monkeypatch):
    import memory_injection as mi
    monkeypatch.setenv("DIARY_DATABASE_URL", "postgresql://localhost:1/nope")
    assert mi.run_hook("session-start", {"cwd": "/x/demo"}) == ""
    assert mi.run_hook("prompt", {"cwd": "/x/demo", "prompt": "rsync Deployment Dorn"}) == ""


# ---------------------------------------------------------------------------
# Tool outputs stay within a token budget
# ---------------------------------------------------------------------------

def test_memory_context_is_bounded_on_large_trees():
    import diary_server
    for i in range(300):
        _upsert(f"/projects/p{i % 30}/node-{i}", f"Titel {i}", "Body " * 20)
    out = diary_server.memory_context()
    assert len(out) < 12000
    assert "/projects/p1" in out  # branch overview still present


def test_project_context_full_excludes_noise_and_is_bounded():
    import diary_server
    _upsert("/projects/demo/real", "Echt", "Relevanter Inhalt.", 0.8)
    _upsert("/projects/demo/auto/xyz-junk", "Junk", "roh", 0.3)
    diary_server.memory_save_extracted("/projects/demo/ext", "Ext", "roh")
    for i in range(60):
        _upsert(f"/projects/demo/n{i}", f"N{i}", "lang " * 200, 0.5)
    out = diary_server.memory_project_context("demo", only_pinned=False)
    assert "Relevanter Inhalt." in out
    assert "xyz-junk" not in out and "/projects/demo/ext" not in out
    assert len(out) < 30000
