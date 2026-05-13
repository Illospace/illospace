"""Focused tests for FastAPI Cortex API-key endpoint functions."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException


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
        self.session = session or MagicMock()

    def add(self, *args, **kwargs):
        return self.session.add(*args, **kwargs)

    async def scalars(self, *args, **kwargs):
        return self.session.scalars(*args, **kwargs)

    async def execute(self, *args, **kwargs):
        return self.session.execute(*args, **kwargs)

    async def scalar(self, *args, **kwargs):
        return self.session.scalar(*args, **kwargs)

    async def get(self, *args, **kwargs):
        return self.session.get(*args, **kwargs)

    async def flush(self, *args, **kwargs):
        return self.session.flush(*args, **kwargs)


def test_list_api_keys_returns_own_shared_and_org_keys():
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
        result = asyncio.run(list_api_keys(MagicMock(), _user(), db=_AsyncSession(session)))

    assert result["own"][0]["id"] == 1
    assert result["shared"][0]["shared_by_name"] == "Alice"
    assert result["org_key"]["prefix"] == "sk-org-key..."


def test_add_api_key_stores_verified_key():
    from brain.app.api.routers.cortex._auth_keys import add_api_key

    session = MagicMock()
    session.scalars.return_value.first.return_value = None

    def add_key(key):
        key.id = 99

    session.add.side_effect = add_key
    with patch("brain.app.api.routers.cortex._auth_keys._verify_provider_api_key") as verify, \
         patch("brain.systems.vault._encrypt", return_value=b"enc") as encrypt:
        result = asyncio.run(
            add_api_key(
                _request({"api_key": "sk-live-key", "provider": "anthropic", "label": "main"}),
                _user(),
                db=_AsyncSession(session),
            )
        )

    assert result["id"] == 99
    assert result["status"] == "stored"
    assert result["verified"] is True
    verify.assert_called_once_with("sk-live-key", "anthropic")
    encrypt.assert_called_once_with("sk-live-key")
    session.add.assert_called_once()


def test_add_api_key_rejects_invalid_provider():
    from brain.app.api.routers.cortex._auth_keys import add_api_key

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            add_api_key(
                _request({"api_key": "sk-x", "provider": "unknown"}),
                _user(),
                db=_AsyncSession(),
            )
        )

    assert exc.value.status_code == 400


def test_set_default_key_updates_user_default():
    from brain.app.api.routers.cortex._auth_keys import set_default_key

    session = MagicMock()
    session.scalars.return_value.first.return_value = 5
    db_user = SimpleNamespace(default_api_key_id=None)
    session.get.return_value = db_user

    result = asyncio.run(set_default_key(_request({"api_key_id": 5}), _user(), db=_AsyncSession(session)))

    assert result == {"status": "default_updated", "api_key_id": 5}
    assert db_user.default_api_key_id == 5


def test_set_default_key_rejects_inaccessible_key():
    from brain.app.api.routers.cortex._auth_keys import set_default_key

    session = MagicMock()
    session.scalars.return_value.first.return_value = None

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            set_default_key(
                _request({"api_key_id": 999}),
                _user(),
                db=_AsyncSession(session),
            )
        )

    assert exc.value.status_code == 404


def test_owner_can_store_org_main_key():
    from brain.app.api.routers.cortex._auth_keys import set_org_main_key

    session = MagicMock()
    with patch("brain.app.api.routers.cortex._auth_keys._verify_provider_api_key"), \
         patch("brain.systems.vault._encrypt", return_value=b"enc"):
        result = asyncio.run(
            set_org_main_key(
                _request({"api_key": "sk-org-key", "provider": "anthropic"}),
                _user(role="owner"),
                db=_AsyncSession(session),
            )
        )

    assert result["status"] == "org_key_stored"
    session.execute.assert_called_once()


def test_member_cannot_store_org_main_key():
    from brain.app.api.routers.cortex._auth_keys import set_org_main_key

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            set_org_main_key(
                _request({"api_key": "sk-x"}),
                _user(role="member"),
                db=_AsyncSession(),
            )
        )

    assert exc.value.status_code == 403


def test_share_key_upserts_share_for_same_org_users():
    from brain.app.api.routers.cortex._auth_keys import share_key

    session = MagicMock()
    session.scalars.return_value.first.side_effect = [SimpleNamespace(id=5), None]
    session.get.side_effect = [
        SimpleNamespace(org_id="org-1"),
        SimpleNamespace(org_id="org-1"),
    ]

    def add_share(share):
        share.id = 77

    session.add.side_effect = add_share
    result = asyncio.run(
        share_key(
            5,
            _request({"shared_with_user_id": "user-2"}),
            _user(),
            db=_AsyncSession(session),
        )
    )
    assert result == {"share_id": 77, "status": "shared"}
    session.add.assert_called_once()


def test_deactivate_key_marks_owned_key_inactive():
    from brain.app.api.routers.cortex._auth_keys import deactivate_key

    key = SimpleNamespace(is_active=True)
    session = MagicMock()
    session.scalars.return_value.first.return_value = key

    result = asyncio.run(deactivate_key(5, _user(), db=_AsyncSession(session)))

    assert result == {"status": "deactivated"}
    assert key.is_active is False


def test_valid_providers_constant_contains_expected_values():
    from brain.app.api.routers.cortex import VALID_PROVIDERS

    assert {"anthropic", "openai", "google"} <= VALID_PROVIDERS
