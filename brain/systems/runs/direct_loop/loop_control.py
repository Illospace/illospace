"""Loop-control policy for the agent runtime."""

from __future__ import annotations

import json
import logging

logger = logging.getLogger("agent")

_STUCK_WARN_THRESHOLD = 3
_STUCK_BREAK_THRESHOLD = 5
_CONSOLIDATION_TOOLS = {"exec_command", "search_files", "read_file", "list_files"}


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
        messages.append({"role": "assistant", "content": [
            {"type": "text", "text": "[Agent terminated: stuck in a loop repeating the same tool call]"},
        ]})
        return True
    return False


def _inject_nudges(
    tool_results: list,
    recent_calls: list[str],
    response,
    session_id: str,
    *,
    agent_context=None,
    warn_threshold: int = _STUCK_WARN_THRESHOLD,
    consolidation_tools: set[str] | frozenset[str] = _CONSOLIDATION_TOOLS,
) -> None:
    """Inject stuck warnings, consolidation nudges, and progress checks."""

    if agent_context is None:
        from brain.systems.runs.tool_handlers import _agent_context as agent_context

    # Stuck warning (not yet at break threshold)
    if (
        len(recent_calls) >= warn_threshold
        and len(set(recent_calls[-warn_threshold:])) == 1
    ):
        tool_results.append({
            "type": "text",
            "text": (
                "[System: You are repeating the same tool call. Try a different strategy. "
                "NOTE: `cd` does not persist between exec_command calls; use `working_dir` or absolute paths.]"
            ),
        })

    # Consolidation nudge (3+ consecutive same tool)
    if len(recent_calls) >= 3:
        names = [fp.split(":", 1)[0] for fp in recent_calls[-3:]]
        if (
            len(set(names)) == 1
            and names[0] in consolidation_tools
            and not (
                len(recent_calls) >= warn_threshold
                and len(set(recent_calls[-warn_threshold:])) == 1
            )
        ):
            tool_results.append({
                "type": "text",
                "text": (
                    f"[System: {len(names)} consecutive {names[0]} calls — consider "
                    "writing a single script via `run_script` instead.]"
                ),
            })

    # Progress injection after cortex_reply
    reply_contents = getattr(agent_context, "reply_contents", [])
    reply_names = [block.name for block in response.content if hasattr(block, "name")]
    if "cortex_reply" in reply_names and len(reply_contents) > 1:
        tool_log = getattr(agent_context, "tool_calls_log", [])
        tool_counts: dict[str, int] = {}
        for tool_name in tool_log:
            tool_counts[tool_name] = tool_counts.get(tool_name, 0) + 1
        previous_replies = [
            f"  Reply {index}: {content[:150]}…"
            for index, content in enumerate(reply_contents[:-1], 1)
        ]
        tool_results.append({
            "type": "text",
            "text": (
                f"[System: {len(reply_contents)} replies staged. Tools: {json.dumps(tool_counts)}.\n"
                f"Previous:\n" + "\n".join(previous_replies) + "\n"
                "Is your latest reply adding NEW information? If done, end your turn.]"
            ),
        })
