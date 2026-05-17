"""Tests for brain/systems/cognition/graph — knowledge graph layer (SQLAlchemy ORM).

Tests mock at the session level using session.execute().mappings().all() etc.
"""

import os
import sys
from unittest.mock import MagicMock, patch, call, AsyncMock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), *([".."] * 1))))


def _mock_session_execute(results_sequence):
    """Helper: build a mock session whose .execute() returns results in sequence.

    Each entry in results_sequence should be:
      - a list of dicts  -> returned by .mappings().all()
      - a dict or None   -> returned by .mappings().first()
      - "all" / "first"  is determined by whichever the caller invokes

    We use a single side_effect list: each execute() returns a result-mock
    whose .mappings().all() / .mappings().first() return the next item.
    """
    session = MagicMock()
    session.execute = AsyncMock()
    call_results = []
    for item in results_sequence:
        result_mock = MagicMock()
        mappings_mock = MagicMock()
        if isinstance(item, list):
            mappings_mock.all.return_value = item
            mappings_mock.first.return_value = item[0] if item else None
        else:
            # single row or None
            mappings_mock.first.return_value = item
            mappings_mock.all.return_value = [item] if item else []
        result_mock.mappings.return_value = mappings_mock
        call_results.append(result_mock)
    session.execute.side_effect = call_results
    return session


class TestEdgeTypes:
    """Test edge type definitions."""

    def test_edge_types_defined(self):
        from brain.systems.cognition.graph import EDGE_TYPES
        assert "similar_to" in EDGE_TYPES
        assert "contradicts" in EDGE_TYPES
        assert "consolidated_from" in EDGE_TYPES
        assert "depends_on" in EDGE_TYPES
        assert "derived_from" in EDGE_TYPES

    def test_edge_weight_bonus_covers_types(self):
        from brain.systems.cognition.graph import EDGE_TYPES, EDGE_WEIGHT_BONUS
        for etype in EDGE_TYPES:
            assert etype in EDGE_WEIGHT_BONUS, f"Missing weight bonus for {etype}"


class TestGraphAugmentedRecall:
    """Test graph-augmented memory retrieval using SQLAlchemy session."""

    async def test_returns_vector_results(self):
        """Should return vector search results even without graph edges."""
        from brain.systems.cognition.graph import graph_augmented_recall

        vector_rows = [
            {"id": 1, "content": "Redis TTL fix", "memory_type": "lesson",
             "salience": 8.0, "emotion_label": "satisfied", "memory_tier": "semantic",
             "visibility": "private", "similarity": 0.85},
            {"id": 2, "content": "Database connection pooling", "memory_type": "pattern",
             "salience": 7.0, "emotion_label": "neutral", "memory_tier": "episodic",
             "visibility": "private", "similarity": 0.72},
        ]
        graph_rows = []

        # execute calls: 1) vector search, 2) graph traversal, 3) update memories, 4) update edges
        session = _mock_session_execute([vector_rows, graph_rows, None, None])

        results = await graph_augmented_recall(session, "[0,0,0]", limit=5)
        assert len(results) == 2
        assert results[0]["id"] == 1  # highest similarity first
        assert results[0]["similarity"] == 0.85

    async def test_graph_edges_boost_score(self):
        """Memories connected via graph edges should get score boost."""
        from brain.systems.cognition.graph import graph_augmented_recall

        vector_rows = [
            {"id": 1, "content": "Main memory", "memory_type": "lesson",
             "salience": 5.0, "emotion_label": "neutral", "memory_tier": "episodic",
             "visibility": "private", "similarity": 0.75},
        ]
        graph_rows = [
            {"source_id": 1, "target_id": 2, "relationship": "contradicts", "weight": 0.9,
             "connected_id": 2, "content": "Contradicting info", "memory_type": "lesson",
             "salience": 7.0, "memory_tier": "semantic", "visibility": "private",
             "emotion_label": "frustrated"},
        ]

        session = _mock_session_execute([vector_rows, graph_rows, None, None])

        results = await graph_augmented_recall(session, "[0,0,0]", limit=5)
        assert len(results) == 2
        ids = [r["id"] for r in results]
        assert 2 in ids

    async def test_empty_results(self):
        """Should handle no results gracefully."""
        from brain.systems.cognition.graph import graph_augmented_recall

        session = _mock_session_execute([[]])

        results = await graph_augmented_recall(session, "[0,0,0]", limit=5)
        assert results == []

    async def test_graph_edges_included_in_result(self):
        """Results should include graph edge information."""
        from brain.systems.cognition.graph import graph_augmented_recall

        vector_rows = [
            {"id": 1, "content": "Memory A", "memory_type": "episode",
             "salience": 5.0, "emotion_label": "neutral", "memory_tier": "episodic",
             "visibility": "private", "similarity": 0.8},
        ]
        graph_rows = [
            {"source_id": 1, "target_id": 2, "relationship": "depends_on", "weight": 1.0,
             "connected_id": 2, "content": "Memory B", "memory_type": "fact",
             "salience": 6.0, "memory_tier": "semantic", "visibility": "private",
             "emotion_label": "neutral"},
        ]
        session = _mock_session_execute([vector_rows, graph_rows, None, None])

        results = await graph_augmented_recall(session, "[0,0,0]", limit=5)
        mem_b = next(r for r in results if r["id"] == 2)
        assert len(mem_b["graph_edges"]) > 0
        assert mem_b["graph_edges"][0]["relationship"] == "depends_on"

    async def test_reviewed_active_results_rank_ahead_of_unreviewed(self):
        """Reviewed-active memories should outrank raw unreviewed ones."""
        from brain.systems.cognition.graph import graph_augmented_recall

        vector_rows = [
            {
                "id": 1,
                "content": "High similarity but unreviewed",
                "memory_type": "lesson",
                "salience": 9.0,
                "emotion_label": "neutral",
                "memory_tier": "episodic",
                "visibility": "private",
                "truth_status": "unknown",
                "review_status": "unreviewed",
                "confidence": 0.45,
                "freshness_score": 0.7,
                "valid_until": None,
                "demoted_at": None,
                "policy_kind": None,
                "policy_scope": None,
                "reviewed_at": None,
                "similarity": 0.96,
            },
            {
                "id": 2,
                "content": "Reviewed active memory",
                "memory_type": "lesson",
                "salience": 6.0,
                "emotion_label": "neutral",
                "memory_tier": "semantic",
                "visibility": "private",
                "truth_status": "reviewed",
                "review_status": "reviewed",
                "confidence": 0.92,
                "freshness_score": 0.85,
                "valid_until": None,
                "demoted_at": None,
                "policy_kind": None,
                "policy_scope": None,
                "reviewed_at": "2026-04-22T00:00:00+00:00",
                "similarity": 0.8,
            },
        ]
        session = _mock_session_execute([vector_rows, [], None, None])

        results = await graph_augmented_recall(session, "[0,0,0]", limit=5)
        assert [item["id"] for item in results[:2]] == [2, 1]

    async def test_quarantined_results_are_suppressed_when_filter_enabled(self, monkeypatch):
        """Quarantined memories should disappear from recall when the guard is on."""
        from brain.systems.cognition.graph import graph_augmented_recall

        monkeypatch.setenv("MEMORY_QUARANTINE_FILTER_ENABLED", "1")
        vector_rows = [
            {
                "id": 1,
                "content": "Quarantined memory",
                "memory_type": "lesson",
                "salience": 9.0,
                "emotion_label": "neutral",
                "memory_tier": "episodic",
                "visibility": "private",
                "truth_status": "quarantined",
                "review_status": "rejected",
                "confidence": 0.2,
                "freshness_score": 0.2,
                "valid_until": None,
                "demoted_at": "2026-04-22T00:00:00+00:00",
                "policy_kind": None,
                "policy_scope": None,
                "reviewed_at": None,
                "similarity": 0.95,
            },
            {
                "id": 2,
                "content": "Safe reviewed memory",
                "memory_type": "lesson",
                "salience": 8.0,
                "emotion_label": "neutral",
                "memory_tier": "semantic",
                "visibility": "private",
                "truth_status": "reviewed",
                "review_status": "reviewed",
                "confidence": 0.9,
                "freshness_score": 0.8,
                "valid_until": None,
                "demoted_at": None,
                "policy_kind": None,
                "policy_scope": None,
                "reviewed_at": "2026-04-22T00:00:00+00:00",
                "similarity": 0.85,
            },
        ]
        session = _mock_session_execute([vector_rows, [], None, None])

        results = await graph_augmented_recall(session, "[0,0,0]", limit=5)
        assert [item["id"] for item in results] == [2]

    async def test_null_similarity_does_not_crash(self):
        """Graph recall should degrade safely if DB returns NULL similarity."""
        from brain.systems.cognition.graph import graph_augmented_recall

        vector_rows = [
            {"id": 1, "content": "Memory A", "memory_type": "episode",
             "salience": 5.0, "emotion_label": "neutral", "memory_tier": "episodic",
             "visibility": "private", "similarity": None},
        ]
        graph_rows = []
        session = _mock_session_execute([vector_rows, graph_rows, None, None])

        results = await graph_augmented_recall(session, "[0,0,0]", limit=5)
        assert len(results) == 1
        assert results[0]["similarity"] == 0.0

    async def test_session_execute_called_with_text(self):
        """Session.execute should be called with text() wrapped SQL."""
        from brain.systems.cognition.graph import graph_augmented_recall
        from sqlalchemy import text

        session = _mock_session_execute([[], ])

        await graph_augmented_recall(session, "[0,0,0]", limit=5)
        # First call should use text()
        first_call_args = session.execute.call_args_list[0]
        sql_arg = first_call_args[0][0]
        # Should be a TextClause (from sqlalchemy.text())
        assert hasattr(sql_arg, 'text'), "SQL should be wrapped in sqlalchemy.text()"


class TestAutoLinkMemory:
    """Test automatic weak candidate creation for new memories."""

    def test_records_valence_candidate_without_contradiction_edge(self):
        """Valence-only signals are ignored after the emotion cleanup."""
        from brain.systems.cognition.graph import auto_link_memory

        similar_rows = [
            {"id": 2, "content": "Opposite view", "emotion_valence": -0.8, "sim": 0.85},
        ]
        valence_row = {"emotion_valence": 0.7}
        contradiction_row = {"id": 77, "status": "needs_review", "severity": 0.425}

        # execute calls: 1) find similar, 2) _get_valence, 3) contradiction candidate record
        session = _mock_session_execute([similar_rows, valence_row, contradiction_row])

        stats = auto_link_memory(session, memory_id=1, content="Positive view", memory_type="lesson")
        assert stats["contradictions"] == 0
        assert stats["contradiction_candidates"] == 0
        assert stats["edges_created"] == 0
        assert stats["contradiction_records"] == 0
        assert stats["contradiction_record_ids"] == []

    def test_no_contradiction_same_valence(self):
        """Should not create contradiction for same-valence memories."""
        from brain.systems.cognition.graph import auto_link_memory

        similar_rows = [
            {"id": 2, "content": "Similar view", "emotion_valence": 0.6, "sim": 0.85},
        ]
        valence_row = {"emotion_valence": 0.7}

        # execute calls: 1) find similar, 2) _get_valence (no _create_edge since no contradiction)
        session = _mock_session_execute([similar_rows, valence_row])

        stats = auto_link_memory(session, memory_id=1, content="Positive view", memory_type="lesson")
        assert stats["contradictions"] == 0


class TestDetectContradictions:
    """Test nightly weak candidate detection."""

    def test_finds_valence_candidates_without_edges(self):
        """Valence-only contradiction scanning is disabled after the emotion cleanup."""
        from brain.systems.cognition.graph import detect_contradictions

        contradiction_rows = [
            {"id1": 1, "content1": "Redis is fast", "v1": 0.7,
             "id2": 2, "content2": "Redis is slow", "v2": -0.5,
             "similarity": 0.85},
        ]
        contradiction_record = {"id": 88, "status": "needs_review", "severity": 0.425}

        # execute calls: 1) find candidates, 2) contradiction candidate record
        session = _mock_session_execute([contradiction_rows, contradiction_record])

        contradictions = detect_contradictions(session)
        assert contradictions == []
        assert session.execute.call_count == 0

    def test_empty_when_no_contradictions(self):
        from brain.systems.cognition.graph import detect_contradictions

        session = _mock_session_execute([[]])

        assert detect_contradictions(session) == []


class TestMemoryNeighborhood:
    """Test graph neighborhood retrieval."""

    async def test_returns_center_and_edges(self):
        from brain.systems.cognition.graph import get_memory_neighborhood

        center_row = {
            "id": 1, "content": "Center memory", "memory_type": "lesson",
            "salience": 8.0, "memory_tier": "semantic", "emotion_label": "neutral",
        }
        neighbor_rows = [
            {"relationship": "similar_to", "weight": 0.9,
             "id": 2, "content": "Neighbor", "memory_type": "episode",
             "salience": 5.0, "memory_tier": "episodic", "direction": "outgoing"},
        ]

        # execute calls: 1) get center (uses .first()), 2) get neighbors (uses .all())
        session = _mock_session_execute([center_row, neighbor_rows])

        result = await get_memory_neighborhood(session, memory_id=1)
        assert result["center"]["id"] == 1
        assert result["edge_count"] == 1
        assert result["edges"][0]["relationship"] == "similar_to"

    async def test_returns_error_for_missing_memory(self):
        from brain.systems.cognition.graph import get_memory_neighborhood

        session = _mock_session_execute([None])

        result = await get_memory_neighborhood(session, memory_id=999)
        assert "error" in result


class TestBrainRecallIntegration:
    """Test that brain_recall uses graph-augmented retrieval via session."""

    @patch("brain.systems.cognition.graph.graph_augmented_recall")
    @patch("brain.systems.memory.embeddings.embed_query")
    @patch("brain.systems.memory.embeddings.vec_to_pg")
    async def test_brain_recall_uses_graph(self, mock_vec, mock_emb, mock_graph):
        """async_tool_brain_recall should try graph-augmented recall first."""
        import importlib
        import brain.app.mcp.server as mcp_server
        importlib.reload(mcp_server)

        mock_emb.return_value = [0.0] * 2000
        mock_vec.return_value = "[0,0,0]"
        mock_graph.return_value = [
            {"id": 1, "content": "Graph result", "type": "lesson",
             "tier": "semantic", "salience": 7.0, "similarity": 0.85,
             "graph_edges": [{"relationship": "depends_on", "from_memory": 2, "weight": 0.9}]},
        ]

        mock_uow = MagicMock()
        mock_session = MagicMock()
        mock_uow.__enter__ = MagicMock(return_value=mock_uow)
        mock_uow.__exit__ = MagicMock(return_value=False)
        mock_uow.__aenter__ = AsyncMock(return_value=mock_uow)
        mock_uow.__aexit__ = AsyncMock(return_value=False)
        mock_uow.session = mock_session
        mock_uow.memories.graph_augmented_recall.return_value = mock_graph.return_value

        with patch("brain.app.mcp.server.UnitOfWork", return_value=mock_uow), \
             patch("brain.app.mcp.server.observe_retrieval", new=AsyncMock(return_value={"retrieval_decision_id": 11, "stage": "brain_recall"})):
            result = await mcp_server.async_tool_brain_recall("redis issues")

        assert result["count"] == 1
        assert result["memories"][0]["tier"] == "semantic"
        assert "graph_context" in result["memories"][0]
        assert result["attention_decision"]["stage"] == "brain_recall"
        mock_uow.memories.graph_augmented_recall.assert_called_once()
        assert mock_uow.memories.graph_augmented_recall.call_args.kwargs["query_embedding"] == mock_emb.return_value

    @patch("brain.systems.cognition.graph.graph_augmented_recall")
    @patch("brain.systems.memory.embeddings.embed_query")
    @patch("brain.systems.memory.embeddings.vec_to_pg")
    async def test_brain_recall_materializes_attention_selection(self, mock_vec, mock_emb, mock_graph):
        """brain_recall should return only preload-selected memories and expose lazy-load candidates."""
        import importlib
        import brain.app.mcp.server as mcp_server
        importlib.reload(mcp_server)

        mock_emb.return_value = [0.0] * 2000
        mock_vec.return_value = "[0,0,0]"
        mock_graph.return_value = [
            {"id": 1, "content": "Graph result one", "type": "lesson",
             "tier": "semantic", "salience": 7.0, "similarity": 0.95},
            {"id": 2, "content": "Graph result two", "type": "lesson",
             "tier": "semantic", "salience": 7.0, "similarity": 0.91},
            {"id": 3, "content": "Graph result three", "type": "lesson",
             "tier": "semantic", "salience": 7.0, "similarity": 0.88},
        ]

        mock_uow = MagicMock()
        mock_session = MagicMock()
        mock_uow.__enter__ = MagicMock(return_value=mock_uow)
        mock_uow.__exit__ = MagicMock(return_value=False)
        mock_uow.__aenter__ = AsyncMock(return_value=mock_uow)
        mock_uow.__aexit__ = AsyncMock(return_value=False)
        mock_uow.session = mock_session
        mock_uow.memories.graph_augmented_recall.return_value = mock_graph.return_value

        decision = {
            "retrieval_decision_id": 13,
            "stage": "brain_recall",
            "query_text": "redis issues",
            "query_fingerprint": "abc",
            "policy_version": "shadow-v1",
            "mode": "shadow",
            "preload_budget_tokens": 360,
            "lazy_budget_tokens": 120,
            "selected_item_ids": [1, 2],
            "suppressed_item_ids": [3],
            "lazy_load_item_ids": [3],
            "omission_risk_score": 0.7,
            "contradiction_risk_score": 0.0,
            "candidate_count": 3,
        }

        with patch("brain.app.mcp.server.UnitOfWork", return_value=mock_uow), \
             patch("brain.app.mcp.server.observe_retrieval", new=AsyncMock(return_value=decision)):
            result = await mcp_server.async_tool_brain_recall("redis issues", attention_debug=True)

        assert [mem["id"] for mem in result["memories"]] == [1, 2]
        assert [mem["id"] for mem in result["suppressed_memories"]] == [3]
        assert [mem["id"] for mem in result["lazy_load_memories"]] == [3]
        assert result["attention_explain"]["decision"]["selected_item_ids"] == [1, 2]

    @patch("brain.systems.cognition.graph.graph_augmented_recall")
    @patch("brain.systems.memory.embeddings.embed_query")
    @patch("brain.systems.memory.embeddings.vec_to_pg")
    async def test_brain_recall_expands_lazy_loads_when_enabled(self, mock_vec, mock_emb, mock_graph):
        """brain_recall should fetch deferred memories when lazy load is explicitly enabled."""
        import importlib
        import brain.app.mcp.server as mcp_server
        importlib.reload(mcp_server)

        mock_emb.return_value = [0.0] * 2000
        mock_vec.return_value = "[0,0,0]"
        mock_graph.return_value = [
            {"id": 1, "content": "Graph result one", "type": "lesson",
             "tier": "semantic", "salience": 7.0, "similarity": 0.95},
            {"id": 2, "content": "Graph result two", "type": "lesson",
             "tier": "semantic", "salience": 7.0, "similarity": 0.91},
            {"id": 3, "content": "Graph result three", "type": "lesson",
             "tier": "semantic", "salience": 7.0, "similarity": 0.88},
        ]

        mock_uow = MagicMock()
        mock_session = MagicMock()
        mock_uow.__enter__ = MagicMock(return_value=mock_uow)
        mock_uow.__exit__ = MagicMock(return_value=False)
        mock_uow.__aenter__ = AsyncMock(return_value=mock_uow)
        mock_uow.__aexit__ = AsyncMock(return_value=False)
        mock_uow.session = mock_session
        mock_uow.memories.graph_augmented_recall.return_value = mock_graph.return_value

        decision = {
            "retrieval_decision_id": 14,
            "stage": "brain_recall",
            "query_text": "redis issues",
            "query_fingerprint": "abc",
            "policy_version": "shadow-v1",
            "mode": "shadow",
            "preload_budget_tokens": 360,
            "lazy_budget_tokens": 120,
            "selected_item_ids": [1, 2],
            "suppressed_item_ids": [3],
            "lazy_load_item_ids": [3],
            "omission_risk_score": 0.7,
            "contradiction_risk_score": 0.0,
            "candidate_count": 3,
        }

        lazy_loaded = [{
            "id": 3,
            "content": "Graph result three",
            "type": "lesson",
            "tier": "semantic",
            "salience": 7.0,
            "visibility": "private",
            "lazy_loaded": True,
        }]

        with patch("brain.app.mcp.server.UnitOfWork", return_value=mock_uow), \
             patch("brain.app.mcp.server.observe_retrieval", new=AsyncMock(return_value=decision)), \
             patch("brain.app.mcp.server.AttentionController.load_lazy_candidates", new=AsyncMock(return_value=lazy_loaded)) as mock_load:
            result = await mcp_server.async_tool_brain_recall("redis issues", attention_debug=True, expand_lazy_load=True)

        assert [mem["id"] for mem in result["memories"]] == [1, 2, 3]
        assert [mem["id"] for mem in result["lazy_loaded_memories"]] == [3]
        mock_load.assert_awaited_once()

    @patch("brain.systems.cognition.graph.graph_augmented_recall", side_effect=Exception("UndefinedColumn"))
    @patch("brain.systems.memory.embeddings.embed_query")
    @patch("brain.systems.memory.embeddings.vec_to_pg")
    async def test_brain_recall_fallback_on_graph_failure(self, mock_vec, mock_emb, _mock_graph):
        """When graph recall fails, should fall back to vector search."""
        import importlib
        import brain.app.mcp.server as mcp_server
        importlib.reload(mcp_server)

        mock_emb.return_value = [0.0] * 2000
        mock_vec.return_value = "[0,0,0]"

        # First UoW: graph recall will raise. Second UoW: fallback vector search
        mock_uow_graph = MagicMock()
        mock_uow_graph.__enter__ = MagicMock(return_value=mock_uow_graph)
        mock_uow_graph.__exit__ = MagicMock(return_value=False)
        mock_uow_graph.__aenter__ = AsyncMock(return_value=mock_uow_graph)
        mock_uow_graph.__aexit__ = AsyncMock(return_value=False)
        mock_uow_graph.session = MagicMock()
        mock_uow_graph.memories.graph_augmented_recall.side_effect = Exception("UndefinedColumn")

        mock_uow_fallback = MagicMock()
        mock_uow_fallback.__enter__ = MagicMock(return_value=mock_uow_fallback)
        mock_uow_fallback.__exit__ = MagicMock(return_value=False)
        mock_uow_fallback.__aenter__ = AsyncMock(return_value=mock_uow_fallback)
        mock_uow_fallback.__aexit__ = AsyncMock(return_value=False)
        fallback_session = MagicMock()
        mock_uow_fallback.session = fallback_session

        # Fallback vector search returns results
        mock_uow_fallback.memories.recall_vector.return_value = [{
            "id": 7,
            "content": "Fallback result",
            "type": "lesson",
            "salience": 8.0,
            "similarity": 0.88,
        }]

        with patch("brain.app.mcp.server.UnitOfWork", side_effect=[mock_uow_graph, mock_uow_fallback]), \
             patch("brain.app.mcp.server.observe_retrieval", new=AsyncMock(return_value={"retrieval_decision_id": 12, "stage": "brain_recall"})):
            result = await mcp_server.async_tool_brain_recall("redis issues")

        assert result["count"] == 1
        assert result["memories"][0]["id"] == 7
