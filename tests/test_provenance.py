"""Provenance (v0.31.0): whether Tom said it or Claude inferred it. Typed
memory provenance keeps inferred guesses from passing as stated facts
(provenance-role collapse in long-term agents)."""
from __future__ import annotations

from unittest.mock import patch

import pytest


def _upsert(path, provenance="", **kw):
    import diary_server
    with patch("diary_embed.embed", return_value=None):
        return diary_server.memory_upsert(path=path, title=kw.get("title", "T"), body=kw.get("body", "B"),
                                          importance=kw.get("importance", 0.8), provenance=provenance)


def _prov(path):
    import diary_db
    with diary_db.get_db() as conn:
        return conn.execute("SELECT provenance FROM memory_nodes WHERE path = %s", (path,)).fetchone()["provenance"]


def test_stored_shown_and_kept_on_update_without_value():
    import diary_server
    _upsert("/feedback/x", provenance="user")
    assert _prov("/feedback/x") == "user"
    assert "Herkunft:   von Tom gesagt" in diary_server.memory_get("/feedback/x")
    _upsert("/feedback/x", body="neu")
    assert _prov("/feedback/x") == "user"
    _upsert("/feedback/x", provenance="claude")
    assert _prov("/feedback/x") == "claude"


def test_unknown_by_default_and_invalid_rejected():
    _upsert("/projects/p/a")
    assert _prov("/projects/p/a") is None
    assert "provenance" in _upsert("/projects/p/b", provenance="hearsay")
    import diary_db
    with diary_db.get_db() as conn:
        assert conn.execute("SELECT 1 FROM memory_nodes WHERE path = '/projects/p/b'").fetchone() is None


def test_extracted_memories_are_user_provenance():
    import diary_server
    with patch("diary_embed.embed", return_value=None):
        diary_server.memory_save_extracted("/projects/p/auto/x", "X", "Tom sagte etwas")
    assert _prov("/projects/p/auto/x") == "user"


def test_injection_marks_claude_inferences():
    import memory_injection as mi
    text = mi.format_prompt_hits([
        {"path": "/a", "title": "A", "body": "x", "provenance": "claude"},
        {"path": "/b", "title": "B", "body": "y", "provenance": "user"},
    ])
    assert "⟨/a⟩ (von Claude erschlossen)" in text
    assert "⟨/b⟩ (von Tom gesagt)" in text
