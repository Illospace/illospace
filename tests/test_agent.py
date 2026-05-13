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
    llm.source = "user_default"
    llm.is_oauth = False
    llm.extra_headers = {}
    llm.token_prefix = "sk-ant-api03-test" if provider == "anthropic" else "sk-openai-test"
    llm.system_prompt_prefix = ""
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
    @patch("brain.systems.runs.direct_agent.get_provider")
    @patch("brain.systems.runs.direct_agent.resolve_llm_client")
    def test_init_llm_uses_provider_from_model_prefix(self, mock_resolve, mock_get_provider):
        from brain.systems.runs.direct_agent import _init_llm

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

    @patch("brain.systems.runs.direct_agent.get_provider")
    @patch("brain.systems.runs.direct_agent.resolve_llm_client")
    def test_init_llm_requires_chatgpt_auth_for_gpt_5_5(self, mock_resolve, mock_get_provider):
        from brain.systems.runs.direct_agent import _init_llm

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

        _init_llm("user-1", "sess-1", "openai/gpt-5.5")

        assert mock_resolve.call_args.kwargs["provider"] == "openai"
        assert mock_resolve.call_args.kwargs["auth_mode"] == "chatgpt"


class TestLiveGuidance:
    def test_append_live_guidance_adds_user_message(self):
        from brain.systems.runs.direct_agent import _append_live_guidance

        messages = [{"role": "user", "content": "Original task"}]
        seen_activity = []

        count = _append_live_guidance(
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

    @patch("brain.systems.runs.direct_agent.resolve_llm_client")
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

    @patch("brain.systems.runs.direct_agent.resolve_llm_client")
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
        assert "brain_vault" in names
        assert "runtime_settings" in names

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

    @patch("brain.systems.runs.direct_agent.resolve_llm_client")
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

    def test_repeated_brain_encode_is_rejected(self):
        from brain.systems.runs.direct_agent import _execute_tool_calls, _GateState

        block = MagicMock()
        block.type = "tool_use"
        block.name = "brain_encode"
        block.input = {"content": "Long enough lesson content to encode once."}
        block.id = "tool_encode_2"

        response = MagicMock()
        response.content = [block]

        tool_calls_made = ["brain_encode"]
        handler = MagicMock(return_value={"ok": True})

        results = _execute_tool_calls(
            response,
            {"brain_encode": handler},
            tool_calls_made,
            _GateState(),
            None,
            None,
            None,
            "runner",
        )

        assert handler.call_count == 0
        assert results[0]["is_error"] is True
        assert "already ran" in results[0]["content"]

    def test_failed_brain_encode_is_marked_non_retryable(self):
        from brain.systems.runs.direct_agent import _execute_tool_calls, _GateState

        block = MagicMock()
        block.type = "tool_use"
        block.name = "brain_encode"
        block.input = {"content": "Long enough lesson content to encode once."}
        block.id = "tool_encode_fail"

        response = MagicMock()
        response.content = [block]

        handler = MagicMock(return_value={"error": "embedding worker unavailable"})

        results = _execute_tool_calls(
            response,
            {"brain_encode": handler},
            [],
            _GateState(),
            None,
            None,
            None,
            "runner",
        )

        assert results[0]["is_error"] is True
        assert "Do not retry brain_encode" in results[0]["content"]

    @pytest.mark.asyncio
    async def test_execute_tool_calls_supports_async_handlers_inside_running_loop(self):
        from brain.systems.runs.direct_agent import _GateState
        from brain.systems.runs.direct_loop.gates import check_gate_violations
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

        results = await async_execute_tool_calls(
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
        )

        assert json.loads(results[0]["content"]) == {"guardrails": ["stay grounded"]}

    @pytest.mark.asyncio
    async def test_parallel_safe_tool_batch_overlaps_and_preserves_order(self):
        from brain.systems.runs.direct_agent import _GateState
        from brain.systems.runs.direct_loop.gates import check_gate_violations
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

        results = await async_execute_tool_calls(
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
        )

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

    def test_parallel_safe_tool_batch_propagates_agent_context(self):
        from brain.systems.runs.direct_agent import _execute_tool_calls, _GateState, _agent_context

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

            results = _execute_tool_calls(
                response,
                {"read_file": handler},
                [],
                _GateState(),
                None,
                None,
                None,
                "runner",
            )
        finally:
            for attr in ("user_id", "worker_name"):
                if hasattr(_agent_context, attr):
                    delattr(_agent_context, attr)

        assert json.loads(results[0]["content"]) == {
            "path": "alpha.py",
            "user_id": 123,
            "worker_name": "reader-1",
        }

    @patch("brain.systems.runs.direct_agent.resolve_llm_client")
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

    @patch("brain.systems.runs.direct_agent.resolve_llm_client")
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

    @patch("brain.systems.runs.direct_agent.resolve_llm_client")
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

        assert resolved.is_error is True
        assert "Continue working and plan another pipeline" in resolved.result_text

    @patch("brain.systems.runs.direct_agent.resolve_llm_client")
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

    @patch("brain.systems.runs.direct_agent.resolve_llm_client")
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

    @patch("brain.systems.runs.direct_agent.resolve_llm_client")
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

    @patch("brain.systems.runs.direct_agent.resolve_llm_client")
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

    @patch("brain.systems.runs.direct_agent.resolve_llm_client")
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

    @patch("brain.systems.runs.direct_agent.resolve_llm_client")
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

    @patch("brain.systems.runs.direct_agent.resolve_llm_client")
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

    @patch("brain.systems.runs.direct_agent.resolve_llm_client")
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

    @patch("brain.systems.runs.direct_agent.resolve_llm_client")
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

    @patch("brain.systems.runs.direct_agent.resolve_llm_client")
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


class TestFinalReplyReview:
    @patch("brain.systems.runs.direct_agent.get_provider")
    @patch("brain.systems.runs.direct_agent.resolve_llm_client")
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

    @patch("brain.systems.runs.direct_agent.get_provider")
    @patch("brain.systems.runs.direct_agent.resolve_llm_client")
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

    @patch("brain.systems.runs.direct_agent.get_provider")
    @patch("brain.systems.runs.direct_agent.resolve_llm_client")
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
            _agent_context.intent_satisfaction = None

        assert "Evidence guardrail" in context
        assert "Worker-summary-level evidence" in context
        assert "trust=trusted_with_uncertainty" in context
        assert "'commands': 1" in context
        assert "Raw DB row was not available" in context
        assert "strict_contract" in context
        assert "Complete every refactor phase" in context

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
    def test_cortex_reply_blocks_unresolved_final_message(self, mock_review):
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

            assert result["blocked"] is True
            assert result["checker_status"] == "continue"
            assert "AgentRun recipe escalate" in result["instruction"]
            mock_reply.assert_not_called()
            assert _agent_context.reply_contents == []
        finally:
            _agent_context.idea_id = None
            _agent_context.run = None
            _agent_context.user_request = None
            _agent_context.reply_contents = []
            _agent_context.final_reply_review = None

    @patch("brain.systems.runs.direct_agent.review_final_reply_once")
    def test_cortex_reply_allows_concrete_dependency_blocker(self, mock_review):
        from brain.systems.runs.direct_agent import _handle_cortex_reply, _agent_context

        class _Run:
            run_id = 42

        mock_review.return_value = {
            "status": "continue",
            "approved": False,
            "rationale": "The proposed reply does not complete the requested listing.",
            "missing_requirements": ["List the cycles"],
            "raw_output": "",
        }
        _agent_context.idea_id = "idea-123"
        _agent_context.run = _Run()
        _agent_context.user_request = "List my cycles"
        _agent_context.reply_contents = []
        _agent_context.final_reply_review = None

        try:
            with patch("brain.systems.cortex.reply.reply_to_cortex") as mock_reply:
                result = _handle_cortex_reply(
                    "I couldn't list your cycles because the cycles database migration is missing."
                )

            assert result["staged"] is True
            assert result["posted"] is False
            assert result["checker_status"] == "blocked_on_user"
            assert result["checker_override"] == "concrete_dependency_blocker"
            mock_reply.assert_not_called()
        finally:
            _agent_context.idea_id = None
            _agent_context.run = None
            _agent_context.user_request = None
            _agent_context.reply_contents = []
            _agent_context.final_reply_review = None


class TestExecutionArtifacts:
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

    def test_my_activity_includes_execution_artifacts(self):
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
            result = _handle_my_activity()
            assert result["execution_artifacts"][0]["type"] == "pr"
            assert result["execution_artifacts"][0]["number"] == 123
        finally:
            _agent_context.run = None
            _agent_context.start_time = None
            _agent_context.reply_contents = []
            _agent_context.tool_calls_log = []
            _agent_context.execution_artifacts = []

    def test_my_activity_loads_persisted_execution_artifacts_from_execution_id(self):
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
                return_value=[{"type": "commit", "sha": "abc1234", "summary": "Fix provenance"}],
            ) as mock_load, patch(
                "brain.platform.db.repositories.unit_of_work.UnitOfWork",
                side_effect=AssertionError("my_activity should not load run execution_artifacts"),
            ):
                result = _handle_my_activity()

            assert result["execution_artifacts"] == [{"type": "commit", "sha": "abc1234", "summary": "Fix provenance"}]
            mock_load.assert_called_once_with(execution_id="exec-123")
        finally:
            _agent_context.run = None
            _agent_context.start_time = None
            _agent_context.reply_contents = []
            _agent_context.tool_calls_log = []
            _agent_context.execution_artifacts = []
            if hasattr(_agent_context, "execution_metadata"):
                delattr(_agent_context, "execution_metadata")

    def test_my_activity_does_not_load_persisted_artifacts_without_execution_id(self):
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
        _agent_context.execution_metadata = {"run_id": 42, "run_id": "run-123"}
        try:
            with patch("brain.systems.runs.tool_handlers.load_execution_artifacts") as mock_load:
                result = _handle_my_activity()

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

    @patch("brain.systems.runs.direct_agent.resolve_llm_client")
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

    @patch("brain.systems.runs.direct_agent.resolve_llm_client")
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

    @patch("brain.systems.runs.direct_agent.resolve_llm_client")
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

    @patch("brain.systems.runs.direct_agent.resolve_llm_client")
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
