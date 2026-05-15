"""Tests for GET /api/overview endpoint.

Mocks at the repository class level so no DB is required.

Run: pytest tests/test_overview_endpoint.py -v --tb=short
"""
from __future__ import annotations

from datetime import date, datetime
from unittest.mock import AsyncMock, MagicMock, patch

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
    @patch("brain.app.api.routers.system.SkillRepository")
    @patch("brain.app.api.routers.system.EdgeRepository")
    @patch("brain.app.api.routers.system.MemoryRepository")
    def test_returns_all_keys(
        self,
        MockMemRepo,
        MockEdgeRepo,
        MockSkillRepo,
        MockConsolRepo,
        client,
    ):
        mem = MockMemRepo.return_value
        mem.a_count_active = AsyncMock(return_value=42)
        mem.a_count_by_type = AsyncMock(return_value={"lesson": 10, "pattern": 5})
        mem.a_recent_activity = AsyncMock(return_value=[
            {"type": "memory", "subtype": "lesson", "detail": "learned X", "ts": "2026-03-18T00:00:00"}
        ])
        mem.a_retrieval_accuracy = AsyncMock(return_value=0.85)

        edge = MockEdgeRepo.return_value
        edge.a_count_all = AsyncMock(return_value=18)

        skill_repo = MockSkillRepo.return_value
        skill_repo.a_overview_summary = AsyncMock(return_value=(
            [
                {"name": "test-skill", "maturity": "developing", "use_count": 5},
                {"name": "deploy", "maturity": "mature", "use_count": 12},
            ],
            2,
            17,
        ))

        consol = MockConsolRepo.return_value
        consol.a_list_recent = AsyncMock(return_value=[_fake_consolidation()])

        resp = client.get("/api/overview")
        assert resp.status_code == 200
        data = resp.json()

        # Top-level scalars
        assert data["memories"] == 42
        assert data["edges"] == 18
        assert data["skills"] == 2
        assert data["executions"] == 17  # 5 + 12
        assert data["retrieval_accuracy"] == 0.85

        # Nested structures
        assert data["memory_types"] == {"lesson": 10, "pattern": 5}
        assert len(data["skill_summary"]) == 2
        assert data["skill_summary"][0]["name"] == "test-skill"
        assert len(data["recent_activity"]) == 1

        # Last consolidation
        assert data["last_consolidation"]["status"] == "completed"
        assert data["last_consolidation"]["memories_created"] == 4

    @patch("brain.app.api.routers.system.ConsolidationRunRepository")
    @patch("brain.app.api.routers.system.SkillRepository")
    @patch("brain.app.api.routers.system.EdgeRepository")
    @patch("brain.app.api.routers.system.MemoryRepository")
    def test_empty_state(
        self,
        MockMemRepo,
        MockEdgeRepo,
        MockSkillRepo,
        MockConsolRepo,
        client,
    ):
        """When everything is empty, should return zeros/nulls without error."""
        mem = MockMemRepo.return_value
        mem.a_count_active = AsyncMock(return_value=0)
        mem.a_count_by_type = AsyncMock(return_value={})
        mem.a_recent_activity = AsyncMock(return_value=[])
        mem.a_retrieval_accuracy = AsyncMock(return_value=None)

        MockEdgeRepo.return_value.a_count_all = AsyncMock(return_value=0)
        MockSkillRepo.return_value.a_overview_summary = AsyncMock(return_value=([], 0, 0))

        MockConsolRepo.return_value.a_list_recent = AsyncMock(return_value=[])

        resp = client.get("/api/overview")
        assert resp.status_code == 200
        data = resp.json()

        assert data["memories"] == 0
        assert data["edges"] == 0
        assert data["skills"] == 0
        assert data["executions"] == 0
        assert data["last_consolidation"] is None
        assert data["retrieval_accuracy"] is None
        assert data["memory_types"] == {}
        assert data["skill_summary"] == []
        assert data["recent_activity"] == []
