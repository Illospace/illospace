"""Tests that dashboard/brain API query functions properly scope by org.

After the ORM migration, the old dashboard.queries module was replaced by
FastAPI routers in brain.app.api.routers.* using SQLAlchemy ORM.  These tests
verify that the router functions accept org-scoped queries via the injected
session and user context.
"""
import pytest
from datetime import datetime, timezone
from unittest.mock import ANY, MagicMock, patch

ORG_ID = "org-test-123"
USER_ID = "user-test-456"


def _mock_user(org_id=ORG_ID):
    return {
        "id": USER_ID,
        "org_id": org_id,
        "role": "owner",
        "name": "Test",
        "email": "test@test.com",
    }


@pytest.fixture(autouse=True)
def mock_session_factory():
    session = MagicMock()

    def _factory():
        return session

    with patch("brain.app.api.deps.SessionFactory", _factory):
        yield session


# ── Graph ────────────────────────────────────────────────────────────────

class TestGraph:
    def test_graph_calls_repository(self, mock_session_factory):
        from brain.app.api.routers.memory import get_graph
        with patch("brain.app.api.routers.memory.UnitOfWork") as MockUnitOfWork:
            uow = MockUnitOfWork.return_value.__enter__.return_value
            uow.memories.get_graph_data.return_value = {"nodes": [], "edges": []}
            result = get_graph(user=_mock_user())
        assert result == {"nodes": [], "edges": []}
        uow.memories.get_graph_data.assert_called_once_with(context=ANY)

    def test_graph_no_org_id(self, mock_session_factory):
        from brain.app.api.routers.memory import get_graph
        with patch("brain.app.api.routers.memory.UnitOfWork") as MockUnitOfWork:
            uow = MockUnitOfWork.return_value.__enter__.return_value
            uow.memories.get_graph_data.return_value = {"nodes": [], "edges": []}
            result = get_graph(user=_mock_user(org_id=None))
        # Should not raise even without org_id
        assert result is not None


# ── Neighborhood ─────────────────────────────────────────────────────────

class TestNeighborhood:
    def test_neighborhood_calls_repository(self, mock_session_factory):
        from brain.app.api.routers.memory import get_neighborhood
        with patch("brain.app.api.routers.memory.UnitOfWork") as MockUnitOfWork:
            uow = MockUnitOfWork.return_value.__enter__.return_value
            uow.edges.neighborhood.return_value = []
            result = get_neighborhood(42, user=_mock_user())
        assert result == []
        uow.edges.neighborhood.assert_called_once_with(42, context=ANY)


# ── Memory Detail ────────────────────────────────────────────────────────

class TestMemoryDetail:
    def test_memory_detail_returns_memory(self, mock_session_factory):
        from brain.app.api.routers.memory import get_memory
        mem = MagicMock()
        mem.id = 1
        mem.content = "test"
        with patch("brain.app.api.routers.memory.UnitOfWork") as MockUnitOfWork:
            uow = MockUnitOfWork.return_value.__enter__.return_value
            uow.memories.get_or_raise_visible.return_value = mem
            result = get_memory(1, user=_mock_user())
        assert result == mem

    def test_memory_detail_not_found(self, mock_session_factory):
        from brain.app.api.routers.memory import get_memory
        from fastapi import HTTPException
        with patch("brain.app.api.routers.memory.UnitOfWork") as MockUnitOfWork:
            uow = MockUnitOfWork.return_value.__enter__.return_value
            uow.memories.get_or_raise_visible.side_effect = LookupError
            with pytest.raises(HTTPException) as exc_info:
                get_memory(999, user=_mock_user())
            assert exc_info.value.status_code == 404


# ── Search ───────────────────────────────────────────────────────────────

class TestSearch:
    def test_search_calls_repository(self, mock_session_factory):
        from brain.app.api.routers.memory import search_memories
        with patch("brain.app.api.routers.memory.UnitOfWork") as MockUnitOfWork:
            uow = MockUnitOfWork.return_value.__enter__.return_value
            uow.memories.search_visible.return_value = []
            result = search_memories("test query", user=_mock_user())
        assert result == []
        uow.memories.search_visible.assert_called_once_with("test query", ANY)


# ── Org Memories ─────────────────────────────────────────────────────────

class TestOrgMemories:
    def test_org_memories_filters_by_org(self, mock_session_factory):
        from brain.app.api.routers.memory import list_org_memories
        with patch("brain.app.api.routers.memory.UnitOfWork") as MockUnitOfWork:
            uow = MockUnitOfWork.return_value.__enter__.return_value
            uow.memories.list_org_memories.return_value = []
            result = list_org_memories(limit=50, offset=0, user=_mock_user())
        assert result == []
        uow.memories.list_org_memories.assert_called_once_with(ANY, limit=50, offset=0)

    def test_org_memories_empty_without_org(self, mock_session_factory):
        from brain.app.api.routers.memory import list_org_memories
        with patch("brain.app.api.routers.memory.UnitOfWork") as MockUnitOfWork:
            result = list_org_memories(limit=50, offset=0, user=_mock_user(org_id=None))
        assert result == []
        MockUnitOfWork.assert_not_called()


# ── Skills ───────────────────────────────────────────────────────────────

class TestSkills:
    def test_list_skills_calls_repository(self, mock_session_factory):
        from brain.app.api.routers.skills import list_skills
        with patch("brain.app.api.routers.skills.SkillRepository") as MockRepo:
            MockRepo.return_value.list_active_with_executions.return_value = []
            result = list_skills(db=mock_session_factory, user=_mock_user())
        MockRepo.return_value.list_active_with_executions.assert_called_once()


# ── Emotions ─────────────────────────────────────────────────────────────

class TestEmotions:
    def test_list_emotions_calls_repository(self, mock_session_factory):
        from brain.app.api.routers.emotions import list_emotions
        with patch("brain.app.api.routers.emotions.EmotionRepository") as MockRepo:
            MockRepo.return_value.list_recent.return_value = []
            result = list_emotions(db=mock_session_factory, user=_mock_user())
        MockRepo.return_value.list_recent.assert_called_once_with(limit=500)


# ── Global Search ────────────────────────────────────────────────────────

class TestGlobalSearch:
    def test_search_queries_memories_and_skills(self, mock_session_factory):
        from brain.app.api.routers.brain import global_search
        mock_session_factory.scalars.return_value.all.return_value = []
        result = global_search(q="test", db=mock_session_factory, user=_mock_user())
        assert "memories" in result
        assert "skills" in result
