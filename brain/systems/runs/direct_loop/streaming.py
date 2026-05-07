"""Streaming helpers for agent provider calls."""

from __future__ import annotations

import time
from typing import Callable


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
