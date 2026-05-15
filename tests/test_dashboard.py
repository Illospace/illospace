"""Tests for the dashboard API — FastAPI route delegation.

The old Flask-based dashboard was migrated to FastAPI routers in brain.app.api.
These tests verify the API endpoints delegate correctly to the repository layer.

Run: pytest tests/test_dashboard.py -v --tb=short
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.testclient import TestClient

from brain.app.api.main import app


# ═══════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════

def _fake_memory(id=1, content="hi", memory_type="lesson", salience=5.0,
                 tags=None, archived=False, created_at=None):
    m = MagicMock()
    m.id = id
    m.content = content
    m.memory_type = memory_type
    m.salience = salience
    m.emotion_valence = 0.0
    m.emotion_arousal = 0.5
    m.emotion_label = None
    m.source = None
    m.tags = tags or []
    m.access_count = 0
    m.last_accessed = None
    m.archived = archived
    m.created_at = created_at or datetime(2026, 3, 18, 12, 0, 0)
    m.scope = "personal"
    m.visibility = "private"
    m.user_id = None
    m.org_id = None
    return m


# ═══════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════

@pytest.fixture()
def client():
    """TestClient with auth and DB dependencies overridden."""
    from brain.app.api.auth import get_current_user
    from brain.app.api.authorization import service_principal_context
    from brain.app.api.deps import get_db

    app.dependency_overrides[get_current_user] = lambda: service_principal_context("test-dashboard")
    app.dependency_overrides[get_db] = lambda: MagicMock()
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.clear()


# ═══════════════════════════════════════════
# Route tests — verify thin delegation
# ═══════════════════════════════════════════

class TestOverviewRoute:

    @patch("brain.app.api.routers.system.ConsolidationRunRepository")
    @patch("brain.app.api.routers.system.SkillRepository")
    @patch("brain.app.api.routers.system.EdgeRepository")
    @patch("brain.app.api.routers.system.MemoryRepository")
    def test_route_500_on_exception(
        self, MockMemRepo, MockEdgeRepo, MockSkillRepo,
        MockConsolRepo, client,
    ):
        """When a repository raises, the global exception handler returns 500."""
        MockMemRepo.return_value.a_count_active = AsyncMock(side_effect=RuntimeError("db down"))
        resp = client.get("/api/overview")
        assert resp.status_code == 500


class TestMemoryDetailRoute:

    @patch("brain.app.api.routers.memory.UnitOfWork")
    def test_route_memory_detail_404(self, MockUnitOfWork, client):
        uow = MockUnitOfWork.return_value.__aenter__.return_value
        uow.memories.get_or_raise_visible = AsyncMock(side_effect=LookupError("not found"))
        resp = client.get("/api/memory/999")
        assert resp.status_code == 404


class TestSkillsRoute:

    @patch("brain.app.api.routers.skills._ensure_builtin_skill_catalog", new_callable=AsyncMock)
    @patch("brain.app.api.routers.skills.SkillRepository")
    def test_route_enhanced_skills_marks_legacy_projection(self, MockSkillRepo, ensure_catalog, client):
        skill = SimpleNamespace(
            id=1,
            name="develop",
            description="Build features",
            procedure="1. Inspect\n2. Patch",
            version=2,
            skill_type="skill",
            maturity="developing",
            confidence=0.7,
            use_count=4,
            success_count=3,
            failure_count=1,
            partial_count=0,
            avg_duration_sec=None,
            last_used=None,
            pitfalls=[],
            refinements=[],
            triggers=[],
            guardrails=[],
            auto_emerged=False,
            model_tier="medium",
            thinking_tier="medium",
            success_rate=0.75,
            children=[],
            executions=[],
            skill_installation_id=None,
            bundle_version_id=None,
            bundle_digest=None,
            overlay_revision=None,
            effective_digest=None,
            source_kind="legacy_db",
            trust_level="private_local",
            graduated_steps=[],
        )
        MockSkillRepo.return_value.a_list_active = AsyncMock(return_value=[skill])

        resp = client.get("/api/skills/enhanced")

        assert resp.status_code == 200
        ensure_catalog.assert_awaited_once_with()
        data = resp.json()
        assert data[0]["skill"]["name"] == "develop"
        assert data[0]["package"]["package_kind"] == "legacy_db"
        assert data[0]["needs_attention"] is False
        assert data[0]["convert_to_bundle_available"] is True
