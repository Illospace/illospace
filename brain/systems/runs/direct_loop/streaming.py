"""Streaming helpers for agent provider calls."""

from __future__ import annotations

import asyncio
import inspect
import time
from typing import Callable

from brain.platform.async_io import run_blocking


def _public_reflection_excerpt(text: str, *, limit: int = 140) -> str:
    """Compact a provider-supplied reasoning summary for live activity."""
    cleaned = " ".join((text or "").split())
    if len(cleaned) <= limit:
        return cleaned
    shortened = cleaned[:limit].rsplit(" ", 1)[0].rstrip(".,;:")
    return f"{shortened}…"


def _approx_token_count(chars: int) -> int:
    if chars <= 0:
        return 0
    return max(1, chars // 4)


def _token_word(count: int) -> str:
    return "token" if count == 1 else "tokens"


def streaming_call(
    provider,
    request,
    cancel_event,
    on_stream_activity,
    on_stream_delta=None,
    *,
    session_id: str,
    tokens,
    start_time: float,
    tool_calls_made: list[str],
    call_start: float,
    make_cancelled_result: Callable,
):
    """Handle streaming API calls with cancellation support."""
    with provider.stream(request) as stream:
        last_progress = time.time()
        last_activity_label = ""
        thinking_chars = 0
        text_chars = 0
        reflection_text = ""
        phase = "waiting"
        for event in stream:
            if cancel_event.is_set():
                try:
                    stream.close()
                except Exception:
                    pass
                return make_cancelled_result(
                    "",
                    False,
                    session_id,
                    tokens,
                    start_time,
                    tool_calls_made,
                    error="Cancelled by runner",
                )
            event_type = getattr(event, "type", "")
            if event_type == "thinking":
                thinking_chars += len(getattr(event, "thinking", "") or "")
                phase = "thinking"
            elif event_type == "reflection":
                reflection_text += getattr(event, "text", "") or ""
                phase = "reflection"
            elif event_type == "text":
                text = getattr(event, "text", "") or ""
                text_chars += len(text)
                if text and on_stream_delta:
                    try:
                        on_stream_delta(text)
                    except Exception:
                        pass
                phase = "writing"
            now = time.time()
            if on_stream_activity and now - last_progress >= 3:
                last_progress = now
                label = ""
                if phase == "reflection":
                    excerpt = _public_reflection_excerpt(reflection_text)
                    label = excerpt
                elif phase == "thinking":
                    elapsed = int(now - call_start)
                    thinking_tokens = _approx_token_count(thinking_chars)
                    suffix = (
                        f", ~{thinking_tokens} internal {_token_word(thinking_tokens)}"
                        if thinking_tokens
                        else ""
                    )
                    label = f"Thinking through the request… ({elapsed}s{suffix})"
                elif phase == "writing":
                    output_tokens = _approx_token_count(text_chars)
                    label = f"Writing response… (~{output_tokens} output {_token_word(output_tokens)})"
                else:
                    label = f"Streaming… ({int(now - call_start)}s)"
                if label and label != last_activity_label:
                    last_activity_label = label
                    on_stream_activity(label)
        return stream.get_final_message()


class _LoopCancelProxy:
    def __init__(self, loop: asyncio.AbstractEventLoop, cancel_event):
        self._loop = loop
        self._cancel_event = cancel_event

    async def _check(self) -> bool:
        checker = getattr(self._cancel_event, "a_is_set", None)
        if checker is None:
            checker = getattr(self._cancel_event, "is_set", None)
        if checker is None:
            return False
        result = checker()
        if inspect.isawaitable(result):
            result = await result
        return bool(result)

    def is_set(self) -> bool:
        future = asyncio.run_coroutine_threadsafe(self._check(), self._loop)
        return bool(future.result())


def _loop_callback(loop: asyncio.AbstractEventLoop, callback):
    if callback is None:
        return None

    def _call(*args, **kwargs):
        result = callback(*args, **kwargs)
        if inspect.isawaitable(result):
            return asyncio.run_coroutine_threadsafe(result, loop).result()
        return result

    return _call


async def async_streaming_call(
    provider,
    request,
    cancel_event,
    on_stream_activity,
    on_stream_delta=None,
    *,
    session_id: str,
    tokens,
    start_time: float,
    tool_calls_made: list[str],
    call_start: float,
    make_cancelled_result: Callable,
):
    """Run the provider's sync streaming SDK at an explicit async boundary."""
    loop = asyncio.get_running_loop()
    return await run_blocking(
        streaming_call,
        provider,
        request,
        _LoopCancelProxy(loop, cancel_event),
        _loop_callback(loop, on_stream_activity),
        _loop_callback(loop, on_stream_delta),
        session_id=session_id,
        tokens=tokens,
        start_time=start_time,
        tool_calls_made=tool_calls_made,
        call_start=call_start,
        make_cancelled_result=make_cancelled_result,
    )
