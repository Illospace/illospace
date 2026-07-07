"""Loop-control policy for the agent runtime."""

from __future__ import annotations

import logging

logger = logging.getLogger("agent")

_STUCK_WARN_THRESHOLD = 3
_STUCK_BREAK_THRESHOLD = 5


def _detect_stuck_loop(
    recent_calls: list[str],
    session_id: str,
    messages: list,
    *,
    warn_threshold: int = _STUCK_WARN_THRESHOLD,
    break_threshold: int = _STUCK_BREAK_THRESHOLD,
) -> bool:
    """Check if the agent is stuck. Returns True if the loop should break."""

    if len(recent_calls) < warn_threshold:
        return False
    tail = recent_calls[-warn_threshold:]
    if len(set(tail)) != 1:
        return False
    repeat_count = sum(1 for fp in reversed(recent_calls) if fp == tail[0])
    if repeat_count >= break_threshold:
        logger.warning("Agent %s: stuck loop (%d identical calls). Breaking.", session_id, repeat_count)
        messages.append({"role": "user", "content": [
            {"type": "text", "text": "[System: Agent terminated: stuck in a loop repeating the same tool call]"},
        ]})
        return True
    return False


def _inject_nudges(
    recent_calls: list[str],
    *,
    warn_threshold: int = _STUCK_WARN_THRESHOLD,
) -> dict | None:
    """Return a standalone reminder message, or None if nothing to say.

    Strategy is the model's job — the harness does not second-guess it (no
    "try a different strategy" / "consolidate into a script" / "is this reply
    adding new information" nudges). The only thing worth surfacing here is a
    durable, non-obvious mechanical fact that trips models up repeatedly.
    The caller appends the returned message AFTER the tool_results message —
    it must never be spliced into the tool_results content array.
    """

    if (
        len(recent_calls) >= warn_threshold
        and len(set(recent_calls[-warn_threshold:])) == 1
    ):
        return {
            "role": "user",
            "content": (
                "[System: NOTE: `cd` does not persist between exec_command calls; "
                "use `working_dir` or absolute paths.]"
            ),
        }
    return None
