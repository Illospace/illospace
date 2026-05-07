"""Illo Brain — Multi-Provider LLM Abstraction.

Providers implement a shared runtime contract and render it into their
native SDK semantics. Anthropic still receives native `messages.*`
payloads, while OpenAI now uses the Responses API natively instead of a
chat-completions compatibility shim.
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from typing import Any, Iterator

import anthropic

from brain.platform.integrations.openai_codex_client import (
    OpenAICodexError,
    OpenAICodexRetryableError,
)
from brain.platform.integrations.transports.anthropic import AnthropicMessagesTransport
from brain.platform.integrations.transports.base import (
    ContentBlock,
    ContentBlockType,
    ImageContentBlock,
    LLMRequest,
    LLMResponse,
    MessageRole,
    MessageValidationError,
    Provider,
    ProviderMessage,
    StreamContext,
    StreamEvent,
    StopReason,
    TextContentBlock,
    ThinkingContentBlock,
    ToolResultContentBlock,
    ToolUseContentBlock,
    Usage,
    content_blocks_from_legacy,
    content_blocks_to_legacy,
    provider_messages_from_legacy,
    provider_messages_to_legacy,
    validate_provider_messages,
    validate_system_blocks,
    validate_tool_definitions,
)
from brain.platform.integrations.transports.openai_responses import (
    OpenAIResponsesTransport,
    _anthropic_messages_to_openai_input,
    _anthropic_tools_to_openai,
    _block_get,
    _coerce_text,
    _append_text_message,
    _extract_openai_text_blocks,
    _extract_openai_text_from_event,
    _extract_openai_reasoning_summary_from_event,
    _extract_openai_reasoning_text_from_event,
    _merge_streamed_output_into_response,
    _openai_output_excerpt,
    _openai_request_debug_summary,
    _openai_response_debug_dump,
    _openai_response_to_unified,
    _summarize_openai_output_shape,
    _system_blocks_to_instructions,
    _truncate_for_log,
    _usage_from_openai,
)
from brain.platform.provider_health import (
    record_provider_failure,
    record_provider_success,
)

logger = logging.getLogger("brain.platform.integrations.providers")


_RETRYABLE_OPENAI_STREAM_TERMS = (
    "429",
    "502",
    "503",
    "529",
    "overloaded",
    "rate limit",
    "rate_limit",
    "temporarily unavailable",
    "try again later",
    "timeout",
    "timed out",
)


def _openai_stream_error_message(error: Any) -> str:
    """Build a retry-classifiable message from an OpenAI streaming error."""
    parts: list[str] = []
    for key in ("message", "detail", "code", "type", "status"):
        value = _block_get(error, key, None)
        if value is not None:
            text = str(value).strip()
            if text:
                parts.append(text)
    return " | ".join(parts)


def _is_retryable_openai_error_message(message: str) -> bool:
    lowered = str(message or "").lower()
    return any(term in lowered for term in _RETRYABLE_OPENAI_STREAM_TERMS)


class _TrackedStreamContext:
    """Wrap provider-native streams so streaming calls update health once."""

    def __init__(
        self,
        wrapped: Any,
        *,
        operation_type: str | None,
        provider: str,
        model: str,
        started: float,
    ):
        self._wrapped = wrapped
        self._operation_type = operation_type
        self._provider = provider
        self._model = model
        self._started = started
        self._recorded = False

    def __enter__(self):
        try:
            return self._wrapped.__enter__()
        except Exception as exc:
            self._record_failure(exc)
            raise

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_val is not None:
            self._record_failure(exc_val)
        elif not self._recorded:
            self._record_success()
        return self._wrapped.__exit__(exc_type, exc_val, exc_tb)

    def _latency_ms(self) -> int:
        return int(round((time.perf_counter() - self._started) * 1000))

    def _record_success(self) -> None:
        if self._recorded:
            return
        self._recorded = True
        record_provider_success(
            operation_type=self._operation_type,
            provider=self._provider,
            model=self._model,
            latency_ms=self._latency_ms(),
        )

    def _record_failure(self, exc: Exception) -> None:
        if self._recorded:
            return
        self._recorded = True
        record_provider_failure(
            operation_type=self._operation_type,
            provider=self._provider,
            model=self._model,
            exc=exc,
            latency_ms=self._latency_ms(),
        )

    def __iter__(self):
        return iter(self._wrapped)

    def get_final_message(self):
        try:
            result = self._wrapped.get_final_message()
            self._record_success()
            return result
        except Exception as exc:
            self._record_failure(exc)
            raise

    def close(self):
        return self._wrapped.close()


# ── Abstract Provider ─────────────────────────────────────────────

class LLMProvider(ABC):
    """Abstract LLM provider interface."""

    @abstractmethod
    def create(self, request: LLMRequest) -> Any:
        """Non-streaming API call using the shared runtime contract."""
        ...

    @abstractmethod
    def stream(self, request: LLMRequest) -> Any:
        """Streaming API call. Returns a context manager yielding StreamEvents."""
        ...

    def is_retryable_error(self, exc: Exception) -> bool:
        """Whether this provider error should go through retry handling."""
        return False

    def is_api_error(self, exc: Exception) -> bool:
        """Whether this exception is an SDK/API error from this provider."""
        return False


# ── Anthropic Provider ────────────────────────────────────────────

class AnthropicProvider(LLMProvider):
    """Thin wrapper around an existing anthropic.Anthropic client.

    Passes through to the native SDK — no response translation needed
    since agent.py already speaks Anthropic natively.
    """

    def __init__(self, client):
        self._client = client
        self.transport = AnthropicMessagesTransport()

    @property
    def raw_client(self):
        """Access the underlying anthropic.Anthropic client for native calls."""
        return self._client

    def _build_kwargs(self, request: LLMRequest) -> dict[str, Any]:
        return self.transport.build_kwargs(request)

    def create(self, request: LLMRequest) -> Any:
        """Pass-through to client.messages.create(). Returns native Anthropic Message."""
        started = time.perf_counter()
        try:
            response = self._client.messages.create(**self._build_kwargs(request))
            record_provider_success(
                operation_type=request.operation_type,
                provider="anthropic",
                model=request.normalized_model,
                latency_ms=int(round((time.perf_counter() - started) * 1000)),
            )
            return response
        except Exception as exc:
            record_provider_failure(
                operation_type=request.operation_type,
                provider="anthropic",
                model=request.normalized_model,
                exc=exc,
                latency_ms=int(round((time.perf_counter() - started) * 1000)),
            )
            raise

    def stream(self, request: LLMRequest) -> Any:
        """Pass-through to client.messages.stream(). Returns native stream context."""
        started = time.perf_counter()
        try:
            stream = self._client.messages.stream(**self._build_kwargs(request))
        except Exception as exc:
            record_provider_failure(
                operation_type=request.operation_type,
                provider="anthropic",
                model=request.normalized_model,
                exc=exc,
                latency_ms=int(round((time.perf_counter() - started) * 1000)),
            )
            raise
        return _TrackedStreamContext(
            stream,
            operation_type=request.operation_type,
            provider="anthropic",
            model=request.normalized_model,
            started=started,
        )

    def is_retryable_error(self, exc: Exception) -> bool:
        return isinstance(exc, anthropic.InternalServerError)

    def is_api_error(self, exc: Exception) -> bool:
        return isinstance(exc, anthropic.APIError)


# ── OpenAI Provider ───────────────────────────────────────────────


def _is_unsupported_openai_param_error(exc: Exception, param_name: str) -> bool:
    message = str(exc).lower()
    return "unsupported parameter" in message and param_name.lower() in message


def _has_openai_reasoning_summary(kwargs: dict[str, Any]) -> bool:
    reasoning = kwargs.get("reasoning")
    return isinstance(reasoning, dict) and bool(reasoning.get("summary"))


def _is_unsupported_openai_reasoning_summary_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return (
        "reasoning.summary" in message
        or ("unsupported parameter" in message and "summary" in message)
        or ("reasoning" in message and "summary" in message and "unsupported" in message)
    )


def _without_openai_reasoning_summary(kwargs: dict[str, Any]) -> dict[str, Any]:
    retry_kwargs = dict(kwargs)
    reasoning = dict(retry_kwargs.get("reasoning") or {})
    reasoning.pop("summary", None)
    if reasoning:
        retry_kwargs["reasoning"] = reasoning
    else:
        retry_kwargs.pop("reasoning", None)
    return retry_kwargs


class OpenAIProvider(LLMProvider):
    """OpenAI provider using the native Responses API."""

    def __init__(self, client):
        self._client = client
        self.transport = OpenAIResponsesTransport()

    @property
    def raw_client(self):
        return self._client

    def _create_with_fallback(self, kwargs: dict[str, Any]) -> Any:
        retry_kwargs = dict(kwargs)
        for _attempt in range(3):
            try:
                return self._client.responses.create(**retry_kwargs)
            except Exception as exc:
                if (
                    _has_openai_reasoning_summary(retry_kwargs)
                    and _is_unsupported_openai_reasoning_summary_error(exc)
                ):
                    logger.info("Retrying OpenAI request without reasoning.summary after unsupported-parameter error")
                    retry_kwargs = _without_openai_reasoning_summary(retry_kwargs)
                    continue
                if (
                    "prompt_cache_retention" in retry_kwargs
                    and _is_unsupported_openai_param_error(exc, "prompt_cache_retention")
                ):
                    retry_kwargs = dict(retry_kwargs)
                    retry_kwargs.pop("prompt_cache_retention", None)
                    logger.info("Retrying OpenAI request without prompt_cache_retention after unsupported-parameter error")
                    continue
                raise
        return self._client.responses.create(**retry_kwargs)

    def _translate_request(self, request: LLMRequest) -> dict[str, Any]:
        return self.transport.build_kwargs(request)

    def create(self, request: LLMRequest) -> LLMResponse:
        with self.stream(request) as stream:
            return stream.get_final_message()

    def stream(self, request: LLMRequest) -> StreamContext:
        """Streaming call using Responses semantic events."""
        started = time.perf_counter()
        recorded = False

        def _latency_ms() -> int:
            return int(round((time.perf_counter() - started) * 1000))

        def _record_success_once() -> None:
            nonlocal recorded
            if recorded:
                return
            recorded = True
            record_provider_success(
                operation_type=request.operation_type,
                provider="openai",
                model=request.normalized_model,
                latency_ms=_latency_ms(),
            )

        def _record_failure_once(exc: Exception) -> None:
            nonlocal recorded
            if recorded:
                return
            recorded = True
            record_provider_failure(
                operation_type=request.operation_type,
                provider="openai",
                model=request.normalized_model,
                exc=exc,
                latency_ms=_latency_ms(),
            )

        stream_kwargs = self._translate_request(request)
        stream_kwargs["stream"] = True
        try:
            raw_stream = self._create_with_fallback(stream_kwargs)
        except Exception as exc:
            _record_failure_once(exc)
            raise

        final_response = None
        collected_text: list[str] = []
        event_type_counts: dict[str, int] = {}
        streamed_output_items: dict[int, Any] = {}

        def _event_generator() -> Iterator[StreamEvent]:
            nonlocal final_response

            try:
                for event in raw_stream:
                    event_type = _block_get(event, "type", "")
                    event_type_counts[event_type] = event_type_counts.get(event_type, 0) + 1
                    if event_type in {"response.output_item.added", "response.output_item.done"}:
                        output_index = _block_get(event, "output_index")
                        item = _block_get(event, "item", None)
                        if isinstance(output_index, int) and item is not None:
                            streamed_output_items[output_index] = item
                    if event_type == "response.completed":
                        final_response = _block_get(event, "response", None)
                        continue
                    if event_type == "error":
                        error = _block_get(event, "error", None)
                        message = _openai_stream_error_message(error) or "OpenAI streaming error"
                        if _is_retryable_openai_error_message(message):
                            raise OpenAICodexRetryableError(message)
                        raise RuntimeError(message)

                    reasoning_summary_delta = _extract_openai_reasoning_summary_from_event(event)
                    if reasoning_summary_delta:
                        yield StreamEvent(type="reflection", text=reasoning_summary_delta)
                        continue

                    reasoning_delta = _extract_openai_reasoning_text_from_event(event)
                    if reasoning_delta:
                        yield StreamEvent(type="thinking", thinking=reasoning_delta)
                        continue

                    delta = _extract_openai_text_from_event(event)
                    if delta:
                        # Some Codex-compatible streams send full text parts on *.done events,
                        # others send incremental deltas. Prefer exact deltas when present,
                        # otherwise emit the text part so the fallback path preserves content.
                        if event_type in {
                            "response.content_part.done",
                            "response.output_item.added",
                            "response.output_item.done",
                        } and collected_text:
                            joined = "".join(collected_text)
                            if delta == joined or delta in joined:
                                continue
                        if delta:
                            collected_text.append(delta)
                            yield StreamEvent(type="text", text=delta)
            except Exception as exc:
                _record_failure_once(exc)
                raise

        def _build_final() -> LLMResponse:
            try:
                if final_response is not None:
                    usage = _usage_from_openai(final_response)
                    final_output = _block_get(final_response, "output", []) or []
                    response_for_parse = final_response
                    if usage.output_tokens > 0 and not final_output and streamed_output_items:
                        response_for_parse = _merge_streamed_output_into_response(final_response, streamed_output_items)
                        final_output = _block_get(response_for_parse, "output", []) or []
                    if usage.output_tokens > 0 and not final_output:
                        logger.warning(
                            "OpenAI response completed with empty output array: request=%s stream_events=%s collected_text_chars=%d collected_text_excerpt=%s",
                            _openai_request_debug_summary(stream_kwargs),
                            event_type_counts,
                            len("".join(collected_text)),
                            _truncate_for_log("".join(collected_text)[:400], limit=500),
                        )
                    unified = _openai_response_to_unified(response_for_parse, request.model)
                    if not unified.content and collected_text:
                        logger.warning(
                            "OpenAI response completed without parseable content; using streamed text fallback: model=%s response_id=%s stream_events=%s collected_text_chars=%d",
                            request.model,
                            _block_get(final_response, "id", ""),
                            event_type_counts,
                            len("".join(collected_text)),
                        )
                        fallback_response = LLMResponse(
                            content=[ContentBlock(type="text", text="".join(collected_text))],
                            stop_reason=unified.stop_reason,
                            usage=unified.usage,
                            model=unified.model,
                        )
                        _record_success_once()
                        return fallback_response
                    _record_success_once()
                    return unified
                fallback = LLMResponse(
                    content=[ContentBlock(type="text", text="".join(collected_text))] if collected_text else [],
                    stop_reason="end_turn",
                    usage=Usage(),
                    model=request.model,
                )
                _record_success_once()
                return fallback
            except Exception as exc:
                _record_failure_once(exc)
                raise

        return StreamContext(_event_generator(), finalizer=_build_final)

    def is_retryable_error(self, exc: Exception) -> bool:
        return (
            exc.__class__.__module__.startswith("openai")
            and exc.__class__.__name__ == "InternalServerError"
        ) or isinstance(exc, OpenAICodexRetryableError) or _is_retryable_openai_error_message(str(exc))

    def is_api_error(self, exc: Exception) -> bool:
        return exc.__class__.__module__.startswith("openai") or isinstance(exc, OpenAICodexError)


# ── Provider Registry ─────────────────────────────────────────────

_PROVIDERS: dict[str, type[LLMProvider]] = {
    "anthropic": AnthropicProvider,
    "openai": OpenAIProvider,
}


def get_provider(name: str, client: Any) -> LLMProvider:
    """Create a provider instance wrapping the given client.

    Args:
        name: Provider name ("anthropic" or "openai")
        client: The SDK client (anthropic.Anthropic or openai.OpenAI)

    Returns:
        LLMProvider instance
    """
    cls = _PROVIDERS.get(name)
    if cls is None:
        raise ValueError(f"Unknown provider '{name}'. Available: {list(_PROVIDERS.keys())}")
    return cls(client)


def get_active_provider() -> str:
    """Get the currently active provider name.

    Resolution order:
    1. ILLO_LLM_PROVIDER env var
    2. Default: "openai"
    """
    import os
    return os.environ.get("ILLO_LLM_PROVIDER", "openai")
