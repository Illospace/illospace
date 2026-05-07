from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException


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

    pin = SimpleNamespace(id="pin-1", created_by_user_id="author-user", archived_at=None)
    db = MagicMock()
    broadcasts = []

    async def fake_broadcast(org_id, event_type, payload):
        broadcasts.append((org_id, event_type, payload))

    monkeypatch.setattr(workspace_pins, "require_org_context", lambda user: "org-1")
    monkeypatch.setattr(workspace_pins, "_get_pin_for_org", lambda session, org_id, pin_id: pin)
    monkeypatch.setattr(workspace_pins.ws_manager, "broadcast_to_org", fake_broadcast)

    result = await workspace_pins.delete_workspace_pin(
        "pin-1",
        db=db,
        user={"id": "author-user", "org_id": "org-1"},
    )

    assert result == {"deleted": {"id": "pin-1"}}
    db.delete.assert_called_once_with(pin)
    db.flush.assert_called_once()
    assert broadcasts == [("org-1", "workspace_pin_deleted", {"pin_id": "pin-1"})]
