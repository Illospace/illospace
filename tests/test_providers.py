"""Tests for the multi-provider LLM abstraction layer."""

from __future__ import annotations

import json
import httpx
import pytest
from unittest.mock import MagicMock, patch

from brain.platform.integrations.openai_codex_client import OpenAICodexError, OpenAICodexRetryableError
from brain.platform.integrations.providers import (
    AnthropicProvider,
    OpenAIProvider,
    ContentBlock,
    LLMRequest,
    LLMResponse,
    StreamContext,
    StreamEvent,
    Usage,
    get_provider,
    get_active_provider,
    _anthropic_tools_to_openai,
    _anthropic_messages_to_openai_input,
    _openai_response_to_unified,
    _system_blocks_to_instructions,
)
from brain.platform.provider_health import (
    provider_health_snapshot,
    reset_provider_health,
)


# ── ContentBlock ──────────────────────────────────────────────────

class TestContentBlock:
    def test_text_block_model_dump(self):
        block = ContentBlock(type="text", text="hello")
        d = block.model_dump()
        assert d == {"type": "text", "text": "hello"}

    def test_tool_use_block_model_dump(self):
        block = ContentBlock(type="tool_use", id="call_123", name="read_file", input={"path": "x.py"})
        d = block.model_dump()
        assert d["type"] == "tool_use"
        assert d["id"] == "call_123"
        assert d["name"] == "read_file"
        assert d["input"] == {"path": "x.py"}

    def test_model_dump_excludes_none(self):
        block = ContentBlock(type="text", text="hi")
        d = block.model_dump(exclude_none=True)
        assert "id" not in d
        assert "name" not in d


# ── Tool Conversion ───────────────────────────────────────────────

class TestToolConversion:
    def test_anthropic_to_openai_tools(self):
        anthropic_tools = [
            {
                "name": "read_file",
                "description": "Read a file",
                "input_schema": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            }
        ]
        result = _anthropic_tools_to_openai(anthropic_tools)
        assert len(result) == 1
        assert result[0]["type"] == "function"
        assert result[0]["name"] == "read_file"
        assert result[0]["parameters"]["required"] == ["path"]

    def test_none_tools(self):
        assert _anthropic_tools_to_openai(None) is None

    def test_empty_tools(self):
        assert _anthropic_tools_to_openai([]) is None


# ── Message Conversion ────────────────────────────────────────────

class TestMessageConversion:
    def test_simple_user_assistant(self):
        msgs = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
        ]
        result = _anthropic_messages_to_openai_input(msgs)
        assert len(result) == 2
        assert result[0] == {"role": "user", "content": "hello"}
        assert result[1] == {"role": "assistant", "content": "hi there"}

    def test_system_blocks(self):
        system = [{"type": "text", "text": "You are helpful."}]
        assert _system_blocks_to_instructions(system) == "You are helpful."

    def test_system_string(self):
        assert _system_blocks_to_instructions("Be helpful") == "Be helpful"

    def test_tool_use_and_result(self):
        msgs = [
            {"role": "user", "content": "read x.py"},
            {"role": "assistant", "content": [
                {"type": "text", "text": "Let me read that."},
                {"type": "tool_use", "id": "call_1", "name": "read_file", "input": {"path": "x.py"}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "call_1", "content": "file contents"},
            ]},
        ]
        result = _anthropic_messages_to_openai_input(msgs)
        assert result[0] == {"role": "user", "content": "read x.py"}
        assert result[1] == {"role": "assistant", "content": "Let me read that."}
        assert result[2]["type"] == "function_call"
        assert result[2]["call_id"] == "call_1"
        assert result[2]["name"] == "read_file"
        assert result[3]["type"] == "function_call_output"
        assert result[3]["call_id"] == "call_1"

    def test_thinking_blocks_stripped(self):
        """Thinking blocks should not appear in OpenAI messages."""
        msgs = [
            {"role": "assistant", "content": [
                {"type": "thinking", "thinking": "Let me think..."},
                {"type": "text", "text": "Here's my answer."},
            ]},
        ]
        result = _anthropic_messages_to_openai_input(msgs)
        assert len(result) == 1
        assert result[0]["content"] == "Here's my answer."


# ── Response Conversion ───────────────────────────────────────────

class TestResponseConversion:
    def _mock_openai_response(self, content="Hello", tool_calls=None, incomplete_reason=None):
        resp = MagicMock()
        message_item = MagicMock()
        message_item.type = "message"
        message_item.role = "assistant"
        message_item.content = []
        if content is not None:
            text_part = MagicMock()
            text_part.type = "output_text"
            text_part.text = content
            message_item.content = [text_part]
        resp.output = [message_item]
        if tool_calls:
            resp.output.extend(tool_calls)
        resp.usage.input_tokens = 100
        resp.usage.output_tokens = 50
        resp.usage.input_tokens_details.cached_tokens = 12
        resp.model = "gpt-5.4"
        resp.incomplete_details = MagicMock()
        resp.incomplete_details.reason = incomplete_reason
        return resp

    def test_text_response(self):
        resp = self._mock_openai_response("Hello world")
        unified = _openai_response_to_unified(resp)
        assert unified.stop_reason == "end_turn"
        assert len(unified.content) == 1
        assert unified.content[0].type == "text"
        assert unified.content[0].text == "Hello world"
        assert unified.usage.input_tokens == 100
        assert unified.usage.output_tokens == 50
        assert unified.usage.cache_read_input_tokens == 12

    def test_tool_call_response(self):
        tc = MagicMock()
        tc.type = "function_call"
        tc.call_id = "call_abc"
        tc.name = "read_file"
        tc.arguments = '{"path": "x.py"}'
        resp = self._mock_openai_response(content=None, tool_calls=[tc])
        unified = _openai_response_to_unified(resp)
        assert unified.stop_reason == "tool_use"
        assert unified.content[0].type == "tool_use"
        assert unified.content[0].name == "read_file"
        assert unified.content[0].input == {"path": "x.py"}

    def test_max_output_tokens_maps_to_max_tokens_stop_reason(self):
        resp = self._mock_openai_response("partial", incomplete_reason="max_output_tokens")
        unified = _openai_response_to_unified(resp)
        assert unified.stop_reason == "max_tokens"

    def test_top_level_output_text_fallback(self):
        resp = MagicMock()
        resp.output = []
        resp.output_text = "Hello from fallback"
        resp.usage.input_tokens = 100
        resp.usage.output_tokens = 50
        resp.usage.input_tokens_details.cached_tokens = 0
        resp.model = "gpt-5.4"
        resp.incomplete_details = None

        unified = _openai_response_to_unified(resp)

        assert len(unified.content) == 1
        assert unified.content[0].text == "Hello from fallback"

    def test_logs_when_openai_response_parses_to_empty_content(self, caplog):
        resp = self._mock_openai_response(content=None)
        with caplog.at_level("WARNING"):
            unified = _openai_response_to_unified(resp, "openai/gpt-5.4")

        assert unified.content == []
        assert "OpenAI response parsed to empty content" in caplog.text
        assert "output_excerpt=" in caplog.text
        assert "response_dump=" in caplog.text
        assert "top_level_keys" in caplog.text

    def test_create_logs_request_summary_when_output_array_is_empty(self, caplog):
        client = MagicMock()
        provider = OpenAIProvider(client)
        resp = self._mock_openai_response(content=None)
        resp.output = []
        client.responses.create.return_value = iter([
            {"type": "response.completed", "response": resp},
        ])

        request = LLMRequest(
            model="openai/gpt-5.4",
            messages=[{"role": "user", "content": "Hello"}],
            max_output_tokens=512,
            system=[{"type": "text", "text": "You are helpful."}],
            reasoning_effort="medium",
        )

        with caplog.at_level("WARNING"):
            unified = provider.create(request)

        assert unified.content == []
        assert "OpenAI response completed with empty output array: request=" in caplog.text
        assert "\"model\": \"gpt-5.4\"" in caplog.text
        assert "\"reasoning\": {\"effort\": \"medium\", \"summary\": \"auto\"}" in caplog.text


# ── Provider Registry ─────────────────────────────────────────────

class TestProviderRegistry:
    def test_get_anthropic_provider(self):
        client = MagicMock()
        p = get_provider("anthropic", client)
        assert isinstance(p, AnthropicProvider)

    def test_get_openai_provider(self):
        client = MagicMock()
        p = get_provider("openai", client)
        assert isinstance(p, OpenAIProvider)

    def test_unknown_provider_raises(self):
        with pytest.raises(ValueError, match="Unknown provider"):
            get_provider("gemini", MagicMock())

    def test_get_active_provider_default(self):
        with patch.dict("os.environ", {}, clear=True):
            # Remove ILLO_LLM_PROVIDER if set
            import os
            os.environ.pop("ILLO_LLM_PROVIDER", None)
            assert get_active_provider() == "openai"

    def test_get_active_provider_env(self):
        with patch.dict("os.environ", {"ILLO_LLM_PROVIDER": "openai"}):
            assert get_active_provider() == "openai"


# ── AnthropicProvider (pass-through) ──────────────────────────────

class TestAnthropicProvider:
    def setup_method(self):
        reset_provider_health()

    def test_create_passthrough(self):
        client = MagicMock()
        client.messages.create.return_value = "mock_response"
        p = AnthropicProvider(client)
        result = p.create(LLMRequest(model="claude-sonnet-4-6", messages=[], max_output_tokens=100))
        assert result == "mock_response"
        client.messages.create.assert_called_once()

    def test_create_records_operation_health_failure(self):
        client = MagicMock()
        client.messages.create.side_effect = TimeoutError("provider timed out")
        p = AnthropicProvider(client)

        with pytest.raises(TimeoutError):
            p.create(LLMRequest(
                model="claude-sonnet-4-6",
                messages=[],
                max_output_tokens=100,
                operation_type="scout",
            ))

        health = provider_health_snapshot()
        entries = health["operations"]["scout"]
        assert entries[0]["provider"] == "anthropic"
        assert entries[0]["model"] == "claude-sonnet-4-6"
        assert entries[0]["status"] == "unavailable"
        assert entries[0]["failures"] == 1
        assert entries[0]["policy"]["fail_open"] is True

    def test_stream_passthrough(self):
        client = MagicMock()
        p = AnthropicProvider(client)
        p.stream(LLMRequest(model="claude-sonnet-4-6", messages=[], max_output_tokens=100))
        client.messages.stream.assert_called_once()

    def test_rate_limit_status_is_retryable(self):
        client = MagicMock()
        p = AnthropicProvider(client)

        class ProviderStatusError(Exception):
            status_code = 429

        assert p.is_retryable_error(ProviderStatusError("rate limit exceeded"))

    def test_client_error_status_is_not_retryable(self):
        client = MagicMock()
        p = AnthropicProvider(client)

        class ProviderStatusError(Exception):
            status_code = 400

        assert not p.is_retryable_error(ProviderStatusError("bad request"))

    def test_remote_protocol_disconnect_is_retryable(self):
        p = AnthropicProvider(MagicMock())

        assert p.is_retryable_error(
            httpx.RemoteProtocolError(
                "peer closed connection without sending complete message body (incomplete chunked read)"
            )
        )


# ── OpenAIProvider ────────────────────────────────────────────────

class TestOpenAIProvider:
    def test_remote_protocol_disconnect_is_retryable(self):
        p = OpenAIProvider(MagicMock())

        assert p.is_retryable_error(
            httpx.RemoteProtocolError(
                "peer closed connection without sending complete message body (incomplete chunked read)"
            )
        )

    def test_stream_translates_request_to_responses_api(self):
        client = MagicMock()
        mock_resp = MagicMock()
        output_message = MagicMock()
        output_message.type = "message"
        output_message.role = "assistant"
        output_text = MagicMock()
        output_text.type = "output_text"
        output_text.text = "Hi"
        output_message.content = [output_text]
        mock_resp.output = [output_message]
        mock_resp.usage.input_tokens = 10
        mock_resp.usage.output_tokens = 5
        mock_resp.usage.input_tokens_details.cached_tokens = 0
        mock_resp.incomplete_details = None
        client.responses.create.return_value = iter([
            {"type": "response.output_text.delta", "delta": "Hi"},
            {"type": "response.completed", "response": mock_resp},
        ])

        p = OpenAIProvider(client)
        with p.stream(LLMRequest(
            model="openai/gpt-4o",
            messages=[{"role": "user", "content": "hi"}],
            system=[{"type": "text", "text": "Be helpful"}],
            max_output_tokens=1000,
            reasoning_effort="high",
            cache_key="illo:test-cache-key",
            cache_retention="24h",
            extra_headers={"anthropic-beta": "something"},  # Should be stripped
        )) as stream:
            result = stream.get_final_message()

        call_kwargs = client.responses.create.call_args.kwargs
        assert call_kwargs["model"] == "gpt-4o"
        assert call_kwargs["instructions"] == "Be helpful"
        assert call_kwargs["input"][0]["role"] == "user"
        assert call_kwargs["max_output_tokens"] == 1000
        assert call_kwargs["reasoning"] == {"effort": "high", "summary": "auto"}
        assert call_kwargs["include"] == ["reasoning.encrypted_content"]
        assert call_kwargs["prompt_cache_key"] == "illo:test-cache-key"
        assert call_kwargs["prompt_cache_retention"] == "24h"
        assert call_kwargs["store"] is False
        assert call_kwargs["extra_headers"] == {"anthropic-beta": "something"}
        assert call_kwargs["stream"] is True

        assert result.stop_reason == "end_turn"
        assert result.content[0].text == "Hi"

    def test_stream_emits_only_complete_reasoning_summary_reflections(self):
        client = MagicMock()
        mock_resp = MagicMock()
        output_message = MagicMock()
        output_message.type = "message"
        output_message.role = "assistant"
        output_text = MagicMock()
        output_text.type = "output_text"
        output_text.text = "Answer"
        output_message.content = [output_text]
        mock_resp.output = [output_message]
        mock_resp.usage.input_tokens = 10
        mock_resp.usage.output_tokens = 5
        mock_resp.usage.input_tokens_details.cached_tokens = 0
        mock_resp.incomplete_details = None
        client.responses.create.return_value = iter([
            {"type": "response.reasoning_summary_text.delta", "delta": "**Understanding setup** I"},
            {
                "type": "response.reasoning_summary_text.done",
                "text": "**Understanding setup** I checked the project context before changing anything.",
            },
            {"type": "response.output_text.delta", "delta": "Answer"},
            {"type": "response.completed", "response": mock_resp},
        ])

        p = OpenAIProvider(client)
        with p.stream(LLMRequest(
            model="openai/gpt-5.4",
            messages=[{"role": "user", "content": "hi"}],
            reasoning_effort="high",
        )) as stream:
            events = list(stream)
            result = stream.get_final_message()

        assert [(event.type, event.text) for event in events] == [
            (
                "reflection",
                "**Understanding setup** I checked the project context before changing anything.",
            ),
            ("text", "Answer"),
        ]
        assert result.content[0].text == "Answer"

    def test_stream_strips_provider_prefix(self):
        client = MagicMock()
        mock_resp = MagicMock()
        output_message = MagicMock()
        output_message.type = "message"
        output_message.role = "assistant"
        output_text = MagicMock()
        output_text.type = "output_text"
        output_text.text = "ok"
        output_message.content = [output_text]
        mock_resp.output = [output_message]
        mock_resp.usage.input_tokens = 5
        mock_resp.usage.output_tokens = 2
        mock_resp.usage.input_tokens_details.cached_tokens = 0
        mock_resp.incomplete_details = None
        client.responses.create.return_value = iter([
            {"type": "response.completed", "response": mock_resp},
        ])

        p = OpenAIProvider(client)
        with p.stream(LLMRequest(model="openai/gpt-4o-mini", messages=[{"role": "user", "content": "hi"}], max_output_tokens=100)) as stream:
            stream.get_final_message()
        call_kwargs = client.responses.create.call_args.kwargs
        assert call_kwargs["model"] == "gpt-4o-mini"
        assert "include" not in call_kwargs

    def test_stream_omits_max_output_tokens_when_not_specified(self):
        client = MagicMock()
        mock_resp = MagicMock()
        output_message = MagicMock()
        output_message.type = "message"
        output_message.role = "assistant"
        output_text = MagicMock()
        output_text.type = "output_text"
        output_text.text = "ok"
        output_message.content = [output_text]
        mock_resp.output = [output_message]
        mock_resp.usage.input_tokens = 5
        mock_resp.usage.output_tokens = 2
        mock_resp.usage.input_tokens_details.cached_tokens = 0
        mock_resp.incomplete_details = None
        client.responses.create.return_value = iter([
            {"type": "response.completed", "response": mock_resp},
        ])

        p = OpenAIProvider(client)
        with p.stream(LLMRequest(model="openai/gpt-4o-mini", messages=[{"role": "user", "content": "hi"}])) as stream:
            stream.get_final_message()
        call_kwargs = client.responses.create.call_args.kwargs
        assert "max_output_tokens" not in call_kwargs

    def test_stream_overload_error_is_retryable(self):
        client = MagicMock()
        client.responses.create.return_value = iter([
            {
                "type": "error",
                "error": {
                    "message": "Our servers are currently overloaded. Please try again later.",
                    "code": "server_overloaded",
                },
            },
        ])

        p = OpenAIProvider(client)

        with pytest.raises(OpenAICodexRetryableError):
            with p.stream(LLMRequest(
                model="openai/gpt-5.5",
                messages=[{"role": "user", "content": "hi"}],
                max_output_tokens=100,
            )) as stream:
                stream.get_final_message()

    def test_runtime_retries_openai_stream_overload_error(self):
        from types import SimpleNamespace

        from brain.systems.runs.direct_loop.retry import api_call_with_retry
        from brain.systems.runs.direct_loop.streaming import streaming_call

        client = MagicMock()
        mock_resp = MagicMock()
        output_message = MagicMock()
        output_message.type = "message"
        output_message.role = "assistant"
        output_text = MagicMock()
        output_text.type = "output_text"
        output_text.text = "Recovered"
        output_message.content = [output_text]
        mock_resp.output = [output_message]
        mock_resp.usage.input_tokens = 5
        mock_resp.usage.output_tokens = 2
        mock_resp.usage.input_tokens_details.cached_tokens = 0
        mock_resp.incomplete_details = None
        client.responses.create.side_effect = [
            iter([
                {
                    "type": "error",
                    "error": {"message": "Our servers are currently overloaded. Please try again later."},
                },
            ]),
            iter([{"type": "response.completed", "response": mock_resp}]),
        ]

        provider = OpenAIProvider(client)
        response = api_call_with_retry(
            provider,
            LLMRequest(
                model="openai/gpt-5.5",
                messages=[{"role": "user", "content": "hi"}],
                max_output_tokens=100,
            ),
            llm=SimpleNamespace(is_oauth=False, build_request_headers=lambda session_id=None: {}),
            cancel_event=SimpleNamespace(is_set=lambda: False),
            on_stream_activity=None,
            on_stream_delta=None,
            session_id="agent-run-64",
            turn=30,
            tokens=SimpleNamespace(),
            start_time=0,
            tool_calls_made=[],
            call_start=0,
            retry_delays=(0,),
            streaming_call=streaming_call,
            make_cancelled_result=lambda *args, **kwargs: None,
            degrade_betas=lambda: False,
        )

        assert response.content[0].text == "Recovered"
        assert client.responses.create.call_count == 2

    def test_stream_normalizes_oversized_prompt_cache_key(self):
        client = MagicMock()
        mock_resp = MagicMock()
        output_message = MagicMock()
        output_message.type = "message"
        output_message.role = "assistant"
        output_text = MagicMock()
        output_text.type = "output_text"
        output_text.text = "ok"
        output_message.content = [output_text]
        mock_resp.output = [output_message]
        mock_resp.usage.input_tokens = 5
        mock_resp.usage.output_tokens = 2
        mock_resp.usage.input_tokens_details.cached_tokens = 0
        mock_resp.incomplete_details = None
        client.responses.create.return_value = iter([
            {"type": "response.completed", "response": mock_resp},
        ])

        p = OpenAIProvider(client)
        with p.stream(LLMRequest(
            model="openai/gpt-4o-mini",
            messages=[{"role": "user", "content": "hi"}],
            cache_key="illo:" + ("x" * 80),
        )) as stream:
            stream.get_final_message()

        cache_key = client.responses.create.call_args.kwargs["prompt_cache_key"]
        assert len(cache_key) <= 64
        assert cache_key.startswith("illo:")
        assert cache_key != "illo:" + ("x" * 80)

    def test_create_uses_streaming_final_message(self):
        client = MagicMock()
        final_response = LLMResponse(
            content=[ContentBlock(type="text", text="Hi from Codex")],
            stop_reason="end_turn",
            usage=Usage(input_tokens=1, output_tokens=1),
            model="openai/gpt-5.4",
        )

        class _FakeStream:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def get_final_message(self):
                return final_response

        p = OpenAIProvider(client)
        with patch.object(p, "stream", return_value=_FakeStream()) as mock_stream:
            result = p.create(LLMRequest(
                model="openai/gpt-5.4",
                messages=[{"role": "user", "content": "hi"}],
                max_output_tokens=100,
            ))

        assert result.content[0].text == "Hi from Codex"
        mock_stream.assert_called_once()

    def test_create_retries_without_prompt_cache_retention_when_rejected(self):
        client = MagicMock()
        unsupported = RuntimeError("Unsupported parameter: prompt_cache_retention")

        mock_resp = MagicMock()
        output_message = MagicMock()
        output_message.type = "message"
        output_message.role = "assistant"
        output_text = MagicMock()
        output_text.type = "output_text"
        output_text.text = "Hi"
        output_message.content = [output_text]
        mock_resp.output = [output_message]
        mock_resp.usage.input_tokens = 10
        mock_resp.usage.output_tokens = 5
        mock_resp.usage.input_tokens_details.cached_tokens = 0
        mock_resp.incomplete_details = None
        client.responses.create.side_effect = [unsupported, iter([
            {"type": "response.completed", "response": mock_resp},
        ])]

        p = OpenAIProvider(client)
        with p.stream(LLMRequest(
            model="openai/gpt-5.4",
            messages=[{"role": "user", "content": "hi"}],
            max_output_tokens=1000,
            cache_key="illo:test-cache-key",
            cache_retention="24h",
        )) as stream:
            result = stream.get_final_message()

        first_call = client.responses.create.call_args_list[0].kwargs
        second_call = client.responses.create.call_args_list[1].kwargs
        assert first_call["prompt_cache_retention"] == "24h"
        assert second_call["prompt_cache_key"] == "illo:test-cache-key"
        assert "prompt_cache_retention" not in second_call
        assert result.content[0].text == "Hi"

    def test_create_retries_without_reasoning_summary_when_rejected(self):
        client = MagicMock()
        unsupported = RuntimeError("Unsupported parameter: reasoning.summary")

        mock_resp = MagicMock()
        output_message = MagicMock()
        output_message.type = "message"
        output_message.role = "assistant"
        output_text = MagicMock()
        output_text.type = "output_text"
        output_text.text = "Hi"
        output_message.content = [output_text]
        mock_resp.output = [output_message]
        mock_resp.usage.input_tokens = 10
        mock_resp.usage.output_tokens = 5
        mock_resp.usage.input_tokens_details.cached_tokens = 0
        mock_resp.incomplete_details = None
        client.responses.create.side_effect = [unsupported, iter([
            {"type": "response.completed", "response": mock_resp},
        ])]

        p = OpenAIProvider(client)
        with p.stream(LLMRequest(
            model="openai/gpt-5.4",
            messages=[{"role": "user", "content": "hi"}],
            max_output_tokens=1000,
            reasoning_effort="high",
        )) as stream:
            result = stream.get_final_message()

        first_call = client.responses.create.call_args_list[0].kwargs
        second_call = client.responses.create.call_args_list[1].kwargs
        assert first_call["reasoning"] == {"effort": "high", "summary": "auto"}
        assert second_call["reasoning"] == {"effort": "high"}
        assert result.content[0].text == "Hi"

    def test_stream_uses_collected_text_when_completed_payload_is_empty(self):
        client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.output = []
        mock_resp.usage.input_tokens = 10
        mock_resp.usage.output_tokens = 5
        mock_resp.usage.input_tokens_details.cached_tokens = 0
        mock_resp.incomplete_details = None
        mock_resp.model = "gpt-5.4"
        mock_resp.id = "resp_123"
        client.responses.create.return_value = iter([
            {"type": "response.output_text.delta", "delta": "Hi"},
            {"type": "response.completed", "response": mock_resp},
        ])

        p = OpenAIProvider(client)
        with p.stream(LLMRequest(
            model="openai/gpt-5.4",
            messages=[{"role": "user", "content": "hi"}],
            max_output_tokens=100,
        )) as stream:
            result = stream.get_final_message()

        assert result.content[0].text == "Hi"
        assert result.usage.output_tokens == 5

    def test_stream_accepts_response_text_delta_events(self):
        client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.output = []
        mock_resp.usage.input_tokens = 10
        mock_resp.usage.output_tokens = 5
        mock_resp.usage.input_tokens_details.cached_tokens = 0
        mock_resp.incomplete_details = None
        mock_resp.model = "gpt-5.4"
        client.responses.create.return_value = iter([
            {"type": "response.text.delta", "delta": "Hi"},
            {"type": "response.completed", "response": mock_resp},
        ])

        p = OpenAIProvider(client)
        with p.stream(LLMRequest(
            model="openai/gpt-5.4",
            messages=[{"role": "user", "content": "hi"}],
            max_output_tokens=100,
        )) as stream:
            result = stream.get_final_message()

        assert result.content[0].text == "Hi"

    def test_stream_accepts_content_part_done_events(self):
        client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.output = []
        mock_resp.usage.input_tokens = 10
        mock_resp.usage.output_tokens = 5
        mock_resp.usage.input_tokens_details.cached_tokens = 0
        mock_resp.incomplete_details = None
        mock_resp.model = "gpt-5.4"
        client.responses.create.return_value = iter([
            {
                "type": "response.content_part.done",
                "part": {"type": "output_text", "text": "Hi from content part"},
            },
            {"type": "response.completed", "response": mock_resp},
        ])

        p = OpenAIProvider(client)
        with p.stream(LLMRequest(
            model="openai/gpt-5.4",
            messages=[{"role": "user", "content": "hi"}],
            max_output_tokens=100,
        )) as stream:
            result = stream.get_final_message()

        assert result.content[0].text == "Hi from content part"

    def test_stream_accepts_output_item_done_message_events(self):
        client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.output = []
        mock_resp.usage.input_tokens = 10
        mock_resp.usage.output_tokens = 5
        mock_resp.usage.input_tokens_details.cached_tokens = 0
        mock_resp.incomplete_details = None
        mock_resp.model = "gpt-5.4"
        client.responses.create.return_value = iter([
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "Hi from item"}],
                },
            },
            {"type": "response.completed", "response": mock_resp},
        ])

        p = OpenAIProvider(client)
        with p.stream(LLMRequest(
            model="openai/gpt-5.4",
            messages=[{"role": "user", "content": "hi"}],
            max_output_tokens=100,
        )) as stream:
            result = stream.get_final_message()

        assert result.content[0].text == "Hi from item"

    def test_stream_accepts_output_item_added_message_events(self):
        client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.output = []
        mock_resp.usage.input_tokens = 10
        mock_resp.usage.output_tokens = 5
        mock_resp.usage.input_tokens_details.cached_tokens = 0
        mock_resp.incomplete_details = None
        mock_resp.model = "gpt-5.4"
        client.responses.create.return_value = iter([
            {
                "type": "response.output_item.added",
                "item": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "Hi from added item"}],
                },
            },
            {"type": "response.completed", "response": mock_resp},
        ])

        p = OpenAIProvider(client)
        with p.stream(LLMRequest(
            model="openai/gpt-5.4",
            messages=[{"role": "user", "content": "hi"}],
            max_output_tokens=100,
        )) as stream:
            result = stream.get_final_message()

        assert result.content[0].text == "Hi from added item"

    def test_stream_reconstructs_function_call_when_completed_output_is_empty(self):
        client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.output = []
        mock_resp.usage.input_tokens = 10
        mock_resp.usage.output_tokens = 5
        mock_resp.usage.input_tokens_details.cached_tokens = 0
        mock_resp.incomplete_details = None
        mock_resp.model = "gpt-5.4"
        client.responses.create.return_value = iter([
            {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {
                    "type": "function_call",
                    "call_id": "call_123",
                    "name": "brain_recall",
                    "arguments": "{\"query\":\"availability\"}",
                },
            },
            {
                "type": "response.function_call_arguments.delta",
                "output_index": 0,
                "delta": "{\"query\":\"availability\"}",
            },
            {
                "type": "response.function_call_arguments.done",
                "output_index": 0,
                "arguments": "{\"query\":\"availability\"}",
            },
            {"type": "response.completed", "response": mock_resp},
        ])

        p = OpenAIProvider(client)
        with p.stream(LLMRequest(
            model="openai/gpt-5.4",
            messages=[{"role": "user", "content": "hi"}],
            max_output_tokens=100,
        )) as stream:
            result = stream.get_final_message()

        assert result.stop_reason == "tool_use"
        assert result.content[0].type == "tool_use"
        assert result.content[0].name == "brain_recall"
        assert result.content[0].input == {"query": "availability"}


# ── MODEL CATALOG ─────────────────────────────────────────────────

class TestModelCatalog:
    def test_get_default_model_anthropic(self):
        from brain.platform.providers.model_policy import get_default_model
        with patch.dict("os.environ", {"ILLO_LLM_PROVIDER": "anthropic"}):
            assert get_default_model(provider="anthropic", include_provider_prefix=False) == "claude-sonnet-4-6"

    def test_get_default_model_openai(self):
        from brain.platform.providers.model_policy import get_default_model
        assert get_default_model(provider="openai", include_provider_prefix=False) == "gpt-5.6-sol"

    def test_get_model_options_default(self):
        from brain.platform.providers.model_policy import get_provider_model_options
        with patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop("ILLO_LLM_PROVIDER", None)
            options = get_provider_model_options()
            assert "gpt-5.4" in options

    def test_model_catalog_has_both_providers(self):
        from brain.platform.providers.model_policy import get_provider_model_catalogs
        catalogs = get_provider_model_catalogs()
        assert catalogs["anthropic"]["default"] == "claude-sonnet-4-6"
        assert catalogs["openai"]["default"] == "gpt-5.6-sol"
        assert "claude-opus-4-6" in catalogs["anthropic"]["options"]
        assert "gpt-5.5" in catalogs["openai"]["options"]


# ── LLM Client (OpenAI path) ─────────────────────────────────────

class TestLLMClientOpenAI:
    @patch("brain.platform.integrations.llm._resolve_key_from_env", return_value=("sk-test-key", "env"))
    @patch("brain.platform.integrations.llm.load_codex_auth_json", return_value=None)
    def test_resolve_openai_client(self, mock_codex, mock_env):
        from brain.platform.integrations.llm import resolve_llm_client
        mock_openai = MagicMock()
        mock_openai.OpenAI.return_value = MagicMock()
        with patch("brain.platform.integrations.llm._import_openai_sdk", return_value=mock_openai):
            result = resolve_llm_client(provider="openai")
            assert result.provider == "openai"
            assert result.is_oauth is False
            assert result.source == "env"

    @patch("brain.platform.integrations.llm._resolve_key_from_env", return_value=("sk-test-key", "env"))
    @patch("brain.platform.integrations.llm._import_openai_sdk", side_effect=RuntimeError("missing openai"))
    @patch("brain.platform.integrations.llm.load_codex_auth_json", return_value=None)
    def test_resolve_openai_missing_sdk_raises_cleanly(self, mock_codex, mock_sdk, mock_env):
        from brain.platform.integrations.llm import resolve_llm_client

        with pytest.raises(RuntimeError, match="missing openai"):
            resolve_llm_client(provider="openai")

    def test_resolve_openai_no_key_raises(self):
        from brain.platform.integrations.llm import resolve_llm_client
        with patch("brain.platform.integrations.llm._resolve_key_from_env", return_value=(None, "none")):
            with patch("brain.platform.integrations.llm.load_codex_auth_json", return_value=None):
                with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
                    resolve_llm_client(provider="openai")

    def test_resolve_openai_from_codex_cache(self):
        from brain.platform.integrations.llm import resolve_llm_client
        from brain.platform.integrations.openai_codex_auth import OpenAICodexCredential

        codex_auth = OpenAICodexCredential(
            access_token="access-123",
            account_id="acct_123",
            auth_mode="chatgpt",
            external_source_path="/tmp/auth.json",
        )
        mock_client = MagicMock()

        with patch("brain.platform.integrations.llm.load_codex_auth_json", return_value=codex_auth), \
             patch("brain.platform.integrations.llm.OpenAICodexClient", return_value=mock_client):
            result = resolve_llm_client(provider="openai")

        assert result.provider == "openai"
        assert result.auth_mode == "chatgpt"
        assert result.is_oauth is True
        assert result.source == "codex_cache"
