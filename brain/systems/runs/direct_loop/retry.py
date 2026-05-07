"""Provider retry helpers for the agent runtime."""

from __future__ import annotations

import logging
import time
from typing import Callable

from brain.platform.integrations.providers import LLMRequest

logger = logging.getLogger("agent")


class _NeverCancelled:
    def is_set(self) -> bool:
        return False


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
            logger.warning(
                "Agent %s turn %d: API 500 (attempt %d/%d), req=%s",
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
            delay = retry_delays[api_attempt]
            api_attempt += 1
            if on_stream_activity:
                on_stream_activity(f"API hiccup — retrying in {delay}s…")
            time.sleep(delay)
            call_start = time.time()
