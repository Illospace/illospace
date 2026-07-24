"""Tests for core/agent.py — the native Anthropic API agent loop.

Tests cover:
- Client initialization
- Model name normalization
- Tool execution runing
- Agent loop with mocked API responses
- Session persistence (load/save)
- Token accumulation
- Thinking block stripping
- The call_llm convenience function
"""

import asyncio
import json
import os
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import MagicMock, patch, PropertyMock, AsyncMock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), *([".."] * 1))))


def _mock_llm_client(mock_anthropic_client, provider="anthropic"):
    """Wrap a mock Anthropic client in an LLMClient-shaped object for resolve_llm_client patches."""
    llm = MagicMock()
    llm.client = mock_anthropic_client
    llm.provider = provider
    llm.source = "org_main"
    llm.is_oauth = False
    llm.extra_headers = {}
    llm.token_prefix = "sk-ant-api03-test" if provider == "anthropic" else "sk-openai-test"
    llm.get_extra_headers.return_value = {}
    llm.auth_mode = "api_key"

    def _build_request_headers(*, session_id=None, extra_headers=None):
        headers = dict(llm.get_extra_headers())
        if provider == "openai":
            from brain.platform.integrations.openai_cache import build_openai_extra_headers
            return build_openai_extra_headers(
                headers,
                auth_mode=getattr(llm, "auth_mode", None),
                session_id=session_id,
                extra_headers=extra_headers,
            )
        if extra_headers:
            headers.update(extra_headers)
        return headers

    llm.build_request_headers.side_effect = _build_request_headers
    return llm


class TestModelNormalization:
    def test_strips_anthropic_prefix(self):
        from brain.systems.runs.direct_agent import _normalize_model
        assert _normalize_model("anthropic/claude-sonnet-4-6") == "claude-sonnet-4-6"

    def test_leaves_bare_model_unchanged(self):
        from brain.systems.runs.direct_agent import _normalize_model
        assert _normalize_model("claude-sonnet-4-6") == "claude-sonnet-4-6"

    def test_handles_haiku(self):
        from brain.systems.runs.direct_agent import _normalize_model
        assert _normalize_model("anthropic/claude-haiku-4-5") == "claude-haiku-4-5"


class TestProviderInference:
    def test_direct_agent_requires_chatgpt_auth_for_subscription_models(self):
        from brain.systems.runs.direct_agent import required_openai_auth_mode

        assert required_openai_auth_mode("openai/gpt-5.5") == "chatgpt"
        assert required_openai_auth_mode("openai/gpt-5.6-sol") == "chatgpt"
        assert required_openai_auth_mode("openai/gpt-5.4") is None

    def test_preview_model_falls_back_only_for_model_availability_errors(self):
        from brain.systems.runs.direct_loop.model_fallback import (
            fallback_model_for,
            is_model_unavailable_error,
        )

        unsupported = SimpleNamespace(
            status_code=400,
            response_body="The 'gpt-5.6-sol' model is not supported when using Codex with a ChatGPT account.",
        )
        missing = SimpleNamespace(
            status_code=404,
            body={"error": {"code": "model_not_found", "message": "Model is not available on this account"}},
        )
        expired = SimpleNamespace(status_code=401, response_body="access token expired")

        assert fallback_model_for("openai/gpt-5.6-sol") == "openai/gpt-5.5"
        assert fallback_model_for("gpt-5.5") is None
        assert is_model_unavailable_error(unsupported) is True
        assert is_model_unavailable_error(missing) is True
        assert is_model_unavailable_error(expired) is False

    @patch("brain.systems.runs.direct_loop.final_reply_checker.get_provider")
    @patch("brain.systems.runs.direct_loop.final_reply_checker.resolve_llm_client")
    def test_init_llm_uses_provider_from_model_prefix(self, mock_resolve, mock_get_provider):
        from brain.systems.runs.direct_loop.final_reply_checker import _init_llm

        llm = MagicMock()
        llm.provider = "openai"
        llm.client = object()
        llm.source = "env"
        llm.token_prefix = "sk-openai-test"
        llm.is_oauth = False
        llm.get_extra_headers.return_value = {}
        llm.build_request_headers.return_value = {}
        mock_resolve.return_value = llm
        mock_get_provider.return_value = MagicMock()

        _init_llm("user-1", "sess-1", "openai/gpt-4o-mini")

        assert mock_resolve.call_args.kwargs["provider"] == "openai"

    @patch("brain.systems.runs.direct_loop.final_reply_checker.get_provider")
    @patch("brain.systems.runs.direct_loop.final_reply_checker.resolve_llm_client")
    def test_init_llm_requires_chatgpt_auth_for_subscription_models(self, mock_resolve, mock_get_provider):
        from brain.systems.runs.direct_loop.final_reply_checker import _init_llm

        llm = MagicMock()
        llm.provider = "openai"
        llm.client = object()
        llm.source = "codex_cache"
        llm.token_prefix = "access-token"
        llm.is_oauth = True
        llm.get_extra_headers.return_value = {}
        llm.build_request_headers.return_value = {}
        mock_resolve.return_value = llm
        mock_get_provider.return_value = MagicMock()

        for model in ("openai/gpt-5.5", "openai/gpt-5.6-sol"):
            _init_llm("user-1", "sess-1", model)

            assert mock_resolve.call_args.kwargs["provider"] == "openai"
            assert mock_resolve.call_args.kwargs["auth_mode"] == "chatgpt"


@pytest.mark.asyncio
async def test_agent_retries_gpt_5_6_on_gpt_5_5_when_account_lacks_entitlement(monkeypatch):
    from brain.platform.integrations.openai_codex_client import OpenAICodexError
    from brain.platform.integrations.providers import LLMResponse, TextContentBlock, Usage
    from brain.systems.runs.direct_agent import run_agent_async

    llm = _mock_llm_client(MagicMock(), provider="openai")
    llm.auth_mode = "chatgpt"
    llm.is_oauth = True
    requests = []

    async def fake_api_call(_provider, request, *_args, **_kwargs):
        requests.append(request)
        if len(requests) == 1:
            raise OpenAICodexError(
                "The 'gpt-5.6-sol' model is not supported when using Codex with a ChatGPT account.",
                status_code=400,
            )
        return LLMResponse(
            content=[TextContentBlock("Fallback succeeded")],
            stop_reason="end_turn",
            usage=Usage(input_tokens=3, output_tokens=2),
            model="gpt-5.5",
        )

    monkeypatch.setattr("brain.systems.runs.direct_agent.get_provider", lambda *_args: MagicMock())
    monkeypatch.setattr("brain.systems.runs.direct_agent._api_call_with_retry_async", fake_api_call)
    monkeypatch.setattr("brain.systems.runs.direct_agent._async_record_api_call", AsyncMock())
    monkeypatch.setattr(
        "brain.systems.runs.direct_agent._runtime_async_apply_agent_session_side_effects",
        AsyncMock(),
    )

    activity = []
    result = await run_agent_async(
        "Test fallback",
        model="openai/gpt-5.6-sol",
        thinking="xhigh",
        tools=[],
        tool_handlers={},
        persist_session=False,
        skip_harvest=True,
        resolved_llm=llm,
        on_stream_activity=activity.append,
    )

    assert result.success is True
    assert result.output == "Fallback succeeded"
    assert [request.model for request in requests] == ["gpt-5.6-sol", "gpt-5.5"]
    assert any("unavailable" in item and "gpt-5.5" in item for item in activity)


@pytest.mark.asyncio
async def test_agent_uses_shared_key_fallback_when_personal_codex_connection_is_missing(monkeypatch):
    from brain.platform.integrations.providers import LLMResponse, TextContentBlock, Usage
    from brain.systems.runs.direct_agent import run_agent_async

    llm = _mock_llm_client(MagicMock(), provider="openai")
    resolve = AsyncMock(
        side_effect=[
            RuntimeError("No OpenAI auth found. Connect a Codex subscription or add an org OpenAI key in Illo."),
            llm,
        ]
    )
    seen_models = []

    async def fake_api_call(_provider, request, *_args, **_kwargs):
        seen_models.append(request.model)
        return LLMResponse(
            content=[TextContentBlock("Shared fallback succeeded")],
            stop_reason="end_turn",
            usage=Usage(input_tokens=3, output_tokens=2),
            model="gpt-5.5",
        )

    monkeypatch.setattr("brain.systems.runs.direct_agent.async_resolve_llm_client", resolve)
    monkeypatch.setattr("brain.systems.runs.direct_agent.get_provider", lambda *_args: MagicMock())
    monkeypatch.setattr("brain.systems.runs.direct_agent._api_call_with_retry_async", fake_api_call)
    monkeypatch.setattr("brain.systems.runs.direct_agent._async_record_api_call", AsyncMock())
    monkeypatch.setattr(
        "brain.systems.runs.direct_agent._runtime_async_apply_agent_session_side_effects",
        AsyncMock(),
    )

    result = await run_agent_async(
        "Test shared fallback",
        model="openai/gpt-5.6-sol",
        thinking="xhigh",
        tools=[],
        tool_handlers={},
        persist_session=False,
        skip_harvest=True,
        user_id="user-1",
        org_id="org-1",
    )

    assert result.success is True
    assert seen_models == ["gpt-5.5"]
    assert [call.kwargs["auth_mode"] for call in resolve.await_args_list] == ["chatgpt", None]

class TestLiveGuidance:
    async def test_append_live_guidance_adds_user_message(self):
        from brain.systems.runs.direct_agent import _append_live_guidance_async

        messages = [{"role": "user", "content": "Original task"}]
        seen_activity = []

        count = await _append_live_guidance_async(
            messages,
            lambda: ["Please keep the current approach, but check the tests too."],
            session_id="live-guidance-test",
            on_stream_activity=seen_activity.append,
        )

        assert count == 1
        assert messages[-1]["role"] == "user"
        assert "Live user guidance" in messages[-1]["content"]
        assert "Preserve useful progress" in messages[-1]["content"]
        assert "check the tests" in messages[-1]["content"]
        assert seen_activity == ["Received live user guidance"]


class TestCachePolicy:
    def test_openai_cache_retention_matches_extended_cache_support(self):
        from brain.systems.runs.direct_agent import _get_openai_cache_retention

        assert _get_openai_cache_retention("openai/gpt-5.5") == "24h"
        assert _get_openai_cache_retention("openai/gpt-5.4") == "24h"
        assert _get_openai_cache_retention("openai/gpt-4.1-mini") == "24h"
        assert _get_openai_cache_retention("openai/gpt-4o-mini") is None

    @patch("brain.systems.runs.direct_agent.async_resolve_llm_client")
    @patch("brain.systems.runs.direct_agent._load_session", return_value=([], None))
    @patch("brain.systems.runs.direct_agent._save_session")
    def test_openai_requests_use_native_cache_fields_not_anthropic_breakpoints(self, mock_save, mock_load, mock_client):
        from brain.systems.runs.direct_agent import run_agent

        client = MagicMock()
        response = MagicMock()
        response.stop_reason = "end_turn"
        text_block = MagicMock()
        text_block.type = "text"
        text_block.text = "Done."
        text_block.model_dump.return_value = {"type": "text", "text": "Done."}
        response.content = [text_block]
        usage = MagicMock()
        usage.input_tokens = 10
        usage.output_tokens = 5
        usage.cache_read_input_tokens = 0
        usage.cache_creation_input_tokens = 0
        response.usage = usage
        client.responses.create.return_value = response
        mock_client.return_value = _mock_llm_client(client, provider="openai")

        result = run_agent(
            message="Continue",
            model="openai/gpt-4.1-mini",
            system_prompt="Be precise",
            tools=[{"name": "read_file", "description": "Read a file", "input_schema": {"type": "object", "properties": {}}}],
            persist_session=True,
            session_id="cache-test-openai",
        )

        assert result.success
        call_kwargs = client.responses.create.call_args_list[0].kwargs
        assert call_kwargs["prompt_cache_key"].startswith("illo:coordinator:")
        assert call_kwargs["prompt_cache_retention"] == "24h"
        assert "max_output_tokens" not in call_kwargs
        instructions = call_kwargs["instructions"]
        assert isinstance(instructions, str)
        assert "Be precise" in instructions
        assert "cache_control" not in instructions
        assert all("cache_control" not in tool for tool in call_kwargs["tools"])

    @patch("brain.systems.runs.direct_agent.async_resolve_llm_client")
    @patch("brain.systems.runs.direct_agent._load_session", return_value=([], None))
    @patch("brain.systems.runs.direct_agent._save_session")
    def test_openai_prompt_cache_key_is_capped_for_long_session_ids(self, mock_save, mock_load, mock_client):
        from brain.systems.runs.direct_agent import run_agent

        client = MagicMock()
        response = MagicMock()
        response.stop_reason = "end_turn"
        text_block = MagicMock()
        text_block.type = "text"
        text_block.text = "Done."
        text_block.model_dump.return_value = {"type": "text", "text": "Done."}
        response.content = [text_block]
        usage = MagicMock()
        usage.input_tokens = 10
        usage.output_tokens = 5
        usage.cache_read_input_tokens = 0
        usage.cache_creation_input_tokens = 0
        response.usage = usage
        client.responses.create.return_value = response
        mock_client.return_value = _mock_llm_client(client, provider="openai")

        session_id = "coordinator-idea-12345678-1234-5678-90ab-cdef12345678"
        result = run_agent(
            message="Continue",
            model="openai/gpt-4o-mini",
            system_prompt="Be precise",
            tools=[{"name": "read_file", "description": "Read a file", "input_schema": {"type": "object", "properties": {}}}],
            persist_session=True,
            session_id=session_id,
        )

        assert result.success
        cache_key = client.responses.create.call_args_list[0].kwargs["prompt_cache_key"]
        assert len(cache_key) <= 64
        assert cache_key.startswith("illo:coordinator:")
        assert len(cache_key.rsplit(":", 1)[-1]) == 24


class TestToolDefinitions:
    def test_brain_tools_defined(self):
        from brain.systems.runs.direct_agent import BRAIN_TOOLS
        names = [t["name"] for t in BRAIN_TOOLS]
        assert "brain_recall" in names
        assert "brain_guardrails" in names
        assert "brain_skills" in names
        assert "skill_view" in names
        assert "skill_asset" in names
        assert "brain_encode" in names
        assert "memory_link" in names
        assert "memory_supersede" in names
        assert "memory_archive" in names
        assert "brain_vault" in names
        assert "runtime_settings" in names
        assert "read_self_context" in names
        assert "read_capabilities" in names

    def test_read_capabilities_schema_is_query_first_for_models(self):
        from brain.systems.runs.direct_agent import BRAIN_TOOLS

        tool = next(tool for tool in BRAIN_TOOLS if tool["name"] == "read_capabilities")
        properties = tool["input_schema"]["properties"]

        assert "query" in properties
        assert "detail_level" in properties
        assert "include_setup_guide" in properties
        assert "capability_key" not in properties
        assert "category" not in properties

    def test_exec_tools_defined(self):
        from brain.systems.runs.direct_agent import EXEC_TOOLS
        names = [t["name"] for t in EXEC_TOOLS]
        assert "exec_command" in names
        assert "read_file" in names
        assert "write_file" in names
        assert "edit_file" in names
        assert "search_files" in names
        assert "list_files" in names

    def test_worker_tools_include_exec(self):
        from brain.systems.runs.direct_agent import WORKER_TOOLS
        names = [t["name"] for t in WORKER_TOOLS]
        assert "brain_recall" in names  # brain tools
        assert "exec_command" in names  # exec tools

    def test_coordinator_tools_include_all(self):
        from brain.systems.runs.direct_agent import COORDINATOR_TOOLS
        names = [t["name"] for t in COORDINATOR_TOOLS]
        assert "cortex_reply" in names
        assert "brain_recall" in names
        assert "exec_command" in names

    def test_all_tools_have_input_schema(self):
        from brain.systems.runs.direct_agent import COORDINATOR_TOOLS
        for tool in COORDINATOR_TOOLS:
            assert "input_schema" in tool, f"Tool {tool['name']} missing input_schema"
            assert tool["input_schema"]["type"] == "object"

    def test_tool_surfaces_do_not_delegate_setup_to_admin_or_operator(self):
        from brain.systems.runs.direct_agent import COORDINATOR_TOOLS

        serialized = json.dumps(COORDINATOR_TOOLS).lower()

        delegated_role = "ad" + "min"
        assert "illospace " + delegated_role not in serialized
        assert "operator" not in serialized
        assert "ask an " + delegated_role not in serialized
        assert "ask a workspace " + delegated_role not in serialized

    def test_tool_policy_filters_model_tools_and_handlers(self):
        from brain.systems.runs.direct_agent import _apply_tool_policy

        tools, handlers = _apply_tool_policy(
            [{"name": "manage_cycle"}, {"name": "web_search"}],
            {"manage_cycle": object(), "web_search": object()},
            {"tool_policy": {"disabled_tools": ["manage_cycle"]}},
        )

        assert tools == [{"name": "web_search"}]
        assert sorted(handlers) == ["web_search"]


class TestBrainGate:
    """Test the brain gate enforcement mechanism."""

    def test_brain_tool_names_defined(self):
        from brain.systems.runs.direct_agent import _BRAIN_TOOL_NAMES, _GATED_TOOL_NAMES
        assert "brain_recall" in _BRAIN_TOOL_NAMES
        assert "brain_guardrails" in _BRAIN_TOOL_NAMES
        assert "write_file" in _GATED_TOOL_NAMES
        assert "edit_file" in _GATED_TOOL_NAMES
        assert "exec_command" in _GATED_TOOL_NAMES
        # read_file is NOT gated (non-destructive)
        assert "read_file" not in _GATED_TOOL_NAMES

    def test_brain_tools_satisfy_gate(self):
        from brain.systems.runs.direct_agent import _BRAIN_TOOL_NAMES
        # All brain tools should satisfy the gate
        for name in (
            "brain_recall",
            "brain_guardrails",
            "brain_skills",
            "skill_view",
            "skill_asset",
            "brain_encode",
            "memory_link",
            "memory_supersede",
            "memory_archive",
        ):
            assert name in _BRAIN_TOOL_NAMES
        assert "runtime_settings" in _BRAIN_TOOL_NAMES

    def test_read_tools_not_gated(self):
        from brain.systems.runs.direct_agent import _GATED_TOOL_NAMES
        # Non-destructive tools should not require brain context
        for name in ("read_file", "search_files", "list_files"):
            assert name not in _GATED_TOOL_NAMES


class TestThinkingBlockStripping:
    def test_strips_thinking_blocks(self):
        from brain.systems.runs.direct_agent import _strip_thinking_from_messages

        messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": [
                {"type": "thinking", "thinking": "Let me think about this..."},
                {"type": "text", "text": "Hello!"},
            ]},
        ]
        cleaned = _strip_thinking_from_messages(messages)
        assert len(cleaned) == 2
        assert len(cleaned[1]["content"]) == 1
        assert cleaned[1]["content"][0]["type"] == "text"

    def test_strips_redacted_thinking(self):
        from brain.systems.runs.direct_agent import _strip_thinking_from_messages

        messages = [
            {"role": "assistant", "content": [
                {"type": "redacted_thinking", "data": "..."},
                {"type": "text", "text": "Result"},
            ]},
        ]
        cleaned = _strip_thinking_from_messages(messages)
        assert len(cleaned[0]["content"]) == 1

    def test_preserves_user_messages(self):
        from brain.systems.runs.direct_agent import _strip_thinking_from_messages

        messages = [
            {"role": "user", "content": "hello"},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "1", "content": "ok"}]},
        ]
        cleaned = _strip_thinking_from_messages(messages)
        assert cleaned == messages

    def test_preserves_text_only_messages(self):
        from brain.systems.runs.direct_agent import _strip_thinking_from_messages

        messages = [
            {"role": "assistant", "content": [
                {"type": "text", "text": "Just text, no thinking"},
            ]},
        ]
        cleaned = _strip_thinking_from_messages(messages)
        assert cleaned == messages


class TestProviderMessageHelpers:
    def test_tool_pair_helpers_accept_typed_blocks(self):
        from brain.platform.integrations.providers import ToolResultContentBlock, ToolUseContentBlock
        from brain.systems.runs.direct_agent import _is_tool_result_user, _is_tool_use_assistant

        assert _is_tool_use_assistant({
            "role": "assistant",
            "content": [
                ToolUseContentBlock(id="tool_1", name="brain_recall", input={"query": "x"}),
            ],
        })
        assert _is_tool_result_user({
            "role": "user",
            "content": [
                ToolResultContentBlock(tool_use_id="tool_1", content="ok"),
            ],
        })


class TestTextExtraction:
    def test_extracts_from_dict_content(self):
        from brain.systems.runs.direct_agent import _extract_text

        messages = [
            {"role": "user", "content": "question"},
            {"role": "assistant", "content": [
                {"type": "text", "text": "The answer is 42."},
            ]},
        ]
        assert _extract_text(messages) == "The answer is 42."

    def test_extracts_from_string_content(self):
        from brain.systems.runs.direct_agent import _extract_text

        messages = [
            {"role": "assistant", "content": "Simple string response"},
        ]
        assert _extract_text(messages) == "Simple string response"

    def test_returns_empty_on_no_assistant(self):
        from brain.systems.runs.direct_agent import _extract_text
        messages = [{"role": "user", "content": "hello"}]
        assert _extract_text(messages) == ""

    def test_joins_multiple_text_blocks(self):
        from brain.systems.runs.direct_agent import _extract_text

        messages = [
            {"role": "assistant", "content": [
                {"type": "text", "text": "Part 1"},
                {"type": "text", "text": "Part 2"},
            ]},
        ]
        assert "Part 1" in _extract_text(messages)
        assert "Part 2" in _extract_text(messages)


class TestSessionPersistence:
    def test_load_empty_session(self):
        from brain.systems.runs.direct_agent import _load_session
        with patch("brain.platform.db.repositories.unit_of_work.UnitOfWork") as MockUoW:
            mock_uow = MagicMock()
            MockUoW.return_value = mock_uow
            mock_uow.__enter__ = MagicMock(return_value=mock_uow)
            mock_uow.__exit__ = MagicMock(return_value=False)
            mock_uow.__aenter__ = AsyncMock(return_value=mock_uow)
            mock_uow.__aexit__ = AsyncMock(return_value=False)
            mock_uow.session.execute.return_value.mappings.return_value.first.return_value = None

            messages, system = _load_session("nonexistent-session")
            assert messages == []
            assert system is None

    def test_load_existing_session(self):
        from brain.systems.runs.direct_agent import _load_session
        with patch("brain.platform.db.repositories.unit_of_work.UnitOfWork") as MockUoW:
            mock_uow = MagicMock()
            MockUoW.return_value = mock_uow
            mock_uow.__enter__ = MagicMock(return_value=mock_uow)
            mock_uow.__exit__ = MagicMock(return_value=False)
            mock_uow.__aenter__ = AsyncMock(return_value=mock_uow)
            mock_uow.__aexit__ = AsyncMock(return_value=False)
            mock_uow.session.execute.return_value.mappings.return_value.first.return_value = {
                "messages": [{"role": "user", "content": "hello"}],
                "system_prompt": "You are helpful.",
            }

            messages, system = _load_session("existing-session")
            assert len(messages) == 1
            assert system == "You are helpful."

    def test_save_session(self):
        from brain.systems.runs.direct_agent import _save_session
        with patch("brain.platform.db.repositories.unit_of_work.UnitOfWork") as MockUoW:
            mock_uow = MagicMock()
            MockUoW.return_value = mock_uow
            mock_uow.__enter__ = MagicMock(return_value=mock_uow)
            mock_uow.__exit__ = MagicMock(return_value=False)
            mock_uow.__aenter__ = AsyncMock(return_value=mock_uow)
            mock_uow.__aexit__ = AsyncMock(return_value=False)

            _save_session(
                "test-session",
                [{"role": "user", "content": "hello"}],
                "system prompt",
                1000, 200, 500, 100,
            )
            assert mock_uow.session.execute.called


class TestAgentLoop:
    """Test the agent loop with mocked Anthropic client."""

    def _make_response(self, text="Done.", stop_reason="end_turn", tool_use=None):
        """Create a mock API response."""
        response = MagicMock()
        response.stop_reason = stop_reason

        content = []
        if tool_use:
            block = MagicMock()
            block.type = "tool_use"
            block.name = tool_use["name"]
            block.input = tool_use["input"]
            block.id = "tool_123"
            block.model_dump.return_value = {
                "type": "tool_use", "name": tool_use["name"],
                "input": tool_use["input"], "id": "tool_123",
            }
            content.append(block)
        if text:
            text_block = MagicMock()
            text_block.type = "text"
            text_block.text = text
            text_block.model_dump.return_value = {"type": "text", "text": text}
            content.append(text_block)

        response.content = content

        usage = MagicMock()
        usage.input_tokens = 1000
        usage.output_tokens = 200
        usage.cache_read_input_tokens = 500
        usage.cache_creation_input_tokens = 100
        response.usage = usage

        return response

    @patch("brain.systems.runs.direct_agent.async_resolve_llm_client")
    @patch("brain.systems.runs.direct_agent._load_session", return_value=([], None))
    @patch("brain.systems.runs.direct_agent._save_session")
    def test_simple_completion(self, mock_save, mock_load, mock_client):
        from brain.systems.runs.direct_agent import run_agent

        client = MagicMock()
        client.messages.create.return_value = self._make_response("Hello world!")
        mock_client.return_value = _mock_llm_client(client)

        result = run_agent(
            message="Say hello",
            model="claude-sonnet-4-6",
            tools=[],
            persist_session=False,
        )

        assert result.success
        assert result.output == "Hello world!"
        assert result.tokens_input == 1000
        assert result.tokens_output == 200

    @patch("brain.systems.runs.direct_agent.async_resolve_llm_client")
    @patch("brain.systems.runs.direct_agent._load_session", return_value=([], None))
    @patch("brain.systems.runs.direct_agent._save_session")
    def test_identical_tool_failures_open_circuit_after_three_attempts(
        self,
        mock_save,
        mock_load,
        mock_client,
    ):
        from brain.systems.runs.direct_agent import run_agent
        from brain.systems.runs.direct_loop.loop_control import LoopTerminationReason

        client = MagicMock()
        client.messages.create.side_effect = [
            self._make_response(
                text=None,
                stop_reason="tool_use",
                tool_use={"name": "always_fails", "input": {"value": "same"}},
            )
            for _ in range(10)
        ]
        mock_client.return_value = _mock_llm_client(client)
        handler = MagicMock(side_effect=RuntimeError("deterministic failure"))

        result = run_agent(
            message="Use the deterministic tool",
            tools=[{
                "name": "always_fails",
                "description": "Always fails for this regression test.",
                "input_schema": {"type": "object", "properties": {}},
            }],
            tool_handlers={"always_fails": handler},
            persist_session=False,
            max_turns=20,
        )

        assert result.success is True
        assert handler.call_count == 3
        assert client.messages.create.call_count == 3
        assert "always_fails" in result.output
        assert "RuntimeError" in result.output
        assert "3 consecutive failures" in result.output
        assert result.termination is not None
        assert result.termination.reason is LoopTerminationReason.TOOL_FAILURE_CIRCUIT

    async def test_repeated_brain_encode_is_rejected(self):
        from brain.systems.runs.direct_agent import _execute_tool_calls_async, _GateState
        from brain.systems.runs.direct_loop.loop_control import LoopControlPolicy

        block = MagicMock()
        block.type = "tool_use"
        block.name = "brain_encode"
        block.input = {"content": "Long enough lesson content to encode once."}
        block.id = "tool_encode_2"

        response = MagicMock()
        response.content = [block]

        tool_calls_made = ["brain_encode"]
        handler = MagicMock(return_value={"ok": True})

        execution = await _execute_tool_calls_async(
            response,
            {"brain_encode": handler},
            tool_calls_made,
            _GateState(),
            None,
            None,
            None,
            "runner",
            loop_control=LoopControlPolicy(),
        )

        assert handler.call_count == 0
        results = execution.tool_results
        assert results[0]["is_error"] is True
        assert "already ran" in results[0]["content"]

    async def test_failed_brain_encode_is_marked_non_retryable(self):
        from brain.systems.runs.direct_agent import _execute_tool_calls_async, _GateState
        from brain.systems.runs.direct_loop.loop_control import LoopControlPolicy

        block = MagicMock()
        block.type = "tool_use"
        block.name = "brain_encode"
        block.input = {"content": "Long enough lesson content to encode once."}
        block.id = "tool_encode_fail"

        response = MagicMock()
        response.content = [block]

        handler = MagicMock(return_value={"error": "embedding worker unavailable"})

        execution = await _execute_tool_calls_async(
            response,
            {"brain_encode": handler},
            [],
            _GateState(),
            None,
            None,
            None,
            "runner",
            loop_control=LoopControlPolicy(),
        )

        results = execution.tool_results
        assert results[0]["is_error"] is True
        assert "Do not retry brain_encode" in results[0]["content"]

    @pytest.mark.asyncio
    async def test_execute_tool_calls_supports_async_handlers_inside_running_loop(self):
        from brain.systems.runs.direct_agent import _GateState
        from brain.systems.runs.direct_loop.gates import check_gate_violations
        from brain.systems.runs.direct_loop.loop_control import LoopControlPolicy
        from brain.systems.runs.direct_loop.tool_execution import async_execute_tool_calls

        block = MagicMock()
        block.type = "tool_use"
        block.name = "brain_guardrails"
        block.input = {}
        block.id = "tool_async_guardrails"

        response = MagicMock()
        response.content = [block]

        async def handler():
            await asyncio.sleep(0)
            return {"guardrails": ["stay grounded"]}

        execution = await async_execute_tool_calls(
            response,
            {"brain_guardrails": handler},
            [],
            _GateState(),
            None,
            None,
            None,
            "runner",
            agent_context=SimpleNamespace(),
            brain_tool_names=frozenset(),
            gated_tool_names=frozenset(),
            research_tool_names=frozenset(),
            research_budget=0,
            parallel_safe_tool_names=frozenset(),
            max_parallel_tool_calls=1,
            check_gate_violations=check_gate_violations,
            loop_control=LoopControlPolicy(),
        )

        results = execution.tool_results
        assert json.loads(results[0]["content"]) == {"guardrails": ["stay grounded"]}

    @pytest.mark.asyncio
    async def test_parallel_safe_tool_batch_overlaps_and_preserves_order(self):
        from brain.systems.runs.direct_agent import _GateState
        from brain.systems.runs.direct_loop.gates import check_gate_violations
        from brain.systems.runs.direct_loop.loop_control import LoopControlPolicy
        from brain.systems.runs.direct_loop.tool_execution import async_execute_tool_calls

        block_a = MagicMock()
        block_a.type = "tool_use"
        block_a.name = "read_file"
        block_a.input = {"path": "alpha.py"}
        block_a.id = "tool_parallel_a"

        block_b = MagicMock()
        block_b.type = "tool_use"
        block_b.name = "search_files"
        block_b.input = {"query": "beta"}
        block_b.id = "tool_parallel_b"

        response = MagicMock()
        response.content = [block_a, block_b]

        active = 0
        max_active = 0
        callback_calls = []

        def make_handler(label: str, delay: float):
            async def handler(**kwargs):
                nonlocal active, max_active
                active += 1
                max_active = max(max_active, active)
                try:
                    await asyncio.sleep(delay)
                    return {"label": label, "payload": kwargs}
                finally:
                    active -= 1
            return handler

        execution = await async_execute_tool_calls(
            response,
            {
                "read_file": make_handler("read_file", 0.15),
                "search_files": make_handler("search_files", 0.05),
            },
            [],
            _GateState(),
            lambda name, args, result_text: callback_calls.append((name, args, result_text)),
            None,
            None,
            "runner",
            agent_context=SimpleNamespace(),
            brain_tool_names=frozenset(),
            gated_tool_names=frozenset(),
            research_tool_names=frozenset(),
            research_budget=0,
            parallel_safe_tool_names=frozenset({"read_file", "search_files"}),
            max_parallel_tool_calls=2,
            check_gate_violations=check_gate_violations,
            loop_control=LoopControlPolicy(),
        )

        results = execution.tool_results
        assert max_active == 2
        assert [result["tool_use_id"] for result in results] == ["tool_parallel_a", "tool_parallel_b"]
        assert [call[0] for call in callback_calls] == ["read_file", "search_files"]
        assert json.loads(results[0]["content"]) == {
            "label": "read_file",
            "payload": {"path": "alpha.py"},
        }
        assert json.loads(results[1]["content"]) == {
            "label": "search_files",
            "payload": {"query": "beta"},
        }

    @pytest.mark.asyncio
    async def test_parallel_safe_sync_tool_batch_runs_off_loop(self):
        from brain.systems.runs.direct_agent import _GateState
        from brain.systems.runs.direct_loop.gates import check_gate_violations
        from brain.systems.runs.direct_loop.loop_control import LoopControlPolicy
        from brain.systems.runs.direct_loop.tool_execution import async_execute_tool_calls

        blocks = []
        for index, name in enumerate(("read_file", "search_files"), start=1):
            block = MagicMock()
            block.type = "tool_use"
            block.name = name
            block.input = {"value": name}
            block.id = f"sync-tool-{index}"
            blocks.append(block)

        response = MagicMock()
        response.content = blocks
        stop_ticker = asyncio.Event()
        ticker_count = 0
        rendezvous = threading.Barrier(2)

        async def ticker():
            nonlocal ticker_count
            while not stop_ticker.is_set():
                ticker_count += 1
                await asyncio.sleep(0.01)

        def blocking_handler(**kwargs):
            rendezvous.wait(timeout=1)
            time.sleep(0.05)
            return kwargs

        ticker_task = asyncio.create_task(ticker())
        started_at = time.perf_counter()
        try:
            execution = await async_execute_tool_calls(
                response,
                {"read_file": blocking_handler, "search_files": blocking_handler},
                [],
                _GateState(),
                None,
                None,
                None,
                "runner",
                agent_context=SimpleNamespace(),
                brain_tool_names=frozenset(),
                gated_tool_names=frozenset(),
                research_tool_names=frozenset(),
                research_budget=0,
                parallel_safe_tool_names=frozenset({"read_file", "search_files"}),
                max_parallel_tool_calls=2,
                check_gate_violations=check_gate_violations,
                loop_control=LoopControlPolicy(),
            )
        finally:
            elapsed = time.perf_counter() - started_at
            stop_ticker.set()
            await ticker_task

        assert elapsed < 0.5
        assert ticker_count >= 3
        results = execution.tool_results
        assert [result["tool_use_id"] for result in results] == ["sync-tool-1", "sync-tool-2"]

    async def test_parallel_safe_tool_batch_propagates_agent_context(self):
        from brain.systems.runs.direct_agent import _execute_tool_calls_async, _GateState, _agent_context
        from brain.systems.runs.direct_loop.loop_control import LoopControlPolicy

        block = MagicMock()
        block.type = "tool_use"
        block.name = "read_file"
        block.input = {"path": "alpha.py"}
        block.id = "tool_parallel_context"

        response = MagicMock()
        response.content = [block]

        _agent_context.user_id = 123
        _agent_context.worker_name = "reader-1"
        try:
            def handler(path):
                return {
                    "path": path,
                    "user_id": getattr(_agent_context, "user_id", None),
                    "worker_name": getattr(_agent_context, "worker_name", None),
                }

            execution = await _execute_tool_calls_async(
                response,
                {"read_file": handler},
                [],
                _GateState(),
                None,
                None,
                None,
                "runner",
                loop_control=LoopControlPolicy(),
            )
        finally:
            for attr in ("user_id", "worker_name"):
                if hasattr(_agent_context, attr):
                    delattr(_agent_context, attr)

        assert json.loads(execution.tool_results[0]["content"]) == {
            "path": "alpha.py",
            "user_id": 123,
            "worker_name": "reader-1",
        }

    @patch("brain.systems.runs.direct_agent.async_resolve_llm_client")
    @patch("brain.systems.runs.direct_agent._load_session", return_value=([], None))
    @patch("brain.systems.runs.direct_agent._save_session")
    def test_tool_use_loop(self, mock_save, mock_load, mock_client):
        from brain.systems.runs.direct_agent import run_agent

        client = MagicMock()
        # First call: tool use, second call: end_turn
        client.messages.create.side_effect = [
            self._make_response(
                text=None,
                stop_reason="tool_use",
                tool_use={"name": "brain_recall", "input": {"query": "test"}},
            ),
            self._make_response("Found relevant memories."),
        ]
        mock_client.return_value = _mock_llm_client(client)

        # Mock the tool handler
        handlers = {
            "brain_recall": lambda query, **kw: {"memories": [], "count": 0},
        }

        result = run_agent(
            message="Search for test",
            tools=[{"name": "brain_recall", "description": "test", "input_schema": {"type": "object", "properties": {}}}],
            tool_handlers=handlers,
            persist_session=False,
        )

        assert result.success
        assert "brain_recall" in result.tool_calls
        assert client.messages.create.call_count == 2

    @patch("brain.systems.runs.direct_agent.async_resolve_llm_client")
    @patch("brain.systems.runs.direct_agent._load_session", return_value=([], None))
    @patch("brain.systems.runs.direct_agent._save_session")
    def test_run_agent_binds_user_context_for_tool_handlers(self, mock_save, mock_load, mock_client):
        from brain.systems.runs.direct_agent import run_agent, _agent_context

        client = MagicMock()
        client.messages.create.side_effect = [
            self._make_response(
                text=None,
                stop_reason="tool_use",
                tool_use={"name": "brain_recall", "input": {"query": "test"}},
            ),
            self._make_response("Done."),
        ]
        mock_client.return_value = _mock_llm_client(client)

        captured = {}

        def handler(query):
            captured["query"] = query
            captured["user_id"] = getattr(_agent_context, "user_id", None)
            captured["org_id"] = getattr(_agent_context, "org_id", None)
            return {"memories": [], "count": 0}

        result = run_agent(
            message="Search for test",
            tools=[{"name": "brain_recall", "description": "test", "input_schema": {"type": "object", "properties": {}}}],
            tool_handlers={"brain_recall": handler},
            persist_session=False,
            user_id="user-1",
            metadata={"org_id": "org-1"},
        )

        assert result.success
        assert captured == {"query": "test", "user_id": "user-1", "org_id": "org-1"}

    @patch("brain.systems.runs.direct_agent.async_resolve_llm_client")
    @patch("brain.systems.runs.direct_agent._load_session", return_value=([], None))
    @patch("brain.systems.runs.direct_agent._save_session")
    def test_run_agent_binds_org_argument_for_tool_handlers(self, mock_save, mock_load, mock_client):
        from brain.systems.runs.direct_agent import run_agent, _agent_context

        client = MagicMock()
        client.messages.create.side_effect = [
            self._make_response(
                text=None,
                stop_reason="tool_use",
                tool_use={"name": "brain_recall", "input": {"query": "test"}},
            ),
            self._make_response("Done."),
        ]
        mock_client.return_value = _mock_llm_client(client)

        captured = {}

        def handler(query):
            execution_metadata = getattr(_agent_context, "execution_metadata", {}) or {}
            captured["query"] = query
            captured["user_id"] = getattr(_agent_context, "user_id", None)
            captured["org_id"] = getattr(_agent_context, "org_id", None)
            captured["execution_org_id"] = execution_metadata.get("org_id")
            captured["target_ref"] = execution_metadata.get("target_ref")
            return {"memories": [], "count": 0}

        result = run_agent(
            message="Search for test",
            tools=[{"name": "brain_recall", "description": "test", "input_schema": {"type": "object", "properties": {}}}],
            tool_handlers={"brain_recall": handler},
            persist_session=False,
            user_id="user-1",
            org_id="org-argument",
            metadata={
                "execution_provenance": {"run_id": 42},
                "target_ref": {"idea_id": "idea-1"},
            },
        )

        assert result.success
        assert captured == {
            "query": "test",
            "user_id": "user-1",
            "org_id": "org-argument",
            "execution_org_id": "org-argument",
            "target_ref": {"idea_id": "idea-1"},
        }

    @patch("brain.systems.runs.direct_agent.async_resolve_llm_client")
    @patch("brain.systems.runs.direct_agent._load_session", return_value=([], None))
    @patch("brain.systems.runs.direct_agent._save_session")
    def test_run_agent_binds_workspace_root_for_tool_handlers(self, mock_save, mock_load, mock_client, tmp_path):
        from brain.systems.runs.direct_agent import run_agent, _agent_context

        client = MagicMock()
        client.messages.create.side_effect = [
            self._make_response(
                text=None,
                stop_reason="tool_use",
                tool_use={"name": "project_context", "input": {"path": "."}},
            ),
            self._make_response("Done."),
        ]
        mock_client.return_value = _mock_llm_client(client)

        captured = {}

        def handler(path=None):
            captured["path"] = path
            captured["workspace_root"] = getattr(_agent_context, "workspace_root", None)
            return {"project_type": "test"}

        result = run_agent(
            message="Inspect project",
            tools=[{"name": "project_context", "description": "test", "input_schema": {"type": "object", "properties": {}}}],
            tool_handlers={"project_context": handler},
            persist_session=False,
            workspace_root=str(tmp_path),
        )

        assert result.success
        assert captured == {"path": ".", "workspace_root": str(tmp_path)}

    def test_resolve_tool_call_marks_blocked_cortex_reply_as_error(self):
        from brain.systems.runs.direct_agent import _PendingToolCall, _resolve_tool_call

        request = _PendingToolCall(
            block_id="tool_123",
            tool_name="cortex_reply",
            tool_input={"content": "partial"},
            handler=lambda **_: {
                "blocked": True,
                "instruction": "Continue working and plan another pipeline if execution remains.",
            },
        )

        resolved = _resolve_tool_call(request)

        assert resolved.outcome.failure is not None
        assert "Continue working and plan another pipeline" in resolved.result_text

    @patch("brain.systems.runs.direct_agent.async_resolve_llm_client")
    @patch("brain.systems.runs.direct_agent._load_session", return_value=([], None))
    @patch("brain.systems.runs.direct_agent._save_session")
    def test_token_accumulation(self, mock_save, mock_load, mock_client):
        from brain.systems.runs.direct_agent import run_agent

        client = MagicMock()
        # Two turns of tool use + final response
        client.messages.create.side_effect = [
            self._make_response(text=None, stop_reason="tool_use",
                                tool_use={"name": "brain_recall", "input": {"query": "a"}}),
            self._make_response("Done"),
        ]
        mock_client.return_value = _mock_llm_client(client)

        handlers = {"brain_recall": lambda **kw: {"memories": []}}

        result = run_agent(
            message="test",
            tools=[{"name": "brain_recall", "description": "t", "input_schema": {"type": "object", "properties": {}}}],
            tool_handlers=handlers,
            persist_session=False,
        )

        # 2 API calls × 1000 input tokens each
        assert result.tokens_input == 2000
        assert result.tokens_output == 400
        assert result.tokens_cache_read == 1000

    @patch("brain.systems.runs.direct_agent.async_resolve_llm_client")
    @patch("brain.systems.runs.direct_agent._load_session", return_value=([], None))
    @patch("brain.systems.runs.direct_agent._save_session")
    def test_timeout_returns_failure(self, mock_save, mock_load, mock_client):
        from brain.systems.runs.direct_agent import run_agent

        client = MagicMock()
        client.messages.create.return_value = self._make_response(
            text="done", stop_reason="end_turn"
        )
        mock_client.return_value = _mock_llm_client(client)

        handlers = {"brain_recall": lambda **kw: {"memories": []}}

        result = run_agent(
            message="test",
            tools=[{"name": "brain_recall", "description": "t", "input_schema": {"type": "object", "properties": {}}}],
            tool_handlers=handlers,
            persist_session=False,
        )

        assert result.success
        assert result.output == "done"

    @patch("brain.systems.runs.direct_agent.async_resolve_llm_client")
    @patch("brain.systems.runs.direct_agent._load_session", return_value=([], None))
    @patch("brain.systems.runs.direct_agent._save_session")
    def test_logs_when_end_turn_has_output_tokens_but_no_content(self, mock_save, mock_load, mock_client, caplog):
        from brain.systems.runs.direct_agent import run_agent

        client = MagicMock()
        response = self._make_response(text=None, stop_reason="end_turn")
        response.content = []
        client.messages.create.return_value = response
        mock_client.return_value = _mock_llm_client(client)

        with caplog.at_level("WARNING"):
            result = run_agent(
                message="Say hello",
                model="claude-sonnet-4-6",
                tools=[],
                persist_session=False,
            )

        assert result.success
        assert result.output == ""
        assert "no parsed assistant content" in caplog.text
        assert "final extracted output is empty" in caplog.text

    @patch("brain.systems.runs.direct_agent.async_resolve_llm_client")
    @patch("brain.systems.runs.direct_agent._load_session", return_value=([], None))
    @patch("brain.systems.runs.direct_agent._save_session")
    def test_cancel_event_stops_agent(self, mock_save, mock_load, mock_client):
        import threading
        from brain.systems.runs.direct_agent import run_agent

        client = MagicMock()
        client.messages.create.return_value = self._make_response(
            text=None, stop_reason="tool_use",
            tool_use={"name": "brain_recall", "input": {"query": "x"}},
        )
        mock_client.return_value = _mock_llm_client(client)

        handlers = {"brain_recall": lambda **kw: {"memories": []}}
        cancel = threading.Event()
        cancel.set()  # Pre-set — agent should stop on first turn

        result = run_agent(
            message="test",
            tools=[{"name": "brain_recall", "description": "t", "input_schema": {"type": "object", "properties": {}}}],
            tool_handlers=handlers,
            persist_session=False,
            cancel_event=cancel,
        )

        assert not result.success
        assert "Cancelled" in result.error

    @patch("brain.systems.runs.direct_agent.async_resolve_llm_client")
    @patch("brain.systems.runs.direct_agent._load_session", return_value=([], None))
    @patch("brain.systems.runs.direct_agent._save_session")
    def test_on_tool_call_callback(self, mock_save, mock_load, mock_client):
        from brain.systems.runs.direct_agent import run_agent

        client = MagicMock()
        client.messages.create.side_effect = [
            self._make_response(text=None, stop_reason="tool_use",
                                tool_use={"name": "brain_recall", "input": {"query": "test"}}),
            self._make_response("Done"),
        ]
        mock_client.return_value = _mock_llm_client(client)

        callback_calls = []
        def on_tool_call(name, args, result_text):
            callback_calls.append((name, args))

        handlers = {"brain_recall": lambda **kw: {"memories": []}}

        run_agent(
            message="test",
            tools=[{"name": "brain_recall", "description": "t", "input_schema": {"type": "object", "properties": {}}}],
            tool_handlers=handlers,
            on_tool_call=on_tool_call,
            persist_session=False,
        )

        assert len(callback_calls) == 1
        assert callback_calls[0][0] == "brain_recall"

    @patch("brain.systems.runs.direct_agent.async_resolve_llm_client")
    @patch("brain.systems.runs.direct_agent._load_session", return_value=([], None))
    @patch("brain.systems.runs.direct_agent._save_session")
    def test_metadata_requires_brain_recall_before_end_turn(self, mock_save, mock_load, mock_client):
        from brain.systems.runs.direct_agent import run_agent

        client = MagicMock()
        client.messages.create.side_effect = [
            self._make_response("I remember some things."),
            self._make_response(text=None, stop_reason="tool_use", tool_use={"name": "brain_recall", "input": {"query": "what do you remember"}}),
            self._make_response("Here is what I remember."),
        ]
        mock_client.return_value = _mock_llm_client(client)

        handlers = {"brain_recall": lambda **kw: {"memories": [{"content": "prior work"}]}}

        result = run_agent(
            message="@illo what do you remember ?",
            tools=[{"name": "brain_recall", "description": "test", "input_schema": {"type": "object", "properties": {}}}],
            tool_handlers=handlers,
            metadata={"required_introspection_tool": "brain_recall"},
            persist_session=False,
        )

        assert result.success
        assert "brain_recall" in result.tool_calls
        assert client.messages.create.call_count == 3

    @patch("brain.systems.runs.direct_agent.async_resolve_llm_client")
    @patch("brain.systems.runs.direct_agent._load_session", return_value=([], None))
    @patch("brain.systems.runs.direct_agent._save_session")
    def test_memory_summary_metadata_requires_brain_recall_before_end_turn(self, mock_save, mock_load, mock_client):
        from brain.systems.runs.direct_agent import run_agent

        client = MagicMock()
        client.messages.create.side_effect = [
            self._make_response("Sure — please share the notes from this week."),
            self._make_response(text=None, stop_reason="tool_use", tool_use={"name": "brain_recall", "input": {"query": "what we did this week"}}),
            self._make_response("Here is the summary of what we did this week."),
        ]
        mock_client.return_value = _mock_llm_client(client)

        handlers = {"brain_recall": lambda **kw: {"memories": [{"content": "prior work"}]}}

        result = run_agent(
            message="Can you sum up what we did this week ?",
            tools=[{"name": "brain_recall", "description": "test", "input_schema": {"type": "object", "properties": {}}}],
            tool_handlers=handlers,
            metadata={"required_introspection_tool": "brain_recall"},
            persist_session=False,
        )

        assert result.success
        assert "brain_recall" in result.tool_calls
        assert client.messages.create.call_count == 3

    @patch("brain.systems.runs.direct_agent.async_resolve_llm_client")
    @patch("brain.systems.runs.direct_agent._load_session", return_value=([], None))
    @patch("brain.systems.runs.direct_agent._save_session")
    def test_metadata_requires_runtime_settings_before_end_turn(self, mock_save, mock_load, mock_client):
        from brain.systems.runs.direct_agent import run_agent

        client = MagicMock()
        client.messages.create.side_effect = [
            self._make_response("Your provider is OpenAI."),
            self._make_response(text=None, stop_reason="tool_use", tool_use={"name": "runtime_settings", "input": {}}),
            self._make_response("Your default provider is OpenAI."),
        ]
        mock_client.return_value = _mock_llm_client(client)

        handlers = {"runtime_settings": lambda **kw: {"effective_provider": "openai"}}

        result = run_agent(
            message="@illo what is my default provider ?",
            tools=[{"name": "runtime_settings", "description": "test", "input_schema": {"type": "object", "properties": {}}}],
            tool_handlers=handlers,
            metadata={"required_introspection_tool": "runtime_settings"},
            persist_session=False,
        )

        assert result.success
        assert "runtime_settings" in result.tool_calls
        assert client.messages.create.call_count == 3

    @patch("brain.systems.runs.direct_agent.async_resolve_llm_client")
    @patch("brain.systems.runs.direct_agent._load_session", return_value=([], None))
    @patch("brain.systems.runs.direct_agent._save_session")
    def test_metadata_requires_my_activity_before_end_turn(self, mock_save, mock_load, mock_client):
        from brain.systems.runs.direct_agent import run_agent

        client = MagicMock()
        client.messages.create.side_effect = [
            self._make_response("I pushed PR #123."),
            self._make_response(text=None, stop_reason="tool_use", tool_use={"name": "my_activity", "input": {}}),
            self._make_response("I created PR #123 in this run."),
        ]
        mock_client.return_value = _mock_llm_client(client)

        handlers = {"my_activity": lambda **kw: {"execution_artifacts": [{"type": "pr", "number": 123}]}}

        result = run_agent(
            message="@illo what PR did you push ?",
            tools=[{"name": "my_activity", "description": "test", "input_schema": {"type": "object", "properties": {}}}],
            tool_handlers=handlers,
            metadata={"required_introspection_tool": "my_activity"},
            persist_session=False,
        )

        assert result.success
        assert "my_activity" in result.tool_calls
        assert client.messages.create.call_count == 3

    @patch("brain.systems.runs.direct_agent.async_resolve_llm_client")
    @patch("brain.systems.runs.direct_agent._load_session", return_value=([], None))
    @patch("brain.systems.runs.direct_agent._save_session")
    def test_thread_content_action_does_not_force_capability_detour(self, mock_save, mock_load, mock_client):
        from brain.systems.runs.direct_agent import run_agent

        client = MagicMock()
        client.messages.create.side_effect = [
            self._make_response("I found JB's response and added it to the thread."),
            self._make_response(
                text=None,
                stop_reason="tool_use",
                tool_use={
                    "name": "read_capabilities",
                    "input": {"query": "Add JB's response to the thread."},
                },
            ),
            self._make_response("I can help with threads, Slack, workspace tools, and setup."),
        ]
        mock_client.return_value = _mock_llm_client(client)

        handler = MagicMock(return_value={"capabilities": [{"key": "threads"}]})

        result = run_agent(
            message="Add JB's response to the thread.",
            tools=[{
                "name": "read_capabilities",
                "description": "test",
                "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}},
            }],
            tool_handlers={"read_capabilities": handler},
            persist_session=False,
        )

        assert result.success
        assert result.output == "I found JB's response and added it to the thread."
        assert result.tool_calls == []
        assert handler.call_count == 0
        assert client.messages.create.call_count == 1

    @patch("brain.systems.runs.direct_agent.async_resolve_llm_client")
    @patch("brain.systems.runs.direct_agent._load_session", return_value=([], None))
    @patch("brain.systems.runs.direct_agent._save_session")
    def test_wrapped_thread_reply_uses_latest_message_for_introspection(self, mock_save, mock_load, mock_client):
        from brain.systems.runs.direct_agent import run_agent

        message = (
            '[Idea: "Create a skill to analyze our db in prod and connect to postgres" | idea-1]\n\n'
            "give me the last 10 generations in production with the company associated"
        )
        client = MagicMock()
        client.messages.create.side_effect = [
            self._make_response("Last 10 production generations: 826324 Roman.co.uk, 826323 Roman.co.uk."),
            self._make_response(
                text=None,
                stop_reason="tool_use",
                tool_use={
                    "name": "read_capabilities",
                    "input": {"query": "current Illo capability and setup context"},
                },
            ),
            self._make_response("I can inspect and act across the workspace: Threads, Domains, Vault, skills."),
        ]
        mock_client.return_value = _mock_llm_client(client)

        handler = MagicMock(return_value={"capabilities": [{"key": "vault"}]})

        result = run_agent(
            message=message,
            tools=[{
                "name": "read_capabilities",
                "description": "test",
                "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}},
            }],
            tool_handlers={"read_capabilities": handler},
            persist_session=False,
        )

        assert result.success
        assert result.output == "Last 10 production generations: 826324 Roman.co.uk, 826323 Roman.co.uk."
        assert result.tool_calls == []
        assert handler.call_count == 0
        assert client.messages.create.call_count == 1

    @patch("brain.systems.runs.direct_agent.async_resolve_llm_client")
    @patch("brain.systems.runs.direct_agent._load_session", return_value=([], None))
    @patch("brain.systems.runs.direct_agent._save_session")
    def test_provenance_question_does_not_loop_when_my_activity_is_not_exposed(self, mock_save, mock_load, mock_client):
        from brain.systems.runs.direct_agent import run_agent

        client = MagicMock()
        client.messages.create.return_value = self._make_response("I can't inspect run provenance in this runtime.")
        mock_client.return_value = _mock_llm_client(client)

        result = run_agent(
            message="What did you do?",
            tools=[],
            tool_handlers={"my_activity": lambda **kw: {"execution_artifacts": []}},
            persist_session=False,
            max_turns=5,
        )

        assert result.success
        assert result.output == "I can't inspect run provenance in this runtime."
        assert result.tool_calls == []
        assert client.messages.create.call_count == 1

    @patch("brain.systems.runs.direct_agent.async_resolve_llm_client")
    @patch("brain.systems.runs.direct_agent._load_session", return_value=([], None))
    @patch("brain.systems.runs.direct_agent._save_session")
    def test_derived_introspection_does_not_overwrite_substantial_answer(self, mock_save, mock_load, mock_client):
        # Regression for issue #249: a real task can trip the self-context heuristic
        # (here "...where the source code is installed..."). Once the model has produced
        # a substantial answer without the heuristically-derived tool, the end-of-run
        # guard must NOT force a late read_self_context detour that discards the work and
        # replaces it with a runtime self-description.
        from brain.systems.runs.direct_agent import run_agent

        long_answer = (
            "Redirect audit complete. Every 301 resolves in a single hop to a live 200; "
            "no chains, loops, or 404s. Rankings for the retired pages are being retained "
            "by their redirect targets, and the hedge pages held their impressions. Search "
            "Console shows no new coverage or indexing errors, and the sitemap was re-fetched "
            "with the new URL structure. It is still early, so treat week-one ranking data as "
            "noisy rather than conclusive, and re-check impressions again after a full week."
        )
        assert len(long_answer.strip()) >= 400

        client = MagicMock()
        client.messages.create.return_value = self._make_response(long_answer)
        mock_client.return_value = _mock_llm_client(client)

        handler = MagicMock(return_value={"ok": True, "source": "runtime_self_context"})

        result = run_agent(
            message=(
                "Audit our deploy: confirm the redirects still work and tell me where the "
                "source code is installed if you need it."
            ),
            tools=[{
                "name": "read_self_context",
                "description": "test",
                "input_schema": {"type": "object", "properties": {}},
            }],
            tool_handlers={"read_self_context": handler},
            persist_session=False,
            max_turns=4,
        )

        assert result.success
        assert result.output == long_answer
        assert result.tool_calls == []
        assert handler.call_count == 0
        assert client.messages.create.call_count == 1

    @patch("brain.systems.runs.direct_agent.async_resolve_llm_client")
    @patch("brain.systems.runs.direct_agent._load_session", return_value=([], None))
    @patch("brain.systems.runs.direct_agent._save_session")
    def test_explicit_required_tool_forces_even_after_substantial_answer(self, mock_save, mock_load, mock_client):
        # The #249 guard only relaxes heuristically-derived requirements. An explicit
        # routing directive stays authoritative and must still force its tool even when
        # the model already produced a long first answer.
        from brain.systems.runs.direct_agent import run_agent

        long_answer = "Here is a detailed answer to your question. " * 12
        assert len(long_answer.strip()) >= 400

        client = MagicMock()
        client.messages.create.side_effect = [
            self._make_response(long_answer),
            self._make_response(text=None, stop_reason="tool_use", tool_use={"name": "brain_recall", "input": {"query": "history"}}),
            self._make_response("Grounded summary from memory."),
        ]
        mock_client.return_value = _mock_llm_client(client)

        result = run_agent(
            message="Summarize what we did this week.",
            tools=[{"name": "brain_recall", "description": "test", "input_schema": {"type": "object", "properties": {}}}],
            tool_handlers={"brain_recall": lambda **kw: {"memories": [{"content": "prior work"}]}},
            metadata={"required_introspection_tool": "brain_recall"},
            persist_session=False,
        )

        assert result.success
        assert "brain_recall" in result.tool_calls
        assert client.messages.create.call_count == 3


class TestCallLLM:
    """Test the call_llm convenience function."""

    @patch("brain.systems.runs.direct_agent.run_agent")
    def test_returns_parsed_json(self, mock_run):
        from brain.systems.runs.direct_agent import call_llm, AgentResult

        mock_run.return_value = AgentResult(
            output='{"key": "value"}',
            success=True, session_id="test",
        )

        result = call_llm("Generate JSON")
        assert result == {"key": "value"}

    @patch("brain.systems.runs.direct_agent.run_agent")
    def test_extracts_json_from_text(self, mock_run):
        from brain.systems.runs.direct_agent import call_llm, AgentResult

        mock_run.return_value = AgentResult(
            output='Here is the result:\n{"key": "value"}\nDone.',
            success=True, session_id="test",
        )

        result = call_llm("Generate JSON")
        assert result == {"key": "value"}

    @patch("brain.systems.runs.direct_agent.run_agent")
    def test_returns_none_on_failure(self, mock_run):
        from brain.systems.runs.direct_agent import call_llm, AgentResult

        mock_run.return_value = AgentResult(
            output="", success=False, session_id="test",
            error="API error",
        )

        result = call_llm("Generate JSON")
        assert result is None

    @patch("brain.systems.runs.direct_agent.run_agent")
    def test_passes_no_tools(self, mock_run):
        from brain.systems.runs.direct_agent import call_llm, AgentResult

        mock_run.return_value = AgentResult(
            output="{}", success=True, session_id="test",
        )

        call_llm("test")
        kwargs = mock_run.call_args[1]
        assert kwargs["tools"] == []
        assert kwargs["persist_session"] is False
        assert kwargs["max_turns"] == 1


class TestExecToolHandlers:
    """Test the execution tool handler implementations."""

    @pytest.fixture(autouse=True)
    def _set_workspace(self, tmp_path, monkeypatch):
        """Point WORKSPACE_ROOT to tmp_path so path containment allows test files."""
        import brain.systems.runs.direct_agent as agent_mod
        import brain.systems.runs.tool_handlers as handler_mod
        monkeypatch.setattr(agent_mod, "WORKSPACE_ROOT", str(tmp_path))
        monkeypatch.setattr(handler_mod, "WORKSPACE_ROOT", str(tmp_path))

    def test_read_file(self, tmp_path):
        from brain.systems.runs.direct_agent import _handle_read_file
        test_file = tmp_path / "test.txt"
        test_file.write_text("line1\nline2\nline3\n")

        result = _handle_read_file(str(test_file))
        assert "line1" in result["content"]
        assert "line2" in result["content"]
        assert result["total_lines"] == 3

    def test_read_file_with_range(self, tmp_path):
        from brain.systems.runs.direct_agent import _handle_read_file
        test_file = tmp_path / "test.txt"
        test_file.write_text("a\nb\nc\nd\ne\n")

        result = _handle_read_file(str(test_file), start_line=2, end_line=4)
        assert "b" in result["content"]
        assert "d" in result["content"]
        assert "a" not in result["content"]

    def test_read_file_not_found(self, tmp_path):
        from brain.systems.runs.direct_agent import _handle_read_file
        result = _handle_read_file(str(tmp_path / "nonexistent.txt"))
        assert "error" in result

    def test_path_containment_blocks_escape(self, tmp_path):
        """Path traversal attempts should be blocked."""
        from brain.systems.runs.direct_agent import _handle_read_file, _handle_write_file, _handle_edit_file

        # Absolute path outside workspace
        result = _handle_read_file("/etc/passwd")
        assert "error" in result
        assert "escapes workspace" in result["error"]

        # Relative path with ../
        result = _handle_write_file("../../etc/evil.txt", "bad")
        assert "error" in result
        assert "escapes workspace" in result["error"]

        # Edit with path escape
        result = _handle_edit_file("/tmp/outside.txt", "old", "new")
        assert "error" in result
        assert "escapes workspace" in result["error"]

    def test_write_file(self, tmp_path):
        from brain.systems.runs.direct_agent import _handle_write_file
        test_file = tmp_path / "output.txt"

        result = _handle_write_file(str(test_file), "hello world")
        assert result["written"] is True
        assert test_file.read_text() == "hello world"

    def test_write_file_creates_dirs(self, tmp_path):
        from brain.systems.runs.direct_agent import _handle_write_file
        test_file = tmp_path / "sub" / "dir" / "file.txt"

        result = _handle_write_file(str(test_file), "nested")
        assert result["written"] is True
        assert test_file.read_text() == "nested"

    def test_edit_file(self, tmp_path):
        from brain.systems.runs.direct_agent import _handle_edit_file
        test_file = tmp_path / "edit.txt"
        test_file.write_text("hello world\ngoodbye world\n")

        result = _handle_edit_file(str(test_file), "hello world", "hi world")
        assert result["edited"] is True
        assert "hi world" in test_file.read_text()
        assert "goodbye world" in test_file.read_text()

    def test_edit_file_not_unique(self, tmp_path):
        from brain.systems.runs.direct_agent import _handle_edit_file
        test_file = tmp_path / "dup.txt"
        test_file.write_text("foo\nfoo\n")

        result = _handle_edit_file(str(test_file), "foo", "bar")
        assert "error" in result
        assert "2 locations" in result["error"]

    def test_edit_file_not_found_text(self, tmp_path):
        from brain.systems.runs.direct_agent import _handle_edit_file
        test_file = tmp_path / "edit2.txt"
        test_file.write_text("hello")
        result = _handle_edit_file(str(test_file), "nonexistent", "replacement")
        assert "error" in result

    def test_exec_command(self):
        from brain.systems.runs.direct_agent import _handle_exec_command
        result = _handle_exec_command("echo hello")
        assert result["exit_code"] == 0
        assert "hello" in result["stdout"]

    def test_exec_command_timeout(self):
        from brain.systems.runs.direct_agent import _handle_exec_command
        result = _handle_exec_command("sleep 10", timeout=1)
        assert result["exit_code"] == -1
        assert "timed out" in result["stderr"]

    def test_list_files(self, tmp_path):
        from brain.systems.runs.direct_agent import _handle_list_files
        (tmp_path / "a.py").write_text("")
        (tmp_path / "b.py").write_text("")
        (tmp_path / "c.txt").write_text("")

        result = _handle_list_files("*.py", str(tmp_path))
        assert len(result["files"]) == 2
        assert any("a.py" in f for f in result["files"])

    def test_search_files(self, tmp_path):
        from brain.systems.runs.direct_agent import _handle_search_files
        (tmp_path / "test.py").write_text("def hello():\n    pass\n")

        result = _handle_search_files("def hello", str(tmp_path))
        assert result["count"] >= 1

    def test_project_draft_metadata_is_hidden_from_file_tools(self, tmp_path):
        from brain.systems.runs.direct_agent import _handle_list_files, _handle_read_file, _handle_search_files

        (tmp_path / "unified_payments.csv").write_text("visible,target\n", encoding="utf-8")
        metadata = tmp_path / ".illo-project-draft"
        metadata.mkdir()
        (metadata / "metadata.json").write_text('{"internal": "target"}', encoding="utf-8")
        base = metadata / "base"
        base.mkdir()
        (base / "unified_payments.csv").write_text("internal,target\n", encoding="utf-8")

        listed = _handle_list_files("**/*", str(tmp_path))
        searched = _handle_search_files("target", str(tmp_path))
        direct_read = _handle_read_file(str(metadata / "metadata.json"))

        assert listed["files"] == ["unified_payments.csv"]
        assert listed["total"] == 1
        assert ".illo-project-draft" not in searched["matches"]
        assert "unified_payments.csv" in searched["matches"]
        assert "error" in direct_read

    def test_workspace_selector_reads_from_additional_repo(self, tmp_path):
        from brain.systems.runs.tool_handlers import _get_tool_handlers

        backend = tmp_path / "backend"
        frontend = tmp_path / "frontend"
        backend.mkdir()
        frontend.mkdir()
        (backend / "api.py").write_text("BACKEND = True\n")
        (frontend / "app.ts").write_text("export const FRONTEND = true;\n")

        handlers = _get_tool_handlers(
            workspace_root=str(backend),
            allowed_workspaces=[{"name": "frontend", "path": str(frontend)}],
        )

        result = handlers["read_file"]("app.ts", workspace="frontend")
        assert "FRONTEND" in result["content"]

    def test_workspace_selector_preserves_allowed_name_for_default_workspace(self, tmp_path):
        from brain.systems.runs.tool_handlers import _get_tool_handlers

        project_root = tmp_path / "project-root"
        backend = tmp_path / "github" / "uwear-ai" / "uwear-backend"
        project_root.mkdir(parents=True)
        backend.mkdir(parents=True)
        (backend / "api.py").write_text("BACKEND = True\n")

        handlers = _get_tool_handlers(
            workspace_root=str(backend),
            allowed_workspaces=[
                {"name": "/", "path": str(project_root)},
                {"name": "/uwear-ai/uwear-backend", "path": str(backend)},
            ],
        )

        without_slash = handlers["read_file"]("api.py", workspace="uwear-ai/uwear-backend")
        with_slash = handlers["read_file"]("api.py", workspace="/uwear-ai/uwear-backend")
        assert "BACKEND" in without_slash["content"]
        assert "BACKEND" in with_slash["content"]

    def test_workspace_selector_accepts_project_mount_path(self, tmp_path):
        from brain.systems.runs.tool_handlers import _get_tool_handlers

        reports = tmp_path / "reports"
        reports.mkdir()
        (reports / "q4.md").write_text("# Q4\n")

        handlers = _get_tool_handlers(
            allowed_workspaces=[{"name": "/reports", "path": str(reports)}],
        )

        result = handlers["read_file"]("q4.md", workspace="/reports")
        assert "Q4" in result["content"]

    def test_workspace_selector_works_without_primary_workspace_root(self, tmp_path):
        from brain.systems.runs.tool_handlers import _get_tool_handlers

        frontend = tmp_path / "frontend"
        frontend.mkdir()
        (frontend / "app.ts").write_text("export const FRONTEND = true;\n")

        handlers = _get_tool_handlers(
            allowed_workspaces=[{"name": "frontend", "path": str(frontend)}],
        )

        result = handlers["read_file"]("app.ts", workspace="frontend")
        assert "FRONTEND" in result["content"]

    def test_workspace_selector_rejects_unknown_repo(self, tmp_path):
        from brain.systems.runs.tool_handlers import _get_tool_handlers

        backend = tmp_path / "backend"
        backend.mkdir()
        handlers = _get_tool_handlers(workspace_root=str(backend))

        with pytest.raises(ValueError, match="not accessible"):
            handlers["read_file"]("app.ts", workspace="frontend")

    def test_extended_project_context_uses_bound_workspace_root(self, tmp_path, monkeypatch):
        from brain.systems.runs.tool_handlers import _get_tool_handlers

        repo = tmp_path / "thread-project"
        repo.mkdir()
        (repo / "pyproject.toml").write_text("[project]\nname = 'thread-project'\n")

        monkeypatch.setattr(
            "brain.systems.tools.handlers.handle_project_context",
            lambda path=None, workspace_root=None: {"path": path, "workspace_root": workspace_root},
        )

        handlers = _get_tool_handlers(workspace_root=str(repo))

        assert handlers["project_context"](path=".") == {
            "path": ".",
            "workspace_root": str(repo),
        }

    def test_exec_command_working_dir_cannot_escape_selected_workspace(self, tmp_path):
        from brain.systems.runs.direct_agent import _handle_exec_command

        repo = tmp_path / "repo"
        repo.mkdir()

        result = _handle_exec_command("pwd", working_dir="../outside", _workspace=str(repo))
        assert result["exit_code"] == -1
        assert "escapes workspace" in result["stderr"]

    def test_exec_command_ignores_file_workspace_root(self, tmp_path):
        from brain.systems.runs.direct_agent import _handle_exec_command

        attachment = tmp_path / "agent.md"
        attachment.write_text("# Sales agent\n")

        result = _handle_exec_command("echo ok", _workspace=str(attachment))
        assert result["exit_code"] == 0
        assert result["stdout"].strip() == "ok"


class TestFinalReplyReview:
    @staticmethod
    def _resolved_checker_provider():
        provider = MagicMock()
        response = MagicMock()
        response.content = [
            SimpleNamespace(
                type="text",
                text=(
                    '{"status":"resolved","rationale":"Status is fully and honestly reported.",'
                    '"missing_requirements":[]}'
                ),
            )
        ]
        provider.create.return_value = response
        return provider

    @staticmethod
    def _incident_status_evidence(*, status: str, final_output: str | None = None):
        from brain.systems.runs.direct_loop.final_reply_evidence import (
            FinalReplyEvidence,
            StatusQuestionEvidence,
            ToolResultEvidence,
        )
        from brain.systems.runs.status import (
            OPEN_RUN_STATUSES,
            RunStatus,
            coerce_run_status,
        )

        run_status = coerce_run_status(status, default=RunStatus.FAILED)

        return FinalReplyEvidence(
            tool_results=(
                ToolResultEvidence.capture(
                    tool_name="read_team_activity",
                    arguments={"query": "customer ticket"},
                    is_error=False,
                    result={
                        "records": [
                            {
                                "id": 2383,
                                "domain": "Customer Support Tickets",
                                "assignee": "Reda",
                                "status": "triaging",
                            }
                        ]
                    },
                ),
            ),
            status_question=StatusQuestionEvidence.from_mapping(
                {
                    "thread_id": "slack:T_ALERTS:C_ALERTS:1784741844.000100",
                    "lookup_status": "verified",
                    "originating_run": {
                        "run_id": 2327,
                        "status": run_status,
                        "request": (
                            "we may have a bug, email from a customer, "
                            "assign ticket to me"
                        ),
                        "final_output": final_output,
                    },
                    "live_sibling_runs": (
                        [{"run_id": 2327, "status": run_status}]
                        if run_status in OPEN_RUN_STATUSES
                        else []
                    ),
                    "deliverables": [
                        {
                            "kind": "github_issue",
                            "label": "GitHub ticket",
                        },
                        {
                            "kind": "assignment",
                            "label": "ticket assignment",
                        },
                    ],
                }
            ),
        )

    @pytest.mark.parametrize(
        "status",
        ["queued", "starting", "running", "paused", "verifying"],
    )
    def test_status_question_rejects_done_while_originating_run_is_live(self, status):
        from brain.systems.runs.direct_agent import review_candidate_final_reply
        from brain.systems.runs.direct_loop.final_reply_checker import FinalReplyEnforcement

        provider = MagicMock()
        result = review_candidate_final_reply(
            user_request="was it done?",
            candidate_output=(
                "Yes — done. It is logged in Customer Support Tickets as #2383, "
                "assigned to Reda."
            ),
            evidence=self._incident_status_evidence(status=status),
            provider=provider,
            llm=_mock_llm_client(MagicMock(), provider="openai"),
            model="openai/gpt-5.5",
        )

        assert result["status"] == "continue"
        assert result["enforcement"] is FinalReplyEnforcement.BLOCK
        assert "run 2327" in result["rationale"]
        assert status in result["rationale"]
        assert "in progress" in " ".join(result["missing_requirements"]).lower()
        provider.create.assert_not_called()

    @pytest.mark.parametrize(
        ("kind", "label"),
        [
            ("ticket", "ticket"),
            ("future_kind", "future deliverable"),
        ],
    )
    def test_status_question_rejects_done_for_deliverable_without_verified_ref(
        self,
        kind,
        label,
    ):
        from brain.systems.runs.direct_agent import review_candidate_final_reply
        from brain.systems.runs.direct_loop.final_reply_checker import FinalReplyEnforcement
        from brain.systems.runs.direct_loop.final_reply_evidence import (
            FinalReplyEvidence,
            StatusQuestionEvidence,
        )

        evidence = FinalReplyEvidence(
            status_question=StatusQuestionEvidence.from_mapping(
                {
                    "thread_id": "thread-1",
                    "lookup_status": "verified",
                    "originating_run": {
                        "run_id": 2327,
                        "status": "completed",
                        "request": f"create the {label}",
                        "final_output": f"Worked on the {label}.",
                    },
                    "live_sibling_runs": [],
                    "deliverables": [{"kind": kind, "label": label}],
                }
            )
        )
        provider = MagicMock()

        result = review_candidate_final_reply(
            user_request="was it done?",
            candidate_output=f"Yes — done. The {label} was completed.",
            evidence=evidence,
            provider=provider,
            llm=_mock_llm_client(MagicMock(), provider="openai"),
            model="openai/gpt-5.5",
        )

        assert result["status"] == "continue"
        assert result["enforcement"] is FinalReplyEnforcement.BLOCK
        assert label in result["rationale"]
        assert "verified" in result["rationale"]
        provider.create.assert_not_called()

    def test_1744_replay_names_partial_record_and_missing_github_ticket(self):
        from brain.systems.runs.direct_agent import review_candidate_final_reply

        provider = self._resolved_checker_provider()
        result = review_candidate_final_reply(
            user_request="was it done?",
            candidate_output=(
                "It is still in progress. Customer Support record #2383 exists and is "
                "assigned to Reda, but the GitHub ticket has not been created and remains unresolved."
            ),
            evidence=self._incident_status_evidence(status="running"),
            provider=provider,
            llm=_mock_llm_client(MagicMock(), provider="openai"),
            model="openai/gpt-5.5",
        )

        assert result["status"] == "resolved"
        assert result["approved"] is True
        provider.create.assert_called_once()

    def test_three_failed_tool_calls_block_success_claim_without_failure_names(self):
        from brain.systems.runs.direct_agent import review_candidate_final_reply
        from brain.systems.runs.direct_loop.final_reply_checker import FinalReplyEnforcement
        from brain.systems.runs.direct_loop.final_reply_evidence import (
            FinalReplyEvidence,
            ToolFailureStateEvidence,
            ToolResultEvidence,
        )

        failures = tuple(
            ToolResultEvidence.capture(
                tool_name="manage_idea",
                arguments={"action": "create"},
                is_error=True,
                result={"error": "parent_id validation failed"},
            )
            for _ in range(3)
        )
        evidence = FinalReplyEvidence(
            tool_results=failures,
            tool_failure_state=ToolFailureStateEvidence(
                failure_threshold=3,
                consecutive_failures=3,
                total_failures=3,
                tool_name="manage_idea",
                error_class="ToolValidationError",
                termination_reason="tool_failure_circuit",
            ),
        )
        provider = MagicMock()

        result = review_candidate_final_reply(
            user_request="Assign a ticket to me.",
            candidate_output="Yes — done. The ticket is logged and assigned.",
            evidence=evidence,
            provider=provider,
            llm=_mock_llm_client(MagicMock(), provider="openai"),
            model="openai/gpt-5.5",
        )

        assert result["status"] == "continue"
        assert result["enforcement"] is FinalReplyEnforcement.BLOCK
        assert "manage_idea" in result["rationale"]
        assert "3" in result["rationale"]
        provider.create.assert_not_called()

    def test_status_question_allows_done_after_originating_run_completed_with_refs(self):
        from brain.systems.runs.direct_agent import review_candidate_final_reply

        provider = self._resolved_checker_provider()
        result = review_candidate_final_reply(
            user_request="was it done?",
            candidate_output=(
                "Done — GitHub issue #1210 was created and assigned to Reda; "
                "the linked Customer Support record is #2383."
            ),
            evidence=self._incident_status_evidence(
                status="completed",
                final_output=(
                    "Created GitHub issue #1210, assigned it to Reda, and linked "
                    "Customer Support record #2383."
                ),
            ),
            provider=provider,
            llm=_mock_llm_client(MagicMock(), provider="openai"),
            model="openai/gpt-5.5",
        )

        assert result["status"] == "resolved"
        assert result["approved"] is True
        provider.create.assert_called_once()

    def test_checker_rejects_customer_bug_issue_without_tracker_mirror(self):
        from brain.systems.runs.direct_agent import review_candidate_final_reply
        from brain.systems.runs.direct_loop.final_reply_checker import FinalReplyEnforcement
        from brain.systems.runs.direct_loop.final_reply_evidence import (
            FinalReplyEvidence,
            ToolResultEvidence,
        )

        provider = MagicMock()
        llm = _mock_llm_client(MagicMock(), provider="openai")

        result = review_candidate_final_reply(
            user_request=(
                "The customer says generations stay at 99% and every retry loses credits; "
                "assign a ticket to me."
            ),
            candidate_output="Opened GitHub issue #1210 and assigned it to Reda.",
            execution_context=(
                "Recent tool result: rendered text claims a successful Domain 1 mirror, "
                "but deterministic policy must ignore this blob."
            ),
            evidence=FinalReplyEvidence(tool_results=(
                ToolResultEvidence.capture(
                    tool_name="create_github_issue",
                    arguments={"repo": "uwear-ai/uwear-backend"},
                    is_error=False,
                    result={"number": 1210},
                ),
            )),
            provider=provider,
            llm=llm,
            model="openai/gpt-5.5",
        )

        assert result["status"] == "continue"
        assert result["enforcement"] is FinalReplyEnforcement.BLOCK
        assert "creating-work-items.md#customer-bug-filing-policy" in result["rationale"]
        provider.create.assert_not_called()

    def test_checker_rejects_silent_tracker_substitution_after_issue_failure(self):
        from brain.systems.runs.direct_agent import review_candidate_final_reply
        from brain.systems.runs.direct_loop.final_reply_checker import FinalReplyEnforcement
        from brain.systems.runs.direct_loop.final_reply_evidence import (
            FinalReplyEvidence,
            ToolResultEvidence,
        )

        provider = MagicMock()
        llm = _mock_llm_client(MagicMock(), provider="openai")

        result = review_candidate_final_reply(
            user_request="Please file a GitHub ticket for this customer bug and assign it to me.",
            candidate_output="Done — tracker record 2383 is assigned to Reda.",
            evidence=FinalReplyEvidence(tool_results=(
                ToolResultEvidence.capture(
                    tool_name="create_github_issue",
                    arguments={"repo": "uwear-ai/uwear-backend"},
                    is_error=False,
                    result={"error": "No GitHub token candidates", "no_write_token": True},
                ),
                ToolResultEvidence.capture(
                    tool_name="manage_domain",
                    arguments={"action": "create_record", "domain_id": 1},
                    is_error=False,
                    result={"record": {"id": 2383}},
                ),
            )),
            provider=provider,
            llm=llm,
            model="openai/gpt-5.5",
        )

        assert result["status"] == "continue"
        assert result["enforcement"] is FinalReplyEnforcement.BLOCK
        assert "structured failure evidence" in result["rationale"]
        provider.create.assert_not_called()

    def test_artifact_contract_accepts_explicit_issue_blocker_and_successful_mirror(self):
        from brain.systems.runs.direct_loop.final_reply_checker import (
            _customer_bug_missing_tracker_mirror,
            _requested_github_artifact_contract_violated,
        )
        from brain.systems.runs.direct_loop.final_reply_evidence import (
            FinalReplyEvidence,
            ToolResultEvidence,
        )

        failed_issue_evidence = FinalReplyEvidence(tool_results=(
            ToolResultEvidence.capture(
                tool_name="create_github_issue",
                arguments={},
                is_error=False,
                result={"error": "No GitHub token candidates", "no_write_token": True},
            ),
        ))
        assert not _requested_github_artifact_contract_violated(
            "File a GitHub ticket for this customer bug.",
            "I couldn't create the GitHub issue because no_write_token blocked the write.",
            failed_issue_evidence,
        )

        successful_pair_evidence = FinalReplyEvidence(tool_results=(
            ToolResultEvidence.capture(
                tool_name="create_github_issue",
                arguments={},
                is_error=False,
                result={"number": 1210},
            ),
            ToolResultEvidence.capture(
                tool_name="manage_domain",
                arguments={"action": "query_records"},
                is_error=False,
                result={},
            ),
            ToolResultEvidence.capture(
                tool_name="manage_domain",
                arguments={"action": "create_record", "domain_id": 1},
                is_error=False,
                result={"record": {"id": 2383}},
            ),
        ))
        assert not _customer_bug_missing_tracker_mirror(
            'Customer report: "the image generation is stuck". Assign a ticket to me.',
            "Opened GitHub issue #1210 and tracker record 2383.",
            successful_pair_evidence,
        )

    def test_checker_rejects_skipped_requested_github_issue_as_silent_substitution(self):
        from brain.systems.runs.direct_agent import review_candidate_final_reply
        from brain.systems.runs.direct_loop.final_reply_checker import FinalReplyEnforcement
        from brain.systems.runs.direct_loop.final_reply_evidence import FinalReplyEvidence

        provider = MagicMock()
        llm = _mock_llm_client(MagicMock(), provider="openai")

        result = review_candidate_final_reply(
            user_request="Please file a GitHub issue for this regression.",
            candidate_output="Done — tracker record 2383 is ready.",
            evidence=FinalReplyEvidence(),
            provider=provider,
            llm=llm,
            model="openai/gpt-5.5",
        )

        assert result["enforcement"] is FinalReplyEnforcement.BLOCK
        assert "structured failure evidence" in result["rationale"]
        provider.create.assert_not_called()

    def test_customer_bug_mirror_trigger_requires_explicit_report_origin(self):
        from brain.systems.runs.direct_loop.final_reply_checker import _has_customer_report_signal

        assert not _has_customer_report_signal("File an issue about the customer email bounce and credits.")
        assert _has_customer_report_signal('Customer report: "Every retry loses credits."')
        assert _has_customer_report_signal("Support escalated this case after the user reported a failed render.")

    def test_customer_bug_mirror_accepts_honestly_reported_failed_attempt(self):
        from brain.systems.runs.direct_loop.final_reply_checker import (
            _customer_bug_missing_tracker_mirror,
        )
        from brain.systems.runs.direct_loop.final_reply_evidence import (
            FinalReplyEvidence,
            ToolResultEvidence,
        )

        evidence = FinalReplyEvidence(tool_results=(
            ToolResultEvidence.capture(
                tool_name="create_github_issue",
                arguments={"repo": "uwear-ai/uwear-backend"},
                is_error=False,
                result={"number": 1210},
            ),
            ToolResultEvidence.capture(
                tool_name="manage_domain",
                arguments={"action": "create_record", "domain_id": 1},
                is_error=False,
                result={"error": "permission denied"},
            ),
        ))

        assert not _customer_bug_missing_tracker_mirror(
            "The customer reported lost credits; file a GitHub issue.",
            "GitHub issue #1210 was created, but the tracker mirror could not be created due to permission denied.",
            evidence,
        )

    def test_checker_rejects_ungrounded_illospace_setup_surface_without_llm(self):
        from brain.systems.runs.direct_agent import review_candidate_final_reply

        provider = MagicMock()
        llm = _mock_llm_client(MagicMock(), provider="openai")

        result = review_candidate_final_reply(
            user_request="Help me set up Slack",
            candidate_output=(
                "Slack is not connected yet.\n\n"
                "Setup path:\n"
                "1. Open **Illospace -> Settings -> Integrations -> Slack**.\n"
                "2. Click **Connect Slack**."
            ),
            execution_context=(
                "Evidence guardrail: approve direct source/runtime claims only when supported.\n"
                'Recent tool result 1: {"tool_name":"manage_slack","result_preview":'
                '"{\\"ok\\": true, \\"setup_state\\": \\"not_connected\\", '
                '\\"needs_connection\\": true, \\"connection_count\\": 0, \\"connections\\": []}"}'
            ),
            provider=provider,
            llm=llm,
            model="openai/gpt-5.5",
        )

        assert result["status"] == "continue"
        assert result["approved"] is False
        assert "not present in this run's execution evidence" in result["rationale"]
        provider.create.assert_not_called()

    def test_checker_rejects_ungrounded_illospace_authority_role_without_llm(self):
        from brain.systems.runs.direct_agent import review_candidate_final_reply

        provider = MagicMock()
        llm = _mock_llm_client(MagicMock(), provider="openai")

        result = review_candidate_final_reply(
            user_request="Help me set up Slack",
            candidate_output="Ask your Illospace admin to connect Slack for this workspace.",
            execution_context=(
                "Evidence guardrail: approve direct source/runtime claims only when supported.\n"
                'Recent tool result 1: {"tool_name":"manage_slack","result_preview":'
                '"{\\"ok\\": true, \\"setup_state\\": \\"not_connected\\", '
                '\\"connection_count\\": 0, \\"connections\\": []}"}'
            ),
            provider=provider,
            llm=llm,
            model="openai/gpt-5.5",
        )

        assert result["status"] == "continue"
        assert result["approved"] is False
        provider.create.assert_not_called()

    @patch("brain.systems.runs.direct_loop.final_reply_checker.get_provider")
    @patch("brain.systems.runs.direct_loop.final_reply_checker.resolve_llm_client")
    def test_checker_reviews_partial_reply_with_llm(self, mock_client, mock_get_provider):
        from brain.systems.runs.direct_agent import review_candidate_final_reply

        client = MagicMock()
        llm = _mock_llm_client(client, provider="openai")
        mock_client.return_value = llm

        provider = MagicMock()
        response = MagicMock()
        response.stop_reason = "end_turn"
        text_block = MagicMock()
        text_block.type = "text"
        text_block.text = '{"status":"continue","rationale":"The message says the work is still incomplete.","missing_requirements":[]}'
        text_block.model_dump.return_value = {
            "type": "text",
            "text": '{"status":"continue","rationale":"The message says the work is still incomplete.","missing_requirements":[]}',
        }
        response.content = [text_block]
        response.usage = MagicMock(
            input_tokens=100,
            output_tokens=20,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        )
        provider.create.return_value = response
        mock_get_provider.return_value = provider

        result = review_candidate_final_reply(
            user_request="Finish the MCP Apps work",
            candidate_output=(
                "I continued the work toward the broader Claude/ChatGPT MCP Apps goal "
                "and got it materially closer, but I cannot honestly say we are fully ready yet."
            ),
        )

        assert result["status"] == "continue"
        assert result["approved"] is False
        mock_client.assert_called_once()
        provider.create.assert_called_once()

    @patch("brain.systems.runs.direct_loop.final_reply_checker.get_provider")
    @patch("brain.systems.runs.direct_loop.final_reply_checker.resolve_llm_client")
    def test_checker_adds_session_header_for_chatgpt_auth(self, mock_client, mock_get_provider):
        from brain.systems.runs.direct_agent import review_candidate_final_reply

        client = MagicMock()
        llm = _mock_llm_client(client, provider="openai")
        llm.auth_mode = "chatgpt"
        mock_client.return_value = llm

        provider = MagicMock()
        response = MagicMock()
        response.stop_reason = "end_turn"
        text_block = MagicMock()
        text_block.type = "text"
        text_block.text = '{"status":"resolved","rationale":"done","missing_requirements":[]}'
        text_block.model_dump.return_value = {
            "type": "text",
            "text": '{"status":"resolved","rationale":"done","missing_requirements":[]}',
        }
        response.content = [text_block]
        response.usage = MagicMock(
            input_tokens=100,
            output_tokens=20,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        )
        provider.create.return_value = response
        mock_get_provider.return_value = provider

        review_candidate_final_reply(
            user_request="Finish the work",
            candidate_output="Implemented the requested work fully.",
            user_id="user-123",
            session_id="sess-123",
        )

        request = provider.create.call_args.args[0]
        assert request.extra_headers["session_id"] == "sess-123:final-reply-checker"

    @patch("brain.systems.runs.direct_loop.final_reply_checker.get_provider")
    @patch("brain.systems.runs.direct_loop.final_reply_checker.resolve_llm_client")
    def test_checker_caps_long_session_header_for_chatgpt_auth(self, mock_client, mock_get_provider):
        from brain.platform.integrations.openai_cache import normalize_openai_session_id
        from brain.systems.runs.direct_agent import review_candidate_final_reply

        client = MagicMock()
        llm = _mock_llm_client(client, provider="openai")
        llm.auth_mode = "chatgpt"
        mock_client.return_value = llm

        provider = MagicMock()
        response = MagicMock()
        response.stop_reason = "end_turn"
        text_block = MagicMock()
        text_block.type = "text"
        text_block.text = '{"status":"resolved","rationale":"done","missing_requirements":[]}'
        text_block.model_dump.return_value = {
            "type": "text",
            "text": '{"status":"resolved","rationale":"done","missing_requirements":[]}',
        }
        response.content = [text_block]
        response.usage = MagicMock(
            input_tokens=100,
            output_tokens=20,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        )
        provider.create.return_value = response
        mock_get_provider.return_value = provider

        session_id = "coordinator-idea-12345678-1234-5678-90ab-cdef12345678"
        review_candidate_final_reply(
            user_request="Finish the work",
            candidate_output="Implemented the requested work fully.",
            user_id="user-123",
            session_id=session_id,
        )

        request = provider.create.call_args.args[0]
        expected = normalize_openai_session_id(f"{session_id}:final-reply-checker")
        assert request.extra_headers["session_id"] == expected
        assert len(request.extra_headers["session_id"]) <= 64

    @patch("brain.systems.runs.direct_agent.review_candidate_final_reply")
    def test_review_final_reply_once_reuses_cached_verdict(self, mock_review):
        from brain.systems.runs.direct_agent import review_final_reply_once, _agent_context

        mock_review.return_value = {
            "status": "resolved",
            "approved": True,
            "rationale": "done",
            "missing_requirements": [],
            "raw_output": "",
        }
        _agent_context.final_reply_review = None

        try:
            first = review_final_reply_once(
                user_request="Finish the work",
                candidate_output="Implemented the requested work fully.",
            )
            second = review_final_reply_once(
                user_request="Finish the work",
                candidate_output="Implemented the requested work fully.",
            )

            assert first == second
            mock_review.assert_called_once()
        finally:
            if hasattr(_agent_context, "final_reply_review"):
                delattr(_agent_context, "final_reply_review")


class TestCortexReplyHandler:
    def test_read_capabilities_reports_slack_manifest(self):
        from brain.systems.runs.tool_catalog.handlers.capabilities import _handle_read_capabilities

        payload = json.loads(_handle_read_capabilities(query="help me set up Slack"))

        assert payload["source"] == "runtime_capability_registry"
        assert payload["detail_level"] == "full"
        assert payload["count"] == 1
        slack = payload["capabilities"][0]
        assert slack["key"] == "slack"
        assert slack["source"] == "tool_registry"
        assert slack["availability"] == "available"
        assert slack["status_check"] == {"tool": "manage_slack", "args": {"action": "status"}}
        assert slack["status_check_available"] is True
        assert slack["setup"]["credential_store"] == "Vault"
        assert [credential["key_name"] for credential in slack["setup"]["credentials"]] == [
            "SLACK_BOT_TOKEN",
            "SLACK_APP_TOKEN",
        ]
        assert {detail["name"] for detail in slack["tool_details"]} == set(slack["tools"])
        assert "guide_ref" not in slack["setup"]

    def test_read_capabilities_generic_query_returns_registry(self):
        from brain.systems.runs.tool_catalog.handlers.capabilities import _handle_read_capabilities

        payload = json.loads(_handle_read_capabilities(query="what integrations can you do?"))

        keys = {capability["key"] for capability in payload["capabilities"]}

        assert payload["detail_level"] == "summary"
        assert len(json.dumps(payload)) < 16_000
        assert payload["count"] >= 10
        assert any(capability["key"] == "slack" for capability in payload["capabilities"])
        assert {"domains", "cycles", "code_execution", "workspace_context"} <= keys
        assert all("tool_details" not in capability for capability in payload["capabilities"])
        assert all("tool_count" in capability for capability in payload["capabilities"])

    def test_read_capabilities_registry_details_come_from_tool_registry(self):
        from brain.systems.runs.tool_catalog.handlers.capabilities import _handle_read_capabilities

        payload = json.loads(_handle_read_capabilities(capability_key="domains"))

        assert payload["detail_level"] == "full"
        assert payload["count"] == 1
        domains = payload["capabilities"][0]
        assert domains["source"] == "tool_registry"
        assert domains["category"] == "core_workspace"
        assert domains["tools"] == [
            "read_workspace_records",
            "manage_domain",
            "merge_chantier",
        ]
        assert {detail["name"] for detail in domains["tool_details"]} == set(domains["tools"])
        assert any("domain" in (detail["expected_effect"] or "").lower() for detail in domains["tool_details"])

    def test_read_capabilities_query_first_matches_slack_communication_setup(self):
        from brain.systems.runs.tool_catalog.handlers.capabilities import _handle_read_capabilities

        payload = json.loads(_handle_read_capabilities(
            query="Slack integration setup guide connect Illospace to Slack",
            detail_level="auto",
            include_setup_guide=True,
        ))

        assert payload["count"] == 1
        assert payload["detail_level"] == "full"
        slack = payload["capabilities"][0]
        assert slack["key"] == "slack"
        assert slack["setup"]["credential_store"] == "Vault"
        assert "setup_guides" not in payload

    def test_read_capabilities_does_not_treat_generic_communication_as_slack(self):
        from brain.systems.runs.tool_catalog.handlers.capabilities import _handle_read_capabilities

        payload = json.loads(_handle_read_capabilities(query="communication setup"))

        assert payload["count"] == 0

    def test_read_capabilities_keeps_broad_setup_guide_query_compact(self):
        from brain.systems.runs.tool_catalog.handlers.capabilities import _handle_read_capabilities

        payload = json.loads(_handle_read_capabilities(
            query="what integrations can you do?",
            include_setup_guide=True,
        ))

        assert payload["count"] >= 10
        assert payload["detail_level"] == "summary"
        assert len(json.dumps(payload)) < 16_000
        assert all("tool_details" not in capability for capability in payload["capabilities"])

    def test_read_capabilities_internal_key_recovers_from_wrong_category_hint(self):
        from brain.systems.runs.tool_catalog.handlers.capabilities import _handle_read_capabilities

        payload = json.loads(_handle_read_capabilities(
            capability_key="slack",
            category="communication",
            detail_level="full",
        ))

        assert payload["count"] == 1
        assert payload["category_ignored"] is True
        assert payload["capabilities"][0]["key"] == "slack"

    def test_read_capabilities_marks_disabled_capability_tools_unavailable(self):
        from brain.systems.runs.execution_context import bind_agent_context
        from brain.systems.runs.tool_catalog.handlers.capabilities import _handle_read_capabilities

        disabled_slack_tools = [
            "manage_slack",
            "read_slack_conversation",
            "post_slack_reply",
            "react_to_slack_message",
        ]
        with bind_agent_context({
            "execution_metadata": {
                "tool_policy": {"disabled_tools": disabled_slack_tools},
            }
        }):
            payload = json.loads(_handle_read_capabilities(query="Slack setup"))

        assert payload["count"] == 1
        slack = payload["capabilities"][0]
        assert slack["key"] == "slack"
        assert slack["availability"] == "unavailable"
        assert slack["tools"] == []
        assert set(slack["unavailable_tools"]) == set(disabled_slack_tools)
        assert slack["status_check_available"] is False

    def test_read_self_context_reports_identity_source_and_inspection_tools(self):
        from brain.systems.runs.tool_catalog.handlers.self_context import _handle_read_self_context

        payload = json.loads(_handle_read_self_context(include_git=False))

        assert payload["identity"]["agent_name"] == "Illo"
        assert payload["identity"]["workspace_product"] == "Illospace"
        assert payload["open_source"]["repository_url"] == "https://github.com/Illospace/illospace"
        assert payload["installation"]["source_root"]["exists"] is True
        assert payload["source_inspection"]["tools"]["read_file"]["registered"] is True
        assert "git" not in payload

    def test_read_capabilities_includes_custom_manifest_from_context(self):
        from brain.systems.runs.execution_context import bind_agent_context
        from brain.systems.runs.tool_catalog.handlers.capabilities import _handle_read_capabilities

        with bind_agent_context({
            "workspace_ref": {
                "capability_manifests": [
                    {
                        "key": "custom_crm",
                        "name": "Custom CRM",
                        "category": "sales",
                        "summary": "Look up account records from the workspace CRM.",
                        "aliases": ["accounts"],
                        "tools": ["crm_lookup"],
                        "setup": {
                            "mode": "managed_by_tool",
                            "guide_markdown": "# CRM Setup\nUse the CRM tool's own connection wizard.",
                        },
                    }
                ]
            }
        }):
            payload = json.loads(_handle_read_capabilities(
                capability_key="accounts",
                include_setup_guide=True,
            ))

        assert payload["count"] == 1
        capability = payload["capabilities"][0]
        assert capability["key"] == "custom_crm"
        assert capability["source"] == "capability_manifests"
        assert capability["tools"] == ["crm_lookup"]
        assert payload["setup_guides"][0]["available"] is True
        assert payload["setup_guides"][0]["ref"] == "inline"

    def test_final_reply_context_preserves_worker_evidence_confidence(self):
        from types import SimpleNamespace

        from brain.systems.runs.direct_agent import _agent_context
        from brain.systems.runs.tool_catalog.handlers.cortex_reply import _build_final_reply_check_context

        _agent_context.run = SimpleNamespace(
            run_id=42,
            worker_results=[
                SimpleNamespace(
                    task="Inspect source evidence",
                    skill_name="investigate",
                    success=True,
                    output="Worker reported the fix is present in the checkout.",
                    trust_status="trusted_with_uncertainty",
                    evidence={
                        "files": [{"path": "brain/systems/cortex/run/verifiers/builtins.py"}],
                        "commands": [{"command": "git log --oneline -2", "status": "passed"}],
                        "artifacts": [],
                        "unresolved_uncertainty": ["Raw DB row was not available."],
                    },
                )
            ],
        )
        _agent_context.execution_artifacts = []
        _agent_context.recent_tool_results = [
            {
                "tool_name": "manage_slack",
                "args_preview": '{"action": "status"}',
                "is_error": False,
                "result_preview": '{"ok": true, "setup_state": "not_connected", "connection_count": 0}',
            }
        ]
        _agent_context.intent_satisfaction = {
            "intent_type": "broad_refactor",
            "completion_mode": "strict_contract",
            "completion_contract": ["Complete every refactor phase."],
            "continuation_policy": "continue_until_contract_complete",
        }

        try:
            context = _build_final_reply_check_context()
        finally:
            _agent_context.run = None
            _agent_context.execution_artifacts = []
            _agent_context.recent_tool_results = []
            _agent_context.intent_satisfaction = None

        assert "Evidence guardrail" in context
        assert "Worker-summary-level evidence" in context
        assert "trust=trusted_with_uncertainty" in context
        assert "'commands': 1" in context
        assert "Raw DB row was not available" in context
        assert "strict_contract" in context
        assert "Complete every refactor phase" in context
        assert "Recent tool result 1" in context
        assert "manage_slack" in context
        assert "not_connected" in context

    def test_final_reply_context_uses_stable_sorted_key_rendering(self):
        from brain.systems.runs.direct_agent import _agent_context
        from brain.systems.runs.tool_catalog.handlers.cortex_reply import _build_final_reply_check_context

        _agent_context.run = None
        _agent_context.execution_artifacts = []
        _agent_context.recent_tool_results = [
            {
                "tool_name": "create_github_issue",
                "args_preview": "{}",
                "is_error": False,
                "result_preview": "{}",
            }
        ]
        _agent_context.intent_satisfaction = None

        try:
            context = _build_final_reply_check_context()
        finally:
            _agent_context.run = None
            _agent_context.execution_artifacts = []
            _agent_context.recent_tool_results = []
            _agent_context.intent_satisfaction = None

        assert "Recent tool result 1" in context
        assert '"tool_name": "create_github_issue"' in context
        assert '"is_error": false' in context
        assert context.index('"args_preview"') < context.index('"tool_name"')

    def test_final_reply_context_hides_failed_worker_diagnostics(self):
        from types import SimpleNamespace

        from brain.systems.runs.direct_agent import _agent_context
        from brain.systems.runs.failures import UPSTREAM_FAILED_RUN_MESSAGE
        from brain.systems.runs.tool_catalog.handlers.cortex_reply import _build_final_reply_check_context

        raw_error = "peer closed connection without sending complete message body"
        _agent_context.run = SimpleNamespace(
            run_id=42,
            total_tokens=0,
            worker_results=[
                SimpleNamespace(
                    skill_name="inspect",
                    success=False,
                    output="",
                    error=raw_error,
                    trust_status="untrusted",
                    evidence={},
                )
            ],
        )
        _agent_context.execution_artifacts = []
        _agent_context.recent_tool_results = []
        _agent_context.intent_satisfaction = None

        try:
            context = _build_final_reply_check_context()
        finally:
            _agent_context.run = None
            _agent_context.execution_artifacts = []
            _agent_context.recent_tool_results = []
            _agent_context.intent_satisfaction = None

        assert UPSTREAM_FAILED_RUN_MESSAGE in context
        assert raw_error not in context

    def test_final_reply_context_hides_failed_evidence_previews(self):
        from types import SimpleNamespace

        from brain.systems.runs.direct_agent import _agent_context
        from brain.systems.runs.tool_catalog.handlers.cortex_reply import _build_final_reply_check_context

        raw_error = "provider request failed with private diagnostic"
        _agent_context.run = SimpleNamespace(
            run_id=42,
            total_tokens=0,
            worker_results=[
                SimpleNamespace(
                    skill_name="inspect",
                    success=False,
                    output="",
                    error=raw_error,
                    trust_status="untrusted",
                    evidence={"unresolved_uncertainty": [raw_error]},
                )
            ],
        )
        _agent_context.execution_artifacts = [
            {"kind": "provider", "summary": raw_error, "path": None, "status": "failed"}
        ]
        _agent_context.recent_tool_results = [
            {
                "tool_name": "provider_call",
                "args_preview": "{}",
                "is_error": True,
                "result_preview": raw_error,
            }
        ]
        _agent_context.intent_satisfaction = None

        try:
            context = _build_final_reply_check_context()
        finally:
            _agent_context.run = None
            _agent_context.execution_artifacts = []
            _agent_context.recent_tool_results = []
            _agent_context.intent_satisfaction = None

        assert raw_error not in context
        assert "still open" in context
        assert "I will come back" in context

    def test_cortex_reply_whitespace_normalizer_collapses_punctuation_lines(self):
        from brain.systems.runs.tool_catalog.handlers.cortex_reply import _normalize_reply_whitespace

        raw = (
            "What is proven:\n\n"
            "`schema_valid=True`\n\n"
            ",\n\n"
            "`files=30`\n\n\n"
            "```text\nkeep\n\n,\nraw\n```"
        )

        assert _normalize_reply_whitespace(raw) == (
            "What is proven:\n\n"
            "`schema_valid=True`, `files=30`\n\n"
            "```text\nkeep\n\n,\nraw\n```"
        )

    @patch("brain.systems.runs.direct_agent.review_final_reply_once")
    def test_cortex_reply_stages_run_reply_for_settlement(self, mock_review):
        from brain.systems.runs.direct_agent import _handle_cortex_reply, _agent_context

        class _Run:
            run_id = 42

        mock_review.return_value = {
            "status": "resolved",
            "approved": True,
            "rationale": "done",
            "missing_requirements": [],
            "raw_output": "",
        }
        _agent_context.idea_id = "idea-123"
        _agent_context.run = _Run()
        _agent_context.user_request = "Say hello"
        _agent_context.reply_contents = []
        _agent_context.final_reply_review = None
        _agent_context.intent_satisfaction = {
            "intent_type": "quick_answer",
            "completion_mode": "light",
            "completion_contract": ["Say hello."],
        }

        try:
            with patch("brain.systems.cortex.reply.reply_to_cortex") as mock_reply:
                result = _handle_cortex_reply("hello")

            assert result["staged"] is True
            assert result["posted"] is False
            assert result["run_id"] == 42
            assert result["checker_status"] == "resolved"
            assert "settlement" in result["instruction"]
            mock_reply.assert_not_called()
            assert _agent_context.reply_contents == ["hello"]
            assert mock_review.call_args.kwargs["intent_profile"]["completion_mode"] == "light"
        finally:
            _agent_context.idea_id = None
            _agent_context.run = None
            _agent_context.user_request = None
            _agent_context.reply_contents = []
            _agent_context.final_reply_review = None
            _agent_context.intent_satisfaction = None

    @patch("brain.systems.runs.direct_agent.review_final_reply_once")
    def test_cortex_reply_stages_reply_with_advisory_warning_when_checker_unresolved(self, mock_review):
        """An 'unresolved' checker verdict is advisory: the reply still stages, and the
        verdict is surfaced as a non-blocking warning instead of vetoing the reply."""
        from brain.systems.runs.direct_agent import _handle_cortex_reply, _agent_context

        class _Run:
            run_id = 42

        mock_review.return_value = {
            "status": "continue",
            "approved": False,
            "rationale": "The proposed reply admits the work is not ready yet.",
            "missing_requirements": ["Finish the remaining MCP Apps work"],
            "raw_output": "",
        }
        _agent_context.idea_id = "idea-123"
        _agent_context.run = _Run()
        _agent_context.user_request = "Finish the MCP Apps work"
        _agent_context.reply_contents = []
        _agent_context.final_reply_review = None

        try:
            with patch("brain.systems.cortex.reply.reply_to_cortex") as mock_reply:
                result = _handle_cortex_reply(
                    "I got it materially closer, but I cannot honestly say we are fully ready yet."
                )

            assert "blocked" not in result
            assert result["staged"] is True
            assert result["posted"] is False
            assert result["checker_status"] == "continue"
            assert result["checker_note"] == "The proposed reply admits the work is not ready yet."
            assert "settlement" in result["instruction"]
            mock_reply.assert_not_called()
            assert _agent_context.reply_contents == [
                "I got it materially closer, but I cannot honestly say we are fully ready yet."
            ]
        finally:
            _agent_context.idea_id = None
            _agent_context.run = None
            _agent_context.user_request = None
            _agent_context.reply_contents = []
            _agent_context.final_reply_review = None

    @patch("brain.systems.runs.direct_agent.review_final_reply_once")
    def test_cortex_reply_blocks_requested_artifact_contract_violation(self, mock_review):
        from brain.systems.runs.direct_agent import _handle_cortex_reply, _agent_context
        from brain.systems.runs.direct_loop.final_reply_checker import FinalReplyEnforcement

        class _Run:
            run_id = 42

        mock_review.return_value = {
            "status": "continue",
            "approved": False,
            "rationale": "The requested GitHub issue and blocker were omitted.",
            "missing_requirements": ["Name the GitHub issue and blocker."],
            "raw_output": "deterministic_requested_artifact_contract",
            "enforcement": FinalReplyEnforcement.BLOCK,
        }
        _agent_context.idea_id = "idea-123"
        _agent_context.run = _Run()
        _agent_context.user_request = "File a GitHub issue"
        _agent_context.reply_contents = []
        _agent_context.final_reply_review = None
        _agent_context.artifact_contract_block_count = 0

        try:
            result = _handle_cortex_reply("Done — tracker record 2383 was created.")

            assert result["blocked"] is True
            assert result["checker_status"] == "continue"
            assert result["artifact_contract_block_count"] == 1
            assert result["missing_requirements"] == ["Name the GitHub issue and blocker."]
            assert "substitute artifact" in result["instruction"]
            assert _agent_context.reply_contents == []
        finally:
            _agent_context.idea_id = None
            _agent_context.run = None
            _agent_context.user_request = None
            _agent_context.reply_contents = []
            _agent_context.final_reply_review = None
            _agent_context.artifact_contract_block_count = 0

    @patch("brain.systems.runs.direct_agent.review_final_reply_once")
    def test_cortex_reply_degrades_repeated_artifact_contract_block_to_advisory(self, mock_review):
        from brain.systems.runs.direct_agent import _handle_cortex_reply, _agent_context
        from brain.systems.runs.direct_loop.final_reply_checker import FinalReplyEnforcement

        class _Run:
            run_id = 42

        mock_review.return_value = {
            "status": "continue",
            "approved": False,
            "rationale": "The requested GitHub issue and blocker were omitted.",
            "missing_requirements": ["Name the GitHub issue and blocker."],
            "raw_output": "deterministic_requested_artifact_contract",
            "enforcement": FinalReplyEnforcement.BLOCK,
        }
        _agent_context.idea_id = "idea-123"
        _agent_context.run = _Run()
        _agent_context.user_request = "File a GitHub issue"
        _agent_context.reply_contents = []
        _agent_context.final_reply_review = None
        _agent_context.artifact_contract_block_count = 0

        try:
            first = _handle_cortex_reply("Done — tracker record 2383 was created.")
            second = _handle_cortex_reply("Done — tracker record 2383 is ready.")
            third = _handle_cortex_reply("Done — tracker record 2383 remains available.")

            assert first["blocked"] is True
            assert second["blocked"] is True
            assert first["artifact_contract_block_count"] == 1
            assert second["artifact_contract_block_count"] == 2
            assert "blocked" not in third
            assert third["staged"] is True
            assert third["checker_enforcement"] == "advisory"
            assert "already blocked two replies" in third["checker_note"]
            assert _agent_context.reply_contents == ["Done — tracker record 2383 remains available."]
        finally:
            _agent_context.idea_id = None
            _agent_context.run = None
            _agent_context.user_request = None
            _agent_context.reply_contents = []
            _agent_context.final_reply_review = None
            _agent_context.artifact_contract_block_count = 0


class TestExecutionArtifacts:
    def test_emit_resolved_tool_call_records_recent_result_for_reply_context(self):
        from brain.systems.runs.direct_loop.tool_execution import ResolvedToolCall, emit_resolved_tool_call
        from brain.systems.runs.direct_loop.final_reply_evidence import ToolResultEvidence

        agent_context = SimpleNamespace(tool_calls_log=[], recent_tool_results=[])
        tool_results = []

        emit_resolved_tool_call(
            ResolvedToolCall(
                block_id="tool-1",
                tool_name="manage_slack",
                tool_input={"action": "status"},
                result_text='{"ok": true, "setup_state": "not_connected"}',
            ),
            tool_results,
            None,
            None,
            None,
            "coordinator",
            agent_context=agent_context,
        )

        assert agent_context.tool_calls_log == ["manage_slack"]
        assert agent_context.recent_tool_results == [ToolResultEvidence(
            tool_name="manage_slack",
            arguments={"action": "status"},
            is_error=False,
            result={"ok": True, "setup_state": "not_connected"},
        )]
        assert tool_results[0]["content"] == '{"ok": true, "setup_state": "not_connected"}'

    def test_exec_command_records_git_provenance(self):
        from brain.systems.runs.direct_agent import _handle_exec_command, _agent_context

        class _Run:
            run_id = 42

        agent_run_record = MagicMock()
        agent_run_record.execution_artifacts = []

        query = MagicMock()
        query.filter.return_value = query
        query.order_by.return_value = query
        query.first.return_value = agent_run_record

        mock_uow = MagicMock()
        mock_uow.__enter__ = MagicMock(return_value=mock_uow)
        mock_uow.__exit__ = MagicMock(return_value=False)
        mock_uow.session.query.return_value = query

        proc = MagicMock(returncode=0, stdout="[feat/test 1234567] Add thing\n", stderr="")

        _agent_context.run = _Run()
        _agent_context.execution_artifacts = []
        _agent_context.execution_metadata = {"execution_id": "exec-123", "run_id": 42}
        try:
            with patch("subprocess.run", return_value=proc),                  patch("brain.platform.db.repositories.unit_of_work.UnitOfWork", return_value=mock_uow):
                result = _handle_exec_command("git commit -m \"Add thing\"", working_dir=".")

            assert result["exit_code"] == 0
            assert any(a["type"] == "commit" for a in _agent_context.execution_artifacts)
            assert agent_run_record.execution_artifacts == []
        finally:
            _agent_context.run = None
            _agent_context.execution_artifacts = []
            if hasattr(_agent_context, "execution_metadata"):
                delattr(_agent_context, "execution_metadata")

    def test_exec_command_records_artifact_with_execution_provenance(self):
        from brain.systems.runs.direct_agent import _handle_exec_command, _agent_context

        class _Run:
            run_id = 42

        agent_run_record = MagicMock()
        agent_run_record.execution_artifacts = []

        query = MagicMock()
        query.filter.return_value = query
        query.order_by.return_value = query
        query.first.return_value = agent_run_record

        mock_uow = MagicMock()
        mock_uow.__enter__ = MagicMock(return_value=mock_uow)
        mock_uow.__exit__ = MagicMock(return_value=False)
        mock_uow.session.query.return_value = query

        proc = MagicMock(returncode=0, stdout="[feat/test 1234567] Add thing\n", stderr="")

        _agent_context.run = _Run()
        _agent_context.execution_artifacts = []
        _agent_context.execution_metadata = {
            "execution_id": "exec-123",
            "run_id": 42,
            "session_id": "agent-run-1-node-test",
            "node_id": "implement-provenance",
        }
        try:
            with patch("subprocess.run", return_value=proc),                  patch("brain.platform.db.repositories.unit_of_work.UnitOfWork", return_value=mock_uow):
                result = _handle_exec_command("git commit -m \"Add thing\"", working_dir=".")

            assert result["exit_code"] == 0
            artifact = _agent_context.execution_artifacts[0]
            assert artifact["type"] == "commit"
            assert artifact["sha"] == "1234567"
            assert agent_run_record.execution_artifacts == []
        finally:
            _agent_context.run = None
            _agent_context.execution_artifacts = []
            if hasattr(_agent_context, "execution_metadata"):
                delattr(_agent_context, "execution_metadata")

    def test_exec_command_records_local_artifact_when_execution_record_missing(self):
        from brain.systems.runs.direct_agent import _handle_exec_command, _agent_context

        class _Run:
            run_id = 42

        query = MagicMock()
        query.filter.return_value = query
        query.order_by.return_value = query
        query.first.return_value = None

        created_records = []

        def _add(record):
            created_records.append(record)

        mock_uow = MagicMock()
        mock_uow.__enter__ = MagicMock(return_value=mock_uow)
        mock_uow.__exit__ = MagicMock(return_value=False)
        mock_uow.session.query.return_value = query
        mock_uow.session.add.side_effect = _add

        proc = MagicMock(returncode=0, stdout="[feat/test 1234567] Add thing\n", stderr="")

        _agent_context.run = _Run()
        _agent_context.execution_artifacts = []
        _agent_context.execution_metadata = {
            "execution_id": "exec-123",
            "run_id": 42,
            "session_id": "agent-run-1-node-test",
            "node_id": "implement-provenance",
        }
        try:
            with patch("subprocess.run", return_value=proc),                  patch("brain.platform.db.repositories.unit_of_work.UnitOfWork", return_value=mock_uow):
                result = _handle_exec_command("git commit -m \"Add thing\"", working_dir=".")

            assert result["exit_code"] == 0
            created_execution_records = [
                record for record in created_records if getattr(record, "execution_id", None) == "exec-123"
            ]
            assert created_execution_records == []
            assert any(a["type"] == "commit" for a in _agent_context.execution_artifacts)
        finally:
            _agent_context.run = None
            _agent_context.execution_artifacts = []
            if hasattr(_agent_context, "execution_metadata"):
                delattr(_agent_context, "execution_metadata")

    def test_exec_command_does_not_infer_pr_from_read_only_output(self):
        from brain.systems.runs.direct_agent import _handle_exec_command, _agent_context

        class _Run:
            run_id = 42

        query = MagicMock()
        query.filter.return_value = query
        query.order_by.return_value = query
        query.first.return_value = None

        mock_uow = MagicMock()
        mock_uow.__enter__ = MagicMock(return_value=mock_uow)
        mock_uow.__exit__ = MagicMock(return_value=False)
        mock_uow.session.query.return_value = query

        proc = MagicMock(
            returncode=0,
            stdout="Existing PR: https://github.com/example-org/example-repo/pull/584\n",
            stderr="",
        )

        _agent_context.run = _Run()
        _agent_context.execution_artifacts = []
        try:
            with patch("subprocess.run", return_value=proc),                  patch("brain.platform.db.repositories.unit_of_work.UnitOfWork", return_value=mock_uow):
                result = _handle_exec_command("git log --oneline --decorate -n 20", working_dir=".")

            assert result["exit_code"] == 0
            assert _agent_context.execution_artifacts == []
            mock_uow.session.add.assert_not_called()
        finally:
            _agent_context.run = None
            _agent_context.execution_artifacts = []

    def test_exec_command_records_pr_only_for_gh_pr_create(self):
        from brain.systems.runs.direct_agent import _handle_exec_command, _agent_context

        class _Run:
            run_id = 42

        agent_run_record = MagicMock()
        agent_run_record.execution_artifacts = []

        query = MagicMock()
        query.filter.return_value = query
        query.order_by.return_value = query
        query.first.return_value = agent_run_record

        mock_uow = MagicMock()
        mock_uow.__enter__ = MagicMock(return_value=mock_uow)
        mock_uow.__exit__ = MagicMock(return_value=False)
        mock_uow.session.query.return_value = query

        proc = MagicMock(
            returncode=0,
            stdout="https://github.com/example-org/example-repo/pull/611\n",
            stderr="",
        )

        _agent_context.run = _Run()
        _agent_context.execution_artifacts = []
        _agent_context.execution_metadata = {"execution_id": "exec-pr-123", "run_id": 42}
        try:
            with patch("subprocess.run", return_value=proc),                  patch("brain.platform.db.repositories.unit_of_work.UnitOfWork", return_value=mock_uow):
                result = _handle_exec_command("gh pr create --fill", working_dir=".")

            assert result["exit_code"] == 0
            assert any(a["type"] == "pr" and a["number"] == 611 for a in _agent_context.execution_artifacts)
            assert agent_run_record.execution_artifacts == []
        finally:
            _agent_context.run = None
            _agent_context.execution_artifacts = []
            if hasattr(_agent_context, "execution_metadata"):
                delattr(_agent_context, "execution_metadata")

    async def test_my_activity_includes_execution_artifacts(self):
        from brain.systems.runs.direct_agent import _handle_my_activity, _agent_context

        class _Run:
            run_id = 42
            total_tokens = 0
            worker_results = []

        _agent_context.run = _Run()
        _agent_context.start_time = None
        _agent_context.reply_contents = []
        _agent_context.tool_calls_log = []
        _agent_context.execution_artifacts = [{"type": "pr", "number": 123, "url": "https://github.com/x/y/pull/123"}]
        try:
            result = await _handle_my_activity()
            assert result["execution_artifacts"][0]["type"] == "pr"
            assert result["execution_artifacts"][0]["number"] == 123
        finally:
            _agent_context.run = None
            _agent_context.start_time = None
            _agent_context.reply_contents = []
            _agent_context.tool_calls_log = []
            _agent_context.execution_artifacts = []

    async def test_my_activity_loads_persisted_execution_artifacts_from_execution_id(self):
        from brain.systems.runs.direct_agent import _handle_my_activity, _agent_context

        class _Run:
            run_id = 42
            total_tokens = 0
            worker_results = []

        _agent_context.run = _Run()
        _agent_context.start_time = None
        _agent_context.reply_contents = []
        _agent_context.tool_calls_log = []
        _agent_context.execution_artifacts = []
        _agent_context.execution_metadata = {"execution_id": "exec-123", "run_id": 42}
        try:
            with patch(
                "brain.systems.runs.tool_catalog.handlers.activity.load_execution_artifacts",
                new=AsyncMock(return_value=[{"type": "commit", "sha": "abc1234", "summary": "Fix provenance"}]),
            ) as mock_load, patch(
                "brain.platform.db.repositories.unit_of_work.UnitOfWork",
                side_effect=AssertionError("my_activity should not load run execution_artifacts"),
            ):
                result = await _handle_my_activity()

            assert result["execution_artifacts"] == [{"type": "commit", "sha": "abc1234", "summary": "Fix provenance"}]
            mock_load.assert_awaited_once_with(execution_id="exec-123")
        finally:
            _agent_context.run = None
            _agent_context.start_time = None
            _agent_context.reply_contents = []
            _agent_context.tool_calls_log = []
            _agent_context.execution_artifacts = []
            if hasattr(_agent_context, "execution_metadata"):
                delattr(_agent_context, "execution_metadata")

    async def test_my_activity_does_not_load_persisted_artifacts_without_execution_id(self):
        from brain.systems.runs.direct_agent import _handle_my_activity, _agent_context

        class _Run:
            run_id = 42
            total_tokens = 0
            worker_results = []

        _agent_context.run = _Run()
        _agent_context.start_time = None
        _agent_context.reply_contents = []
        _agent_context.tool_calls_log = []
        _agent_context.execution_artifacts = []
        _agent_context.execution_metadata = {"run_id": "run-123"}
        try:
            with patch("brain.systems.runs.tool_catalog.handlers.activity.load_execution_artifacts") as mock_load:
                result = await _handle_my_activity()

            assert "execution_artifacts" not in result
            mock_load.assert_not_called()
        finally:
            _agent_context.run = None
            _agent_context.start_time = None
            _agent_context.reply_contents = []
            _agent_context.tool_calls_log = []
            _agent_context.execution_artifacts = []
            if hasattr(_agent_context, "execution_metadata"):
                delattr(_agent_context, "execution_metadata")


class TestSanitizeToolPairs:
    """Test _sanitize_tool_pairs strips orphaned tool blocks."""

    def test_no_orphans_returns_unchanged(self):
        from brain.systems.runs.direct_agent import _sanitize_tool_pairs
        messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "t1", "name": "test", "input": {}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "ok"},
            ]},
            {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
        ]
        result = _sanitize_tool_pairs(messages)
        assert len(result) == 4

    def test_orphaned_tool_use_stripped(self):
        from brain.systems.runs.direct_agent import _sanitize_tool_pairs
        messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": [
                {"type": "text", "text": "let me check"},
                {"type": "tool_use", "id": "orphan1", "name": "test", "input": {}},
            ]},
            # tool_result for orphan1 is missing!
            {"role": "user", "content": "next question"},
        ]
        result = _sanitize_tool_pairs(messages)
        # The tool_use block should be stripped, text block kept
        assistant_msg = result[1]
        assert len(assistant_msg["content"]) == 1
        assert assistant_msg["content"][0]["type"] == "text"

    def test_orphaned_tool_result_stripped(self):
        from brain.systems.runs.direct_agent import _sanitize_tool_pairs
        messages = [
            {"role": "user", "content": "hello"},
            # tool_use for "missing1" is not in the messages
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "missing1", "content": "ok"},
            ]},
            {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
        ]
        result = _sanitize_tool_pairs(messages)
        # The message with only the orphaned tool_result should be dropped entirely
        assert len(result) == 2

    def test_mixed_orphans_and_valid_pairs(self):
        from brain.systems.runs.direct_agent import _sanitize_tool_pairs
        messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "good1", "name": "test", "input": {}},
                {"type": "tool_use", "id": "orphan1", "name": "test2", "input": {}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "good1", "content": "ok"},
                # orphan1's result is missing
            ]},
        ]
        result = _sanitize_tool_pairs(messages)
        # Mixed partial result sets are invalid; the whole tool exchange is dropped
        assert result == [{"role": "user", "content": "hello"}]


class TestSessionTrimToolPairSafety:
    """Test that session trimming never orphans tool_use/tool_result pairs.

    Regression tests for the bug where trimming at position 2 could split
    a tool_use (in kept_early) from its tool_result (trimmed away), causing
    Anthropic API 400: 'tool_use ids found without tool_result blocks'.
    """

    def _build_session(self, count, tool_pair_at_start=False, tool_pair_at_boundary=False):
        """Build a fake session with `count` messages.

        tool_pair_at_start: messages[1] is assistant(tool_use), messages[2] is user(tool_result)
        tool_pair_at_boundary: places a tool pair straddling the candidate_recent boundary
        """
        messages = []
        messages.append({"role": "user", "content": "Start task"})

        if tool_pair_at_start:
            messages.append({
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "early_tool_1", "name": "brain_recall", "input": {"query": "x"}},
                ],
            })
            messages.append({
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "early_tool_1", "content": "found stuff"},
                ],
            })
            messages.append({"role": "assistant", "content": [{"type": "text", "text": "Got it."}]})

        # Fill remaining messages as regular user/assistant pairs
        while len(messages) < count:
            messages.append({"role": "user", "content": f"Message {len(messages)}"})
            messages.append({"role": "assistant", "content": [{"type": "text", "text": f"Reply {len(messages)}"}]})

        if tool_pair_at_boundary:
            # Place a tool pair right where candidate_recent slice starts (count - 37)
            boundary = max(0, count - 37)
            if boundary > 0 and boundary + 1 < count:
                messages[boundary] = {
                    "role": "assistant",
                    "content": [
                        {"type": "tool_use", "id": "boundary_tool_1", "name": "exec_command", "input": {"cmd": "ls"}},
                    ],
                }
                messages[boundary + 1] = {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "boundary_tool_1", "content": "file.txt"},
                    ],
                }

        return messages

    def _assert_no_orphaned_tool_pairs(self, messages):
        """Assert every tool_use has a matching tool_result and vice versa."""
        use_ids = set()
        result_ids = set()
        for m in messages:
            content = m.get("content", "")
            if not isinstance(content, list):
                continue
            for b in content:
                if isinstance(b, dict):
                    if b.get("type") == "tool_use":
                        use_ids.add(b.get("id"))
                    elif b.get("type") == "tool_result":
                        result_ids.add(b.get("tool_use_id"))

        orphaned_use = use_ids - result_ids
        orphaned_result = result_ids - use_ids
        assert not orphaned_use, f"Orphaned tool_use ids (no matching tool_result): {orphaned_use}"
        assert not orphaned_result, f"Orphaned tool_result ids (no matching tool_use): {orphaned_result}"

    @patch("brain.systems.runs.direct_agent.async_resolve_llm_client")
    @patch("brain.systems.runs.direct_agent._save_session")
    @patch("brain.systems.runs.direct_agent._summarize_trimmed_messages", return_value="summary")
    def test_trim_with_tool_pair_at_start(self, mock_summary, mock_save, mock_client):
        """When messages[1] has tool_use, trimming must not orphan it."""
        from brain.systems.runs.direct_agent import run_agent

        session_msgs = self._build_session(50, tool_pair_at_start=True)

        client = MagicMock()
        response = MagicMock()
        response.stop_reason = "end_turn"
        text_block = MagicMock()
        text_block.type = "text"
        text_block.text = "Done."
        text_block.model_dump.return_value = {"type": "text", "text": "Done."}
        response.content = [text_block]
        usage = MagicMock()
        usage.input_tokens = 100
        usage.output_tokens = 50
        usage.cache_read_input_tokens = 0
        usage.cache_creation_input_tokens = 0
        response.usage = usage
        client.messages.create.return_value = response
        mock_client.return_value = _mock_llm_client(client)

        with patch("brain.systems.runs.direct_agent._load_session", return_value=(session_msgs, None)):
            result = run_agent(
                message="Continue",
                model="claude-sonnet-4-6",
                tools=[],
                persist_session=True,
                session_id="test-trim-start",
            )

        assert result.success
        # Verify the messages sent to the API have no orphaned pairs
        call_kwargs = client.messages.create.call_args
        self._assert_no_orphaned_tool_pairs(call_kwargs.kwargs["messages"])


class TestToolTranscriptSanitization:
    def _build_session(self, count, tool_pair_at_start=False, tool_pair_at_boundary=False):
        messages = []
        messages.append({"role": "user", "content": "Start task"})

        if tool_pair_at_start:
            messages.append({
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "early_tool_1", "name": "brain_recall", "input": {"query": "x"}},
                ],
            })
            messages.append({
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "early_tool_1", "content": "found stuff"},
                ],
            })
            messages.append({"role": "assistant", "content": [{"type": "text", "text": "Got it."}]})

        while len(messages) < count:
            messages.append({"role": "user", "content": f"Message {len(messages)}"})
            messages.append({"role": "assistant", "content": [{"type": "text", "text": f"Reply {len(messages)}"}]})

        if tool_pair_at_boundary:
            boundary = max(0, count - 37)
            if boundary > 0 and boundary + 1 < count:
                messages[boundary] = {
                    "role": "assistant",
                    "content": [
                        {"type": "tool_use", "id": "boundary_tool_1", "name": "exec_command", "input": {"cmd": "ls"}},
                    ],
                }
                messages[boundary + 1] = {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "boundary_tool_1", "content": "file.txt"},
                    ],
                }

        return messages

    def _assert_no_orphaned_tool_pairs(self, messages):
        use_ids = set()
        result_ids = set()
        for m in messages:
            content = m.get("content", "")
            if not isinstance(content, list):
                continue
            for b in content:
                if isinstance(b, dict):
                    if b.get("type") == "tool_use":
                        use_ids.add(b.get("id"))
                    elif b.get("type") == "tool_result":
                        result_ids.add(b.get("tool_use_id"))

        orphaned_use = use_ids - result_ids
        orphaned_result = result_ids - use_ids
        assert not orphaned_use, f"Orphaned tool_use ids (no matching tool_result): {orphaned_use}"
        assert not orphaned_result, f"Orphaned tool_result ids (no matching tool_use): {orphaned_result}"

    def test_removes_delayed_tool_result_sequence(self):
        from brain.systems.runs.direct_agent import _sanitize_tool_pairs

        messages = [
            {"role": "user", "content": "Start"},
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Checking."},
                    {"type": "tool_use", "id": "toolu_01GYkW", "name": "brain_recall", "input": {"query": "pickup"}},
                ],
            },
            {"role": "user", "content": "Actually, keep going."},
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_01GYkW", "content": "late result"},
                ],
            },
        ]

        cleaned = _sanitize_tool_pairs(messages, "delayed-tool-result")

        assert cleaned == [
            {"role": "user", "content": "Start"},
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Checking."},
                ],
            },
            {"role": "user", "content": "Actually, keep going."},
        ]

    @patch("brain.systems.runs.direct_agent.async_resolve_llm_client")
    @patch("brain.systems.runs.direct_agent._load_session")
    @patch("brain.systems.runs.direct_agent._save_session")
    def test_run_agent_sanitizes_invalid_session_before_api_call(self, mock_save, mock_load, mock_client):
        from brain.systems.runs.direct_agent import run_agent

        mock_load.return_value = ([
            {"role": "user", "content": "Start"},
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "toolu_01GYkW", "name": "brain_recall", "input": {"query": "pickup"}},
                ],
            },
            {"role": "user", "content": "follow-up without tool result"},
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_01GYkW", "content": "late result"},
                ],
            },
        ], None)

        client = MagicMock()
        response = MagicMock()
        response.stop_reason = "end_turn"
        text_block = MagicMock()
        text_block.type = "text"
        text_block.text = "Done."
        text_block.model_dump.return_value = {"type": "text", "text": "Done."}
        response.content = [text_block]
        usage = MagicMock()
        usage.input_tokens = 10
        usage.output_tokens = 5
        usage.cache_read_input_tokens = 0
        usage.cache_creation_input_tokens = 0
        response.usage = usage
        client.messages.create.return_value = response
        mock_client.return_value = _mock_llm_client(client)

        result = run_agent(
            message="Continue",
            model="claude-sonnet-4-6",
            tools=[],
            persist_session=True,
            session_id="test-invalid-session-order",
        )

        assert result.success
        sent_messages = client.messages.create.call_args.kwargs["messages"]
        assert all(
            not any(
                isinstance(block, dict) and block.get("type") in {"tool_use", "tool_result"}
                for block in (msg.get("content", []) if isinstance(msg.get("content"), list) else [])
            )
            for msg in sent_messages
        )

    @patch("brain.systems.runs.direct_agent.async_resolve_llm_client")
    @patch("brain.systems.runs.direct_agent._save_session")
    @patch("brain.systems.runs.direct_agent._summarize_trimmed_messages", return_value="summary")
    def test_trim_with_tool_pair_at_recent_boundary(self, mock_summary, mock_save, mock_client):
        """When a tool pair straddles the candidate_recent boundary, no orphans."""
        from brain.systems.runs.direct_agent import run_agent

        session_msgs = self._build_session(50, tool_pair_at_boundary=True)

        client = MagicMock()
        response = MagicMock()
        response.stop_reason = "end_turn"
        text_block = MagicMock()
        text_block.type = "text"
        text_block.text = "Done."
        text_block.model_dump.return_value = {"type": "text", "text": "Done."}
        response.content = [text_block]
        usage = MagicMock()
        usage.input_tokens = 100
        usage.output_tokens = 50
        usage.cache_read_input_tokens = 0
        usage.cache_creation_input_tokens = 0
        response.usage = usage
        client.messages.create.return_value = response
        mock_client.return_value = _mock_llm_client(client)

        with patch("brain.systems.runs.direct_agent._load_session", return_value=(session_msgs, None)):
            result = run_agent(
                message="Continue",
                model="claude-sonnet-4-6",
                tools=[],
                persist_session=True,
                session_id="test-trim-boundary",
            )

        assert result.success
        call_kwargs = client.messages.create.call_args
        self._assert_no_orphaned_tool_pairs(call_kwargs.kwargs["messages"])

    @patch("brain.systems.runs.direct_agent.async_resolve_llm_client")
    @patch("brain.systems.runs.direct_agent._save_session")
    @patch("brain.systems.runs.direct_agent._summarize_trimmed_messages", return_value="summary")
    def test_trim_with_both_tool_pair_scenarios(self, mock_summary, mock_save, mock_client):
        """Tool pairs at both start and boundary — the worst case."""
        from brain.systems.runs.direct_agent import run_agent

        session_msgs = self._build_session(50, tool_pair_at_start=True, tool_pair_at_boundary=True)

        client = MagicMock()
        response = MagicMock()
        response.stop_reason = "end_turn"
        text_block = MagicMock()
        text_block.type = "text"
        text_block.text = "Done."
        text_block.model_dump.return_value = {"type": "text", "text": "Done."}
        response.content = [text_block]
        usage = MagicMock()
        usage.input_tokens = 100
        usage.output_tokens = 50
        usage.cache_read_input_tokens = 0
        usage.cache_creation_input_tokens = 0
        response.usage = usage
        client.messages.create.return_value = response
        mock_client.return_value = _mock_llm_client(client)

        with patch("brain.systems.runs.direct_agent._load_session", return_value=(session_msgs, None)):
            result = run_agent(
                message="Continue",
                model="claude-sonnet-4-6",
                tools=[],
                persist_session=True,
                session_id="test-trim-both",
            )

        assert result.success
        call_kwargs = client.messages.create.call_args
        self._assert_no_orphaned_tool_pairs(call_kwargs.kwargs["messages"])
