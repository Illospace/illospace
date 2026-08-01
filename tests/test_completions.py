"""Tests for provider-neutral completion helpers."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


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
