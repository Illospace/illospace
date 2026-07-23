"""Read the canonical worker-swap snapshot from the application database."""

from __future__ import annotations

import asyncio

from sqlalchemy import select

from brain.contracts.statuses import OPEN_RUN_STATUS_VALUES
from brain.contracts.worker_swap import WorkerSwapSnapshot, worker_swap_snapshot
from brain.platform.db.models.agent_run import AgentRunRow
from brain.platform.db.repositories.unit_of_work import UnitOfWork


async def read_worker_swap_snapshot() -> WorkerSwapSnapshot:
    async with UnitOfWork() as uow:
        rows = (
            await uow.session.execute(
                select(AgentRunRow.id, AgentRunRow.status)
                .where(AgentRunRow.status.in_(OPEN_RUN_STATUS_VALUES))
                .order_by(AgentRunRow.id.asc())
            )
        ).all()
    return worker_swap_snapshot(rows)


def main() -> None:
    print(asyncio.run(read_worker_swap_snapshot()).as_json())


if __name__ == "__main__":
    main()
