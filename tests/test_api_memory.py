"""Tests for memory router — graph, search."""
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient


@pytest.fixture(autouse=True)
def mock_session_factory():
    session = MagicMock()
    yield session


def _make_memory(**overrides):
    defaults = {
        "id": 1,
        "content": "Test memory",
        "memory_type": "fact",
        "salience": 0.8,
        "emotion_valence": 0.5,
        "emotion_arousal": 0.3,
        "emotion_label": None,
        "source": None,
        "memory_tier": "episodic",
        "truth_status": "unknown",
        "review_status": "unreviewed",
        "confidence": 0.5,
        "freshness_score": 0.5,
        "valid_from": None,
        "valid_until": None,
        "reviewed_at": None,
        "reviewed_by": None,
        "demoted_at": None,
        "demotion_reason": None,
        "tags": [],
        "access_count": 0,
        "last_accessed": None,
        "created_at": datetime.now(timezone.utc),
        "scope": "personal",
        "visibility": "private",
        "user_id": "system",
        "org_id": None,
    }
    defaults.update(overrides)
    obj = MagicMock()
    for k, v in defaults.items():
        setattr(obj, k, v)
    return obj


def _make_edge(**overrides):
    defaults = {
        "id": 1,
        "source_id": 1,
        "target_id": 2,
        "relationship": "related",
        "weight": 1.0,
    }
    defaults.update(overrides)
    obj = MagicMock()
    for k, v in defaults.items():
        setattr(obj, k, v)
    return obj


def _make_uow():
    uow = MagicMock()
    uow.__aenter__ = AsyncMock(return_value=uow)
    uow.__aexit__ = AsyncMock(return_value=False)
    uow.memories = MagicMock()
    uow.memories.get_graph_data = AsyncMock()
    uow.memories.search_visible = AsyncMock()
    uow.memories.get_truth_snapshot = AsyncMock()
    uow.session = MagicMock()
    uow.session.flush = AsyncMock()
    return uow


@pytest_asyncio.fixture
async def client(mock_session_factory):
    from brain.app.api.deps import get_db
    from brain.app.api.main import app

    class _AsyncSession:
        async def flush(self):
            return None

        async def run_sync(self, fn):
            return fn(mock_session_factory)

    async def _get_db():
        yield _AsyncSession()

    overrides = dict(app.dependency_overrides)
    app.dependency_overrides[get_db] = _get_db
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(overrides)


@pytest.mark.asyncio
async def test_graph_endpoint(client, mock_session_factory):
    mem = _make_memory()
    edge = _make_edge()
    mock_uow = _make_uow()
    mock_uow.memories.get_graph_data.return_value = {
        "nodes": [mem],
        "edges": [edge],
    }
    with patch("brain.app.api.routers.memory.UnitOfWork", return_value=mock_uow):
        resp = await client.get("/api/memory/graph")
    assert resp.status_code == 200
    data = resp.json()
    assert "nodes" in data
    assert "edges" in data
    assert len(data["nodes"]) == 1
    assert len(data["edges"]) == 1


@pytest.mark.asyncio
async def test_search_endpoint(client, mock_session_factory):
    mem = _make_memory(content="vector embeddings")
    mock_uow = _make_uow()
    mock_uow.memories.search_visible.return_value = [mem]
    with patch("brain.app.api.routers.memory.UnitOfWork", return_value=mock_uow):
        resp = await client.get("/api/memory/search?q=vector")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["content"] == "vector embeddings"


@pytest.mark.asyncio
async def test_search_requires_query(client, mock_session_factory):
    resp = await client.get("/api/memory/search")
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_truth_endpoint(client, mock_session_factory):
    mem = _make_memory(content="truthy memory")
    truth_snapshot = {
        "memory": mem,
        "state": {
            "truth_status": "reviewed",
            "review_status": "reviewed",
            "confidence": 0.9,
            "freshness_score": 0.8,
        },
        "contradictions": [],
        "reviews": [],
        "conservative_filter_enabled": False,
    }
    mock_uow = _make_uow()
    mock_uow.memories.get_truth_snapshot.return_value = truth_snapshot
    with patch("brain.app.api.routers.memory.UnitOfWork", return_value=mock_uow):
        resp = await client.get("/api/memory/1/truth")
    assert resp.status_code == 200
    data = resp.json()
    assert data["state"]["truth_status"] == "reviewed"
    assert data["memory"]["content"] == "truthy memory"


@pytest.mark.asyncio
async def test_truth_review_promotes_with_evidence_and_confidence(client, mock_session_factory):
    mem = _make_memory(content="review me", memory_tier="episodic")
    truth_snapshot = {
        "memory": mem,
        "state": {
            "truth_status": "reviewed",
            "review_status": "reviewed",
            "confidence": 0.91,
            "freshness_score": 0.91,
        },
        "contradictions": [],
        "reviews": [],
        "conservative_filter_enabled": True,
    }
    with patch("brain.app.api.routers.memory.MemoryRepository") as MockRepo, \
         patch("brain.app.api.routers.memory.async_record_memory_review", new=AsyncMock(return_value={"id": 1})):
        repo = MockRepo.return_value
        repo.a_get_or_raise_visible = AsyncMock(return_value=mem)
        repo.a_list_contradictions = AsyncMock(return_value=[])
        repo.a_get_truth_snapshot = AsyncMock(return_value=truth_snapshot)
        resp = await client.post(
            "/api/memory/1/truth/review",
            json={
                "action": "promote",
                "confidence": 0.91,
                "evidence": {"quote": "Human verified this"},
                "rationale": "Human verified",
                "to_tier": "semantic",
            },
        )

    assert resp.status_code == 200
    assert mem.memory_tier == "semantic"
    assert mem.truth_status == "reviewed"
    assert mem.review_status == "reviewed"


@pytest.mark.asyncio
async def test_truth_review_requires_evidence(client, mock_session_factory):
    mem = _make_memory(content="review me", memory_tier="episodic")
    with patch("brain.app.api.routers.memory.MemoryRepository") as MockRepo:
        repo = MockRepo.return_value
        repo.a_get_or_raise_visible = AsyncMock(return_value=mem)
        repo.a_list_contradictions = AsyncMock(return_value=[])
        resp = await client.post(
            "/api/memory/1/truth/review",
            json={
                "action": "promote",
                "confidence": 0.91,
                "evidence": {},
                "rationale": "No evidence",
                "to_tier": "semantic",
            },
        )

    assert resp.status_code == 400
    assert "evidence" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_truth_review_demotes_with_evidence_and_confidence(client, mock_session_factory):
    mem = _make_memory(content="demote me", memory_tier="semantic", truth_status="reviewed", review_status="reviewed")
    truth_snapshot = {
        "memory": mem,
        "state": {
            "truth_status": "tentative",
            "review_status": "reviewed",
            "confidence": 0.42,
            "freshness_score": 0.4,
        },
        "contradictions": [],
        "reviews": [],
        "conservative_filter_enabled": True,
    }
    with patch("brain.app.api.routers.memory.MemoryRepository") as MockRepo, \
         patch("brain.app.api.routers.memory.async_record_memory_review", new=AsyncMock(return_value={"id": 2})), \
         patch("brain.platform.db.repositories.memory_dag.MemorySummaryRepository") as MockSummaryRepo, \
         patch("brain.platform.db.repositories.narratives.NarrativeRepository") as MockNarrativeRepo:
        repo = MockRepo.return_value
        repo.a_get_or_raise_visible = AsyncMock(return_value=mem)
        repo.a_list_contradictions = AsyncMock(return_value=[])
        repo.a_get_truth_snapshot = AsyncMock(return_value=truth_snapshot)
        MockSummaryRepo.return_value.a_mark_stale_for_memory = AsyncMock(return_value=0)
        MockNarrativeRepo.return_value.a_mark_stale_for_memory = AsyncMock(return_value=0)
        resp = await client.post(
            "/api/memory/1/truth/review",
            json={
                "action": "demote",
                "confidence": 0.42,
                "evidence": {"quote": "This is stale"},
                "rationale": "Human marked this as stale",
                "to_tier": "episodic",
            },
        )

    assert resp.status_code == 200
    assert mem.memory_tier == "episodic"
    assert mem.truth_status == "tentative"
    assert mem.demoted_at is not None
