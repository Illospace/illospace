"""Provider-neutral runtime types and transport protocol."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any, Protocol

from brain.platform.integrations.transports.messages import (
    ContentBlock,
    ContentBlockType,
    ImageContentBlock,
    MessageRole,
    MessageValidationError,
    Provider,
    ProviderMessage,
    StopReason,
    TextContentBlock,
    ThinkingContentBlock,
    ToolResultContentBlock,
    ToolUseContentBlock,
    content_blocks_from_legacy,
    content_blocks_to_legacy,
    normalize_stop_reason,
    provider_messages_from_legacy,
    provider_messages_to_legacy,
    validate_provider_messages,
    validate_system_blocks,
    validate_tool_definitions,
)


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


@dataclass
class LLMResponse:
    """Unified response matching Anthropic's Message shape."""

    content: list[ContentBlock]
    stop_reason: StopReason | str
    usage: Usage
    model: str = ""

    def __post_init__(self) -> None:
        self.content = content_blocks_from_legacy(self.content)
        self.stop_reason = normalize_stop_reason(self.stop_reason)


@dataclass
class StreamEvent:
    """A streaming event for progress tracking."""

    type: str
    text: str | None = None
    thinking: str | None = None


@dataclass(frozen=True)
class LLMRequest:
    """Provider-neutral runtime request built by the agent loop."""

    model: str
    messages: list[dict]
    max_output_tokens: int | None = None
    system: list[dict] | str | None = None
    tools: list[dict] | None = None
    reasoning_effort: str | None = None
    cache_key: str | None = None
    cache_retention: str | None = None
    extra_headers: dict[str, str] | None = None
    response_format: dict[str, Any] | None = None
    operation_type: str | None = None

    @property
    def normalized_model(self) -> str:
        """Strip provider prefixes for SDK calls."""
        for prefix in ("anthropic/", "openai/", "anthropic:", "openai:"):
            if self.model.startswith(prefix):
                return self.model[len(prefix):]
        return self.model


class StreamContext:
    """Context manager for streaming responses. Mimics anthropic's stream interface."""

    def __init__(
        self,
        events_iter: Iterator[StreamEvent],
        final_message: LLMResponse | None = None,
        finalizer: Callable[[], LLMResponse] | None = None,
    ):
        self._events = events_iter
        self._final = final_message
        self._finalizer = finalizer

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def __iter__(self):
        return self._events

    def get_final_message(self) -> LLMResponse:
        for _ in self._events:
            pass
        if self._final is None and self._finalizer is not None:
            self._final = self._finalizer()
        return self._final

    def close(self):
        pass


class ProviderTransport(Protocol):
    """Protocol for translating provider-neutral requests to SDK kwargs."""

    def build_kwargs(self, request: LLMRequest) -> dict[str, Any]:
        """Build provider-native SDK kwargs."""
        ...
