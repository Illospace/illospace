from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from brain.app.api.ws.auth import WsTokenClaims
from brain.app.api.ws.manager import ConnectionManager


@pytest.fixture
def manager():
    return ConnectionManager()


def _claims(
    user_id: str,
    org_id: str = "org-1",
    session_id: str | None = None,
) -> WsTokenClaims:
    return WsTokenClaims(
        user_id=user_id,
        org_id=org_id,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=1),
        session_id=session_id or f"session-{user_id}",
    )


@pytest.mark.asyncio
async def test_connect_registers_user(manager):
    ws = AsyncMock()
    await manager.connect(_claims("user-1"), ws)
    assert "user-1" in manager.connections
    ws.accept.assert_called_once()


@pytest.mark.asyncio
async def test_multiple_tabs_per_user(manager):
    ws1, ws2 = AsyncMock(), AsyncMock()
    await manager.connect(_claims("user-1", session_id="session-a"), ws1)
    await manager.connect(_claims("user-1", session_id="session-b"), ws2)
    assert len(manager.connections["user-1"]) == 2


@pytest.mark.asyncio
async def test_disconnect_removes_socket(manager):
    ws = AsyncMock()
    await manager.connect(_claims("user-1"), ws)
    await manager.disconnect("user-1", ws)
    assert "user-1" not in manager.connections


@pytest.mark.asyncio
async def test_broadcast_sends_to_all(manager):
    ws1, ws2 = AsyncMock(), AsyncMock()
    await manager.connect(_claims("user-1"), ws1)
    await manager.connect(_claims("user-2"), ws2)
    await manager.broadcast("test_event", {"key": "value"})
    ws1.send_json.assert_called()
    ws2.send_json.assert_called()


@pytest.mark.asyncio
async def test_broadcast_excludes_user(manager):
    ws1, ws2 = AsyncMock(), AsyncMock()
    await manager.connect(_claims("user-1"), ws1)
    await manager.connect(_claims("user-2"), ws2)
    # Reset mocks after connect (which broadcasts presence events)
    ws1.send_json.reset_mock()
    ws2.send_json.reset_mock()
    await manager.broadcast("test", {}, exclude="user-1")
    ws1.send_json.assert_not_called()
    ws2.send_json.assert_called()


@pytest.mark.asyncio
async def test_broadcast_to_org_only_sends_to_matching_org(manager):
    ws1, ws2 = AsyncMock(), AsyncMock()
    await manager.connect(_claims("user-1", org_id="org-1"), ws1)
    await manager.connect(_claims("user-2", org_id="org-2"), ws2)
    ws1.send_json.reset_mock()
    ws2.send_json.reset_mock()

    await manager.broadcast_to_org("org-1", "ops_update", {"runs": []})

    assert manager.connected_org_ids == ["org-1", "org-2"]
    ws1.send_json.assert_called_once_with({"type": "ops_update", "runs": []})
    ws2.send_json.assert_not_called()


@pytest.mark.asyncio
async def test_broadcast_product_event_only_sends_to_matching_org(manager):
    ws1, ws2 = AsyncMock(), AsyncMock()
    await manager.connect(_claims("user-1", org_id="org-1"), ws1)
    await manager.connect(_claims("user-2", org_id="org-2"), ws2)
    ws1.send_json.reset_mock()
    ws2.send_json.reset_mock()

    delivered = await manager.broadcast_product_event(
        "browser_session_frame",
        {"session_id": "session-1"},
        org_id="org-1",
    )

    assert delivered is True
    ws1.send_json.assert_called_once_with(
        {"type": "browser_session_frame", "session_id": "session-1", "org_id": "org-1"}
    )
    ws2.send_json.assert_not_called()


@pytest.mark.asyncio
async def test_broadcast_product_event_drops_missing_scope(manager):
    ws1, ws2 = AsyncMock(), AsyncMock()
    await manager.connect(_claims("user-1", org_id="org-1"), ws1)
    await manager.connect(_claims("user-2", org_id="org-2"), ws2)
    ws1.send_json.reset_mock()
    ws2.send_json.reset_mock()

    delivered = await manager.broadcast_product_event(
        "browser_session_frame",
        {"session_id": "session-1"},
    )

    assert delivered is False
    ws1.send_json.assert_not_called()
    ws2.send_json.assert_not_called()


@pytest.mark.asyncio
async def test_presence_connect_disconnect_is_org_scoped(manager):
    ws1, ws2, ws3 = AsyncMock(), AsyncMock(), AsyncMock()
    await manager.connect(_claims("user-1", org_id="org-1"), ws1)
    await manager.connect(_claims("user-2", org_id="org-2"), ws2)
    ws1.send_json.reset_mock()
    ws2.send_json.reset_mock()

    await manager.connect(_claims("user-3", org_id="org-1"), ws3)

    ws1.send_json.assert_called_once_with(
        {"type": "presence", "user_id": "user-3", "status": "online"}
    )
    ws2.send_json.assert_not_called()

    ws1.send_json.reset_mock()
    ws2.send_json.reset_mock()
    await manager.disconnect("user-3", ws3)

    ws1.send_json.assert_called_once_with(
        {"type": "presence", "user_id": "user-3", "status": "offline"}
    )
    ws2.send_json.assert_not_called()


@pytest.mark.asyncio
async def test_broadcast_cleans_stale(manager):
    ws_good, ws_stale = AsyncMock(), AsyncMock()
    ws_stale.send_json.side_effect = Exception("closed")
    await manager.connect(_claims("user-1"), ws_good)
    await manager.connect(_claims("user-2"), ws_stale)
    await manager.broadcast("test", {"data": 1})
    assert "user-2" not in manager.connections
