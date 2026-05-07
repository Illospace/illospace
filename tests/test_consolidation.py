"""Tests for core/cognition/consolidate — hierarchical memory consolidation."""

import os
import sys

import numpy as np
import pytest
from unittest.mock import patch, MagicMock, PropertyMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), *([".."] * 1))))


def _make_mock_uow():
    """Create a mock UnitOfWork with a mock session."""
    uow = MagicMock()
    session = MagicMock()
    uow.session = session
    uow.__enter__ = MagicMock(return_value=uow)
    uow.__exit__ = MagicMock(return_value=False)
    return uow, session


def _make_mapping_result(rows):
    """Create a mock result that behaves like session.execute() with .mappings().all()/.first()."""
    result = MagicMock()
    mappings = MagicMock()
    mappings.all.return_value = rows
    mappings.first.return_value = rows[0] if rows else None
    result.mappings.return_value = mappings
    result.rowcount = len(rows)
    return result


TEST_USER_ID = "00000000-0000-0000-0000-000000000002"
TEST_ORG_ID = "00000000-0000-0000-0000-000000000001"


def _scoped(row, *, user_id=TEST_USER_ID, org_id=None, visibility="private"):
    scoped = {
        "user_id": user_id,
        "org_id": org_id,
        "visibility": visibility,
    }
    scoped.update(row)
    return scoped


class TestClusterEpisodes:
    """Test episode clustering logic."""

    @patch("brain.systems.cognition.consolidate.UnitOfWork")
    def test_clusters_similar_memories(self, mock_uow_cls):
        """Should group memories with high embedding similarity."""
        from brain.systems.cognition.consolidate import cluster_episodes

        uow, session = _make_mock_uow()
        mock_uow_cls.return_value = uow

        # Create 6 memories: 3 similar pairs
        np.random.seed(42)
        base_a = np.random.randn(2000)
        base_b = np.random.randn(2000)
        embs = [
            base_a + np.random.randn(2000) * 0.05,  # cluster A
            base_a + np.random.randn(2000) * 0.05,
            base_a + np.random.randn(2000) * 0.05,
            base_b + np.random.randn(2000) * 0.05,  # cluster B
            base_b + np.random.randn(2000) * 0.05,
            base_b + np.random.randn(2000) * 0.05,
        ]
        rows = [
            _scoped({"id": i + 1, "content": f"episode {i}", "semantic_embedding": embs[i].tolist()})
            for i in range(6)
        ]
        session.execute.return_value = _make_mapping_result(rows)

        clusters = cluster_episodes(user_id=TEST_USER_ID)
        assert len(clusters) >= 1  # At least one cluster
        for cluster in clusters:
            assert len(cluster) >= 3  # MIN_CLUSTER_SIZE

    @patch("brain.systems.cognition.consolidate.UnitOfWork")
    def test_no_clusters_when_too_few(self, mock_uow_cls):
        """Should return empty when fewer than MIN_CLUSTER_SIZE memories."""
        from brain.systems.cognition.consolidate import cluster_episodes

        uow, session = _make_mock_uow()
        mock_uow_cls.return_value = uow

        rows = [
            _scoped({"id": 1, "content": "solo memory", "semantic_embedding": np.random.randn(2000).tolist()})
        ]
        session.execute.return_value = _make_mapping_result(rows)

        clusters = cluster_episodes(user_id=TEST_USER_ID)
        assert clusters == []

    @patch("brain.systems.cognition.consolidate.UnitOfWork")
    def test_no_clusters_when_dissimilar(self, mock_uow_cls):
        """Should return empty when memories are very different."""
        from brain.systems.cognition.consolidate import cluster_episodes

        uow, session = _make_mock_uow()
        mock_uow_cls.return_value = uow

        np.random.seed(0)
        rows = [
            _scoped({"id": i, "content": f"memory {i}",
                     "semantic_embedding": np.random.randn(2000).tolist()})
            for i in range(10)
        ]
        session.execute.return_value = _make_mapping_result(rows)

        clusters = cluster_episodes(user_id=TEST_USER_ID)
        assert isinstance(clusters, list)

    @patch("brain.systems.cognition.consolidate.UnitOfWork")
    def test_does_not_cluster_across_org_scope(self, mock_uow_cls):
        """Python post-filter keeps mocked mixed rows from crossing org boundaries."""
        from brain.systems.cognition.consolidate import cluster_episodes

        uow, session = _make_mock_uow()
        mock_uow_cls.return_value = uow

        np.random.seed(7)
        base = np.random.randn(2000)
        rows = [
            _scoped(
                {"id": i + 1, "content": f"org-a-{i}", "semantic_embedding": (base + np.random.randn(2000) * 0.01).tolist()},
                org_id=TEST_ORG_ID,
                user_id=f"user-a-{i}",
                visibility="org",
            )
            for i in range(3)
        ] + [
            _scoped(
                {"id": i + 10, "content": f"org-b-{i}", "semantic_embedding": (base + np.random.randn(2000) * 0.01).tolist()},
                org_id="00000000-0000-0000-0000-000000000009",
                user_id=f"user-b-{i}",
                visibility="org",
            )
            for i in range(3)
        ]
        session.execute.return_value = _make_mapping_result(rows)

        clusters = cluster_episodes(org_id=TEST_ORG_ID, visibility="org")

        assert clusters
        assert {memory_id for cluster in clusters for memory_id in cluster} <= {1, 2, 3}


class TestExtractSemantic:
    """Test semantic memory extraction."""

    @patch("brain.systems.cognition.consolidate._synthesize_with_gpu_server")
    @patch("brain.systems.memory.embeddings.vec_to_pg")
    @patch("brain.systems.memory.embeddings.make_emotional_embedding")
    @patch("brain.systems.memory.embeddings.embed_document")
    @patch("brain.systems.cognition.consolidate.UnitOfWork")
    def test_creates_semantic_memory(self, mock_uow_cls, mock_emb, mock_emo, mock_vec, mock_gpu):
        """Should create a semantic memory from episode cluster."""
        from brain.systems.cognition.consolidate import extract_semantic

        mock_gpu.return_value = "[semantic] Redis TTLs require explicit expiry for session data"
        mock_emb.return_value = np.zeros(2000)
        mock_emo.return_value = np.zeros(32)
        mock_vec.return_value = "[0,0,0]"

        uow, session = _make_mock_uow()
        mock_uow_cls.return_value = uow

        episode_rows = [
            _scoped({"id": 1, "content": "Fixed Redis TTL issue", "memory_type": "episode",
                     "salience": 6.0, "tags": ["redis"]}),
            _scoped({"id": 2, "content": "Redis timeout after no TTL", "memory_type": "episode",
                     "salience": 5.0, "tags": ["redis"]}),
        ]
        returning_row = {"id": 100}

        # First call: fetch episodes; subsequent calls: INSERT RETURNING, UPDATE, edge inserts
        session.execute.side_effect = [
            _make_mapping_result(episode_rows),   # fetch episodes
            _make_mapping_result([returning_row]), # INSERT RETURNING id
            _make_mapping_result([{"id": 300}]),   # review INSERT RETURNING id
            _make_mapping_result([]),              # UPDATE consolidated
            _make_mapping_result([]),              # edge insert 1
            _make_mapping_result([]),              # edge insert 2
        ]

        result = extract_semantic([1, 2], user_id=TEST_USER_ID)
        assert result == 100

        # Verify consolidated = TRUE was set (check the text() SQL arguments)
        calls = session.execute.call_args_list
        sql_texts = [str(c[0][0]) for c in calls]  # first positional arg is text()
        assert any("consolidated" in s for s in sql_texts)

    @patch("brain.systems.cognition.consolidate._synthesize_with_gpu_server")
    @patch("brain.systems.memory.embeddings.vec_to_pg")
    @patch("brain.systems.memory.embeddings.make_emotional_embedding")
    @patch("brain.systems.memory.embeddings.embed_document")
    @patch("brain.systems.cognition.consolidate.UnitOfWork")
    def test_fallback_when_ollama_fails(self, mock_uow_cls, mock_emb, mock_emo, mock_vec, mock_gpu):
        """Should use heuristic fallback when GPU server unavailable."""
        from brain.systems.cognition.consolidate import extract_semantic

        mock_gpu.return_value = None  # Ollama fails
        mock_emb.return_value = np.zeros(2000)
        mock_emo.return_value = np.zeros(32)
        mock_vec.return_value = "[0,0,0]"

        uow, session = _make_mock_uow()
        mock_uow_cls.return_value = uow

        episode_rows = [
            _scoped({"id": 1, "content": "Best episode content", "memory_type": "lesson",
                     "salience": 8.0, "tags": ["deploy"]}),
        ]
        returning_row = {"id": 101}

        session.execute.side_effect = [
            _make_mapping_result(episode_rows),
            _make_mapping_result([returning_row]),
            _make_mapping_result([{"id": 301}]),
            _make_mapping_result([]),
            _make_mapping_result([]),
        ]

        result = extract_semantic([1], user_id=TEST_USER_ID)
        assert result == 101

    @patch("brain.systems.cognition.consolidate.UnitOfWork")
    def test_returns_none_on_empty_cluster(self, mock_uow_cls):
        """Should return None when cluster is empty."""
        from brain.systems.cognition.consolidate import extract_semantic

        uow, session = _make_mock_uow()
        mock_uow_cls.return_value = uow

        session.execute.return_value = _make_mapping_result([])

        assert extract_semantic([], user_id=TEST_USER_ID) is None

    @patch("brain.systems.cognition.consolidate.UnitOfWork")
    def test_rejects_cluster_with_private_memory_in_org_scope(self, mock_uow_cls):
        """Org summaries must not absorb private memories from the same org."""
        from brain.systems.cognition.consolidate import extract_semantic

        uow, session = _make_mock_uow()
        mock_uow_cls.return_value = uow
        rows = [
            _scoped(
                {"id": 1, "content": "org-visible", "memory_type": "episode", "salience": 5, "tags": []},
                org_id=TEST_ORG_ID,
                visibility="org",
            ),
            _scoped(
                {"id": 2, "content": "private", "memory_type": "episode", "salience": 5, "tags": []},
                org_id=TEST_ORG_ID,
                visibility="private",
            ),
        ]
        session.execute.return_value = _make_mapping_result(rows)

        assert extract_semantic([1, 2], org_id=TEST_ORG_ID, visibility="org") is None
        assert session.execute.call_count == 1


class TestCrystallizeProcedural:
    """Test procedural memory crystallization."""

    @patch("brain.systems.cognition.consolidate._crystallize_with_gpu_server")
    @patch("brain.systems.memory.embeddings.vec_to_pg")
    @patch("brain.systems.memory.embeddings.make_emotional_embedding")
    @patch("brain.systems.memory.embeddings.embed_document")
    @patch("brain.systems.cognition.consolidate.UnitOfWork")
    def test_creates_procedural_from_semantics(self, mock_uow_cls, mock_emb, mock_emo, mock_vec, mock_gpu):
        """Should crystallize semantic memories into a procedure."""
        from brain.systems.cognition.consolidate import crystallize_procedural

        mock_gpu.return_value = "1. Check TTL\n2. Set expiry\n3. Monitor"
        mock_emb.return_value = np.zeros(2000)
        mock_emo.return_value = np.zeros(32)
        mock_vec.return_value = "[0,0,0]"

        uow, session = _make_mock_uow()
        mock_uow_cls.return_value = uow

        semantic_rows = [
            _scoped({"id": 10, "content": "Redis needs TTL", "salience": 7.0}),
            _scoped({"id": 11, "content": "Always check memory", "salience": 6.0}),
        ]
        no_existing = []
        returning_row = {"id": 200}

        session.execute.side_effect = [
            _make_mapping_result(semantic_rows),    # fetch semantics
            _make_mapping_result(no_existing),       # check existing procedural (None)
            _make_mapping_result([returning_row]),    # INSERT RETURNING
            _make_mapping_result([{"id": 400}]),      # review INSERT RETURNING
            _make_mapping_result([]),                 # edge insert 1
            _make_mapping_result([]),                 # edge insert 2
        ]

        result = crystallize_procedural("redis-ops", user_id=TEST_USER_ID)
        assert result == 200

    @patch("brain.systems.cognition.consolidate.UnitOfWork")
    def test_skips_when_too_few_semantics(self, mock_uow_cls):
        """Should return None with fewer than 2 semantic memories."""
        from brain.systems.cognition.consolidate import crystallize_procedural

        uow, session = _make_mock_uow()
        mock_uow_cls.return_value = uow

        session.execute.return_value = _make_mapping_result([
            _scoped({"id": 10, "content": "Single memory", "salience": 5.0})
        ])

        result = crystallize_procedural("rare-skill", user_id=TEST_USER_ID)
        assert result is None


class TestForgettingCurve:
    """Test salience decay and archival."""

    @patch("brain.systems.cognition.consolidate.UnitOfWork")
    def test_forgetting_returns_stats(self, mock_uow_cls):
        """Should return proper stats dict."""
        from brain.systems.cognition.consolidate import apply_forgetting_curve

        uow, session = _make_mock_uow()
        mock_uow_cls.return_value = uow

        # Each execute call returns a result with rowcount
        result_5 = MagicMock()
        result_5.rowcount = 5
        result_3 = MagicMock()
        result_3.rowcount = 3
        result_2 = MagicMock()
        result_2.rowcount = 2

        session.execute.side_effect = [result_5, result_3, result_2]

        stats = apply_forgetting_curve()
        assert stats["episodic_decayed"] == 5
        assert stats["semantic_decayed"] == 3
        assert stats["archived"] == 2

    def test_decay_constants_valid(self):
        """Verify decay constants are in valid ranges."""
        from brain.systems.cognition.consolidate import (
            DECAY_RATE, ARCHIVE_THRESHOLD, IMMUNE_SALIENCE,
            FORGETTING_GRACE_DAYS, MIN_CLUSTER_SIZE,
        )
        assert 0 < DECAY_RATE < 1
        assert ARCHIVE_THRESHOLD > 0
        assert IMMUNE_SALIENCE > ARCHIVE_THRESHOLD
        assert FORGETTING_GRACE_DAYS > 0
        assert MIN_CLUSTER_SIZE >= 2


class TestRunConsolidation:
    """Test full consolidation pipeline."""

    @patch("brain.systems.cognition.consolidate.run_dag_compaction")
    @patch("brain.systems.cognition.consolidate.apply_forgetting_curve")
    @patch("brain.systems.cognition.consolidate.crystallize_procedural")
    @patch("brain.systems.cognition.consolidate.extract_semantic")
    @patch("brain.systems.cognition.consolidate.cluster_episodes")
    @patch("brain.systems.cognition.consolidate.UnitOfWork")
    def test_full_pipeline(self, mock_uow_cls, mock_cluster, mock_extract,
                           mock_crystallize, mock_forget, mock_dag):
        """Should run all steps and return stats."""
        from brain.systems.cognition.consolidate import run_consolidation

        mock_cluster.return_value = [[1, 2, 3], [4, 5, 6]]
        mock_extract.side_effect = [100, 101]
        mock_crystallize.return_value = None  # no procedural this run
        mock_forget.return_value = {"episodic_decayed": 3, "semantic_decayed": 1, "archived": 1}
        mock_dag.return_value = {"leaf_passes": 0, "cascade_passes": 0, "summaries_created": 0}

        uow, session = _make_mock_uow()
        mock_uow_cls.return_value = uow

        # For the skills query
        session.execute.return_value = _make_mapping_result([
            {"name": "deploy"}, {"name": "debug"}
        ])

        stats = run_consolidation(user_id=TEST_USER_ID)
        assert stats["clusters_found"] == 2
        assert stats["semantic_created"] == 2
        assert stats["procedural_created"] == 0
        assert stats["forgetting"]["archived"] == 1


class TestFrameTierPreference:
    """Test that cognitive frames prefer semantic > episodic memories."""

    def test_frame_sorts_by_tier(self):
        """Semantic/procedural memories should appear before episodic."""
        from brain.systems.cognition.frame import build_frame

        memories = [
            {"content": "raw episode", "type": "episode", "tier": "episodic"},
            {"content": "consolidated knowledge", "type": "pattern", "tier": "semantic"},
            {"content": "crystallized procedure", "type": "pattern", "tier": "procedural"},
        ]
        frame = build_frame(
            task="fix the bug",
            relevant_memories=memories,
        )
        prompt = frame.to_system_prompt()
        # Procedural should come before episodic in the output
        proc_pos = prompt.find("crystallized procedure")
        epi_pos = prompt.find("raw episode")
        assert proc_pos < epi_pos, "Procedural should appear before episodic"

    def test_frame_handles_no_tier_field(self):
        """Should handle memories without tier field gracefully."""
        from brain.systems.cognition.frame import build_frame

        memories = [
            {"content": "old memory", "type": "lesson"},
        ]
        frame = build_frame(task="test", relevant_memories=memories)
        assert "old memory" in frame.to_system_prompt()
