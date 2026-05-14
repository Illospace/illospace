"""Coverage for workspace member access approvals."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from brain.app.api.routers import brain

pytestmark = pytest.mark.asyncio


def _user(**overrides):
    fields = {
        "id": "user-1",
        "org_id": "org-1",
        "role": "member",
        "principal_type": "human",
    }
    fields.update(overrides)
    return fields


def _target(**overrides):
    fields = {
        "id": "pending-1",
        "org_id": "org-1",
        "approved": False,
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


async def test_workspace_member_can_approve_pending_user_in_same_org():
    db = MagicMock()
    pending = _target()
    db.get = AsyncMock(return_value=pending)

    result = await brain.approve_user("pending-1", db=db, user=_user())

    assert result == {"ok": True, "user_id": "pending-1", "approved": True}
    assert pending.approved is True


async def test_workspace_member_cannot_approve_user_outside_org():
    db = MagicMock()
    db.get = AsyncMock(return_value=_target(org_id="org-2"))

    with pytest.raises(HTTPException) as exc:
        await brain.approve_user("pending-1", db=db, user=_user())

    assert exc.value.status_code == 404


async def test_service_principal_cannot_approve_pending_user():
    db = MagicMock()
    db.get = AsyncMock(return_value=_target())

    with pytest.raises(HTTPException) as exc:
        await brain.approve_user("pending-1", db=db, user=_user(principal_type="service"))

    assert exc.value.status_code == 403
