"""Anthropic Messages transport formatting."""

from __future__ import annotations

from typing import Any

from brain.platform.integrations.transports.base import (
    LLMRequest,
    Provider,
    validate_provider_messages,
    validate_system_blocks,
    validate_tool_definitions,
)


class AnthropicMessagesTransport:
    """Build Anthropic Messages SDK kwargs from an LLMRequest."""

    def build_kwargs(self, request: LLMRequest) -> dict[str, Any]:
        validate_provider_messages(request.messages, provider=Provider.ANTHROPIC)
        validate_system_blocks(request.system)
        validate_tool_definitions(request.tools)

        kwargs: dict[str, Any] = {
            "model": request.normalized_model,
            "messages": request.messages,
        }
        if request.max_output_tokens is not None:
            kwargs["max_tokens"] = request.max_output_tokens
        if request.system:
            kwargs["system"] = request.system
        if request.tools:
            kwargs["tools"] = request.tools
        if request.reasoning_effort and request.reasoning_effort != "none":
            kwargs["thinking"] = {"type": "adaptive"}
            kwargs["output_config"] = {"effort": request.reasoning_effort}
        if request.extra_headers:
            kwargs["extra_headers"] = dict(request.extra_headers)
        return kwargs
