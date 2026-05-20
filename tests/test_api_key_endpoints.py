"""Focused tests for FastAPI Cortex org API-key endpoint functions."""
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


async def test_list_api_keys_returns_org_keys_only():
    from brain.app.api.routers.cortex._auth_keys import list_api_keys

    session = MagicMock()
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
    session.scalars.return_value.all.return_value = [org_key]

    with patch("brain.systems.vault._decrypt", return_value="sk-org-key"):
        result = await list_api_keys(MagicMock(), _user(), db=_AsyncSession(session))

    assert set(result) == {"org_keys", "org_key"}
    assert result["org_key"]["id"] == 3
    assert result["org_key"]["prefix"] == "sk-org-key..."


async def test_add_api_key_stores_org_key_for_owner():
    from brain.app.api.routers.cortex._auth_keys import add_api_key

    session = MagicMock()
    session.scalars.return_value.first.return_value = None

    def add_key(key):
        key.id = 99

    session.add.side_effect = add_key
    with patch("brain.app.api.routers.cortex._auth_keys._verify_provider_api_key") as verify, \
         patch("brain.systems.vault._encrypt", return_value=b"enc") as encrypt:
        result = await add_api_key(
            _request({"api_key": "sk-live-key", "provider": "anthropic", "label": "main"}),
            _user(role="owner"),
            db=_AsyncSession(session),
        )

    assert result["id"] == 99
    assert result["status"] == "org_key_stored"
    assert result["verified"] is True
    verify.assert_called_once_with("sk-live-key", "anthropic")
    encrypt.assert_called_once_with("sk-live-key")
    session.add.assert_called_once()


async def test_member_cannot_store_org_key():
    from brain.app.api.routers.cortex._auth_keys import add_api_key

    with pytest.raises(HTTPException) as exc:
        await add_api_key(
            _request({"api_key": "sk-live-key", "provider": "anthropic", "label": "main"}),
            _user(role="member"),
            db=_AsyncSession(),
        )

    assert exc.value.status_code == 403


async def test_add_api_key_rejects_invalid_provider():
    from brain.app.api.routers.cortex._auth_keys import add_api_key

    with pytest.raises(HTTPException) as exc:
        await add_api_key(
            _request({"api_key": "sk-x", "provider": "unknown"}),
            _user(role="owner"),
            db=_AsyncSession(),
        )

    assert exc.value.status_code == 400


async def test_owner_can_store_org_main_key():
    from brain.app.api.routers.cortex._auth_keys import set_org_main_key

    session = MagicMock()
    session.scalars.return_value.first.return_value = None

    def add_key(key):
        key.id = 5

    session.add.side_effect = add_key
    with patch("brain.app.api.routers.cortex._auth_keys._verify_provider_api_key"), \
         patch("brain.systems.vault._encrypt", return_value=b"enc"):
        result = await set_org_main_key(
            _request({"api_key": "sk-org-key", "provider": "anthropic"}),
            _user(role="owner"),
            db=_AsyncSession(session),
        )

    assert result["status"] == "org_key_stored"
    assert result["id"] == 5


async def test_delete_key_deletes_org_key_for_owner():
    from brain.app.api.routers.cortex._auth_keys import delete_key

    session = MagicMock()
    session.execute.return_value = SimpleNamespace(rowcount=1)

    result = await delete_key(5, _user(role="owner"), db=_AsyncSession(session))

    assert result == {"status": "deleted"}
    session.execute.assert_called_once()


async def test_delete_key_rejects_missing_org_key():
    from brain.app.api.routers.cortex._auth_keys import delete_key

    session = MagicMock()
    session.execute.return_value = SimpleNamespace(rowcount=0)

    with pytest.raises(HTTPException) as exc:
        await delete_key(5, _user(role="owner"), db=_AsyncSession(session))

    assert exc.value.status_code == 404


async def test_valid_providers_constant_contains_expected_values():
    from brain.app.api.routers.cortex import VALID_PROVIDERS

    assert {"anthropic", "openai", "google"} <= VALID_PROVIDERS
