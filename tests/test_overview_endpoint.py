"""Tests for GET /api/overview endpoint.

Mocks at the repository class level so no DB is required.

Run: pytest tests/test_overview_endpoint.py -v --tb=short
"""
from __future__ import annotations

from datetime import date, datetime
from unittest.mock import MagicMock, patch

import pytest
from starlette.testclient import TestClient

from brain.app.api.main import app

# ── Helpers ──────────────────────────────────────────────────


def _fake_skill(name: str = "test-skill", maturity: str = "developing", use_count: int = 5):
    s = MagicMock()
    s.name = name
    s.maturity = maturity
    s.use_count = use_count
    return s


def _fake_emotion(
    label="curious",
    valence=0.7,
    arousal=0.5,
    trigger_summary="learning",
    timestamp=None,
):
    e = MagicMock()
    e.label = label
    e.valence = valence
    e.arousal = arousal
    e.trigger_summary = trigger_summary
    e.timestamp = timestamp or datetime(2026, 3, 18, 12, 0, 0)
    return e


def _fake_consolidation():
    c = MagicMock()
    c.status = "completed"
    c.run_date = date(2026, 3, 17)
    c.completed_at = datetime(2026, 3, 17, 3, 0, 0)
    c.memories_created = 4
    c.edges_created = 2
    c.memories_decayed = 1
    return c


# ── Fixtures ─────────────────────────────────────────────────


@pytest.fixture()
def client():
    """TestClient with auth and DB dependencies overridden."""
    from brain.app.api.auth import get_current_user
    from brain.app.api.deps import get_db

    app.dependency_overrides[get_current_user] = lambda: {
        "id": "system",
        "role": "admin",
        "internal": True,
    }
    app.dependency_overrides[get_db] = lambda: MagicMock()
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.clear()


# ── Tests ────────────────────────────────────────────────────


class TestOverviewEndpoint:
    """GET /api/overview returns a well-structured overview payload."""

    @patch("brain.app.api.routers.system.ConsolidationRunRepository")
    @patch("brain.app.api.routers.system.EmotionRepository")
    @patch("brain.app.api.routers.system.SkillRepository")
    @patch("brain.app.api.routers.system.EdgeRepository")
    @patch("brain.app.api.routers.system.MemoryRepository")
    def test_returns_all_keys(
        self,
        MockMemRepo,
        MockEdgeRepo,
        MockSkillRepo,
        MockEmotionRepo,
        MockConsolRepo,
        client,
    ):
        mem = MockMemRepo.return_value
        mem.count_active.return_value = 42
        mem.count_by_type.return_value = {"lesson": 10, "pattern": 5}
        mem.recent_activity.return_value = [
            {"type": "memory", "subtype": "lesson", "detail": "learned X", "ts": "2026-03-18T00:00:00"}
        ]
        mem.retrieval_accuracy.return_value = 0.85

        edge = MockEdgeRepo.return_value
        edge.count_all.return_value = 18

        skill_repo = MockSkillRepo.return_value
        skill_repo.overview_summary.return_value = (
            [
                {"name": "test-skill", "maturity": "developing", "use_count": 5},
                {"name": "deploy", "maturity": "mature", "use_count": 12},
            ],
            2,
            17,
        )

        emotion = MockEmotionRepo.return_value
        emotion.count_all.return_value = 99
        emotion.avg_valence_7d.return_value = 0.65
        emotion.list_recent.return_value = [_fake_emotion()]

        consol = MockConsolRepo.return_value
        consol.list_recent.return_value = [_fake_consolidation()]

        resp = client.get("/api/overview")
        assert resp.status_code == 200
        data = resp.json()

        # Top-level scalars
        assert data["memories"] == 42
        assert data["edges"] == 18
        assert data["skills"] == 2
        assert data["executions"] == 17  # 5 + 12
        assert data["snapshots"] == 99
        assert data["avg_valence_7d"] == 0.65
        assert data["retrieval_accuracy"] == 0.85

        # Nested structures
        assert data["memory_types"] == {"lesson": 10, "pattern": 5}
        assert len(data["skill_summary"]) == 2
        assert data["skill_summary"][0]["name"] == "test-skill"
        assert len(data["recent_activity"]) == 1

        # Latest emotion
        assert data["latest_emotion"]["label"] == "curious"
        assert data["latest_emotion"]["valence"] == 0.7

        # Last consolidation
        assert data["last_consolidation"]["status"] == "completed"
        assert data["last_consolidation"]["memories_created"] == 4

    @patch("brain.app.api.routers.system.ConsolidationRunRepository")
    @patch("brain.app.api.routers.system.EmotionRepository")
    @patch("brain.app.api.routers.system.SkillRepository")
    @patch("brain.app.api.routers.system.EdgeRepository")
    @patch("brain.app.api.routers.system.MemoryRepository")
    def test_empty_state(
        self,
        MockMemRepo,
        MockEdgeRepo,
        MockSkillRepo,
        MockEmotionRepo,
        MockConsolRepo,
        client,
    ):
        """When everything is empty, should return zeros/nulls without error."""
        mem = MockMemRepo.return_value
        mem.count_active.return_value = 0
        mem.count_by_type.return_value = {}
        mem.recent_activity.return_value = []
        mem.retrieval_accuracy.return_value = None

        MockEdgeRepo.return_value.count_all.return_value = 0
        MockSkillRepo.return_value.overview_summary.return_value = ([], 0, 0)

        emotion = MockEmotionRepo.return_value
        emotion.count_all.return_value = 0
        emotion.avg_valence_7d.return_value = None
        emotion.list_recent.return_value = []

        MockConsolRepo.return_value.list_recent.return_value = []

        resp = client.get("/api/overview")
        assert resp.status_code == 200
        data = resp.json()

        assert data["memories"] == 0
        assert data["edges"] == 0
        assert data["skills"] == 0
        assert data["executions"] == 0
        assert data["snapshots"] == 0
        assert data["avg_valence_7d"] is None
        assert data["latest_emotion"] is None
        assert data["last_consolidation"] is None
        assert data["retrieval_accuracy"] is None
        assert data["memory_types"] == {}
        assert data["skill_summary"] == []
        assert data["recent_activity"] == []

    @patch("brain.app.api.routers.system.ConsolidationRunRepository")
    @patch("brain.app.api.routers.system.EmotionRepository")
    @patch("brain.app.api.routers.system.SkillRepository")
    @patch("brain.app.api.routers.system.EdgeRepository")
    @patch("brain.app.api.routers.system.MemoryRepository")
    def test_response_types(
        self,
        MockMemRepo,
        MockEdgeRepo,
        MockSkillRepo,
        MockEmotionRepo,
        MockConsolRepo,
        client,
    ):
        """Verify correct types for every top-level key."""
        mem = MockMemRepo.return_value
        mem.count_active.return_value = 1
        mem.count_by_type.return_value = {"lesson": 1}
        mem.recent_activity.return_value = []
        mem.retrieval_accuracy.return_value = 0.9

        MockEdgeRepo.return_value.count_all.return_value = 0
        MockSkillRepo.return_value.overview_summary.return_value = (
            [{"name": "test-skill", "maturity": "developing", "use_count": 5}],
            1,
            5,
        )

        emotion = MockEmotionRepo.return_value
        emotion.count_all.return_value = 1
        emotion.avg_valence_7d.return_value = 0.5
        emotion.list_recent.return_value = [_fake_emotion()]

        MockConsolRepo.return_value.list_recent.return_value = [_fake_consolidation()]

        resp = client.get("/api/overview")
        data = resp.json()

        assert isinstance(data["memories"], int)
        assert isinstance(data["edges"], int)
        assert isinstance(data["skills"], int)
        assert isinstance(data["executions"], int)
        assert isinstance(data["snapshots"], int)
        assert isinstance(data["avg_valence_7d"], (float, int))
        assert isinstance(data["retrieval_accuracy"], (float, int))
        assert isinstance(data["latest_emotion"], dict)
        assert isinstance(data["memory_types"], dict)
        assert isinstance(data["skill_summary"], list)
        assert isinstance(data["recent_activity"], list)
        assert isinstance(data["last_consolidation"], dict)
