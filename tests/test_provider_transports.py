from __future__ import annotations

from unittest.mock import MagicMock

import pytest


def test_provider_facade_reexports_transport_types():
    from brain.platform.integrations import providers
    from brain.platform.integrations.transports.base import (
        ContentBlock,
        ContentBlockType,
        ImageContentBlock,
        LLMRequest,
        LLMResponse,
        MessageRole,
        Provider,
        StopReason,
        StreamContext,
        StreamEvent,
        TextContentBlock,
        ThinkingContentBlock,
        ToolResultContentBlock,
        ToolUseContentBlock,
        Usage,
    )

    assert providers.ContentBlock is ContentBlock
    assert providers.ContentBlockType is ContentBlockType
    assert providers.ImageContentBlock is ImageContentBlock
    assert providers.LLMRequest is LLMRequest
    assert providers.LLMResponse is LLMResponse
    assert providers.MessageRole is MessageRole
    assert providers.Provider is Provider
    assert providers.StopReason is StopReason
    assert providers.StreamContext is StreamContext
    assert providers.StreamEvent is StreamEvent
    assert providers.TextContentBlock is TextContentBlock
    assert providers.ThinkingContentBlock is ThinkingContentBlock
    assert providers.ToolResultContentBlock is ToolResultContentBlock
    assert providers.ToolUseContentBlock is ToolUseContentBlock
    assert providers.Usage is Usage


def test_content_block_wrappers_round_trip_legacy_dicts():
    from brain.platform.integrations.transports.base import (
        ContentBlockType,
        ImageContentBlock,
        TextContentBlock,
        ThinkingContentBlock,
        ToolResultContentBlock,
        ToolUseContentBlock,
        content_blocks_from_legacy,
        content_blocks_to_legacy,
    )

    blocks = [
        TextContentBlock("hello"),
        ImageContentBlock(source={"type": "base64", "media_type": "image/png", "data": "abc"}),
        ThinkingContentBlock("private reasoning", signature="sig"),
        ToolUseContentBlock(id="tool_1", name="brain_recall", input={"query": "x"}),
        ToolResultContentBlock(tool_use_id="tool_1", content={"ok": True}, is_error=False),
    ]

    legacy = content_blocks_to_legacy(blocks)
    typed = content_blocks_from_legacy(legacy)

    assert [block.type for block in typed] == [
        ContentBlockType.TEXT,
        ContentBlockType.IMAGE,
        ContentBlockType.THINKING,
        ContentBlockType.TOOL_USE,
        ContentBlockType.TOOL_RESULT,
    ]
    assert content_blocks_to_legacy(typed) == legacy
    assert legacy[3] == {
        "type": "tool_use",
        "id": "tool_1",
        "name": "brain_recall",
        "input": {"query": "x"},
    }


def test_anthropic_messages_transport_builds_native_kwargs():
    from brain.platform.integrations.transports.anthropic import AnthropicMessagesTransport
    from brain.platform.integrations.transports.base import LLMRequest

    request = LLMRequest(
        model="anthropic/claude-sonnet-4-6",
        messages=[{"role": "user", "content": "hello"}],
        max_output_tokens=123,
        system=[{"type": "text", "text": "Be helpful"}],
        tools=[{"name": "read_file", "input_schema": {"type": "object"}}],
        reasoning_effort="high",
        extra_headers={"x-test": "1"},
    )

    kwargs = AnthropicMessagesTransport().build_kwargs(request)

    assert kwargs["model"] == "claude-sonnet-4-6"
    assert kwargs["messages"] == request.messages
    assert kwargs["max_tokens"] == 123
    assert kwargs["system"] == request.system
    assert kwargs["tools"] == request.tools
    assert kwargs["thinking"] == {"type": "adaptive"}
    assert kwargs["output_config"] == {"effort": "high"}
    assert kwargs["extra_headers"] == {"x-test": "1"}


def test_anthropic_messages_transport_validates_message_shape():
    from brain.platform.integrations.transports.anthropic import AnthropicMessagesTransport
    from brain.platform.integrations.transports.base import LLMRequest, MessageValidationError

    request = LLMRequest(
        model="anthropic/claude-sonnet-4-6",
        messages=[{"role": "assistant", "content": [
            {"type": "tool_result", "tool_use_id": "tool_1", "content": "ok"},
        ]}],
    )

    with pytest.raises(MessageValidationError, match="assistant messages cannot contain tool_result"):
        AnthropicMessagesTransport().build_kwargs(request)


def test_openai_responses_transport_builds_responses_kwargs():
    from brain.platform.integrations.transports.base import LLMRequest
    from brain.platform.integrations.transports.openai_responses import OpenAIResponsesTransport

    request = LLMRequest(
        model="openai/gpt-5.4",
        messages=[{"role": "user", "content": "hello"}],
        max_output_tokens=456,
        system=[{"type": "text", "text": "Be precise."}],
        tools=[{
            "name": "brain_recall",
            "description": "Search memory",
            "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}},
        }],
        reasoning_effort="medium",
        cache_key="illo:" + ("x" * 90),
        response_format={"type": "json_schema", "name": "Decision", "schema": {"type": "object"}},
        extra_headers={"session_id": "session:" + ("y" * 90)},
    )

    kwargs = OpenAIResponsesTransport().build_kwargs(request)

    assert kwargs["model"] == "gpt-5.4"
    assert kwargs["input"] == [{"role": "user", "content": "hello"}]
    assert kwargs["instructions"] == "Be precise."
    assert kwargs["max_output_tokens"] == 456
    assert kwargs["store"] is False
    assert kwargs["tools"][0]["type"] == "function"
    assert kwargs["tools"][0]["name"] == "brain_recall"
    assert kwargs["reasoning"] == {"effort": "medium", "summary": "auto"}
    assert kwargs["include"] == ["reasoning.encrypted_content"]
    assert kwargs["text"]["format"]["name"] == "Decision"
    assert len(kwargs["prompt_cache_key"]) <= 64
    assert len(kwargs["extra_headers"]["session_id"]) <= 64


def test_openai_responses_transport_can_disable_reasoning_summaries(monkeypatch):
    from brain.platform.integrations.transports.base import LLMRequest
    from brain.platform.integrations.transports.openai_responses import OpenAIResponsesTransport

    monkeypatch.setenv("ILLO_OPENAI_REASONING_SUMMARY", "none")
    request = LLMRequest(
        model="openai/gpt-5.4",
        messages=[{"role": "user", "content": "hello"}],
        reasoning_effort="low",
    )

    kwargs = OpenAIResponsesTransport().build_kwargs(request)

    assert kwargs["reasoning"] == {"effort": "low"}


def test_openai_reasoning_event_extractors_separate_summary_from_raw():
    from brain.platform.integrations.transports.openai_responses import (
        _extract_openai_reasoning_summary_from_event,
        _extract_openai_reasoning_text_from_event,
    )

    assert _extract_openai_reasoning_summary_from_event({
        "type": "response.reasoning_summary_text.delta",
        "delta": "Checking the run state.",
    }) == "Checking the run state."
    assert _extract_openai_reasoning_summary_from_event({
        "type": "response.reasoning_text.delta",
        "delta": "raw private reasoning",
    }) == ""
    assert _extract_openai_reasoning_summary_from_event({
        "type": "response.output_item.done",
        "item": {
            "type": "reasoning",
            "summary": [{"type": "summary_text", "text": "Public summary."}],
        },
    }) == "Public summary."
    assert _extract_openai_reasoning_summary_from_event({
        "type": "response.reasoning_summary_part.added",
        "part": {"type": "summary_text", "text": ""},
    }) == ""
    assert _extract_openai_reasoning_summary_from_event({
        "type": "response.reasoning_summary_part.done",
        "part": {"type": "summary_text", "text": "Assessing project changes."},
    }) == "Assessing project changes."
    assert _extract_openai_reasoning_text_from_event({
        "type": "response.reasoning_text.delta",
        "delta": "raw private reasoning",
    }) == "raw private reasoning"


def test_openai_responses_transport_converts_typed_tool_blocks():
    from brain.platform.integrations.transports.base import (
        LLMRequest,
        ToolResultContentBlock,
        ToolUseContentBlock,
    )
    from brain.platform.integrations.transports.openai_responses import OpenAIResponsesTransport

    request = LLMRequest(
        model="openai/gpt-5.4",
        messages=[
            {"role": "assistant", "content": [
                ToolUseContentBlock(id="call_1", name="brain_recall", input={"query": "memory"}),
            ]},
            {"role": "user", "content": [
                ToolResultContentBlock(tool_use_id="call_1", content={"memories": []}),
            ]},
        ],
    )

    kwargs = OpenAIResponsesTransport().build_kwargs(request)

    assert kwargs["input"] == [
        {
            "type": "function_call",
            "call_id": "call_1",
            "name": "brain_recall",
            "arguments": '{"query": "memory"}',
        },
        {
            "type": "function_call_output",
            "call_id": "call_1",
            "output": '{"memories": []}',
        },
    ]


def test_openai_responses_transport_converts_user_images():
    from brain.platform.integrations.transports.base import ImageContentBlock, LLMRequest, TextContentBlock
    from brain.platform.integrations.transports.openai_responses import OpenAIResponsesTransport

    request = LLMRequest(
        model="openai/gpt-5.4",
        messages=[
            {"role": "user", "content": [
                TextContentBlock("What is in this image?"),
                ImageContentBlock(source={"type": "base64", "media_type": "image/png", "data": "abc123"}),
            ]},
        ],
    )

    kwargs = OpenAIResponsesTransport().build_kwargs(request)

    assert kwargs["input"] == [{
        "role": "user",
        "content": [
            {"type": "input_text", "text": "What is in this image?"},
            {"type": "input_image", "image_url": "data:image/png;base64,abc123"},
        ],
    }]


def test_openai_responses_transport_validates_message_shape():
    from brain.platform.integrations.transports.base import LLMRequest, MessageValidationError
    from brain.platform.integrations.transports.openai_responses import OpenAIResponsesTransport

    request = LLMRequest(
        model="openai/gpt-5.4",
        messages=[{"role": "assistant", "content": [
            {"type": "tool_result", "tool_use_id": "tool_1", "content": "ok"},
        ]}],
    )

    with pytest.raises(MessageValidationError, match="assistant messages cannot contain tool_result"):
        OpenAIResponsesTransport().build_kwargs(request)


def test_openai_response_helpers_remain_available_from_facade():
    from brain.platform.integrations.providers import _openai_response_to_unified
    from brain.platform.integrations.transports.base import ContentBlockType, StopReason
    from brain.platform.integrations.transports.openai_responses import _openai_response_to_unified as transport_helper

    assert _openai_response_to_unified is transport_helper

    response = MagicMock()
    message_item = MagicMock()
    message_item.type = "message"
    message_item.role = "assistant"
    text_part = MagicMock()
    text_part.type = "output_text"
    text_part.text = "hello from transport"
    message_item.content = [text_part]
    response.output = [message_item]
    response.usage.input_tokens = 10
    response.usage.output_tokens = 4
    response.usage.input_tokens_details.cached_tokens = 2
    response.incomplete_details = None
    response.model = "gpt-5.4"

    unified = _openai_response_to_unified(response)

    assert unified.content[0].text == "hello from transport"
    assert unified.content[0].type == ContentBlockType.TEXT
    assert unified.stop_reason == StopReason.END_TURN
    assert unified.usage.cache_read_input_tokens == 2


def test_openai_response_tool_call_output_is_typed():
    from brain.platform.integrations.transports.base import ContentBlockType, StopReason
    from brain.platform.integrations.transports.openai_responses import _openai_response_to_unified

    response = {
        "output": [{
            "type": "function_call",
            "call_id": "call_123",
            "name": "brain_recall",
            "arguments": '{"query": "typed"}',
        }],
        "usage": {"input_tokens": 3, "output_tokens": 7},
        "incomplete_details": None,
        "model": "gpt-5.4",
    }

    unified = _openai_response_to_unified(response)

    assert unified.stop_reason == StopReason.TOOL_USE
    assert unified.content[0].type == ContentBlockType.TOOL_USE
    assert unified.content[0].id == "call_123"
    assert unified.content[0].name == "brain_recall"
    assert unified.content[0].input == {"query": "typed"}
