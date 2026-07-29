"""Provider retry helpers for the agent runtime."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Callable

from brain.platform.async_io import run_blocking
from brain.platform.integrations.provider_error_sentinel import (
    is_retryable_provider_error,
    provider_error_kind,
)
from brain.platform.integrations.providers import LLMRequest
from brain.systems.runs.tool_catalog.metadata import is_write_side_effect_class
from brain.systems.runs.tool_catalog.registry import get_tool_registration

logger = logging.getLogger("agent")
_MAX_RETRY_AFTER_SECONDS = 60.0


@dataclass(frozen=True, slots=True)
class ResponseTextRetryDecision:
    """Classification plus the replay-safety policy for one model response."""

    provider_error_kind: str | None
    should_retry: bool
    inspect_response: bool
    withhold_stream: bool
    spawned_worker: bool
    tool_history_safety: str


def _spawned_worker_invocation(
    metadata: Mapping[str, object],
    *,
    tool_call_source: str,
) -> bool:
    provenance = metadata.get("execution_provenance")
    if not isinstance(provenance, Mapping):
        provenance = {}
    return bool(
        tool_call_source == "worker"
        and (
            provenance.get("spawned_by_tool") is True
            or str(provenance.get("origin") or "") == "spawn_worker"
        )
    )


def _tool_history_safety(tool_calls_made: Sequence[str]) -> str:
    for tool_name in tool_calls_made:
        registration = get_tool_registration(tool_name)
        if registration is None:
            return "unknown"
        if is_write_side_effect_class(registration.side_effect_class):
            return "write"
    return "read_only"


def response_text_retry_decision(
    response_text: str,
    *,
    scheduled_result_contract: bool,
    metadata: Mapping[str, object],
    tool_call_source: str,
    tool_calls_made: Sequence[str],
) -> ResponseTextRetryDecision:
    """Decide provider-text handling from provenance, history, and text."""

    spawned_worker = _spawned_worker_invocation(
        metadata,
        tool_call_source=tool_call_source,
    )
    history_safety = _tool_history_safety(tool_calls_made)
    inspect_response = bool(scheduled_result_contract or spawned_worker)
    detected_kind = (
        provider_error_kind(response_text)
        if inspect_response and str(response_text or "").strip()
        else None
    )
    retry_safe = bool(
        scheduled_result_contract
        or (spawned_worker and history_safety == "read_only")
    )
    return ResponseTextRetryDecision(
        provider_error_kind=detected_kind,
        should_retry=bool(
            retry_safe and is_retryable_provider_error(detected_kind)
        ),
        inspect_response=inspect_response,
        withhold_stream=inspect_response,
        spawned_worker=spawned_worker,
        tool_history_safety=history_safety,
    )


class _NeverCancelled:
    def is_set(self) -> bool:
        return False


async def _async_delay(seconds: float) -> None:
    event = asyncio.Event()
    try:
        await asyncio.wait_for(event.wait(), timeout=max(0.0, float(seconds)))
    except TimeoutError:
        return


def _blocking_delay(seconds: float) -> None:
    threading.Event().wait(max(0.0, float(seconds)))


def _retry_after_seconds(headers: dict[str, str]) -> float | None:
    value = None
    for key, candidate in headers.items():
        if str(key).lower() == "retry-after":
            value = str(candidate).strip()
            break
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            seconds = (retry_at - datetime.now(timezone.utc)).total_seconds()
        except Exception:
            return None
    return max(0.0, min(seconds, _MAX_RETRY_AFTER_SECONDS))


def api_call_with_retry(
    provider,
    request: LLMRequest,
    llm,
    cancel_event,
    on_stream_activity,
    on_stream_delta,
    *,
    session_id: str,
    turn: int,
    tokens,
    start_time: float,
    tool_calls_made: list[str],
    call_start: float,
    retry_delays: tuple[int, ...],
    streaming_call: Callable,
    make_cancelled_result: Callable,
    degrade_betas: Callable[[], bool],
    is_cancelled_result: Callable[[object], bool] | None = None,
):
    """Make an API call with retry on provider retryable errors."""
    is_cancelled_result = is_cancelled_result or (lambda response: response.__class__.__name__ == "AgentResult")
    should_stream = bool(cancel_event or on_stream_activity or on_stream_delta)
    stream_cancel_event = cancel_event or _NeverCancelled()
    api_attempt = 0
    while True:
        try:
            if should_stream:
                response = streaming_call(
                    provider,
                    request,
                    stream_cancel_event,
                    on_stream_activity,
                    on_stream_delta,
                    session_id=session_id,
                    tokens=tokens,
                    start_time=start_time,
                    tool_calls_made=tool_calls_made,
                    call_start=call_start,
                    make_cancelled_result=make_cancelled_result,
                )
                if is_cancelled_result(response):
                    return response
            else:
                response = provider.create(request)
            return response
        except Exception as exc:
            if not provider.is_retryable_error(exc):
                raise
            resp = getattr(exc, "response", None)
            headers = dict(getattr(resp, "headers", {}) or {}) if resp else {}
            retry_after = _retry_after_seconds(headers)
            logger.warning(
                "Agent %s turn %d: provider retryable error (attempt %d/%d), req=%s",
                session_id,
                turn,
                api_attempt + 1,
                len(retry_delays) + 1,
                headers.get("x-request-id", "unknown"),
            )
            if api_attempt >= len(retry_delays):
                if llm.is_oauth and degrade_betas():
                    request = LLMRequest(
                        model=request.model,
                        messages=request.messages,
                        max_output_tokens=request.max_output_tokens,
                        system=request.system,
                        tools=request.tools,
                        reasoning_effort=request.reasoning_effort,
                        cache_key=request.cache_key,
                        cache_retention=request.cache_retention,
                        extra_headers=llm.build_request_headers(session_id=session_id),
                        operation_type=request.operation_type,
                    )
                    logger.info("Agent %s: degraded betas, retrying", session_id)
                    api_attempt = 0
                    call_start = time.time()
                    continue
                raise
            delay = retry_after if retry_after is not None else retry_delays[api_attempt]
            api_attempt += 1
            if on_stream_activity:
                on_stream_activity(f"API hiccup — retrying in {delay}s…")
            _blocking_delay(delay)
            call_start = time.time()


async def async_api_call_with_retry(
    provider,
    request: LLMRequest,
    llm,
    cancel_event,
    on_stream_activity,
    on_stream_delta,
    *,
    session_id: str,
    turn: int,
    tokens,
    start_time: float,
    tool_calls_made: list[str],
    call_start: float,
    retry_delays: tuple[int, ...],
    streaming_call: Callable,
    make_cancelled_result: Callable,
    degrade_betas: Callable[[], bool],
    is_cancelled_result: Callable[[object], bool] | None = None,
):
    """Make a provider call from async runtime code without blocking its event loop."""
    is_cancelled_result = is_cancelled_result or (lambda response: response.__class__.__name__ == "AgentResult")
    should_stream = bool(cancel_event or on_stream_activity or on_stream_delta)
    stream_cancel_event = cancel_event or _NeverCancelled()
    api_attempt = 0
    while True:
        try:
            if should_stream:
                response = await streaming_call(
                    provider,
                    request,
                    stream_cancel_event,
                    on_stream_activity,
                    on_stream_delta,
                    session_id=session_id,
                    tokens=tokens,
                    start_time=start_time,
                    tool_calls_made=tool_calls_made,
                    call_start=call_start,
                    make_cancelled_result=make_cancelled_result,
                )
                if is_cancelled_result(response):
                    return response
            else:
                response = await run_blocking(provider.create, request)
            return response
        except Exception as exc:
            if not provider.is_retryable_error(exc):
                raise
            resp = getattr(exc, "response", None)
            headers = dict(getattr(resp, "headers", {}) or {}) if resp else {}
            retry_after = _retry_after_seconds(headers)
            logger.warning(
                "Agent %s turn %d: provider retryable error (attempt %d/%d), req=%s",
                session_id,
                turn,
                api_attempt + 1,
                len(retry_delays) + 1,
                headers.get("x-request-id", "unknown"),
            )
            if api_attempt >= len(retry_delays):
                if llm.is_oauth and degrade_betas():
                    request = LLMRequest(
                        model=request.model,
                        messages=request.messages,
                        max_output_tokens=request.max_output_tokens,
                        system=request.system,
                        tools=request.tools,
                        reasoning_effort=request.reasoning_effort,
                        cache_key=request.cache_key,
                        cache_retention=request.cache_retention,
                        extra_headers=llm.build_request_headers(session_id=session_id),
                        operation_type=request.operation_type,
                    )
                    logger.info("Agent %s: degraded betas, retrying", session_id)
                    api_attempt = 0
                    call_start = time.time()
                    continue
                raise
            delay = retry_after if retry_after is not None else retry_delays[api_attempt]
            api_attempt += 1
            if on_stream_activity:
                result = on_stream_activity(f"API hiccup — retrying in {delay}s…")
                if hasattr(result, "__await__"):
                    await result
            await _async_delay(delay)
            call_start = time.time()
