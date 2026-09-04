"""Tests for provider-neutral completion helpers."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class UnavailableModelError(Exception):
    status_code = 404


@pytest.mark.asyncio
@pytest.mark.parametrize("async_mode", [False, True])
@pytest.mark.parametrize("route", [
    ["gpt-6-astra", "gpt-5.6-sol"],
    ["gpt-6-astra", "gpt-5.6-sol", "gpt-5.5"],
    ["gpt-5.6-luna", "gpt-5.6-sol"],
])
async def test_completion_retries_unavailable_catalog_models(async_mode, route):
    from brain.platform.integrations import completions

    llm = MagicMock(provider="openai")
    llm.build_request_headers.return_value = {"chatgpt-account-id": "acct-1"}
    provider = MagicMock()
    provider.create.side_effect = [
        *[UnavailableModelError("model is not available on this account") for _ in route[:-1]],
        MagicMock(content=[MagicMock(type="text", text="Fallback works")]),
    ]
    with (
        patch.object(completions, "resolve_llm_client", return_value=llm) as sync_resolve,
        patch.object(completions, "async_resolve_llm_client", new=AsyncMock(return_value=llm)) as async_resolve,
        patch.object(completions, "get_provider", return_value=provider),
    ):
        kwargs = {"model": f"openai/{route[0]}", "reasoning_effort": "high", "user_id": "user-1", "org_id": "org-1"}
        selected_models = []
        if async_mode:
            kwargs["on_model_selected"] = selected_models.append
        result = await completions.async_simple_text_completion("hi", **kwargs) if async_mode else completions.simple_text_completion("hi", **kwargs)

    assert result == "Fallback works"
    requests = [call.args[0] for call in provider.create.call_args_list]
    assert [request.model for request in requests] == route
    assert all(request.reasoning_effort == "high" for request in requests)
    assert all(request.extra_headers == {"chatgpt-account-id": "acct-1"} for request in requests)
    if async_mode:
        assert selected_models == [f"openai/{model}" for model in route]
    resolve_calls = async_resolve.await_args_list if async_mode else sync_resolve.call_args_list
    assert len(resolve_calls) == len(route)
    assert all(call.kwargs == {"user_id": "user-1", "org_id": "org-1", "provider": "openai", "auth_mode": "chatgpt"} for call in resolve_calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("async_mode", [False, True])
@pytest.mark.parametrize("cyclic", [False, True])
async def test_completion_stops_on_unrelated_errors_or_cyclic_fallback(async_mode, cyclic):
    from brain.platform.integrations import completions

    llm = MagicMock(provider="openai")
    llm.build_request_headers.return_value = {}
    error = UnavailableModelError("model is unavailable") if cyclic else RuntimeError("rate limit exceeded")
    provider = MagicMock()
    provider.create.side_effect = error
    with (
        patch.object(completions, "resolve_llm_client", return_value=llm),
        patch.object(completions, "async_resolve_llm_client", new=AsyncMock(return_value=llm)),
        patch.object(completions, "get_provider", return_value=provider),
        patch.object(completions, "fallback_model_for", side_effect=lambda model: "openai/gpt-5.6-sol" if model == "gpt-6-astra" else "openai/gpt-6-astra"),
        pytest.raises(type(error)),
    ):
        if async_mode:
            await completions.async_simple_text_completion("hi", model="openai/gpt-6-astra")
        else:
            completions.simple_text_completion("hi", model="openai/gpt-6-astra")
    assert provider.create.call_count == (2 if cyclic else 1)


@pytest.mark.asyncio
async def test_async_title_generation_uses_completion_fallback():
    from brain.platform.integrations import completions
    from brain.systems.cortex.title_generation import _async_generate_with_provider_title_model

    llm = MagicMock(provider="openai")
    llm.build_request_headers.return_value = {}
    provider = MagicMock()
    provider.create.side_effect = [
        UnavailableModelError("model is unavailable"),
        MagicMock(content=[MagicMock(type="text", text="Provider Fallback Review")]),
    ]
    with (
        patch.object(completions, "async_resolve_llm_client", new=AsyncMock(return_value=llm)),
        patch.object(completions, "get_provider", return_value=provider),
    ):
        title = await _async_generate_with_provider_title_model("a title", provider="openai", model="openai/gpt-6-astra", user_id="user-1", org_id="org-1")
    assert title == "Provider Fallback Review"
    requests = [call.args[0] for call in provider.create.call_args_list]
    assert [request.model for request in requests] == ["gpt-6-astra", "gpt-5.6-sol"]
    assert all(request.operation_type == "title_generation" for request in requests)


class TestSimpleTextCompletion:
    @patch("brain.platform.integrations.completions.get_provider")
    @patch("brain.platform.integrations.completions.resolve_llm_client")
    def test_returns_text_from_provider_response(self, mock_resolve, mock_get_provider):
        from brain.platform.integrations.completions import (
            DEFAULT_SIMPLE_COMPLETION_SYSTEM_PROMPT,
            simple_text_completion,
        )

        llm = MagicMock()
        llm.provider = "openai"
        llm.client = object()
        llm.build_request_headers.return_value = {"X-Test": "1"}
        mock_resolve.return_value = llm

        provider = MagicMock()
        provider.create.return_value = MagicMock(
            content=[
                MagicMock(type="thinking", text=None),
                MagicMock(type="text", text="hello"),
                MagicMock(type="text", text="world"),
            ]
        )
        mock_get_provider.return_value = provider

        result = simple_text_completion("hi", model="openai/gpt-4o-mini", max_tokens=20)

        assert result == "hello\nworld"
        request = provider.create.call_args.args[0]
        assert request.model == "gpt-4o-mini"
        assert request.system == DEFAULT_SIMPLE_COMPLETION_SYSTEM_PROMPT
        assert request.extra_headers == {"X-Test": "1"}

    @patch("brain.platform.integrations.completions.get_provider")
    @patch("brain.platform.integrations.completions.resolve_llm_client")
    def test_passes_reasoning_effort_to_provider_request(self, mock_resolve, mock_get_provider):
        from brain.platform.integrations.completions import simple_text_completion

        llm = MagicMock()
        llm.provider = "openai"
        llm.client = object()
        llm.build_request_headers.return_value = {}
        mock_resolve.return_value = llm

        provider = MagicMock()
        provider.create.return_value = MagicMock(content=[MagicMock(type="text", text="ok")])
        mock_get_provider.return_value = provider

        assert simple_text_completion("hi", reasoning_effort="low") == "ok"
        request = provider.create.call_args.args[0]
        assert request.reasoning_effort == "low"

    @patch("brain.platform.integrations.completions.get_provider")
    @patch("brain.platform.integrations.completions.resolve_llm_client")
    def test_returns_none_when_no_text_blocks(self, mock_resolve, mock_get_provider):
        from brain.platform.integrations.completions import simple_text_completion

        llm = MagicMock()
        llm.provider = "anthropic"
        llm.client = object()
        llm.build_request_headers.return_value = {}
        mock_resolve.return_value = llm

        provider = MagicMock()
        provider.create.return_value = MagicMock(content=[MagicMock(type="tool_use", text=None)])
        mock_get_provider.return_value = provider

        assert simple_text_completion("hi") is None

    @patch("brain.platform.integrations.completions.get_provider")
    @patch("brain.platform.integrations.completions.resolve_llm_client")
    def test_preserves_explicit_system_prompt(self, mock_resolve, mock_get_provider):
        from brain.platform.integrations.completions import simple_text_completion

        llm = MagicMock()
        llm.provider = "openai"
        llm.client = object()
        llm.build_request_headers.return_value = {}
        mock_resolve.return_value = llm

        provider = MagicMock()
        provider.create.return_value = MagicMock(content=[MagicMock(type="text", text="ok")])
        mock_get_provider.return_value = provider

        assert simple_text_completion("hi", system_prompt="custom system") == "ok"
        request = provider.create.call_args.args[0]
        assert request.system == "custom system"


@pytest.mark.asyncio
async def test_async_completion_resolves_stored_codex_auth_without_sync_fallback():
    from brain.platform.integrations.completions import async_simple_text_completion

    llm = MagicMock()
    llm.provider = "openai"
    llm.client = object()
    llm.build_request_headers.return_value = {"chatgpt-account-id": "acct-1"}

    provider = MagicMock()
    provider.create.return_value = MagicMock(
        content=[MagicMock(type="text", text="resolved with stored auth")]
    )

    with (
        patch(
            "brain.platform.integrations.completions.async_resolve_llm_client",
            new=AsyncMock(return_value=llm),
        ) as async_resolve,
        patch(
            "brain.platform.integrations.completions.resolve_llm_client",
            side_effect=RuntimeError(
                "No OpenAI auth found. user Codex subscription credentials require "
                "async_resolve_llm_client."
            ),
        ) as sync_resolve,
        patch(
            "brain.platform.integrations.completions.get_provider",
            return_value=provider,
        ),
    ):
        result = await async_simple_text_completion(
            "read these files",
            model="openai/gpt-5.6-sol",
            user_id="user-1",
            org_id="org-1",
        )

    assert result == "resolved with stored auth"
    async_resolve.assert_awaited_once_with(
        user_id="user-1",
        org_id="org-1",
        provider="openai",
        auth_mode="chatgpt",
    )
    sync_resolve.assert_not_called()
    request = provider.create.call_args.args[0]
    assert request.extra_headers == {"chatgpt-account-id": "acct-1"}
