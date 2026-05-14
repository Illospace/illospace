"""Provider retry helpers for the agent runtime."""

from __future__ import annotations

import logging
import threading
import time
import asyncio
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Callable

from brain.platform.integrations.providers import LLMRequest
from brain.platform.async_io import run_blocking

logger = logging.getLogger("agent")
_MAX_RETRY_AFTER_SECONDS = 60.0


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
