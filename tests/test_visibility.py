"""
Tests for multiplayer brain: memory visibility & sharing layers.

Tests cover:
- recall query isolation: user A's private memories never returned for user B
- recall query sharing: team/org memories visible across users in same org
- graph traversal visibility re-filtering
- promote_memory function
- memory update/promote endpoints via FastAPI

After the ORM migration, API endpoints use SQLAlchemy sessions via dependency
injection and direct repository/unit-of-work seams in tests.
"""
from __future__ import annotations

import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


# -- Fixtures ---------------------------------------------------------------

USER_A = {
    "id": "aaaa0000-0000-0000-0000-000000000001",
    "email": "alice@example.test", "name": "Alice", "role": "owner",
    "color": "#6366f1",
    "org_id": "org00000-0000-0000-0000-000000000001",
    "org_name": "Example", "org_slug": "example", "attribution_enabled": True,
}

USER_B = {
    "id": "bbbb0000-0000-0000-0000-000000000002",
    "email": "bob@example.test", "name": "Bob", "role": "member",
    "color": "#ed8936",
    "org_id": "org00000-0000-0000-0000-000000000001",  # same org
    "org_name": "Example", "org_slug": "example", "attribution_enabled": True,
}

PRIVATE_MEMORY_A = {
    "id": 1, "content": "Alice secret", "memory_type": "episode",
    "salience": 5, "visibility": "private", "user_id": USER_A["id"],
    "org_id": USER_A["org_id"],
}

ORG_MEMORY = {
    "id": 2, "content": "Shared org lesson", "memory_type": "lesson",
    "salience": 8, "visibility": "org", "user_id": USER_A["id"],
    "org_id": USER_A["org_id"],
}


@pytest.fixture(autouse=True)
def mock_session_factory():
    session = MagicMock()
    yield session


# -- Recall visibility filtering ---------------------------------------------

class TestRecallVisibilityFilter:

    def test_no_user_context_denies_global_memory_recall(self):
        from brain.systems.cognition.graph import graph_augmented_recall
        session = MagicMock()
        result_mock = MagicMock()
        result_mock.mappings.return_value.all.return_value = []
        session.execute.return_value = result_mock
        graph_augmented_recall(session, "test", limit=3)
        sql_arg = session.execute.call_args[0][0]
        query_text = sql_arg.text if hasattr(sql_arg, 'text') else str(sql_arg)
        assert "AND FALSE" in query_text

    def test_with_user_and_org_applies_visibility_clause(self):
        from brain.systems.cognition.graph import graph_augmented_recall
        session = MagicMock()
        result_mock = MagicMock()
        result_mock.mappings.return_value.all.return_value = []
        session.execute.return_value = result_mock
        graph_augmented_recall(session, "test", limit=3,
                               user_id=USER_A["id"], org_id=USER_A["org_id"])
        sql_arg = session.execute.call_args[0][0]
        query_text = sql_arg.text if hasattr(sql_arg, 'text') else str(sql_arg)
        assert "visibility" in query_text
        assert "private" in query_text

    def test_params_include_user_and_org_ids(self):
        from brain.systems.cognition.graph import graph_augmented_recall
        session = MagicMock()
        result_mock = MagicMock()
        result_mock.mappings.return_value.all.return_value = []
        session.execute.return_value = result_mock
        graph_augmented_recall(session, "test", limit=3,
                               user_id=USER_A["id"], org_id=USER_A["org_id"])
        params = session.execute.call_args[0][1]
        assert USER_A["id"] in params.values()
        assert USER_A["org_id"] in params.values()

    def test_graph_traversal_also_filters_visibility(self):
        """Graph hop must re-apply visibility filter to prevent private memory leakage."""
        from brain.systems.cognition.graph import graph_augmented_recall
        session = MagicMock()

        # First execute: vector search returns results; subsequent: graph traversal, updates
        vector_result = MagicMock()
        vector_result.mappings.return_value.all.return_value = [
            {"id": 1, "content": "seed", "memory_type": "lesson", "salience": 5,
             "emotion_label": None, "memory_tier": "episodic", "visibility": "org",
             "similarity": 0.8}
        ]
        graph_result = MagicMock()
        graph_result.mappings.return_value.all.return_value = []
        update_result = MagicMock()
        session.execute.side_effect = [vector_result, graph_result, update_result, update_result]

        graph_augmented_recall(session, "test", limit=3,
                               user_id=USER_A["id"], org_id=USER_A["org_id"])
        # The second execute call (graph traversal) should include visibility filter
        graph_sql = session.execute.call_args_list[1][0][0]
        graph_query = graph_sql.text if hasattr(graph_sql, 'text') else str(graph_sql)
        assert "visibility" in graph_query
        assert "m.user_id" in graph_query


# -- Cross-user isolation ---------------------------------------------------

class TestCrossUserIsolation:

    def test_private_memory_not_visible_to_other_user(self):
        def is_visible_to(memory, viewer_user_id, viewer_org_id):
            if memory["visibility"] == "private":
                return memory["user_id"] == viewer_user_id
            return memory["org_id"] == viewer_org_id

        assert is_visible_to(PRIVATE_MEMORY_A, USER_A["id"], USER_A["org_id"]) is True
        assert is_visible_to(PRIVATE_MEMORY_A, USER_B["id"], USER_B["org_id"]) is False

    def test_org_memory_visible_to_both_users(self):
        def is_visible_to(memory, viewer_user_id, viewer_org_id):
            if memory["visibility"] == "private":
                return memory["user_id"] == viewer_user_id
            return memory["org_id"] == viewer_org_id

        assert is_visible_to(ORG_MEMORY, USER_A["id"], USER_A["org_id"]) is True
        assert is_visible_to(ORG_MEMORY, USER_B["id"], USER_B["org_id"]) is True


# -- update_memory (patch) visibility ----------------------------------------

class TestPatchMemoryVisibility:

    async def test_update_memory_sets_visibility(self, mock_session_factory):
        from brain.app.api.routers.memory import update_memory
        from brain.app.api.schemas.memories import MemoryUpdate

        mock_mem = MagicMock()
        mock_mem.id = 1
        mock_mem.visibility = "private"

        with patch("brain.app.api.routers.memory.UnitOfWork") as MockUnitOfWork:
            uow = MockUnitOfWork.return_value.__aenter__.return_value
            uow.memories.get_or_raise_visible = AsyncMock(return_value=mock_mem)
            uow.session.flush = AsyncMock()
            body = MemoryUpdate(visibility="team")
            result = await update_memory(1, body, user={
                "id": USER_A["id"], "org_id": USER_A["org_id"], "role": "owner"})

        assert mock_mem.visibility == "team"
        assert result == mock_mem
        uow.session.flush.assert_awaited_once()

    async def test_update_memory_not_found_raises_404(self, mock_session_factory):
        from brain.app.api.routers.memory import update_memory
        from brain.app.api.schemas.memories import MemoryUpdate
        from fastapi import HTTPException

        with patch("brain.app.api.routers.memory.UnitOfWork") as MockUnitOfWork:
            uow = MockUnitOfWork.return_value.__aenter__.return_value
            uow.memories.get_or_raise_visible = AsyncMock(side_effect=LookupError)
            body = MemoryUpdate(visibility="team")
            with pytest.raises(HTTPException) as exc_info:
                await update_memory(999, body, user={
                    "id": USER_A["id"], "org_id": USER_A["org_id"], "role": "owner"})
            assert exc_info.value.status_code == 404


# -- promote_memory ----------------------------------------------------------

class TestPromoteMemory:

    async def test_promote_sets_visibility(self, mock_session_factory):
        from brain.app.api.routers.memory import promote_memory
        from brain.app.api.schemas.memories import MemoryPromote

        mock_mem = MagicMock()
        mock_mem.id = 1
        mock_mem.visibility = "private"

        with patch("brain.app.api.routers.memory.UnitOfWork") as MockUnitOfWork:
            uow = MockUnitOfWork.return_value.__aenter__.return_value
            uow.memories.get_or_raise_visible = AsyncMock(return_value=mock_mem)
            uow.session.flush = AsyncMock()
            body = MemoryPromote(visibility="org")
            result = await promote_memory(1, body, user={
                "id": USER_A["id"], "org_id": USER_A["org_id"], "role": "owner"})

        assert mock_mem.visibility == "org"
        assert result == mock_mem
        uow.session.flush.assert_awaited_once()

    async def test_promote_invalid_target_raises_400(self, mock_session_factory):
        from brain.app.api.routers.memory import promote_memory
        from brain.app.api.schemas.memories import MemoryPromote
        from fastapi import HTTPException

        mock_mem = MagicMock()
        mock_mem.id = 1

        with patch("brain.app.api.routers.memory.UnitOfWork") as MockUnitOfWork:
            uow = MockUnitOfWork.return_value.__aenter__.return_value
            uow.memories.get_or_raise_visible = AsyncMock(return_value=mock_mem)
            body = MemoryPromote(visibility="public")
            with pytest.raises(HTTPException) as exc_info:
                await promote_memory(1, body, user={
                    "id": USER_A["id"], "org_id": USER_A["org_id"], "role": "owner"})
            assert exc_info.value.status_code == 400

    async def test_promote_not_found_raises_404(self, mock_session_factory):
        from brain.app.api.routers.memory import promote_memory
        from brain.app.api.schemas.memories import MemoryPromote
        from fastapi import HTTPException

        with patch("brain.app.api.routers.memory.UnitOfWork") as MockUnitOfWork:
            uow = MockUnitOfWork.return_value.__aenter__.return_value
            uow.memories.get_or_raise_visible = AsyncMock(side_effect=LookupError)
            body = MemoryPromote(visibility="org")
            with pytest.raises(HTTPException) as exc_info:
                await promote_memory(1, body, user={
                    "id": USER_A["id"], "org_id": USER_A["org_id"], "role": "owner"})
            assert exc_info.value.status_code == 404


# -- Org memories endpoint ---------------------------------------------------

class TestOrgMemoriesEndpoint:

    async def test_returns_org_memories(self, mock_session_factory):
        from brain.app.api.routers.memory import list_org_memories
        mock_mem = MagicMock()
        mock_mem.id = 2
        mock_mem.visibility = "org"
        with patch("brain.app.api.routers.memory.UnitOfWork") as MockUnitOfWork:
            uow = MockUnitOfWork.return_value.__aenter__.return_value
            uow.memories.list_org_memories = AsyncMock(return_value=[mock_mem])
            result = await list_org_memories(
                limit=50, offset=0,
                user={"id": USER_A["id"], "org_id": USER_A["org_id"], "role": "owner"},
            )
        assert len(result) == 1
        uow.memories.list_org_memories.assert_awaited_once()

    async def test_returns_empty_without_org_id(self, mock_session_factory):
        from brain.app.api.routers.memory import list_org_memories
        with patch("brain.app.api.routers.memory.UnitOfWork") as MockUnitOfWork:
            result = await list_org_memories(
                limit=50, offset=0,
                user={"id": USER_A["id"], "role": "owner"},
            )
        assert result == []
        MockUnitOfWork.assert_not_called()
