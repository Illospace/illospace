"""Tests for costs endpoint and stale-idea detection.

Migrated from raw SQL + dashboard.queries to FastAPI TestClient
with mocked token-usage aggregation.

Run: pytest tests/test_costs.py -v --tb=short
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.exc import SQLAlchemyError
from starlette.testclient import TestClient

from brain.app.api.main import app


# ── Helpers ──

def _fake_run(
    id=1,
    idea_id="idea-001",
    model_used="anthropic/claude-opus-4-6",
    skill_used="develop",
    status="completed",
    tokens_input=1000,
    tokens_output=500,
    estimated_cost=0.05,
    created_at=None,
    event="test",
):
    d = MagicMock()
    d.id = id
    d.idea_id = idea_id
    d.model_used = model_used
    d.skill_used = skill_used
    d.status = status
    d.tokens_input = tokens_input
    d.tokens_output = tokens_output
    d.tokens_total = tokens_input + tokens_output
    d.estimated_cost = estimated_cost
    d.created_at = created_at or datetime.now(timezone.utc)
    d.event = event
    d.cache_read = 0
    d.cache_write = 0
    return d


# ── Fixtures ──

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
    db = MagicMock()
    db.execute = AsyncMock(return_value=SimpleNamespace(all=lambda: []))
    db.scalars = AsyncMock(return_value=SimpleNamespace(all=lambda: []))
    db.scalar = AsyncMock(return_value=0)
    db.get = AsyncMock(return_value=None)
    app.dependency_overrides[get_db] = lambda: db
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.clear()


# ── Tests: GET /api/costs/ ──

class TestGetCosts:

    @patch("brain.app.api.routers.costs.async_summarize_recent_run_usage")
    def test_returns_structure(self, mock_usage, client):
        mock_usage.return_value = []
        result = client.get("/api/costs/")
        assert result.status_code == 200
        data = result.json()
        assert "summary" in data
        assert "month" in data
        assert "by_model" in data
        assert "by_skill" in data
        assert "daily" in data
        assert "top_ideas" in data

    @patch("brain.app.api.routers.costs.async_summarize_recent_run_usage")
    def test_summary_counts(self, mock_usage, client):
        idea = "idea-001"
        runs = [
            _fake_run(id=1, idea_id=idea, estimated_cost=0.10,
                           tokens_input=2000, tokens_output=1000),
            _fake_run(id=2, idea_id=idea, estimated_cost=0.05,
                           tokens_input=500, tokens_output=200),
        ]
        mock_usage.return_value = runs

        result = client.get("/api/costs/")
        data = result.json()
        s = data["summary"]
        assert s["total_runs"] == 2
        assert s["total_cost"] >= 0.15

    @patch("brain.app.api.routers.costs.async_summarize_recent_run_usage")
    def test_by_model_groups(self, mock_usage, client):
        runs = [
            _fake_run(id=1, model_used="anthropic/claude-opus-4-6", estimated_cost=0.10),
            _fake_run(id=2, model_used="anthropic/claude-haiku-4-5", estimated_cost=0.01),
        ]
        mock_usage.return_value = runs

        result = client.get("/api/costs/")
        data = result.json()
        models = {m["model"]: m for m in data["by_model"]}
        assert "anthropic/claude-opus-4-6" in models
        assert "anthropic/claude-haiku-4-5" in models

    @patch("brain.app.api.routers.costs.async_summarize_recent_run_usage")
    def test_by_model_normalizes_provider_prefixes(self, mock_usage, client):
        runs = [
            _fake_run(id=1, model_used="openai:gpt-5.4", estimated_cost=0.10),
            _fake_run(id=2, model_used="openai/gpt-5.4", estimated_cost=0.02),
            _fake_run(id=3, model_used="gpt-5.4", estimated_cost=0.03),
        ]
        mock_usage.return_value = runs

        result = client.get("/api/costs/")
        data = result.json()
        models = {m["model"]: m for m in data["by_model"]}
        assert set(models) == {"openai/gpt-5.4"}
        assert models["openai/gpt-5.4"]["provider"] == "openai"
        assert models["openai/gpt-5.4"]["normalized_model"] == "gpt-5.4"
        assert models["openai/gpt-5.4"]["runs"] == 3

    @patch("brain.app.api.routers.costs.async_summarize_recent_run_usage")
    def test_by_skill_groups(self, mock_usage, client):
        runs = [
            _fake_run(id=1, skill_used="develop", estimated_cost=0.10),
            _fake_run(id=2, skill_used="investigate", estimated_cost=0.02),
        ]
        mock_usage.return_value = runs

        result = client.get("/api/costs/")
        data = result.json()
        skills = {s["skill"]: s for s in data["by_skill"]}
        assert "develop" in skills
        assert "investigate" in skills

    @patch("brain.app.api.routers.costs.async_summarize_recent_run_usage")
    def test_daily_list(self, mock_usage, client):
        runs = [
            _fake_run(id=1, created_at=datetime(2026, 3, 20, 12, 0, 0, tzinfo=timezone.utc)),
            _fake_run(id=2, created_at=datetime(2026, 3, 21, 12, 0, 0, tzinfo=timezone.utc)),
        ]
        mock_usage.return_value = runs

        result = client.get("/api/costs/")
        data = result.json()
        assert isinstance(data["daily"], list)
        assert len(data["daily"]) >= 1

    @patch("brain.app.api.routers.costs.async_summarize_recent_run_usage")
    def test_month_cost(self, mock_usage, client):
        runs = [
            _fake_run(id=1, estimated_cost=0.25,
                           created_at=datetime.now(timezone.utc)),
        ]
        mock_usage.return_value = runs

        result = client.get("/api/costs/")
        data = result.json()
        assert float(data["month"]["month_cost"]) >= 0.25

    @patch("brain.app.api.routers.costs.async_summarize_recent_run_usage")
    def test_empty_runs(self, mock_usage, client):
        """No runs should return zeroed summary."""
        mock_usage.return_value = []

        result = client.get("/api/costs/")
        data = result.json()
        assert data["summary"]["total_runs"] == 0
        assert data["summary"]["total_cost"] == 0

    @patch("brain.app.api.routers.costs.async_summarize_recent_run_usage")
    def test_top_ideas_limited(self, mock_usage, client):
        """Top ideas list should be capped at 10."""
        # Create 15 runs with different idea_ids
        runs = [
            _fake_run(id=i, idea_id=f"idea-{i:03d}", estimated_cost=0.01)
            for i in range(15)
        ]
        mock_usage.return_value = runs

        result = client.get("/api/costs/")
        data = result.json()
        assert len(data["top_ideas"]) <= 10


# ── Tests: GET /api/brain/stale-ideas ──

class TestGetStaleIdeas:

    @patch("brain.app.api.routers.brain.stale_ideas")
    def test_returns_list(self, mock_stale_fn, client):
        """Stale ideas endpoint should return a list."""
        mock_stale_fn.return_value = []
        resp = client.get("/api/stale-ideas?threshold=30")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)


def test_run_breakdown_wraps_legacy_agent_api_calls_table():
    from brain.app.api.routers.costs import run_breakdown

    db = MagicMock()
    db.execute = AsyncMock(side_effect=SQLAlchemyError("legacy table"))
    db.rollback = AsyncMock()

    result = asyncio.run(run_breakdown(42, db=db, user={"id": "system"}))

    assert result == {
        "run_id": 42,
        "trace_id": "run:42",
        "turns": [],
        "summary": None,
    }
    assert db.rollback.await_count == 2


@pytest.mark.asyncio
async def test_async_summarize_recent_run_usage_uses_async_session():
    from brain.systems.runs.token_usage import async_summarize_recent_run_usage

    created_at = datetime(2026, 5, 13, tzinfo=timezone.utc)
    run = SimpleNamespace(
        id=7,
        trace_id="trace-7",
        thread_id="idea-7",
        profile="fast",
        recipe=None,
        status="completed",
        created_at=created_at,
        metadata_={},
        target_ref={},
        model_policy={},
    )
    call = SimpleNamespace(
        run_id=7,
        model="openai/gpt-5.4",
        api_calls=2,
        tokens_input=10,
        tokens_output=5,
        cache_read=3,
        cache_write=1,
        last_call_at=created_at,
    )

    session = MagicMock()
    session.scalars = AsyncMock(return_value=SimpleNamespace(all=lambda: [run]))
    session.execute = AsyncMock(return_value=SimpleNamespace(all=lambda: [call]))

    rows = await async_summarize_recent_run_usage(session, limit=10, org_id="org-1")

    assert rows[0]["id"] == 7
    assert rows[0]["tokens_total"] == 15
    assert rows[0]["api_calls"] == 2
    assert rows[0]["model_used"] == "openai/gpt-5.4"
