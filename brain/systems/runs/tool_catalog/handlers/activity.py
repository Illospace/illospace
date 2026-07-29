"""Agent activity and runtime annotation tool handlers."""

from __future__ import annotations

import logging
import time

from brain.systems.runs.execution_artifacts import load_execution_artifacts
from brain.systems.runs.execution_context import _agent_context
from brain.systems.runs.runtime_activity import load_run_activity

logger = logging.getLogger("agent")


async def _handle_my_activity() -> dict:
    """Return the current agent's own activity trace for self-assessment."""
    run_id = getattr(_agent_context, "run_id", None)
    start_time = getattr(_agent_context, "start_time", None)
    reply_contents = getattr(_agent_context, "reply_contents", [])
    tool_calls_log = getattr(_agent_context, "tool_calls_log", [])
    execution_artifacts = getattr(_agent_context, "execution_artifacts", [])

    elapsed = int(time.time() - start_time) if start_time else 0
    tool_counts: dict[str, int] = {}
    for name in tool_calls_log:
        tool_counts[name] = tool_counts.get(name, 0) + 1

    replies_summary = [
        f"Reply {index}: {content[:200]}{'...' if len(content) > 200 else ''}"
        for index, content in enumerate(reply_contents, 1)
    ]

    result: dict = {
        "elapsed_sec": elapsed,
        "tool_calls": tool_counts,
        "total_tool_calls": len(tool_calls_log),
        "cortex_replies_staged": len(reply_contents),
    }
    if replies_summary:
        result["reply_summaries"] = replies_summary
    if execution_artifacts:
        result["execution_artifacts"] = execution_artifacts
    if run_id is not None:
        try:
            result.update(await load_run_activity(int(run_id)))
        except Exception:
            logger.warning(
                "Failed to load durable run activity for my_activity",
                extra={"run_id": run_id},
                exc_info=True,
            )
            result.update(
                tokens_used=0,
                run_budget_tokens_used=0,
                workers_spawned=0,
            )
    if not result.get("execution_artifacts"):
        try:
            execution_metadata = getattr(_agent_context, "execution_metadata", {}) or {}
            execution_id = execution_metadata.get("execution_id")
            if execution_id:
                persisted = await load_execution_artifacts(execution_id=execution_id)
                if persisted:
                    result["execution_artifacts"] = persisted
        except Exception:
            logger.debug("Failed to load execution artifacts for my_activity", exc_info=True)

    return result

__all__ = ["_handle_my_activity"]
