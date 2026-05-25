"""Health tier and ops snapshot tests."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

import brain.app.api.main as api_main
from brain.app.ops import health


@pytest_asyncio.fixture
async def client():
    transport = ASGITransport(app=api_main.app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


class _Result:
    def __init__(self, *, scalar_value=None, scalar_values=None):
        self._scalar_value = scalar_value
        self._scalar_values = list(scalar_values or [])

    def scalar(self):
        return self._scalar_value

    def scalars(self):
        return SimpleNamespace(all=lambda: self._scalar_values)


class _ScalarResult:
    def __init__(self, values):
        self._values = list(values)

    def all(self):
        return self._values


@pytest.mark.asyncio
async def test_live_endpoint_is_cheap_and_ok(client):
    resp = await client.get("/api/health/live")

    assert resp.status_code == 200
    data = resp.json()
    assert data["tier"] == "live"
    assert data["status"] == "alive"
    assert data["ok"] is True


@pytest.mark.asyncio
async def test_readiness_snapshot_checks_database_migration_and_event_backbone(monkeypatch):
    session = MagicMock()
    session.execute = AsyncMock(side_effect=[
        _Result(scalar_value=1),
        _Result(scalar_values=["head-1"]),
    ])

    monkeypatch.setattr(health, "_apply_statement_timeout", AsyncMock())
    monkeypatch.setattr(health, "_alembic_head_revisions", lambda: {"head-1"})
    monkeypatch.setattr(
        "brain.systems.runs.event_log.async_run_event_backbone_status",
        AsyncMock(return_value={
            "consumer_name": "api.websocket_fanout",
            "consumer_running": True,
            "health": "healthy",
            "lag": 0,
            "last_error": None,
        }),
    )

    snapshot = await health.readiness_health_snapshot(consumer_running=True, session=session)

    assert snapshot["status"] == "ready"
    assert snapshot["ready"] is True
    assert snapshot["checks"]["database"]["status"] == "ok"
    assert snapshot["checks"]["migration_head"]["status"] == "ok"
    assert snapshot["checks"]["event_backbone"]["status"] == "ok"


@pytest.mark.asyncio
async def test_readiness_snapshot_fails_when_migration_is_behind(monkeypatch):
    session = MagicMock()
    session.execute = AsyncMock(side_effect=[
        _Result(scalar_value=1),
        _Result(scalar_values=["old-head"]),
    ])

    monkeypatch.setattr(health, "_apply_statement_timeout", AsyncMock())
    monkeypatch.setattr(health, "_alembic_head_revisions", lambda: {"new-head"})
    monkeypatch.setattr(
        "brain.systems.runs.event_log.async_run_event_backbone_status",
        AsyncMock(return_value={"health": "healthy", "lag": 0, "last_error": None}),
    )

    snapshot = await health.readiness_health_snapshot(consumer_running=True, session=session)

    assert snapshot["status"] == "not_ready"
    assert snapshot["ready"] is False
    assert snapshot["checks"]["migration_head"]["status"] == "failed"
    assert snapshot["failures"] == [
        {
            "check": "migration_head",
            "status": "failed",
            "summary": "database is not at Alembic head",
        }
    ]


@pytest.mark.asyncio
async def test_readiness_snapshot_fails_when_event_backbone_has_error(monkeypatch):
    session = MagicMock()
    session.execute = AsyncMock(side_effect=[
        _Result(scalar_value=1),
        _Result(scalar_values=["head-1"]),
    ])

    monkeypatch.setattr(health, "_apply_statement_timeout", AsyncMock())
    monkeypatch.setattr(health, "_alembic_head_revisions", lambda: {"head-1"})
    monkeypatch.setattr(
        "brain.systems.runs.event_log.async_run_event_backbone_status",
        AsyncMock(return_value={
            "consumer_name": "api.websocket_fanout",
            "consumer_running": True,
            "health": "degraded",
            "lag": 0,
            "last_error": "broadcast failed",
        }),
    )

    snapshot = await health.readiness_health_snapshot(consumer_running=True, session=session)

    assert snapshot["status"] == "not_ready"
    assert snapshot["checks"]["event_backbone"]["status"] == "failed"
    assert snapshot["failures"][0]["check"] == "event_backbone"


@pytest.mark.asyncio
async def test_ready_endpoint_returns_503_when_not_ready(client):
    with patch("brain.app.api.routers.system.readiness_health_snapshot", new=AsyncMock(return_value={
        "tier": "ready",
        "status": "not_ready",
        "ready": False,
        "ok": False,
        "checks": {},
        "failures": [{"check": "database", "status": "failed", "summary": "database query failed"}],
    })):
        resp = await client.get("/api/health/ready")

    assert resp.status_code == 503
    assert resp.json()["status"] == "not_ready"


@pytest.mark.asyncio
async def test_deep_health_reports_degradation_without_secrets(monkeypatch):
    from brain.platform.provider_health import record_provider_failure, reset_provider_health

    reset_provider_health()
    record_provider_failure(
        operation_type="scout",
        provider="openai",
        model="gpt-5.4-mini",
        exc="provider failed with sk-test-secret-token",
    )

    async def embedding_check(_session=None):
        return health.HealthCheck(
            name="embedding",
            status="ok",
            summary="embedding configured",
            latency_ms=1,
            details={"api_key_configured": True},
        )

    monkeypatch.setattr(
        health,
        "_embedding_health_check",
        embedding_check,
    )

    async def scheduler_check(_session=None):
        return health.HealthCheck(
            name="scheduler",
            status="ok",
            summary="scheduler healthy",
            latency_ms=1,
        )

    async def run_check(_session=None):
        return health.HealthCheck(
            name="run",
            status="degraded",
            summary="1 recent failed run",
            latency_ms=1,
            details={"recent_failures": [{"error": "Bearer super-secret-token"}]},
        )

    async def cycle_backlog_check(_session=None):
        return health.HealthCheck(
            name="legacy_cycle_backlog",
            status="ok",
            summary="legacy cycle scheduler has no stale due work",
            latency_ms=1,
        )

    monkeypatch.setattr(health, "_scheduler_health_check", scheduler_check)
    monkeypatch.setattr(health, "_run_health_check", run_check)
    monkeypatch.setattr(health, "_legacy_cycle_backlog_health_check", cycle_backlog_check)

    snapshot = await health.deep_health_snapshot(consumer_running=True)
    payload = str(snapshot)

    assert snapshot["tier"] == "deep"
    assert snapshot["status"] == "unhealthy"
    assert snapshot["checks"]["providers"]["status"] == "failed"
    assert "sk-test-secret-token" not in payload
    assert "super-secret-token" not in payload
    assert "[redacted]" in payload
    reset_provider_health()


@pytest.mark.asyncio
async def test_embedding_health_uses_db_runtime_memory_credentials(monkeypatch):
    import brain.systems.runtime_settings.memory as memory_settings

    session = MagicMock()
    runtime = memory_settings.EmbeddingRuntimeConfig(
        backend="api",
        provider="gemini",
        api_model="gemini-embedding-2",
        cpu_model="all-MiniLM-L6-v2",
        dimensions=768,
        api_key="db-backed-memory-key",
    )
    monkeypatch.setattr(
        memory_settings,
        "async_get_embedding_runtime_config",
        AsyncMock(return_value=runtime),
    )

    check = await health._embedding_health_check(session)

    assert check.status == "ok"
    assert check.details["settings_source"] == "db_runtime_memory"
    assert check.details["api_key_configured"] is True
    assert "db-backed-memory-key" not in str(check.to_dict())


@pytest.mark.asyncio
async def test_deep_health_exposes_legacy_cycle_backlog(monkeypatch):
    async def embedding_check(_session=None):
        return health.HealthCheck(
            name="embedding",
            status="ok",
            summary="embedding configured",
            latency_ms=1,
        )

    async def scheduler_check(_session=None):
        return health.HealthCheck(
            name="scheduler",
            status="ok",
            summary="scheduler healthy",
            latency_ms=1,
        )

    async def run_check(_session=None):
        return health.HealthCheck(
            name="run",
            status="ok",
            summary="run queue healthy",
            latency_ms=1,
        )

    async def cycle_backlog_check(_session=None):
        return health.HealthCheck(
            name="legacy_cycle_backlog",
            status="degraded",
            summary="1 stale due cycle(s)",
            latency_ms=1,
            details={"stale_due_cycles_count": 1, "stale_active_cycle_runs_count": 0},
        )

    def provider_check():
        return health.HealthCheck(
            name="providers",
            status="ok",
            summary="providers healthy",
            latency_ms=1,
        )

    monkeypatch.setattr(health, "_embedding_health_check", embedding_check)
    monkeypatch.setattr(health, "_provider_health_check", provider_check)
    monkeypatch.setattr(health, "_scheduler_health_check", scheduler_check)
    monkeypatch.setattr(health, "_run_health_check", run_check)
    monkeypatch.setattr(health, "_legacy_cycle_backlog_health_check", cycle_backlog_check)

    snapshot = await health.deep_health_snapshot(session=MagicMock())

    assert snapshot["status"] == "degraded"
    assert snapshot["checks"]["legacy_cycle_backlog"]["status"] == "degraded"
    assert snapshot["checks"]["legacy_cycle_backlog"]["details"]["stale_due_cycles_count"] == 1


@pytest.mark.asyncio
async def test_legacy_cycle_backlog_health_reports_stale_due_cycles(monkeypatch):
    now = datetime(2026, 5, 25, 12, 0, tzinfo=timezone.utc)
    cycle = SimpleNamespace(
        id=7,
        user_id="user-1",
        org_id="org-1",
        name="Daily digest",
        enabled=True,
        next_run_at=now - timedelta(minutes=45),
        last_run_at=None,
        last_status=None,
        last_error=None,
    )
    cycle_run = SimpleNamespace(
        id=11,
        cycle_id=7,
        status="queued",
        scheduled_for=now - timedelta(minutes=40),
        started_at=None,
        completed_at=None,
        skip_reason=None,
        error=None,
        idea_id=None,
        run_id=None,
    )
    session = MagicMock()
    session.scalar = AsyncMock(side_effect=[1, 1])
    session.scalars = AsyncMock(
        side_effect=[_ScalarResult([cycle]), _ScalarResult([cycle_run])]
    )
    monkeypatch.setattr(health, "_apply_statement_timeout", AsyncMock())
    monkeypatch.setattr(health, "_utc_now", lambda: now)

    check = await health._legacy_cycle_backlog_health_check(session)

    assert check.status == "degraded"
    assert check.summary == "1 stale due cycle(s), 1 stale active cycle run(s)"
    assert check.details["stale_due_cycles"][0]["id"] == 7
    assert check.details["stale_active_cycle_runs"][0]["id"] == 11
    assert "user-1" not in str(check.to_dict())
    assert "org-1" not in str(check.to_dict())
    assert "Daily digest" not in str(check.to_dict())
