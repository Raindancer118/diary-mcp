"""DIARY_FORCE_TAGS: an agent's diary server (e.g. Jules') stamps every memory
it writes, so its entries stay separable from Tom's own."""
from __future__ import annotations

from unittest.mock import patch


def _upsert(path, tags=""):
    import diary_server
    with patch("diary_embed.embed", return_value=None):
        return diary_server.memory_upsert(path=path, title="T", body="B", tags=tags)


def test_forced_tags_are_added(monkeypatch):
    import diary_server
    monkeypatch.setenv("DIARY_FORCE_TAGS", "jules")
    _upsert("/user/x", tags="musik")
    tags_line = next(l for l in diary_server.memory_get("/user/x").splitlines() if l.startswith("Tags:"))
    assert "jules" in tags_line and "musik" in tags_line


def test_not_duplicated_and_off_by_default(monkeypatch):
    import diary_server
    monkeypatch.setenv("DIARY_FORCE_TAGS", "jules")
    _upsert("/user/y", tags="Jules")
    tags_line = next(l for l in diary_server.memory_get("/user/y").splitlines() if l.startswith("Tags:"))
    assert tags_line.lower().count("jules") == 1
    monkeypatch.delenv("DIARY_FORCE_TAGS")
    _upsert("/user/z")
    assert "jules" not in diary_server.memory_get("/user/z")
