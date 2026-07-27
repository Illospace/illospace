"""Activity reads backed by durable run ledgers."""

from __future__ import annotations

from sqlalchemy import func, select

from brain.platform.db.models.agent_run import AgentRunEventRow
from brain.platform.db.repositories.unit_of_work import UnitOfWork
from brain.systems.runs.token_usage import async_summarize_run_usage


async def load_run_activity(run_id: int) -> dict[str, int]:
    """Read current-run tokens and spawned-worker count from durable ledgers."""

    async with UnitOfWork() as uow:
        usage = await async_summarize_run_usage(uow.session, run_id)
        workers_spawned = await uow.session.scalar(
            select(func.count(AgentRunEventRow.id)).where(
                AgentRunEventRow.run_id == run_id,
                AgentRunEventRow.event_type == "run.worker_spawned",
            )
        )
    return {
        "tokens_used": int((usage or {}).get("tokens_total") or 0),
        "workers_spawned": int(workers_spawned or 0),
    }


__all__ = ["load_run_activity"]
