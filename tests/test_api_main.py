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


class _AsyncSession:
    def __init__(self, session):
        self.session = session

    async def run_sync(self, fn):
        return fn(self.session)


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

    def fake_get_db():
        yield mock_db

    mock_mem_repo = MagicMock()
    mock_mem_repo.count_active.return_value = 3
    mock_skill_repo = MagicMock()
    mock_skill_repo.list_active.return_value = [1, 2]

    app.dependency_overrides[get_db] = lambda: _AsyncSession(mock_db)
    try:
        with patch("brain.platform.db.repositories.memories.MemoryRepository", return_value=mock_mem_repo), \
        patch("brain.platform.db.repositories.skills.SkillRepository", return_value=mock_skill_repo), \
        patch("brain.systems.runs.event_log.run_event_backbone_status", return_value={
            "consumer_name": "api.websocket_fanout",
            "health": "lagging",
            "lag": 2,
            "consumer_running": True,
        }):
            resp = await client.get("/api/health")
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 200
    data = resp.json()
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

    def scoped_snapshot(scope):
        return [{"org_id": scope.org_id}]

    with patch("brain.app.api.main.asyncio.sleep", new=AsyncMock()), \
        patch(
            "brain.systems.runs.cortex.read_models.serialize_active_runs",
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


def test_sync_publish_drops_unscoped_product_event(monkeypatch):
    ws_manager = MagicMock()
    ws_manager.broadcast_product_event = AsyncMock(return_value=False)
    ws_manager.broadcast = AsyncMock()
    loop = asyncio.new_event_loop()
    monkeypatch.setattr(api_main, "_main_loop", loop)

    try:
        with patch("brain.app.api.routers.ws.ws_manager", ws_manager), patch(
            "brain.systems.cortex.events.resolve_event_org_id",
            return_value=None,
        ):
            api_main._sync_publish("browser_session_frame", {"session_id": "session-1"})
    finally:
        loop.close()
        monkeypatch.setattr(api_main, "_main_loop", None)

    ws_manager.broadcast_product_event.assert_awaited_once_with(
        "browser_session_frame",
        {"session_id": "session-1"},
        org_id=None,
        allow_global=False,
    )
    ws_manager.broadcast.assert_not_called()


def test_sync_publish_resolves_org_scope_before_fanout(monkeypatch):
    ws_manager = MagicMock()
    ws_manager.broadcast_product_event = AsyncMock(return_value=True)
    loop = asyncio.new_event_loop()
    monkeypatch.setattr(api_main, "_main_loop", loop)

    try:
        with patch("brain.app.api.routers.ws.ws_manager", ws_manager), patch(
            "brain.systems.cortex.events.resolve_event_org_id",
            return_value="org-1",
        ):
            api_main._sync_publish("browser_session_frame", {"idea_id": "idea-1"})
    finally:
        loop.close()
        monkeypatch.setattr(api_main, "_main_loop", None)

    ws_manager.broadcast_product_event.assert_awaited_once_with(
        "browser_session_frame",
        {"idea_id": "idea-1", "org_id": "org-1"},
        org_id="org-1",
        allow_global=False,
    )


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
            with patch("brain.app.api.main._ensure_starting_skill_bundle") as ensure_bundle:
                async with api_main.lifespan(app):
                    pass

    ensure_bundle.assert_called_once_with()


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
    with patch("brain.app.api.main._should_start_inline_runner", return_value=True):
        with patch("brain.systems.cortex.events.set_publisher") as mock_set_publisher:
            with patch("brain.systems.runs.cortex.start_runner") as mock_start_runner:
                async with api_main.lifespan(app):
                    pass

    mock_set_publisher.assert_called_once()
    mock_start_runner.assert_called_once()
