"""Tests for LLM client resolution — the unified auth path via brain.platform.integrations.llm."""
import json
import os
import time
from unittest.mock import AsyncMock, MagicMock, patch
import pytest


@pytest.mark.asyncio
async def test_async_resolve_llm_client_uses_stored_anthropic_key():
    """User/org credentials are only resolved through the async auth path."""
    from brain.platform.integrations.llm import async_resolve_llm_client

    mock_resolve = AsyncMock(return_value=("sk-ant-api03-test-key", "org_main"))
    with patch("brain.platform.integrations.llm._async_resolve_key_from_db", mock_resolve), \
         patch("brain.platform.integrations.llm._build_anthropic_client") as mock_build:
        mock_build.return_value = MagicMock(
            client=MagicMock(), provider="anthropic", source="",
            auth_mode="api_key", is_oauth=False, extra_headers={}, token_prefix="sk-ant-api03-test",
            get_extra_headers=MagicMock(return_value={}),
        )
        result = await async_resolve_llm_client(user_id="user-1", provider="anthropic", session=object())

    assert result.source == "org_main"
    mock_build.assert_called_once_with("sk-ant-api03-test-key")
    mock_resolve.assert_awaited_once()


@pytest.mark.asyncio
async def test_async_resolve_llm_client_rejects_anthropic_without_any_key():
    """An explicit Anthropic route cannot produce a client without a credential."""
    from brain.platform.integrations.llm import async_resolve_llm_client

    with patch(
        "brain.platform.integrations.llm._async_resolve_key_from_db",
        new=AsyncMock(return_value=(None, "none")),
    ), patch(
        "brain.platform.integrations.llm._resolve_key_from_env",
        return_value=(None, "none"),
    ), patch("brain.platform.integrations.llm._build_anthropic_client") as mock_build:
        with pytest.raises(
            RuntimeError,
            match="No API key found for anthropic. Add one in Settings",
        ):
            await async_resolve_llm_client(
                user_id="user-1",
                org_id="org-1",
                provider="anthropic",
                session=object(),
            )

    mock_build.assert_not_called()


def test_resolve_llm_client_falls_back_to_env():
    """The sync resolver only uses local/env fallback auth."""
    from brain.platform.integrations.llm import resolve_llm_client

    with patch("brain.platform.integrations.llm._resolve_key_from_env", return_value=("sk-ant-api03-env-key", "env")), \
         patch("brain.platform.integrations.llm._build_anthropic_client") as mock_build:
        mock_build.return_value = MagicMock(
            client=MagicMock(), provider="anthropic", source="",
            auth_mode="api_key", is_oauth=False, extra_headers={}, token_prefix="sk-ant-api03-env-",
            get_extra_headers=MagicMock(return_value={}),
        )
        result = resolve_llm_client(user_id="user-1", provider="anthropic")

    assert result.source == "env"


def test_resolve_llm_client_raises_when_no_key():
    """RuntimeError when no key can be found anywhere."""
    from brain.platform.integrations.llm import resolve_llm_client

    with patch("brain.platform.integrations.llm._resolve_key_from_env", return_value=(None, "none")), \
         patch("brain.platform.integrations.llm.load_codex_auth_json", return_value=None):
        with pytest.raises(RuntimeError, match="No API key found"):
            resolve_llm_client(user_id="user-1", provider="anthropic")


def test_resolve_llm_client_detects_oauth_token():
    """Setup tokens (sk-ant-oat*) are detected and get OAuth headers."""
    from brain.platform.integrations.llm import resolve_llm_client

    with patch("brain.platform.integrations.llm._resolve_key_from_env", return_value=("sk-ant-oat01-setup-token", "env")), \
         patch("brain.platform.integrations.llm._build_anthropic_client") as mock_build:
        mock_build.return_value = MagicMock(
            client=MagicMock(), provider="anthropic", source="",
            auth_mode="api_key", is_oauth=True, extra_headers={"anthropic-beta": "oauth-2025-04-20"},
            token_prefix="sk-ant-oat01-setu",
            get_extra_headers=MagicMock(return_value={"anthropic-beta": "oauth-2025-04-20"}),
        )
        result = resolve_llm_client(user_id="user-1", provider="anthropic")

    assert result.is_oauth is True
    assert result.source == "env"


def test_resolve_llm_client_default_provider_coerces_anthropic_to_openai():
    """Implicit provider resolution never defaults hosted runtime to Anthropic."""
    from brain.platform.integrations.llm import ResolvedProviderAuth, resolve_llm_client

    with patch("brain.platform.providers.model_policy.resolve_default_provider", return_value="anthropic"), \
         patch(
             "brain.platform.integrations.llm._resolve_openai_local_auth",
             return_value=ResolvedProviderAuth(token="sk-openai-test", source="env", auth_mode="api_key"),
         ), \
         patch("brain.platform.integrations.llm._build_openai_client") as mock_build, \
         patch("brain.platform.integrations.llm._build_anthropic_client") as mock_anthropic_build:
        mock_build.return_value = MagicMock(
            client=MagicMock(), provider="openai", source="env",
            auth_mode="api_key", is_oauth=False, extra_headers={}, token_prefix="sk-openai-test",
            get_extra_headers=MagicMock(return_value={}),
        )
        result = resolve_llm_client(user_id="user-1")

    assert result.provider == "openai"
    mock_build.assert_called_once_with("sk-openai-test", "env")
    mock_anthropic_build.assert_not_called()


def test_resolve_openai_client_uses_codex_cache_when_available():
    """OpenAI can resolve directly from the local Codex auth cache."""
    from brain.platform.integrations.llm import resolve_llm_client
    from brain.platform.integrations.openai_codex_auth import OpenAICodexCredential

    mock_client = MagicMock()
    codex_auth = OpenAICodexCredential(
        access_token="access-token-123",
        refresh_token="refresh-token-123",
        account_id="acct_123",
        auth_mode="chatgpt",
        external_source_path="/tmp/auth.json",
    )

    env = {
        "ILLO_ENV": "development",
        "ILLO_LLM_TIMEOUT_SECONDS": "123",
        "ILLO_ALLOW_LOCAL_CODEX_AUTH_FALLBACK": "1",
    }
    with patch.dict(os.environ, env, clear=False), \
         patch("brain.platform.integrations.llm.load_codex_auth_json", return_value=codex_auth), \
         patch("brain.platform.integrations.llm.OpenAICodexClient", return_value=mock_client) as mock_codex_cls:
        result = resolve_llm_client(user_id="user-1", provider="openai")

    assert result.provider == "openai"
    assert result.auth_mode == "chatgpt"
    assert result.is_oauth is True
    assert result.source == "codex_cache"
    assert result.get_extra_headers()["chatgpt-account-id"] == "acct_123"
    assert result.client is mock_client
    assert mock_codex_cls.call_args.kwargs["timeout"] == 123.0


@pytest.mark.asyncio
async def test_async_resolve_openai_client_refreshes_expired_codex_credential():
    """Expired ChatGPT/Codex access tokens should refresh before client construction."""
    from brain.platform.integrations.llm import async_resolve_llm_client
    from brain.platform.integrations.openai_codex_auth import (
        OpenAICodexCredential,
        encode_codex_auth_payload,
    )

    expired_payload = json.dumps(encode_codex_auth_payload(OpenAICodexCredential(
        access_token="expired-access",
        refresh_token="refresh-token-123",
        account_id="acct_123",
        expires_at=time.time() - 30,
        auth_mode="chatgpt",
    )))
    refreshed = OpenAICodexCredential(
        access_token="fresh-access",
        refresh_token="refresh-token-456",
        account_id="acct_123",
        expires_at=time.time() + 1800,
        auth_mode="chatgpt",
    )
    mock_client = MagicMock()

    mock_resolve = AsyncMock(return_value=(expired_payload, "codex_subscription"))
    mock_persist = AsyncMock()
    with patch("brain.platform.integrations.llm._async_resolve_key_from_db", mock_resolve), \
         patch("brain.platform.integrations.llm.refresh_codex_access_token", return_value=refreshed) as mock_refresh, \
         patch("brain.platform.integrations.llm._async_persist_refreshed_openai_codex_db_credential", mock_persist), \
         patch("brain.platform.integrations.llm.OpenAICodexClient", return_value=mock_client) as mock_codex_cls:
        result = await async_resolve_llm_client(user_id="user-1", provider="openai", session=object())

    mock_refresh.assert_called_once_with("refresh-token-123")
    mock_persist.assert_awaited_once()
    assert mock_persist.call_args.kwargs["user_id"] == "user-1"
    assert mock_persist.call_args.kwargs["source"] == "codex_subscription"
    assert mock_persist.call_args.kwargs["cred"].access_token == "fresh-access"
    assert mock_persist.call_args.kwargs["cred"].refresh_token == "refresh-token-456"
    assert result.provider == "openai"
    assert result.auth_mode == "chatgpt"
    assert result.source == "codex_subscription"
    assert result.token_prefix == "fresh-access"[:18]
    assert mock_codex_cls.call_args.args[0] == "fresh-access"


@pytest.mark.asyncio
async def test_async_resolve_openai_client_continues_when_refreshed_credential_persist_fails():
    """A successful token refresh should not fail the current model call if DB writeback hiccups."""
    from brain.platform.integrations.llm import async_resolve_llm_client
    from brain.platform.integrations.openai_codex_auth import (
        OpenAICodexCredential,
        encode_codex_auth_payload,
    )

    expired_payload = json.dumps(encode_codex_auth_payload(OpenAICodexCredential(
        access_token="expired-access",
        refresh_token="refresh-token-123",
        account_id="acct_123",
        expires_at=time.time() - 30,
        auth_mode="chatgpt",
    )))
    refreshed = OpenAICodexCredential(
        access_token="fresh-access",
        refresh_token="refresh-token-456",
        account_id="acct_123",
        expires_at=time.time() + 1800,
        auth_mode="chatgpt",
    )
    mock_client = MagicMock()

    mock_resolve = AsyncMock(return_value=(expired_payload, "codex_subscription"))
    mock_persist = AsyncMock(side_effect=RuntimeError("db unavailable"))
    with patch("brain.platform.integrations.llm._async_resolve_key_from_db", mock_resolve), \
         patch("brain.platform.integrations.llm.refresh_codex_access_token", return_value=refreshed), \
         patch("brain.platform.integrations.llm._async_persist_refreshed_openai_codex_db_credential", mock_persist), \
         patch("brain.platform.integrations.llm.OpenAICodexClient", return_value=mock_client) as mock_codex_cls:
        result = await async_resolve_llm_client(user_id="user-1", provider="openai", session=object())

    assert result.token_prefix == "fresh-access"[:18]
    assert mock_codex_cls.call_args.args[0] == "fresh-access"


def test_resolve_openai_client_does_not_use_codex_cache_in_production_by_default():
    """Shared/prod deployments should not silently reuse a host-global Codex login."""
    from brain.platform.integrations.llm import resolve_llm_client
    from brain.platform.integrations.openai_codex_auth import OpenAICodexCredential

    codex_auth = OpenAICodexCredential(
        access_token="access-token-123",
        refresh_token="refresh-token-123",
        account_id="acct_123",
        auth_mode="chatgpt",
        external_source_path="/tmp/auth.json",
    )

    with patch.dict(os.environ, {"ILLO_ENV": "production"}, clear=False), \
         patch("brain.platform.integrations.llm.load_codex_auth_json", return_value=codex_auth), \
         patch("brain.platform.integrations.llm._resolve_key_from_env", return_value=(None, "none")):
        with pytest.raises(RuntimeError, match="user Codex subscription"):
            resolve_llm_client(user_id="user-1", provider="openai")


def test_resolve_openai_client_can_reenable_codex_cache_with_explicit_override():
    """An explicit override can still enable the local fallback when needed."""
    from brain.platform.integrations.llm import resolve_llm_client
    from brain.platform.integrations.openai_codex_auth import OpenAICodexCredential

    mock_client = MagicMock()
    codex_auth = OpenAICodexCredential(
        access_token="access-token-123",
        refresh_token="refresh-token-123",
        account_id="acct_123",
        auth_mode="chatgpt",
        external_source_path="/tmp/auth.json",
    )

    with patch.dict(os.environ, {"ILLO_ENV": "production", "ILLO_ALLOW_LOCAL_CODEX_AUTH_FALLBACK": "1"}, clear=False), \
         patch("brain.platform.integrations.llm.load_codex_auth_json", return_value=codex_auth), \
         patch("brain.platform.integrations.llm.OpenAICodexClient", return_value=mock_client):
        result = resolve_llm_client(user_id="user-1", provider="openai")

    assert result.provider == "openai"
    assert result.source == "codex_cache"


def test_resolve_openai_client_falls_back_to_env_api_key_when_no_codex_cache():
    """OpenAI env API keys still work when no Codex auth is available."""
    from brain.platform.integrations.llm import resolve_llm_client

    with patch("brain.platform.integrations.llm.load_codex_auth_json", return_value=None), \
         patch("brain.platform.integrations.llm._resolve_key_from_env", return_value=("sk-openai-test", "env")), \
         patch("brain.platform.integrations.llm._import_openai_sdk") as mock_sdk:
        mock_sdk.return_value.OpenAI.return_value = MagicMock()
        result = resolve_llm_client(user_id="user-1", provider="openai")

    assert result.provider == "openai"
    assert result.auth_mode == "api_key"
    assert result.source == "env"


def test_build_openai_client_normalizes_prompt_cache_key_on_raw_sdk_calls():
    from brain.platform.integrations.llm import _build_openai_client

    mock_client = MagicMock()
    mock_openai = MagicMock()
    mock_openai.OpenAI.return_value = mock_client
    original_create = mock_client.responses.create

    with patch("brain.platform.integrations.llm._import_openai_sdk", return_value=mock_openai):
        result = _build_openai_client("sk-openai-test", source="env")

    result.client.responses.create(
        prompt_cache_key="illo:" + ("x" * 80),
        extra_headers={
            "session_id": "coordinator-idea-12345678-1234-5678-90ab-cdef12345678:final-reply-checker",
        },
    )
    call_kwargs = original_create.call_args.kwargs
    cache_key = call_kwargs["prompt_cache_key"]
    assert len(cache_key) <= 64
    assert cache_key.startswith("illo:")
    assert len(call_kwargs["extra_headers"]["session_id"]) <= 64


def test_build_openai_client_applies_configured_timeout(monkeypatch):
    from brain.platform.integrations.llm import _build_openai_client

    mock_openai = MagicMock()
    mock_openai.OpenAI.return_value = MagicMock()
    monkeypatch.setenv("ILLO_LLM_TIMEOUT_SECONDS", "111.5")

    with patch("brain.platform.integrations.llm._import_openai_sdk", return_value=mock_openai):
        _build_openai_client("sk-openai-test", source="env")

    assert mock_openai.OpenAI.call_args.kwargs["timeout"] == 111.5


def test_llm_client_build_request_headers_merges_and_normalizes_openai_session():
    from brain.platform.integrations.llm import LLMClient

    llm = LLMClient(
        client=object(),
        provider="openai",
        source="codex_cache",
        auth_mode="chatgpt",
        is_oauth=True,
        extra_headers={
            "chatgpt-account-id": "acct_123",
            "originator": "illo-brain",
        },
        token_prefix="",
    )

    headers = llm.build_request_headers(
        session_id="coordinator-idea-12345678-1234-5678-90ab-cdef12345678:final-reply-checker",
        extra_headers={"x-illo-test": "1"},
    )

    assert headers["chatgpt-account-id"] == "acct_123"
    assert headers["originator"] == "illo-brain"
    assert headers["x-illo-test"] == "1"
    assert len(headers["session_id"]) <= 64


def test_build_auth_adapter_uses_auth_token_for_setup_tokens():
    """Setup tokens use auth_token= (not api_key=) in the Anthropic SDK."""
    with patch("brain.platform.integrations.anthropic_adapter.get_oauth_betas", return_value=["oauth-2025-04-20"]), \
         patch("anthropic.Anthropic") as mock_sdk:
        from brain.platform.integrations.anthropic_adapter import build_auth_adapter
        build_auth_adapter("sk-ant-oat01-real-looking", timeout=30)

    kwargs = mock_sdk.call_args.kwargs
    assert kwargs["api_key"] is None
    assert kwargs["auth_token"] == "sk-ant-oat01-real-looking"
    assert kwargs["default_headers"]["x-app"] == "cli"
