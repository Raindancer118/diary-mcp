"""
Test suite for diary-mcp memory operations.

Covers: upsert/update, access tracking, tombstones, two-tier, ranking,
hybrid search, sync round-trip, extracted lifecycle, project config.
"""
from __future__ import annotations

import importlib
import os
import time
from datetime import datetime, timedelta
from unittest.mock import patch

import psycopg
from psycopg.rows import dict_row
import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _local_conn():
    """Open a raw dict_row connection to the local test DB."""
    import diary_db
    return psycopg.connect(diary_db.get_database_url(), row_factory=dict_row)


def _remote_conn():
    """Open a raw dict_row connection to the remote test DB."""
    url = os.environ.get("DIARY_REMOTE_URL")
    return psycopg.connect(url, row_factory=dict_row)


def _upsert(path, title="Title", body="Body", importance=0.5,
            origin="curated", valid_until="", tags=""):
    """Call memory_upsert and return the result string."""
    import diary_server
    return diary_server.memory_upsert(
        path=path, title=title, body=body,
        importance=importance, origin=origin,
        valid_until=valid_until, tags=tags,
    )


def _get_node(path, conn=None):
    """Fetch a memory_node row (including tombstoned) directly."""
    close = False
    if conn is None:
        conn = _local_conn()
        close = True
    try:
        row = conn.execute(
            "SELECT * FROM memory_nodes WHERE path = %s", (path,)
        ).fetchone()
        return row
    finally:
        if close:
            conn.close()


# ===========================================================================
# 1. memory_upsert — create + update
# ===========================================================================

class TestMemoryUpsert:
    def test_create_returns_created(self):
        import diary_server
        result = diary_server.memory_upsert("/user/test-create", "T", "B")
        assert "erstellt" in result

    def test_create_stores_fields(self):
        _upsert("/user/store-fields", title="My Title", body="My Body",
                importance=0.8, tags="a, b")
        node = _get_node("/user/store-fields")
        assert node is not None
        assert node["title"] == "My Title"
        assert node["body"] == "My Body"
        assert abs(node["importance"] - 0.8) < 0.01
        assert "a" in node["tags"]
        assert "b" in node["tags"]

    def test_update_is_idempotent_second_upsert(self):
        _upsert("/user/idempotent", title="v1", body="first")
        r2 = _upsert("/user/idempotent", title="v2", body="second")
        assert "aktualisiert" in r2
        node = _get_node("/user/idempotent")
        assert node["title"] == "v2"
        assert node["body"] == "second"

    def test_parent_auto_creation(self):
        # The path /user already exists as seed; but a deeper path should auto-create parents
        _upsert("/user/deep/child/leaf", title="Leaf", body="deep node")
        conn = _local_conn()
        try:
            # Intermediate node /user/deep should exist
            row = conn.execute(
                "SELECT id FROM memory_nodes WHERE path = %s", ("/user/deep",)
            ).fetchone()
            assert row is not None, "/user/deep should have been auto-created"
            # Leaf should exist and have a parent_id
            leaf = conn.execute(
                "SELECT parent_id FROM memory_nodes WHERE path = %s",
                ("/user/deep/child/leaf",),
            ).fetchone()
            assert leaf is not None
            assert leaf["parent_id"] is not None
        finally:
            conn.close()

    def test_embedding_stored_when_available(self):
        """If embed() returns a non-None vector, embedding column should be populated."""
        fake_vec = [0.1] * 384
        with patch("diary_embed.embed", return_value=fake_vec):
            _upsert("/user/with-embed", title="Embed", body="test embedding")
        node = _get_node("/user/with-embed")
        assert node["embedding"] is not None
        assert len(node["embedding"]) == 384

    def test_embedding_none_when_model_unavailable(self):
        """If embed() returns None, embedding column should stay NULL."""
        with patch("diary_embed.embed", return_value=None):
            _upsert("/user/no-embed", title="No embed", body="no embedding")
        node = _get_node("/user/no-embed")
        assert node["embedding"] is None


# ===========================================================================
# 2. memory_get — access tracking
# ===========================================================================

class TestMemoryGet:
    def test_get_increments_access_count(self):
        _upsert("/user/access-track", title="T", body="B")
        import diary_server
        # Read initial count
        node_before = _get_node("/user/access-track")
        initial = node_before["access_count"]
        diary_server.memory_get("/user/access-track")
        diary_server.memory_get("/user/access-track")
        node_after = _get_node("/user/access-track")
        assert node_after["access_count"] == initial + 2

    def test_get_updates_accessed_at(self):
        _upsert("/user/accessed-at", title="T", body="B")
        import diary_server
        diary_server.memory_get("/user/accessed-at")
        node = _get_node("/user/accessed-at")
        assert node["accessed_at"] is not None

    def test_get_returns_not_found_for_deleted(self):
        _upsert("/user/get-deleted", title="T", body="B")
        import diary_server
        diary_server.memory_delete("/user/get-deleted")
        result = diary_server.memory_get("/user/get-deleted")
        # message contains "gefunden" (as in "nicht gefunden" / "Kein ... gefunden")
        assert "gefunden" in result.lower()

    def test_get_warns_on_expired(self):
        import diary_server
        past = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        _upsert("/user/expired-get", title="T", body="B", valid_until=past)
        result = diary_server.memory_get("/user/expired-get")
        # Should contain expiry warning (ABGELAUFEN)
        assert "ABGELAUFEN" in result or "abgelaufen" in result.lower()


# ===========================================================================
# 3. memory_tree / memory_context — exclude extracted + tombstoned
# ===========================================================================

class TestMemoryTreeContext:
    def test_tree_excludes_tombstoned(self):
        _upsert("/user/tombstone-tree", title="T", body="B")
        import diary_server
        diary_server.memory_delete("/user/tombstone-tree")
        result = diary_server.memory_tree("/user")
        assert "tombstone-tree" not in result

    def test_tree_excludes_extracted_by_default(self):
        import diary_server
        diary_server.memory_save_extracted("/user/extracted-tree", "Ext", "body")
        result = diary_server.memory_tree("/user")
        assert "extracted-tree" not in result

    def test_tree_includes_extracted_when_requested(self):
        import diary_server
        diary_server.memory_save_extracted("/user/extracted-vis", "Ext", "body")
        result = diary_server.memory_tree("/user", include_extracted=True)
        assert "extracted-vis" in result

    def test_context_excludes_extracted(self):
        import diary_server
        diary_server.memory_save_extracted("/user/ctx-extracted", "Ext", "body")
        result = diary_server.memory_context()
        # The extracted node should not appear in the tree section
        assert "ctx-extracted" not in result.split("RECENTLY UPDATED")[0]

    def test_context_shows_extracted_count_hint(self):
        import diary_server
        diary_server.memory_save_extracted("/user/hint-ext", "Ext", "body")
        result = diary_server.memory_context()
        assert "auto-extrahierte" in result

    def test_context_excludes_tombstoned(self):
        _upsert("/user/ctx-tombstone", title="T", body="B")
        import diary_server
        diary_server.memory_delete("/user/ctx-tombstone")
        result = diary_server.memory_context()
        assert "ctx-tombstone" not in result


# ===========================================================================
# 4. Tombstones — soft-delete + cascade + row persists
# ===========================================================================

class TestTombstones:
    def test_delete_soft_deletes_node(self):
        _upsert("/feedback/soft-del", title="T", body="B")
        import diary_server
        diary_server.memory_delete("/feedback/soft-del")
        row = _get_node("/feedback/soft-del")
        assert row is not None, "Row should still exist (tombstone)"
        assert row["deleted_at"] is not None

    def test_delete_cascades_to_children(self):
        _upsert("/feedback/parent-del", title="Parent", body="P")
        _upsert("/feedback/parent-del/child", title="Child", body="C")
        import diary_server
        diary_server.memory_delete("/feedback/parent-del")
        parent = _get_node("/feedback/parent-del")
        child = _get_node("/feedback/parent-del/child")
        assert parent["deleted_at"] is not None
        assert child["deleted_at"] is not None

    def test_deleted_node_vanishes_from_tree(self):
        _upsert("/feedback/vanish", title="V", body="B")
        import diary_server
        diary_server.memory_delete("/feedback/vanish")
        result = diary_server.memory_tree("/feedback")
        assert "vanish" not in result

    def test_deleted_node_vanishes_from_search(self):
        _upsert("/feedback/search-del-xxx", title="UniqueSearchTerm999", body="B")
        import diary_server
        diary_server.memory_delete("/feedback/search-del-xxx")
        result = diary_server.memory_search("UniqueSearchTerm999")
        # The node path should not appear in results; the query term may appear
        # in the "nothing found" message, so we check for the path specifically.
        assert "/feedback/search-del-xxx" not in result

    def test_upsert_revives_tombstone(self):
        _upsert("/feedback/revive", title="Old", body="Old body")
        import diary_server
        diary_server.memory_delete("/feedback/revive")
        # Revive by upserting again
        _upsert("/feedback/revive", title="Revived", body="New body")
        node = _get_node("/feedback/revive")
        assert node["deleted_at"] is None
        assert node["title"] == "Revived"


# ===========================================================================
# 5. Two-tier: extracted hidden by default, visible with include_extracted
# ===========================================================================

class TestTwoTier:
    def test_extracted_hidden_from_default_search(self):
        import diary_server
        diary_server.memory_save_extracted(
            "/user/tier2-hidden", "UniqueExtractedABC", "unique body XYZ"
        )
        result = diary_server.memory_search("UniqueExtractedABC")
        # Not in main results
        assert "/user/tier2-hidden" not in result or "tier2-hidden" not in result.split("Hinweis")[0]

    def test_extracted_count_hint_appears(self):
        import diary_server
        diary_server.memory_save_extracted(
            "/user/tier2-hint", "UniqueExtractedHINT", "hint body"
        )
        result = diary_server.memory_search("UniqueExtractedHINT")
        # The hint about extracted results should appear
        assert "auto-extrahierten" in result or "weitere Treffer" in result

    def test_extracted_visible_with_include_extracted(self):
        import diary_server
        diary_server.memory_save_extracted(
            "/user/tier2-vis", "ExtractedVisTitle", "extracted vis body"
        )
        result = diary_server.memory_search("ExtractedVisTitle", include_extracted=True)
        assert "tier2-vis" in result

    def test_curated_always_visible(self):
        _upsert("/user/curated-always", title="CuratedAlwaysTitle", body="curated content")
        import diary_server
        result = diary_server.memory_search("CuratedAlwaysTitle")
        assert "curated-always" in result


# ===========================================================================
# 6. Ranking — importance + expired filtering
# ===========================================================================

class TestRanking:
    def test_higher_importance_outranks_lower(self):
        """Two nodes with near-identical content: the high-importance one ranks first."""
        import diary_server
        with patch("diary_embed.embed", return_value=None):
            _upsert("/user/rank-low", title="RankingTestXXX", body="same content ranking", importance=0.1)
            _upsert("/user/rank-high", title="RankingTestXXX High", body="same content ranking", importance=0.9)
        result = diary_server.memory_search("RankingTestXXX")
        pos_high = result.find("rank-high")
        pos_low = result.find("rank-low")
        assert pos_high != -1 and pos_low != -1, "Both results should appear"
        assert pos_high < pos_low, "High importance should rank before low importance"

    def test_expired_hidden_by_default(self):
        import diary_server
        past = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        _upsert("/user/expired-hidden", title="ExpiredXYZ", body="expired content", valid_until=past)
        result = diary_server.memory_search("ExpiredXYZ")
        assert "expired-hidden" not in result

    def test_expired_shown_with_include_expired(self):
        import diary_server
        past = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        _upsert("/user/expired-shown", title="ExpiredShownABC", body="expired content 2", valid_until=past)
        result = diary_server.memory_search("ExpiredShownABC", include_expired=True)
        assert "expired-shown" in result


# ===========================================================================
# 7. Hybrid search + FTS-only fallback when embeddings unavailable
# ===========================================================================

class TestHybridSearch:
    def test_fts_returns_results(self):
        """FTS-only search (embed=None) should still return results."""
        with patch("diary_embed.embed", return_value=None):
            _upsert("/user/fts-only-node", title="FTSOnlyTitle", body="full text search test content")
        import diary_server
        with patch("diary_embed.embed", return_value=None):
            result = diary_server.memory_search("FTSOnlyTitle")
        assert "fts-only-node" in result

    def test_hybrid_search_returns_results(self):
        """With embeddings available, hybrid search should return results."""
        fake_vec = [float(i % 10) / 10 for i in range(384)]
        with patch("diary_embed.embed", return_value=fake_vec):
            _upsert("/user/hybrid-node", title="HybridSearchTitle", body="hybrid test content")
        import diary_server
        # Use FTS-only to verify (semantic path depends on pgvector which may vary)
        result = diary_server.memory_search("HybridSearchTitle")
        assert "hybrid-node" in result

    def test_graceful_fallback_when_embed_returns_none(self):
        """Monkeypatch embed to return None — search should not crash, fall back to FTS."""
        _upsert("/user/fallback-node", title="FallbackFTSSearch", body="fallback content test")
        import diary_server
        with patch("diary_embed.embed", return_value=None):
            result = diary_server.memory_search("FallbackFTSSearch")
        # Should find something or at least not raise
        assert isinstance(result, str)
        assert "fallback-node" in result

    def test_no_crash_when_both_fts_and_embed_miss(self):
        """When no FTS match and embed=None, should return a 'nothing found' message."""
        import diary_server
        with patch("diary_embed.embed", return_value=None):
            result = diary_server.memory_search("TermThatDefinitelyDoesNotExist99999")
        assert isinstance(result, str)


# ===========================================================================
# 8. Sync round-trip
# ===========================================================================

class TestSync:
    def _reset_remote(self):
        """Clear memory_nodes on the remote test DB."""
        remote_url = os.environ.get("DIARY_REMOTE_URL")
        conn = psycopg.connect(remote_url, row_factory=dict_row, autocommit=False)
        try:
            conn.execute("DELETE FROM memory_nodes")
            conn.commit()
        finally:
            conn.close()
        # Re-init remote schema (re-seeds categories)
        import diary_db
        orig = os.environ.get("DIARY_DATABASE_URL")
        os.environ["DIARY_DATABASE_URL"] = remote_url
        importlib.reload(diary_db)
        diary_db.init_db()
        if orig:
            os.environ["DIARY_DATABASE_URL"] = orig
        else:
            os.environ.pop("DIARY_DATABASE_URL", None)
        importlib.reload(diary_db)

    def test_push_pull_converges(self):
        """Create a local node, sync, remote should have it; modify on remote, sync back."""
        self._reset_remote()
        _upsert("/user/sync-converge", title="SyncTest", body="initial body")
        import diary_server
        result = diary_server.memory_sync()
        assert "fehlgeschlagen" not in result.lower()

        # Verify remote has the node
        remote_url = os.environ.get("DIARY_REMOTE_URL")
        rconn = psycopg.connect(remote_url, row_factory=dict_row)
        try:
            row = rconn.execute(
                "SELECT title FROM memory_nodes WHERE path = %s AND deleted_at IS NULL",
                ("/user/sync-converge",),
            ).fetchone()
            assert row is not None, "Node should have been pushed to remote"
            assert row["title"] == "SyncTest"
        finally:
            rconn.close()

    def test_delete_propagates_and_does_not_resurrect(self):
        """Delete a node locally, sync, remote should tombstone it; re-sync should not resurrect."""
        self._reset_remote()
        _upsert("/user/sync-delete", title="DeletePropagation", body="to be deleted")
        import diary_server
        # First sync: push the node
        diary_server.memory_sync()
        # Delete locally
        diary_server.memory_delete("/user/sync-delete")
        # Second sync: propagate tombstone
        diary_server.memory_sync()

        # Remote should have the node tombstoned
        remote_url = os.environ.get("DIARY_REMOTE_URL")
        rconn = psycopg.connect(remote_url, row_factory=dict_row)
        try:
            row = rconn.execute(
                "SELECT deleted_at FROM memory_nodes WHERE path = %s",
                ("/user/sync-delete",),
            ).fetchone()
            assert row is not None, "Tombstone row should exist on remote"
            assert row["deleted_at"] is not None, "Remote node should be tombstoned"
        finally:
            rconn.close()

        # Third sync: idempotent, should not resurrect
        result = diary_server.memory_sync()
        local_node = _get_node("/user/sync-delete")
        assert local_node["deleted_at"] is not None, "Local node must remain tombstoned after re-sync"

    def test_sync_is_idempotent(self):
        """Second sync with no changes should report 0 pushed / 0 pulled."""
        self._reset_remote()
        _upsert("/user/idempotent-sync", title="Idempotent", body="sync body")
        import diary_server
        diary_server.memory_sync()  # First sync: push
        result2 = diary_server.memory_sync()  # Second sync: nothing to do
        # The result should show 0 pushed and 0 pulled
        assert "0 gepusht" in result2 or ("gepusht" in result2 and "0 gepullt" in result2)

    def test_clean_sync_reports_no_conflict(self):
        """A normal sync (no concurrent edits) must NOT report any conflict."""
        self._reset_remote()
        _upsert("/user/clean-sync", title="Clean", body="no conflict here")
        import diary_server
        r1 = diary_server.memory_sync()  # establishes last_sync
        assert "Konflikt" not in r1
        # Edit only one side, sync again — last-write-wins, but no conflict.
        _upsert("/user/clean-sync", title="Clean", body="edited locally only")
        r2 = diary_server.memory_sync()
        assert "Konflikt" not in r2

    def test_concurrent_edit_reports_conflict(self):
        """Same path edited differently on BOTH sides between syncs => 1 reported conflict,
        and last-write-wins still converges both DBs to the newer edit."""
        self._reset_remote()
        path = "/user/conflict-node"
        _upsert(path, title="Conflict", body="base body")
        import diary_server

        # First sync establishes last_sync on both sides and propagates the base row.
        r1 = diary_server.memory_sync()
        assert "fehlgeschlagen" not in r1.lower()
        assert "Konflikt" not in r1

        # --- edit the SAME path differently on each side, AFTER last_sync ---
        # Remote edit first (older updated_at).
        remote_url = os.environ.get("DIARY_REMOTE_URL")
        rconn = psycopg.connect(remote_url, row_factory=dict_row)
        try:
            rconn.execute(
                "UPDATE memory_nodes SET body = %s, updated_at = now() WHERE path = %s",
                ("REMOTE edit", path),
            )
            rconn.commit()
        finally:
            rconn.close()

        # Ensure the local edit has a strictly newer updated_at so local wins.
        time.sleep(0.05)
        lconn = _local_conn()
        try:
            lconn.execute(
                "UPDATE memory_nodes SET body = %s, updated_at = now() WHERE path = %s",
                ("LOCAL edit", path),
            )
            lconn.commit()
        finally:
            lconn.close()

        # Second sync: must detect exactly one conflict (local newer => local wins).
        r2 = diary_server.memory_sync()
        assert "fehlgeschlagen" not in r2.lower()
        assert "1 Konflikt" in r2, r2
        assert path in r2
        assert "local gewann" in r2, r2

        # Last-write-wins converged: both sides hold the LOCAL edit.
        local_body = _get_node(path)["body"]
        assert local_body == "LOCAL edit"
        rconn = psycopg.connect(remote_url, row_factory=dict_row)
        try:
            remote_body = rconn.execute(
                "SELECT body FROM memory_nodes WHERE path = %s", (path,)
            ).fetchone()["body"]
        finally:
            rconn.close()
        assert remote_body == "LOCAL edit"


# ===========================================================================
# 9. Extracted lifecycle
# ===========================================================================

class TestExtractedLifecycle:
    def test_extracted_default_valid_until_set(self):
        """memory_save_extracted should set valid_until ~90 days out."""
        import diary_server
        diary_server.memory_save_extracted("/user/ext-ttl", "Extracted TTL", "body")
        node = _get_node("/user/ext-ttl")
        assert node["valid_until"] is not None
        # Should be in the future (compare timezone-aware vs timezone-aware)
        from datetime import timezone
        assert node["valid_until"].astimezone() > datetime.now().astimezone()

    def test_promote_clears_valid_until(self):
        """memory_promote should clear valid_until and set origin=curated."""
        import diary_server
        diary_server.memory_save_extracted("/user/ext-promote", "Promote Me", "body")
        diary_server.memory_promote("/user/ext-promote", importance=0.8)
        node = _get_node("/user/ext-promote")
        assert node["origin"] == "curated"
        assert node["valid_until"] is None
        assert abs(node["importance"] - 0.8) < 0.01

    def test_prune_tombstones_expired_extracted(self):
        """memory_prune_extracted should tombstone extracted nodes with expired valid_until."""
        import diary_server
        # Insert an extracted node with a past valid_until directly
        past = (datetime.now() - timedelta(days=1)).isoformat()
        with _local_conn() as conn:
            conn.execute(
                """INSERT INTO memory_nodes (path, slug, type, title, body, origin, valid_until)
                   VALUES (%s, %s, 'note', %s, %s, 'extracted', %s)""",
                ("/user/ext-expired", "ext-expired", "Expired Extracted", "body", past),
            )
        result = diary_server.memory_prune_extracted(expired_only=True)
        assert "tombstonet" in result
        node = _get_node("/user/ext-expired")
        assert node["deleted_at"] is not None

    def test_prune_does_not_touch_curated(self):
        """memory_prune_extracted should never tombstone curated nodes."""
        _upsert("/user/ext-safe-curated", title="Curated Safe", body="should not be pruned")
        import diary_server
        diary_server.memory_prune_extracted(expired_only=False)
        node = _get_node("/user/ext-safe-curated")
        assert node["deleted_at"] is None, "Curated node should not be pruned"

    def test_prune_does_not_touch_non_expired_extracted(self):
        """memory_prune_extracted(expired_only=True) should not touch future-valid extracted nodes."""
        import diary_server
        future = (datetime.now() + timedelta(days=60)).strftime("%Y-%m-%d")
        diary_server.memory_save_extracted("/user/ext-future", "Future Extracted", "body")
        # Override valid_until to something in future
        with _local_conn() as conn:
            conn.execute(
                "UPDATE memory_nodes SET valid_until = %s WHERE path = %s",
                (future, "/user/ext-future"),
            )
        diary_server.memory_prune_extracted(expired_only=True)
        node = _get_node("/user/ext-future")
        assert node["deleted_at"] is None, "Non-expired extracted node should not be pruned"


# ===========================================================================
# 10. Project config
# ===========================================================================

class TestProjectConfig:
    def test_set_and_get_project_config_round_trip(self):
        import diary_server
        result = diary_server.memory_set_project_config("test-proj-config", auto_extract=True)
        assert "auto_extract" in result
        # Get it back
        got = diary_server.memory_get_project_config("test-proj-config")
        assert "true" in got.lower() or '"auto_extract": true' in got

    def test_set_auto_extract_false(self):
        import diary_server
        diary_server.memory_set_project_config("test-proj-off", auto_extract=False)
        got = diary_server.memory_get_project_config("test-proj-off")
        assert "false" in got.lower() or '"auto_extract": false' in got

    def test_project_node_created_automatically(self):
        """Setting config on a non-existent project slug should auto-create the node."""
        import diary_server
        diary_server.memory_set_project_config("brand-new-project")
        node = _get_node("/projects/brand-new-project")
        assert node is not None
        assert node["deleted_at"] is None

    def test_get_nonexistent_project_config(self):
        import diary_server
        result = diary_server.memory_get_project_config("nonexistent-xyz-proj")
        assert "noch keinen" in result or "nicht gefunden" in result.lower()


# ===========================================================================
# 11. Project dir aliases + slug resolver
# ===========================================================================

class TestProjectDirAliases:
    """Tests for memory_set_project_dir, memory_unset_project_dir and the slug resolver."""

    def test_set_project_dir_creates_node_and_stores_dir(self):
        import diary_server
        result = diary_server.memory_set_project_dir("alias-proj", "/srv/projects/alias-proj")
        assert "alias-proj" in result
        node = _get_node("/projects/alias-proj")
        assert node is not None and node["deleted_at"] is None
        cfg = node["config"] or {}
        assert "/srv/projects/alias-proj" in (cfg.get("dirs") or [])

    def test_set_project_dir_is_idempotent(self):
        import diary_server
        diary_server.memory_set_project_dir("idem-proj", "/srv/idem")
        result2 = diary_server.memory_set_project_dir("idem-proj", "/srv/idem")
        # Second call: already registered message
        assert "bereits" in result2 or "hinzugefügt" in result2
        node = _get_node("/projects/idem-proj")
        dirs = (node["config"] or {}).get("dirs") or []
        assert dirs.count("/srv/idem") == 1

    def test_set_project_dir_multiple_dirs(self):
        import diary_server
        diary_server.memory_set_project_dir("multi-dir-proj", "/srv/a")
        diary_server.memory_set_project_dir("multi-dir-proj", "/srv/b")
        node = _get_node("/projects/multi-dir-proj")
        dirs = (node["config"] or {}).get("dirs") or []
        assert "/srv/a" in dirs
        assert "/srv/b" in dirs

    def test_set_project_dir_absolute_path_required(self):
        import diary_server
        result = diary_server.memory_set_project_dir("fail-proj", "relative/path")
        assert "absolut" in result.lower() or "fehler" in result.lower()

    def test_unset_project_dir_removes_entry(self):
        import diary_server
        diary_server.memory_set_project_dir("unset-proj", "/srv/remove-me")
        result = diary_server.memory_unset_project_dir("unset-proj", "/srv/remove-me")
        assert "entfernt" in result
        node = _get_node("/projects/unset-proj")
        dirs = (node["config"] or {}).get("dirs") or []
        assert "/srv/remove-me" not in dirs

    def test_unset_nonexistent_dir_reports_not_registered(self):
        import diary_server
        diary_server.memory_set_project_dir("unset2-proj", "/srv/keep")
        result = diary_server.memory_unset_project_dir("unset2-proj", "/srv/ghost")
        assert "nicht" in result.lower()

    def test_get_project_config_shows_dirs(self):
        import diary_server
        diary_server.memory_set_project_dir("config-dirs-proj", "/srv/shown")
        got = diary_server.memory_get_project_config("config-dirs-proj")
        assert "/srv/shown" in got

    def test_set_project_dir_preserves_auto_extract(self):
        """Adding a dir must not clobber existing config keys like auto_extract."""
        import diary_server
        diary_server.memory_set_project_config("preserve-proj", auto_extract=True)
        diary_server.memory_set_project_dir("preserve-proj", "/srv/preserve")
        got = diary_server.memory_get_project_config("preserve-proj")
        assert "true" in got.lower()
        assert "/srv/preserve" in got


class TestSlugResolver:
    """Tests for the scripts/_slug_resolve.py resolver."""

    def _resolver(self):
        import importlib, sys
        from pathlib import Path
        scripts_dir = str(Path(__file__).resolve().parent.parent / "scripts")
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)
        import _slug_resolve
        importlib.reload(_slug_resolve)
        return _slug_resolve.slug_from_cwd

    def test_exact_match(self):
        import diary_server
        diary_server.memory_set_project_dir("exact-resolve", "/exact/path/myproject")
        resolver = self._resolver()
        assert resolver("/exact/path/myproject") == "exact-resolve"

    def test_subdirectory_prefix_match(self):
        import diary_server
        diary_server.memory_set_project_dir("prefix-resolve", "/prefix/proj")
        resolver = self._resolver()
        # A subdir should resolve to the same project
        assert resolver("/prefix/proj/src/deep/nested") == "prefix-resolve"

    def test_longer_prefix_wins(self):
        """When two projects have prefix-matching dirs, the longer (more specific) one wins."""
        import diary_server
        diary_server.memory_set_project_dir("outer-resolve", "/nested/outer")
        diary_server.memory_set_project_dir("inner-resolve", "/nested/outer/inner")
        resolver = self._resolver()
        assert resolver("/nested/outer/inner/src") == "inner-resolve"

    def test_exact_match_beats_prefix(self):
        """Exact match takes priority even if another project has a longer prefix."""
        import diary_server
        diary_server.memory_set_project_dir("exact-beats", "/some/dir")
        diary_server.memory_set_project_dir("prefix-beats", "/some/dir/sub")
        resolver = self._resolver()
        assert resolver("/some/dir") == "exact-beats"

    def test_fallback_to_basename(self):
        """An unknown directory falls back to basename slugification."""
        resolver = self._resolver()
        result = resolver("/home/user/My Cool Project")
        assert result == "my-cool-project"

    def test_trailing_slash_stripped(self):
        import diary_server
        diary_server.memory_set_project_dir("trailing-resolve", "/trailing/dir")
        resolver = self._resolver()
        assert resolver("/trailing/dir/") == "trailing-resolve"

    def test_no_dirs_key_falls_back(self):
        """A project node without config.dirs should not interfere with fallback."""
        import diary_server
        # Create a project node with no dirs
        diary_server.memory_set_project_config("nodirs-proj", auto_extract=False)
        resolver = self._resolver()
        # cwd does not match anything; fallback should be basename of this unknown path
        result = resolver("/completely/unknown/path/nodirs-proj-other")
        assert result == "nodirs-proj-other"


# ===========================================================================
# 13. Knowledge-graph tools (graphify-inspired): explain, path, stats,
#     inferred links, query-graph, report
# ===========================================================================

class TestKnowledgeGraph:
    def _node_with_vec(self, path, vec, title="T", body="B"):
        import diary_server
        with patch("diary_embed.embed", return_value=vec):
            diary_server.memory_upsert(path=path, title=title, body=body, importance=0.5)

    def test_link_defaults_to_explicit_origin(self):
        _upsert("/user/kg-a", title="A", body="a")
        _upsert("/user/kg-b", title="B", body="b")
        import diary_server
        diary_server.memory_link("/user/kg-a", "/user/kg-b")
        conn = _local_conn()
        try:
            row = conn.execute(
                "SELECT ml.link_origin AS origin FROM memory_links ml JOIN memory_nodes n ON ml.from_id = n.id "
                "WHERE n.path = %s",
                ("/user/kg-a",),
            ).fetchone()
            assert row["origin"] == "explicit"
        finally:
            conn.close()

    def test_explain_reports_degree_and_tag(self):
        _upsert("/user/kg-e1", title="E1", body="e1")
        _upsert("/user/kg-e2", title="E2", body="e2")
        import diary_server
        diary_server.memory_link("/user/kg-e1", "/user/kg-e2", rel_type="supports")
        result = diary_server.memory_explain("/user/kg-e1")
        assert "Degree:      1" in result
        assert "EXPLICIT" in result
        assert "kg-e2" in result

    def test_explain_not_found(self):
        import diary_server
        result = diary_server.memory_explain("/user/does-not-exist-kg")
        assert "nicht gefunden" in result

    def test_explain_orphan_has_zero_degree(self):
        _upsert("/user/kg-orphan", title="O", body="o")
        import diary_server
        result = diary_server.memory_explain("/user/kg-orphan")
        assert "Degree:      0" in result
        assert "Waisen-Node" in result

    def test_path_direct_link(self):
        _upsert("/user/kg-p1", title="P1", body="p1")
        _upsert("/user/kg-p2", title="P2", body="p2")
        import diary_server
        diary_server.memory_link("/user/kg-p1", "/user/kg-p2", rel_type="requires")
        result = diary_server.memory_path("/user/kg-p1", "/user/kg-p2")
        assert "1 Hop" in result
        assert "requires" in result

    def test_path_multi_hop(self):
        _upsert("/user/kg-m1", title="M1", body="m1")
        _upsert("/user/kg-m2", title="M2", body="m2")
        _upsert("/user/kg-m3", title="M3", body="m3")
        import diary_server
        diary_server.memory_link("/user/kg-m1", "/user/kg-m2")
        diary_server.memory_link("/user/kg-m2", "/user/kg-m3")
        result = diary_server.memory_path("/user/kg-m1", "/user/kg-m3")
        assert "2 Hops" in result

    def test_path_no_route_found(self):
        _upsert("/user/kg-iso1", title="I1", body="i1")
        _upsert("/user/kg-iso2", title="I2", body="i2")
        import diary_server
        result = diary_server.memory_path("/user/kg-iso1", "/user/kg-iso2")
        assert "Kein Pfad" in result

    def test_path_unknown_node(self):
        import diary_server
        result = diary_server.memory_path("/user/nope-a", "/user/nope-b")
        assert "nicht gefunden" in result

    def test_path_same_node(self):
        _upsert("/user/kg-same", title="S", body="s")
        import diary_server
        result = diary_server.memory_path("/user/kg-same", "/user/kg-same")
        assert "derselbe Node" in result

    def test_graph_stats_god_node_and_orphan(self):
        _upsert("/user/kg-hub", title="Hub", body="hub")
        _upsert("/user/kg-leaf1", title="L1", body="l1")
        _upsert("/user/kg-leaf2", title="L2", body="l2")
        _upsert("/user/kg-lonely", title="Lonely", body="lonely")
        import diary_server
        diary_server.memory_link("/user/kg-hub", "/user/kg-leaf1")
        diary_server.memory_link("/user/kg-hub", "/user/kg-leaf2")
        result = diary_server.memory_graph_stats(top_n=5)
        assert "kg-hub" in result.split("Waisen")[0]
        assert "kg-lonely" in result

    def test_infer_links_creates_above_threshold(self):
        half = 192
        vec_a = [1.0] * half + [0.0] * half
        vec_b = [1.0] * half + [0.0] * half
        vec_c = [0.0] * half + [1.0] * half
        self._node_with_vec("/user/kg-inf-a", vec_a)
        self._node_with_vec("/user/kg-inf-b", vec_b)
        self._node_with_vec("/user/kg-inf-c", vec_c)
        import diary_server
        result = diary_server.memory_infer_links(threshold=0.9)
        assert "kg-inf-a" in result and "kg-inf-b" in result
        assert "kg-inf-c" not in result
        conn = _local_conn()
        try:
            row = conn.execute(
                "SELECT ml.link_origin AS origin FROM memory_links ml "
                "JOIN memory_nodes a ON ml.from_id = a.id JOIN memory_nodes b ON ml.to_id = b.id "
                "WHERE (a.path = %s AND b.path = %s) OR (a.path = %s AND b.path = %s)",
                ("/user/kg-inf-a", "/user/kg-inf-b", "/user/kg-inf-b", "/user/kg-inf-a"),
            ).fetchone()
            assert row is not None
            assert row["origin"] == "inferred"
        finally:
            conn.close()

    def test_infer_links_skips_existing_pair(self):
        half = 192
        vec = [1.0] * half + [0.0] * half
        self._node_with_vec("/user/kg-skip-a", vec)
        self._node_with_vec("/user/kg-skip-b", vec)
        import diary_server
        diary_server.memory_link("/user/kg-skip-a", "/user/kg-skip-b")
        result = diary_server.memory_infer_links(threshold=0.9)
        assert "Keine neuen" in result

    def test_infer_links_respects_threshold(self):
        half = 192
        vec_a = [1.0] * half + [0.0] * half
        vec_b = [0.0] * half + [1.0] * half
        self._node_with_vec("/user/kg-thr-a", vec_a)
        self._node_with_vec("/user/kg-thr-b", vec_b)
        import diary_server
        result = diary_server.memory_infer_links(threshold=0.9)
        assert "Keine neuen" in result

    def test_query_graph_expands_neighbors(self):
        half = 192
        vec = [1.0] * half + [0.0] * half
        self._node_with_vec("/user/kg-q1", vec, title="QueryHitTitle", body="query hit body")
        _upsert("/user/kg-q2", title="Neighbor", body="neighbor body")
        import diary_server
        diary_server.memory_link("/user/kg-q1", "/user/kg-q2", rel_type="related")
        with patch("diary_embed.embed", return_value=vec):
            result = diary_server.memory_query_graph("query hit body")
        assert "kg-q1" in result
        assert "kg-q2" in result

    def test_query_graph_unavailable_embed_falls_back_gracefully(self):
        import diary_server
        with patch("diary_embed.embed", return_value=None):
            result = diary_server.memory_query_graph("whatever")
        assert "memory_search" in result

    def test_report_contains_sections(self):
        _upsert("/user/kg-r1", title="R1", body="r1")
        _upsert("/user/kg-r2", title="R2", body="r2")
        import diary_server
        diary_server.memory_link("/user/kg-r1", "/user/kg-r2")
        result = diary_server.memory_report()
        assert result.startswith("# Memory Graph Report")
        assert "Kernkonzepte" in result
        assert "Cluster" in result
        assert "Waisen" in result

    def test_report_empty_scope(self):
        import diary_server
        result = diary_server.memory_report("/projects/nonexistent-scope-xyz")
        assert "Keine Nodes" in result

    def test_path_reports_true_direction_when_traversed_backward(self):
        """memory_link(A, B, 'requires') means A requires B — memory_path(B, A) must
        still report that direction, not print it as if B requires A."""
        _upsert("/user/kg-dir-a", title="A", body="a")
        _upsert("/user/kg-dir-b", title="B", body="b")
        import diary_server
        diary_server.memory_link("/user/kg-dir-a", "/user/kg-dir-b", rel_type="requires")
        forward = diary_server.memory_path("/user/kg-dir-a", "/user/kg-dir-b")
        backward = diary_server.memory_path("/user/kg-dir-b", "/user/kg-dir-a")
        # Both directions must render the TRUE stored relationship (A requires B) —
        # either as "A --requires--> B" or, when traversed back-to-front, as the
        # equivalent "B <--requires-- A". Neither may ever claim "B requires A".
        true_rel_forward = "/user/kg-dir-a --requires--> /user/kg-dir-b"
        true_rel_backward = "/user/kg-dir-b <--requires-- /user/kg-dir-a"
        false_rel = "/user/kg-dir-b --requires--> /user/kg-dir-a"
        assert true_rel_forward in forward
        assert true_rel_forward in backward or true_rel_backward in backward
        assert false_rel not in forward and false_rel not in backward

    def test_link_promotes_inferred_to_explicit_for_symmetric_type(self):
        """A memory_infer_links-created 'related' edge, later confirmed via memory_link,
        must be promoted to origin='explicit' rather than left as a permanent
        second/duplicate 'inferred' edge."""
        half = 192
        vec = [1.0] * half + [0.0] * half
        import diary_server
        with patch("diary_embed.embed", return_value=vec):
            diary_server.memory_upsert(path="/user/kg-promote-a", title="A", body="a", importance=0.5)
            diary_server.memory_upsert(path="/user/kg-promote-b", title="B", body="b", importance=0.5)
        diary_server.memory_infer_links(threshold=0.9)

        conn = _local_conn()
        try:
            before = conn.execute(
                "SELECT ml.link_origin FROM memory_links ml "
                "JOIN memory_nodes a ON ml.from_id = a.id JOIN memory_nodes b ON ml.to_id = b.id "
                "WHERE (a.path = %s AND b.path = %s) OR (a.path = %s AND b.path = %s)",
                ("/user/kg-promote-a", "/user/kg-promote-b", "/user/kg-promote-b", "/user/kg-promote-a"),
            ).fetchone()
            assert before["link_origin"] == "inferred"
        finally:
            conn.close()

        # Confirm the same pair explicitly, possibly in the opposite direction to
        # however memory_infer_links happened to store it.
        diary_server.memory_link("/user/kg-promote-a", "/user/kg-promote-b", rel_type="related")

        conn = _local_conn()
        try:
            rows = conn.execute(
                "SELECT ml.link_origin FROM memory_links ml "
                "JOIN memory_nodes a ON ml.from_id = a.id JOIN memory_nodes b ON ml.to_id = b.id "
                "WHERE ((a.path = %s AND b.path = %s) OR (a.path = %s AND b.path = %s)) "
                "AND ml.rel_type = 'related'",
                ("/user/kg-promote-a", "/user/kg-promote-b", "/user/kg-promote-b", "/user/kg-promote-a"),
            ).fetchall()
            assert len(rows) == 1, "must promote the existing edge, not add a duplicate"
            assert rows[0]["link_origin"] == "explicit"
        finally:
            conn.close()

    def test_link_directional_types_stay_direction_specific(self):
        """Unlike symmetric types, 'requires' A->B and B->A are different facts and
        must remain two distinct edges."""
        _upsert("/user/kg-reqdir-a", title="A", body="a")
        _upsert("/user/kg-reqdir-b", title="B", body="b")
        import diary_server
        diary_server.memory_link("/user/kg-reqdir-a", "/user/kg-reqdir-b", rel_type="requires")
        diary_server.memory_link("/user/kg-reqdir-b", "/user/kg-reqdir-a", rel_type="requires")
        conn = _local_conn()
        try:
            rows = conn.execute(
                "SELECT from_id, to_id FROM memory_links WHERE rel_type = 'requires'"
            ).fetchall()
            assert len(rows) == 2
        finally:
            conn.close()


# ===========================================================================
# 14. diary_embed helper guards (normalize/dot) — mirror cosine()'s safety
# ===========================================================================

class TestEmbedHelpers:
    def test_dot_returns_zero_for_length_mismatch(self):
        import diary_embed
        assert diary_embed.dot([1.0, 2.0], [1.0, 2.0, 3.0]) == 0.0

    def test_dot_returns_zero_for_empty_vectors(self):
        import diary_embed
        assert diary_embed.dot([], []) == 0.0
        assert diary_embed.dot([1.0], []) == 0.0

    def test_normalize_preserves_direction(self):
        import diary_embed
        unit = diary_embed.normalize([3.0, 4.0])
        assert abs(diary_embed.dot(unit, unit) - 1.0) < 1e-9

    def test_normalize_zero_vector_stays_zero(self):
        import diary_embed
        assert diary_embed.normalize([0.0, 0.0]) == [0.0, 0.0]


# ===========================================================================
# 15. Diary table sync (memory_sync_diary) — projects/milestones/tasks/logs/
#     reminders/wiki_pages/errors_solutions, matched by name/sync_id since their
#     SERIAL ids are independent per DB.
# ===========================================================================

def _reset_diary():
    """Truncate all diary tables on both local and remote test DBs so diary-sync
    tests are independent of each other and of tables seeded by other test classes."""
    for url in (os.environ.get("DIARY_DATABASE_URL"), os.environ.get("DIARY_REMOTE_URL")):
        conn = psycopg.connect(url, row_factory=dict_row)
        try:
            conn.execute(
                "TRUNCATE projects, milestones, tasks, logs, reminders, "
                "wiki_pages, errors_solutions RESTART IDENTITY CASCADE"
            )
            conn.commit()
        finally:
            conn.close()


class TestSyncDiary:
    def test_push_pull_converges_with_fk_resolution(self):
        """Create a project + milestone + task + log + reminder + wiki page +
        error/solution locally, sync, and verify they all land on the remote with
        their foreign keys correctly re-resolved (not the raw local integer ids,
        which are independent per DB)."""
        _reset_diary()
        import diary_server

        diary_server.add_project("diary-sync-proj", "in progress")
        diary_server.add_milestone("diary-sync-proj", "M1")
        diary_server.add_log_entry("diary-sync-proj", "hello log")
        diary_server.add_reminder("diary-sync-proj", "2027-01-01", "check in")
        diary_server.add_wiki_page("diary-sync-proj", "Home", "wiki content")
        diary_server.add_error_solution("diary-sync-proj", "boom", "fixed it")

        local_conn = _local_conn()
        try:
            m = local_conn.execute(
                "SELECT id FROM milestones WHERE project_id = "
                "(SELECT id FROM projects WHERE name = %s)",
                ("diary-sync-proj",),
            ).fetchone()
        finally:
            local_conn.close()
        diary_server.add_task("diary-sync-proj", m["id"], "T1")

        result = diary_server.memory_sync_diary()
        assert "fehlgeschlagen" not in result.lower(), result

        rconn = _remote_conn()
        try:
            rp = rconn.execute(
                "SELECT * FROM projects WHERE name = %s", ("diary-sync-proj",)
            ).fetchone()
            assert rp is not None
            assert rp["status"] == "in progress"

            rm = rconn.execute(
                "SELECT * FROM milestones WHERE project_id = %s", (rp["id"],)
            ).fetchone()
            assert rm is not None and rm["title"] == "M1"

            rt = rconn.execute(
                "SELECT * FROM tasks WHERE milestone_id = %s", (rm["id"],)
            ).fetchone()
            assert rt is not None and rt["title"] == "T1"

            rlog = rconn.execute(
                "SELECT * FROM logs WHERE project_id = %s", (rp["id"],)
            ).fetchall()
            assert any(l["entry"] == "hello log" for l in rlog)

            rrem = rconn.execute(
                "SELECT * FROM reminders WHERE project_id = %s", (rp["id"],)
            ).fetchone()
            assert rrem is not None and rrem["note"] == "check in"

            rwiki = rconn.execute(
                "SELECT * FROM wiki_pages WHERE project_id = %s", (rp["id"],)
            ).fetchone()
            assert rwiki is not None and rwiki["title"] == "Home"

            rerr = rconn.execute(
                "SELECT * FROM errors_solutions WHERE project_id = %s", (rp["id"],)
            ).fetchone()
            assert rerr is not None and rerr["error_msg"] == "boom"
        finally:
            rconn.close()

    def test_delete_propagates_and_does_not_resurrect(self):
        """Soft-deleting a milestone (which cascades to its tasks) must propagate
        as a tombstone on sync, and not be resurrected by a later sync."""
        _reset_diary()
        import diary_server

        diary_server.add_project("diary-sync-del", "")
        diary_server.add_milestone("diary-sync-del", "M-del")
        local_conn = _local_conn()
        try:
            m = local_conn.execute(
                "SELECT id FROM milestones WHERE project_id = "
                "(SELECT id FROM projects WHERE name = %s)",
                ("diary-sync-del",),
            ).fetchone()
        finally:
            local_conn.close()
        diary_server.add_task("diary-sync-del", m["id"], "T-del")

        diary_server.memory_sync_diary()  # push base state

        diary_server.delete_milestone("diary-sync-del", m["id"])
        diary_server.memory_sync_diary()  # propagate tombstone

        rconn = _remote_conn()
        try:
            rp = rconn.execute(
                "SELECT id FROM projects WHERE name = %s", ("diary-sync-del",)
            ).fetchone()
            rm = rconn.execute(
                "SELECT deleted_at FROM milestones WHERE project_id = %s", (rp["id"],)
            ).fetchone()
            assert rm is not None and rm["deleted_at"] is not None
            rt = rconn.execute(
                "SELECT deleted_at FROM tasks WHERE milestone_id IN "
                "(SELECT id FROM milestones WHERE project_id = %s)", (rp["id"],)
            ).fetchone()
            assert rt is not None and rt["deleted_at"] is not None
        finally:
            rconn.close()

        # Third sync must not resurrect the local tombstone.
        diary_server.memory_sync_diary()
        local_conn = _local_conn()
        try:
            lm = local_conn.execute(
                "SELECT deleted_at FROM milestones WHERE id = %s", (m["id"],)
            ).fetchone()
        finally:
            local_conn.close()
        assert lm["deleted_at"] is not None

    def test_delete_project_cascade_propagates_children_too(self):
        """Regression: delete_project() tombstones the project AND its children in
        the same transaction. Since projects sync before their children, the child
        push must resolve its (now-tombstoned) parent by name regardless of
        deleted_at — otherwise the parent lookup silently "fails" and the child's
        own tombstone never reaches the remote."""
        _reset_diary()
        import diary_server

        diary_server.add_project("diary-sync-del-cascade", "")
        diary_server.add_milestone("diary-sync-del-cascade", "M-cascade")
        local_conn = _local_conn()
        try:
            m = local_conn.execute(
                "SELECT id FROM milestones WHERE project_id = "
                "(SELECT id FROM projects WHERE name = %s)",
                ("diary-sync-del-cascade",),
            ).fetchone()
        finally:
            local_conn.close()
        diary_server.add_task("diary-sync-del-cascade", m["id"], "T-cascade")

        diary_server.memory_sync_diary()  # push base state

        diary_server.delete_project("diary-sync-del-cascade")
        result = diary_server.memory_sync_diary()  # propagate all tombstones at once
        assert "fehlgeschlagen" not in result.lower(), result

        rconn = _remote_conn()
        try:
            rp = rconn.execute(
                "SELECT deleted_at FROM projects WHERE name = %s",
                ("diary-sync-del-cascade",),
            ).fetchone()
            assert rp is not None and rp["deleted_at"] is not None, "project tombstone must propagate"

            rm = rconn.execute(
                "SELECT m.deleted_at FROM milestones m JOIN projects p ON m.project_id = p.id "
                "WHERE p.name = %s",
                ("diary-sync-del-cascade",),
            ).fetchone()
            assert rm is not None and rm["deleted_at"] is not None, (
                "milestone tombstone must propagate even though its parent project "
                "was tombstoned in the same sync"
            )

            rt = rconn.execute(
                "SELECT t.deleted_at FROM tasks t "
                "JOIN milestones m ON t.milestone_id = m.id JOIN projects p ON m.project_id = p.id "
                "WHERE p.name = %s",
                ("diary-sync-del-cascade",),
            ).fetchone()
            assert rt is not None and rt["deleted_at"] is not None, "task tombstone must propagate too"
        finally:
            rconn.close()

    def test_sync_is_idempotent(self):
        """A second sync with no changes must push/pull 0 rows for every table."""
        _reset_diary()
        import diary_server

        diary_server.add_project("diary-sync-idem", "")
        diary_server.add_milestone("diary-sync-idem", "M1")
        diary_server.memory_sync_diary()  # first sync: pushes everything

        result2 = diary_server.memory_sync_diary()
        assert "fehlgeschlagen" not in result2.lower(), result2
        assert "Gesamt: 0 gepusht, 0 gepullt" in result2, result2

    def test_pull_direction_also_resolves_fks(self):
        """A project + milestone created directly on the remote must pull down
        with a correctly resolved LOCAL milestone -> project link."""
        _reset_diary()
        import diary_server

        rconn = _remote_conn()
        try:
            rconn.execute(
                "INSERT INTO projects (name, status) VALUES (%s, %s)",
                ("diary-sync-pull", "remote-created"),
            )
            rp = rconn.execute(
                "SELECT id FROM projects WHERE name = %s", ("diary-sync-pull",)
            ).fetchone()
            rconn.execute(
                "INSERT INTO milestones (project_id, title) VALUES (%s, %s)",
                (rp["id"], "Remote Milestone"),
            )
            rconn.commit()
        finally:
            rconn.close()

        result = diary_server.memory_sync_diary()
        assert "fehlgeschlagen" not in result.lower(), result

        local_conn = _local_conn()
        try:
            lp = local_conn.execute(
                "SELECT id FROM projects WHERE name = %s", ("diary-sync-pull",)
            ).fetchone()
            assert lp is not None
            lm = local_conn.execute(
                "SELECT * FROM milestones WHERE project_id = %s", (lp["id"],)
            ).fetchone()
            assert lm is not None and lm["title"] == "Remote Milestone"
        finally:
            local_conn.close()


# ===========================================================================
# 16. memory_links sync — extension to memory_sync(), matched by
#     (from_path, to_path, rel_type) since link UUIDs are independent per DB.
# ===========================================================================

class TestMemoryLinksSync:
    def test_links_push_pull_converge(self):
        """A link created locally must sync to the remote, resolved against the
        remote's own (different) UUIDs for the same paths."""
        import diary_server

        _upsert("/user/link-a", title="A", body="a")
        _upsert("/user/link-b", title="B", body="b")
        diary_server.memory_link("/user/link-a", "/user/link-b", "related", "test note")

        result = diary_server.memory_sync()
        assert "fehlgeschlagen" not in result.lower(), result

        remote_url = os.environ.get("DIARY_REMOTE_URL")
        rconn = psycopg.connect(remote_url, row_factory=dict_row)
        try:
            row = rconn.execute(
                "SELECT ml.note, ml.rel_type FROM memory_links ml "
                "JOIN memory_nodes fn ON ml.from_id = fn.id "
                "JOIN memory_nodes tn ON ml.to_id = tn.id "
                "WHERE fn.path = %s AND tn.path = %s",
                ("/user/link-a", "/user/link-b"),
            ).fetchone()
            assert row is not None, "Link should have been pushed to remote"
            assert row["rel_type"] == "related"
            assert row["note"] == "test note"
        finally:
            rconn.close()

    def test_links_sync_is_idempotent(self):
        """A second sync with no link changes must report 0 pushed/pulled links."""
        import diary_server

        _upsert("/user/link-idem-a", title="A", body="a")
        _upsert("/user/link-idem-b", title="B", body="b")
        diary_server.memory_link("/user/link-idem-a", "/user/link-idem-b", "related")
        diary_server.memory_sync()  # first sync: pushes the link

        result2 = diary_server.memory_sync()
        assert "Links: 0 gepusht, 0 gepullt" in result2, result2


# ===========================================================================
# 17. Keyword-trigger auto-injection (v0.9.0) — deterministic, no embeddings/LLM.
#     memory_set_keywords() sets trigger_keywords; memory_check_triggers()
#     matches a text against them with strict word-boundary regex.
# ===========================================================================

class TestKeywordTriggers:
    def test_set_keywords_returns_confirmation(self):
        import diary_server

        _upsert("/user/kw-set", title="T", body="B")
        result = diary_server.memory_set_keywords("/user/kw-set", "npm plus admin, foo-bar")
        assert "npm plus admin" in result
        assert "foo-bar" in result

    def test_set_keywords_missing_node_reports_not_found(self):
        import diary_server

        result = diary_server.memory_set_keywords("/user/does-not-exist-kw", "x")
        assert "nicht gefunden" in result

    def test_empty_keywords_clears_them(self):
        import diary_server

        _upsert("/user/kw-clear", title="T", body="B")
        diary_server.memory_set_keywords("/user/kw-clear", "something")
        result = diary_server.memory_set_keywords("/user/kw-clear", "")
        assert "entfernt" in result
        node = _get_node("/user/kw-clear")
        assert node["trigger_keywords"] == []

    def test_check_triggers_exact_phrase_match(self):
        import diary_server

        _upsert("/user/kw-match", title="NPM Plus Admin Doku", body="So funktioniert es.")
        diary_server.memory_set_keywords("/user/kw-match", "npm plus admin")

        result = diary_server.memory_check_triggers("Wie richte ich NPM Plus Admin ein?")
        assert "/user/kw-match" in result
        assert "So funktioniert es." in result

    def test_check_triggers_is_case_insensitive(self):
        import diary_server

        _upsert("/user/kw-case", title="Case Test", body="Inhalt")
        diary_server.memory_set_keywords("/user/kw-case", "DeployTarget")

        result = diary_server.memory_check_triggers("bitte den deploytarget pruefen")
        assert "/user/kw-case" in result

    def test_check_triggers_no_match_returns_empty(self):
        import diary_server

        _upsert("/user/kw-nomatch", title="No Match", body="Inhalt")
        diary_server.memory_set_keywords("/user/kw-nomatch", "very-specific-term")

        result = diary_server.memory_check_triggers("Ein voellig unrelated Satz.")
        assert result == ""

    def test_check_triggers_rejects_substring_false_positive(self):
        """'npm plus admin' as a keyword must not match inside a longer run of
        similar-looking words — only the exact contiguous phrase counts."""
        import diary_server

        _upsert("/user/kw-substr", title="Substr Test", body="Inhalt")
        diary_server.memory_set_keywords("/user/kw-substr", "npm plus admin")

        result = diary_server.memory_check_triggers("adminpanel npm pluswert admin")
        assert result == ""

    def test_check_triggers_no_keywords_set_anywhere_returns_empty(self):
        import diary_server

        result = diary_server.memory_check_triggers("irgendein Text ohne Bezug")
        assert result == ""
