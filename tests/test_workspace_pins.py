from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException


def _pin(**overrides):
    from datetime import datetime, timezone

    fields = {
        "id": "pin-1",
        "org_id": "org-1",
        "label": "Launch",
        "color": "#57CFA0",
        "position_x": 1.0,
        "position_y": 2.0,
        "pin_metadata": {},
        "created_by_user_id": "author-user",
        "archived_at": None,
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


def test_workspace_pin_color_default_accepts_creator_color():
    from brain.app.api.routers.workspace_pins import DEFAULT_PIN_COLOR, _normalize_pin_color

    assert _normalize_pin_color("#57CFA0") == "#57CFA0"
    assert _normalize_pin_color("not-a-color") is None
    assert (_normalize_pin_color(None) or DEFAULT_PIN_COLOR) == DEFAULT_PIN_COLOR


def test_workspace_pin_author_guard_blocks_other_users():
    from brain.app.api.routers.workspace_pins import _require_pin_author

    pin = SimpleNamespace(created_by_user_id="author-user")

    _require_pin_author(pin, {"id": "author-user"})
    with pytest.raises(HTTPException) as exc:
        _require_pin_author(pin, {"id": "other-user"})

    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_workspace_pin_delete_removes_record(monkeypatch):
    from brain.app.api.routers import workspace_pins

    pin = _pin()
    db = MagicMock()
    db.delete = AsyncMock()
    db.flush = AsyncMock()
    broadcasts = []

    async def fake_broadcast(org_id, event_type, payload):
        broadcasts.append((org_id, event_type, payload))

    monkeypatch.setattr(workspace_pins, "require_org_context", lambda user: "org-1")
    monkeypatch.setattr(workspace_pins, "_get_pin_for_org", AsyncMock(return_value=pin))
    monkeypatch.setattr(workspace_pins.ws_manager, "broadcast_to_org", fake_broadcast)

    result = await workspace_pins.delete_workspace_pin(
        "pin-1",
        db=db,
        user={"id": "author-user", "org_id": "org-1"},
    )

    assert result == {"deleted": {"id": "pin-1"}}
    db.delete.assert_awaited_once_with(pin)
    db.flush.assert_awaited_once()
    assert broadcasts == [("org-1", "workspace_pin_deleted", {"pin_id": "pin-1"})]


@pytest.mark.asyncio
async def test_workspace_pin_position_update_allows_workspace_member(monkeypatch):
    from brain.app.api.routers import workspace_pins
    from brain.app.api.schemas.workspace_pins import WorkspacePinUpdate

    pin = _pin()
    db = MagicMock()
    db.flush = AsyncMock()
    db.refresh = AsyncMock()
    broadcasts = []

    async def fake_broadcast(org_id, event_type, payload):
        broadcasts.append((org_id, event_type, payload))

    monkeypatch.setattr(workspace_pins, "require_org_context", lambda user: "org-1")
    monkeypatch.setattr(workspace_pins, "_get_pin_for_org", AsyncMock(return_value=pin))
    monkeypatch.setattr(workspace_pins.ws_manager, "broadcast_to_org", fake_broadcast)

    result = await workspace_pins.update_workspace_pin(
        "pin-1",
        WorkspacePinUpdate(position_x=12.5, position_y=-4.0),
        db=db,
        user={"id": "workspace-member", "org_id": "org-1"},
    )

    assert pin.position_x == 12.5
    assert pin.position_y == -4.0
    assert result.position_x == 12.5
    assert result.position_y == -4.0
    db.flush.assert_awaited_once()
    db.refresh.assert_awaited_once_with(pin)
    assert broadcasts[0][0:2] == ("org-1", "workspace_pin_updated")


@pytest.mark.asyncio
async def test_workspace_pin_non_position_update_still_requires_author(monkeypatch):
    from brain.app.api.routers import workspace_pins
    from brain.app.api.schemas.workspace_pins import WorkspacePinUpdate

    pin = _pin()
    db = MagicMock()

    monkeypatch.setattr(workspace_pins, "require_org_context", lambda user: "org-1")
    monkeypatch.setattr(workspace_pins, "_get_pin_for_org", AsyncMock(return_value=pin))

    with pytest.raises(HTTPException) as exc:
        await workspace_pins.update_workspace_pin(
            "pin-1",
            WorkspacePinUpdate(label="Renamed"),
            db=db,
            user={"id": "workspace-member", "org_id": "org-1"},
        )

    assert exc.value.status_code == 403
