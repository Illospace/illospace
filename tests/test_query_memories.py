from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.dialects import postgresql


def test_query_memories_accepts_user_id_and_null_scores(mock_uow, mock_embeddings):
    from brain.app.cli.memory import query_memories

    row = {
        "id": 1,
        "content": "Test memory",
        "memory_type": "lesson",
        "salience": 8.0,
        "emotion_label": "neutral",
        "tags": [],
        "created_at": None,
        "memory_tier": "semantic",
        "combined_score": None,
        "semantic_score": None,
        "recency_score": None,
        "emotion_score": None,
    }
    mock_uow.memories.query_ranked.return_value = {
        "results": [row],
        "spread_activation": [],
    }

    with patch("brain.app.cli.memory.UnitOfWork", return_value=mock_uow), \
         patch("brain.app.cli.memory.observe_retrieval", return_value={"retrieval_decision_id": 1, "stage": "memory_query"}) as mock_observe:
        result = query_memories(
            query="test",
            limit=1,
            emotion_context="neutral",
            user_id="user-123",
            org_id="org-123",
        )

    assert result["results"][0]["scores"]["combined"] == 0.0
    assert result["results"][0]["scores"]["semantic"] == 0.0
    assert result["attention_decision"]["stage"] == "memory_query"
    mock_observe.assert_called_once()
    mock_uow.memories.query_ranked.assert_called_once()


def test_query_memories_applies_selection_and_debug_view(mock_uow, mock_embeddings):
    from brain.app.cli.memory import query_memories

    rows = [
        {
            "id": 1,
            "content": "Selected memory",
            "memory_type": "lesson",
            "salience": 8.0,
            "emotion_label": "neutral",
            "tags": [],
            "created_at": None,
            "memory_tier": "semantic",
            "combined_score": 0.91,
            "semantic_score": 0.91,
            "recency_score": 0.7,
            "emotion_score": 0.0,
        },
        {
            "id": 2,
            "content": "Lazy candidate",
            "memory_type": "episode",
            "salience": 6.0,
            "emotion_label": "neutral",
            "tags": [],
            "created_at": None,
            "memory_tier": "episodic",
            "combined_score": 0.71,
            "semantic_score": 0.71,
            "recency_score": 0.5,
            "emotion_score": 0.0,
        },
    ]
    mock_uow.memories.query_ranked.return_value = {
        "results": rows,
        "spread_activation": [],
    }

    decision = {
        "retrieval_decision_id": 7,
        "stage": "memory_query",
        "query_text": "test",
        "query_fingerprint": "abc",
        "policy_version": "shadow-v1",
        "mode": "shadow",
        "preload_budget_tokens": 120,
        "lazy_budget_tokens": 40,
        "selected_item_ids": [1],
        "suppressed_item_ids": [2],
        "lazy_load_item_ids": [2],
        "omission_risk_score": 0.8,
        "contradiction_risk_score": 0.1,
        "candidate_count": 2,
    }

    with patch("brain.app.cli.memory.UnitOfWork", return_value=mock_uow), \
         patch("brain.app.cli.memory.observe_retrieval", return_value=decision):
        result = query_memories(
            query="test",
            limit=2,
            emotion_context="neutral",
            user_id="user-123",
            org_id="org-123",
            attention_debug=True,
        )

    assert [row["id"] for row in result["results"]] == [1]
    assert [row["id"] for row in result["suppressed_results"]] == [2]
    assert [row["id"] for row in result["lazy_load_results"]] == [2]
    assert result["attention_explain"]["decision"]["selected_item_ids"] == [1]


def test_query_memories_expands_lazy_loads_when_enabled(mock_uow, mock_embeddings):
    from brain.app.cli.memory import query_memories

    rows = [
        {
            "id": 1,
            "content": "Selected memory",
            "memory_type": "lesson",
            "salience": 8.0,
            "emotion_label": "neutral",
            "tags": [],
            "created_at": None,
            "memory_tier": "semantic",
            "combined_score": 0.91,
            "semantic_score": 0.91,
            "recency_score": 0.7,
            "emotion_score": 0.0,
        },
        {
            "id": 2,
            "content": "Lazy candidate",
            "memory_type": "episode",
            "salience": 6.0,
            "emotion_label": "neutral",
            "tags": [],
            "created_at": None,
            "memory_tier": "episodic",
            "combined_score": 0.71,
            "semantic_score": 0.71,
            "recency_score": 0.5,
            "emotion_score": 0.0,
        },
    ]
    mock_uow.memories.query_ranked.return_value = {
        "results": rows,
        "spread_activation": [],
    }

    decision = {
        "retrieval_decision_id": 7,
        "stage": "memory_query",
        "query_text": "test",
        "query_fingerprint": "abc",
        "policy_version": "shadow-v1",
        "mode": "shadow",
        "preload_budget_tokens": 120,
        "lazy_budget_tokens": 40,
        "selected_item_ids": [1],
        "suppressed_item_ids": [2],
        "lazy_load_item_ids": [2],
        "omission_risk_score": 0.8,
        "contradiction_risk_score": 0.1,
        "candidate_count": 2,
    }

    lazy_loaded = [{
        "id": 2,
        "content": "Lazy candidate",
        "type": "episode",
        "tier": "episodic",
        "salience": 6.0,
        "visibility": "private",
        "lazy_loaded": True,
    }]

    with patch("brain.app.cli.memory.UnitOfWork", return_value=mock_uow), \
         patch("brain.app.cli.memory.observe_retrieval", return_value=decision), \
         patch("brain.app.cli.memory.AttentionController.load_lazy_candidates", return_value=lazy_loaded) as mock_load:
        result = query_memories(
            query="test",
            limit=2,
            emotion_context="neutral",
            user_id="user-123",
            org_id="org-123",
            expand_lazy_load=True,
        )

    assert [row["id"] for row in result["results"]] == [1, 2]
    assert [row["id"] for row in result["lazy_loaded_results"]] == [2]
    mock_load.assert_called_once()


def test_repository_touch_memories_updates_access_count():
    from brain.platform.db.repositories.memories import MemoryRepository

    session = MagicMock()
    repo = MemoryRepository(session)

    repo.touch_memories([1, 2])

    stmt = session.execute.call_args.args[0]
    compiled = str(
        stmt.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert "UPDATE memories SET" in compiled
    assert "access_count=(coalesce(memories.access_count, 0) + 1)" in compiled
    assert "memories.id IN (1, 2)" in compiled


def test_graph_context_applies_visibility_scope_to_recursive_query():
    from brain.platform.db.repositories.memories import MemoryRepository
    from brain.platform.db.repositories.memory_visibility import MemoryVisibilityContext

    session = MagicMock()
    session.execute.return_value.mappings.return_value.all.return_value = []
    repo = MemoryRepository(session)

    repo.get_graph_context(
        42,
        context=MemoryVisibilityContext(user_id="user-1", org_id="org-1"),
    )

    sql_text = str(session.execute.call_args.args[0])
    params = session.execute.call_args.args[1]
    assert "context_user_id" in sql_text
    assert "context_org_id" in sql_text
    assert "COALESCE(m.visibility, 'private')" in sql_text
    assert params["context_user_id"] == "user-1"
    assert params["context_org_id"] == "org-1"


def test_connect_memories_propagates_edge_failure_for_uow_rollback(mock_uow):
    from brain.app.cli.memory import connect_memories

    mock_uow.edges.upsert_edge.side_effect = RuntimeError("edge insert failed")

    with patch("brain.app.cli.memory.UnitOfWork", return_value=mock_uow), pytest.raises(RuntimeError):
        connect_memories(1, 2, "related_to")

    exc_type, exc, _tb = mock_uow.__exit__.call_args.args
    assert exc_type is RuntimeError
    assert str(exc) == "edge insert failed"
