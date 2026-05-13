from unittest.mock import AsyncMock, patch

import pytest

pytestmark = pytest.mark.asyncio


def _status_payload(**overrides):
    payload = {
        "provider": "anthropic",
        "effective_provider": "openai",
        "is_selected_provider": False,
        "status": "available",
        "authenticated": True,
        "method": "api_key",
        "auth_mode": "api_key",
        "has_personal_db_key": True,
        "has_org_db_key": True,
        "has_db_keys": True,
        "runtime_key_available": True,
        "runtime_key_source": "org_main",
        "runtime_key_scope": "org",
        "runtime_uses_db_key": True,
        "runtime_uses_external_auth": False,
        "setup_required": False,
    }
    payload.update(overrides)
    return payload


async def test_auth_status_reports_runtime_db_key_state():
    from brain.app.api.routers.cortex import auth_status

    user = {"id": "user-1", "org_id": "org-1", "role": "owner"}

    with patch(
        "brain.app.api.routers.cortex._misc.async_get_provider_auth_status",
        AsyncMock(return_value=_status_payload()),
    ):
        data = await auth_status(provider="anthropic", user=user, db=object())

    assert data["authenticated"] is True
    assert data["has_personal_db_key"] is True
    assert data["has_org_db_key"] is True
    assert data["runtime_uses_db_key"] is True
    assert data["runtime_key_source"] == "org_main"
    assert data["runtime_key_scope"] == "org"
    assert data["is_selected_provider"] is False
    assert data["status"] == "available"
    assert data["setup_required"] is False


async def test_auth_status_requires_db_key_even_if_env_key_exists():
    from brain.app.api.routers.cortex import auth_status

    user = {"id": "user-1", "org_id": "org-1", "role": "owner"}

    with patch(
        "brain.platform.providers.model_policy.async_resolve_default_provider",
        AsyncMock(return_value="anthropic"),
    ), patch(
        "brain.app.api.routers.cortex._misc.async_get_provider_auth_status",
        AsyncMock(return_value=_status_payload(
            authenticated=False,
            runtime_key_available=False,
            runtime_uses_db_key=False,
            runtime_key_source="none",
            runtime_key_scope="none",
            status="not_configured",
            setup_required=True,
            has_personal_db_key=False,
            has_org_db_key=False,
            has_db_keys=False,
        )),
    ):
        data = await auth_status(user=user, db=object())

    assert data["authenticated"] is False
    assert data["runtime_uses_db_key"] is False
    assert data["runtime_key_source"] == "none"
    assert data["status"] == "not_configured"
    assert data["setup_required"] is True


async def test_auth_status_reports_openai_codex_cache_runtime():
    from brain.app.api.routers.cortex import auth_status

    user = {"id": "user-1", "org_id": "org-1", "role": "owner"}

    with patch(
        "brain.app.api.routers.cortex._misc.async_get_provider_auth_status",
        AsyncMock(return_value=_status_payload(
            provider="openai",
            effective_provider="openai",
            is_selected_provider=True,
            status="in_use",
            method="chatgpt",
            auth_mode="chatgpt",
            has_personal_db_key=False,
            has_org_db_key=False,
            has_db_keys=False,
            runtime_key_source="codex_cache",
            runtime_key_scope="external",
            runtime_uses_db_key=False,
            runtime_uses_external_auth=True,
        )),
    ):
        data = await auth_status(provider="openai", user=user, db=object())

    assert data["provider"] == "openai"
    assert data["authenticated"] is True
    assert data["method"] == "chatgpt"
    assert data["runtime_key_source"] == "codex_cache"
    assert data["runtime_key_scope"] == "external"
    assert data["status"] == "in_use"
    assert data["runtime_uses_external_auth"] is True
