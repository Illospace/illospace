# tests/test_api_main.py
"""Smoke tests for FastAPI app startup and health endpoint."""
import asyncio

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from unittest.mock import AsyncMock, MagicMock, patch

import brain.app.api.main as api_main
from brain.app.api.deps import get_db

app = api_main.app


@pytest_asyncio.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_health(client):
    resp = await client.get("/api/health")
    assert resp.status_code == 200
    data = resp.json()
    # Health endpoint returns status: "ok" when DB is up, "degraded" when DB is down
    assert data["status"] in ("ok", "degraded")


@pytest.mark.asyncio
async def test_health_includes_run_event_backbone(client):
    mock_db = MagicMock()
    mock_db.scalar = AsyncMock(side_effect=[3, 2])

    app.dependency_overrides[get_db] = lambda: mock_db
    try:
        with patch("brain.app.ops.health._apply_statement_timeout", new=AsyncMock()), \
        patch("brain.systems.runs.event_log.async_run_event_backbone_status", new=AsyncMock(return_value={
            "consumer_name": "api.websocket_fanout",
            "health": "lagging",
            "lag": 2,
            "consumer_running": True,
        })):
            resp = await client.get("/api/health")
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 200
    data = resp.json()
    assert data["memory_count"] == 3
    assert data["skill_count"] == 2
    assert data["run_event_backbone"]["health"] == "lagging"
    assert data["run_event_backbone"]["lag"] == 2
    assert data["run_event_backbone"]["consumer_running"] is True


@pytest.mark.asyncio
async def test_openapi_spec(client):
    resp = await client.get("/api/openapi.json")
    assert resp.status_code == 200
    assert "paths" in resp.json()


@pytest.mark.asyncio
async def test_flush_ops_snapshot_broadcasts_scoped_snapshot_per_org():
    ws_manager = MagicMock()
    ws_manager.connected_org_ids = ["org-a", "org-b"]
    ws_manager.broadcast_to_org = AsyncMock()

    async def scoped_snapshot(scope):
        return [{"org_id": scope.org_id}]

    with patch("brain.app.api.main.asyncio.sleep", new=AsyncMock()), \
        patch(
            "brain.systems.runs.cortex.read_models.serialize_active_runs_async",
            side_effect=scoped_snapshot,
        ) as serialize, \
        patch("brain.app.api.routers.ws.ws_manager", ws_manager):
        await api_main._flush_ops_snapshot()

    assert [call.args[0].org_id for call in serialize.call_args_list] == [
        "org-a",
        "org-b",
    ]
    assert ws_manager.broadcast_to_org.await_args_list[0].args == (
        "org-a",
        "ops_update",
        {"runs": [{"org_id": "org-a"}]},
    )
    assert ws_manager.broadcast_to_org.await_args_list[1].args == (
        "org-b",
        "ops_update",
        {"runs": [{"org_id": "org-b"}]},
    )


@pytest.mark.asyncio
async def test_product_event_publish_drops_unscoped_product_event(monkeypatch):
    ws_manager = MagicMock()
    ws_manager.broadcast_product_event = AsyncMock(return_value=False)
    ws_manager.broadcast = AsyncMock()
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(api_main, "_main_loop", loop)

    with patch("brain.app.api.routers.ws.ws_manager", ws_manager), patch(
        "brain.systems.cortex.events.resolve_event_org_id_async",
        AsyncMock(return_value=None),
    ):
        api_main._schedule_product_event_publish("browser_session_frame", {"session_id": "session-1"})
        await asyncio.sleep(0)
        await asyncio.sleep(0)
    monkeypatch.setattr(api_main, "_main_loop", None)

    ws_manager.broadcast_product_event.assert_awaited_once_with(
        "browser_session_frame",
        {"session_id": "session-1"},
        org_id=None,
        allow_global=False,
    )
    ws_manager.broadcast.assert_not_called()


@pytest.mark.asyncio
async def test_product_event_publish_resolves_org_scope_before_fanout(monkeypatch):
    ws_manager = MagicMock()
    ws_manager.broadcast_product_event = AsyncMock(return_value=True)
    resolve_org = AsyncMock(return_value="org-1")

    loop = asyncio.get_running_loop()
    monkeypatch.setattr(api_main, "_main_loop", loop)

    with patch("brain.app.api.routers.ws.ws_manager", ws_manager), patch(
        "brain.systems.cortex.events.resolve_event_org_id_async",
        resolve_org,
    ):
        api_main._schedule_product_event_publish("browser_session_frame", {"idea_id": "idea-1"})
        await asyncio.sleep(0)
        await asyncio.sleep(0)
    monkeypatch.setattr(api_main, "_main_loop", None)

    ws_manager.broadcast_product_event.assert_awaited_once_with(
        "browser_session_frame",
        {"idea_id": "idea-1", "org_id": "org-1"},
        org_id="org-1",
        allow_global=False,
    )
    resolve_org.assert_awaited_once_with({"idea_id": "idea-1"})


@pytest.mark.asyncio
async def test_ensure_starting_skill_bundle_awaits_async_catalog():
    ensure = AsyncMock()

    with patch("brain.systems.skills.builtin.ensure_builtin_skills_cached", ensure):
        await api_main._ensure_starting_skill_bundle()

    ensure.assert_awaited_once_with(ttl_seconds=0)


@pytest.mark.asyncio
async def test_lifespan_skips_inline_runner_by_default():
    with patch("brain.app.api.main._should_start_inline_runner", return_value=False):
        with patch("brain.systems.cortex.events.set_publisher") as mock_set_publisher:
            with patch("brain.systems.runs.cortex.start_runner") as mock_start_runner:
                async with api_main.lifespan(app):
                    pass

    mock_set_publisher.assert_called_once()
    mock_start_runner.assert_not_called()


@pytest.mark.asyncio
async def test_lifespan_ensures_starting_skill_bundle():
    with patch("brain.app.api.main._should_start_inline_runner", return_value=False):
        with patch("brain.systems.cortex.events.set_publisher"):
            with patch("brain.app.api.main._ensure_starting_skill_bundle", new=AsyncMock()) as ensure_bundle:
                async with api_main.lifespan(app):
                    pass

    ensure_bundle.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_lifespan_hosts_scheduler_overdue_monitor_outside_daemon():
    monitor_started = asyncio.Event()

    async def monitor_loop():
        monitor_started.set()
        await asyncio.Future()

    monitor = MagicMock()
    monitor.name = "scheduler_overdue_monitor"
    monitor.run = monitor_loop

    with patch("brain.app.api.main._should_start_inline_runner", return_value=False):
        with patch("brain.app.api.main._should_start_run_event_consumer", return_value=False):
            with patch("brain.systems.cortex.events.set_publisher"):
                with patch(
                    "brain.app.api.main._ensure_starting_skill_bundle",
                    new=AsyncMock(),
                ):
                    with patch(
                        "brain.app.api.main.SchedulerOverdueMonitor",
                        return_value=monitor,
                    ) as monitor_type:
                        async with api_main.lifespan(app):
                            await asyncio.wait_for(monitor_started.wait(), timeout=1)
                            assert api_main._scheduler_overdue_monitor_task is not None
                            assert (
                                api_main._scheduler_overdue_monitor_task.get_name()
                                == "scheduler_overdue_monitor"
                            )

    monitor_type.assert_called_once_with()
    assert api_main._scheduler_overdue_monitor_task is None


@pytest.mark.asyncio
async def test_lifespan_hosts_stale_run_reaper_outside_daemon():
    reaper_started = asyncio.Event()

    async def reaper_loop():
        reaper_started.set()
        await asyncio.Future()

    reaper = MagicMock()
    reaper.name = "stale_run_reaper"
    reaper.run = reaper_loop

    with patch("brain.app.api.main._should_start_inline_runner", return_value=False):
        with patch("brain.app.api.main._should_start_run_event_consumer", return_value=False):
            with patch("brain.systems.cortex.events.set_publisher"):
                with patch(
                    "brain.app.api.main._ensure_starting_skill_bundle",
                    new=AsyncMock(),
                ):
                    with patch(
                        "brain.app.api.main.StaleRunReaper",
                        return_value=reaper,
                    ) as reaper_type:
                        async with api_main.lifespan(app):
                            await asyncio.wait_for(reaper_started.wait(), timeout=1)
                            assert api_main._stale_run_reaper_task is not None
                            assert (
                                api_main._stale_run_reaper_task.get_name()
                                == "stale_run_reaper"
                            )

    reaper_type.assert_called_once_with()
    assert api_main._stale_run_reaper_task is None


def test_inline_runner_honors_launcher_dispatcher_env(monkeypatch):
    monkeypatch.delenv("CORTEX_INLINE_RUNNER", raising=False)
    monkeypatch.setenv("CORTEX_INLINE_DISPATCHER", "1")

    assert api_main._should_start_inline_runner() is True


def test_inline_runner_env_can_disable_launcher_dispatcher(monkeypatch):
    monkeypatch.setenv("CORTEX_INLINE_RUNNER", "0")
    monkeypatch.setenv("CORTEX_INLINE_DISPATCHER", "1")

    assert api_main._should_start_inline_runner() is False


@pytest.mark.asyncio
async def test_lifespan_can_start_inline_runner_when_enabled():
    from brain.systems.runs.cortex import DrainResult

    with patch("brain.app.api.main._should_start_inline_runner", return_value=True):
        with patch("brain.systems.cortex.events.set_publisher") as mock_set_publisher:
            with patch("brain.systems.runs.cortex.start_runner") as mock_start_runner:
                with patch("brain.systems.cycles.start_cycle_scheduler"), patch(
                    "brain.systems.cycles.stop_cycle_scheduler",
                ), patch(
                    "brain.systems.runs.cortex.stop_runner",
                    return_value=DrainResult(),
                ):
                    async with api_main.lifespan(app):
                        pass

    mock_set_publisher.assert_called_once()
    mock_start_runner.assert_called_once()


@pytest.mark.asyncio
async def test_lifespan_recovers_inline_runs_that_time_out_during_drain():
    from datetime import datetime, timezone

    from brain.systems.runs.cortex import DrainResult
    from brain.systems.runs.interruption import RunInterruption

    recovered = (
        RunInterruption(
            run_id=2330,
            reason="worker_shutdown_drain_timeout",
            interrupted_at=datetime(2026, 7, 22, 17, 55, tzinfo=timezone.utc),
            requeued=True,
        ),
    )
    recover = AsyncMock(return_value=recovered)
    with patch("brain.app.api.main._should_start_inline_runner", return_value=True):
        with patch("brain.systems.cortex.events.set_publisher"):
            with patch("brain.systems.runs.cortex.start_runner"):
                with patch("brain.systems.cycles.start_cycle_scheduler"), patch(
                    "brain.systems.cycles.stop_cycle_scheduler",
                ), patch(
                    "brain.systems.runs.cortex.stop_runner",
                    return_value=DrainResult(timed_out_run_ids=(2330,)),
                ), patch(
                    "brain.systems.runs.interruption.interrupt_and_requeue_run_ids",
                    new=recover,
                ):
                    async with api_main.lifespan(app):
                        pass

    recover.assert_awaited_once_with(
        (2330,),
        reason="worker_shutdown_drain_timeout",
    )
