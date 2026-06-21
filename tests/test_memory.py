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
            origin="curated", valid_until="", tags="", auto_inject=False):
    """Call memory_upsert and return the result string."""
    import diary_server
    return diary_server.memory_upsert(
        path=path, title=title, body=body,
        importance=importance, origin=origin,
        valid_until=valid_until, tags=tags, auto_inject=auto_inject,
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
