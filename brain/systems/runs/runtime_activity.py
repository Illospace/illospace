"""Activity reads backed by durable run ledgers."""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select

from brain.platform.db.models.agent_run import AgentRunEventRow
from brain.platform.db.repositories.unit_of_work import UnitOfWork
from brain.systems.runs.token_usage import async_summarize_run_usage
from brain.systems.runs.tool_event_read_model import tool_call_summary


def cumulative_run_budget_tokens(usage: dict | None) -> int:
    """Return the cache-aware token volume used by the cumulative run budget.

    Cached reads count because repeatedly sending a large cached context is the
    failure mode this budget detects. Cache writes count too: the usage ledger
    reports those processed prompt tokens separately from ``tokens_total``.
    """

    usage = usage or {}
    return sum(
        max(0, int(usage.get(field) or 0))
        for field in ("tokens_total", "cache_read", "cache_write")
    )


async def load_run_activity(run_id: int) -> dict[str, Any]:
    """Read current-run token metrics and spawned workers from durable ledgers."""

    async with UnitOfWork() as uow:
        usage = await async_summarize_run_usage(uow.session, run_id)
        workers_spawned = await uow.session.scalar(
            select(func.count(AgentRunEventRow.id)).where(
                AgentRunEventRow.run_id == run_id,
                AgentRunEventRow.event_type == "run.worker_spawned",
            )
        )
        write_activity = await tool_call_summary(uow.session, run_id)
    return {
        "tokens_used": int((usage or {}).get("tokens_total") or 0),
        "run_budget_tokens_used": cumulative_run_budget_tokens(usage),
        "workers_spawned": int(workers_spawned or 0),
        "last_write_tool_call_at": write_activity.get("last_write_tool_call_at"),
        "seconds_since_last_write_tool_call": write_activity.get(
            "seconds_since_last_write_tool_call"
        ),
    }


__all__ = ["cumulative_run_budget_tokens", "load_run_activity"]
