"""Focused tests for FastAPI Cortex API-key endpoint functions."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

pytestmark = pytest.mark.asyncio


def _request(payload: dict):
    request = MagicMock()
    request.json = AsyncMock(return_value=payload)
    return request


def _user(**overrides):
    data = {"id": "user-1", "org_id": "org-1", "role": "member"}
    data.update(overrides)
    return data


class _AsyncSession:
    def __init__(self, session: MagicMock | None = None):
        self._backend = session or MagicMock()

    def add(self, *args, **kwargs):
        return self._backend.add(*args, **kwargs)

    async def scalars(self, *args, **kwargs):
        return self._backend.scalars(*args, **kwargs)

    async def execute(self, *args, **kwargs):
        return self._backend.execute(*args, **kwargs)

    async def scalar(self, *args, **kwargs):
        return self._backend.scalar(*args, **kwargs)

    async def get(self, *args, **kwargs):
        return self._backend.get(*args, **kwargs)

    async def flush(self, *args, **kwargs):
        return self._backend.flush(*args, **kwargs)


async def test_list_api_keys_returns_own_shared_and_org_keys():
    from brain.app.api.routers.cortex._auth_keys import list_api_keys

    session = MagicMock()
    own_key = SimpleNamespace(
        id=1,
        provider="anthropic",
        label="default",
        is_active=True,
        created_at=None,
        last_used_at=None,
        total_tokens_used=0,
        estimated_cost_usd=0.0,
    )
    org_key = SimpleNamespace(
        id=3,
        provider="openai",
        label="org",
        encrypted_key=b"enc",
        created_at=None,
        last_used_at=None,
        total_tokens_used=0,
        estimated_cost_usd=0.0,
    )
    session.scalars.return_value.all.side_effect = [[own_key], [org_key]]
    session.execute.return_value.all.return_value = [
        SimpleNamespace(_mapping={
            "id": 2,
            "provider": "anthropic",
            "label": "shared",
            "is_active": True,
            "last_used_at": None,
            "total_tokens_used": 0,
            "estimated_cost_usd": 0.0,
            "shared_by_name": "Alice",
            "shared_at": None,
        })
    ]

    with patch("brain.systems.vault._decrypt", return_value="sk-org-key"):
        result = await list_api_keys(MagicMock(), _user(), db=_AsyncSession(session))

    assert result["own"][0]["id"] == 1
    assert result["shared"][0]["shared_by_name"] == "Alice"
    assert result["org_key"]["prefix"] == "sk-org-key..."


async def test_add_api_key_rejects_invalid_provider():
    from brain.app.api.routers.cortex._auth_keys import add_api_key

    with pytest.raises(HTTPException) as exc:
        await add_api_key(
            _request({"api_key": "sk-x", "provider": "unknown"}),
            _user(),
            db=_AsyncSession(),
        )

    assert exc.value.status_code == 400


async def test_set_default_key_rejects_inaccessible_key():
    from brain.app.api.routers.cortex._auth_keys import set_default_key

    session = MagicMock()
    session.scalars.return_value.first.return_value = None

    with pytest.raises(HTTPException) as exc:
        await set_default_key(
            _request({"api_key_id": 999}),
            _user(),
            db=_AsyncSession(session),
        )

    assert exc.value.status_code == 404


async def test_member_cannot_store_org_main_key():
    from brain.app.api.routers.cortex._auth_keys import set_org_main_key

    with pytest.raises(HTTPException) as exc:
        await set_org_main_key(
            _request({"api_key": "sk-x"}),
            _user(role="member"),
            db=_AsyncSession(),
        )

    assert exc.value.status_code == 403


async def test_share_key_rejects_cross_org_target():
    from brain.app.api.routers.cortex._auth_keys import share_key

    session = MagicMock()
    session.scalars.return_value.first.return_value = SimpleNamespace(id=5)
    session.get.side_effect = [
        SimpleNamespace(org_id="org-1"),
        SimpleNamespace(org_id="org-2"),
    ]

    with pytest.raises(HTTPException) as exc:
        await share_key(
            5,
            _request({"shared_with_user_id": "user-2"}),
            _user(),
            db=_AsyncSession(session),
        )

    assert exc.value.status_code == 403
