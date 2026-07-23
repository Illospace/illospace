#!/usr/bin/env python3
"""Record the nightly reconstructive-memory health inventory."""
from __future__ import annotations

import argparse
import asyncio
import json
from datetime import date
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from brain.platform.db.models.reconstructive_memory import MemoryNode
from brain.platform.db.repositories.memory_health import MemoryHealthRepository
from brain.platform.db.repositories.unit_of_work import UnitOfWork

CHECK_TYPE = "reconstructive_inventory"


async def record_nightly_memory_health(
    session: AsyncSession,
    target_date: date,
    *,
    org_id: str | None = None,
) -> dict[str, Any]:
    """Persist one source-backed inventory check for the nightly run."""
    statement = select(func.count(MemoryNode.id)).where(
        MemoryNode.archived_at.is_(None)
    )
    if org_id is not None:
        statement = statement.where(MemoryNode.org_id == org_id)
    active_memory_nodes = int(await session.scalar(statement) or 0)
    details = {
        "target_date": target_date.isoformat(),
        "active_memory_nodes": active_memory_nodes,
        "memory_system": "reconstructive",
    }
    entry = await MemoryHealthRepository(session).a_log_check(
        CHECK_TYPE,
        "ok",
        details,
        org_id=org_id,
    )
    return {
        "id": entry.id,
        "check_type": entry.check_type,
        "status": entry.status,
        "details": details,
    }


async def run_nightly_memory_health(
    target_date: date,
    *,
    org_id: str | None = None,
) -> dict[str, Any]:
    async with UnitOfWork() as uow:
        return await record_nightly_memory_health(
            uow.session,
            target_date,
            org_id=org_id,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", type=date.fromisoformat, default=date.today())
    parser.add_argument("--org-id")
    args = parser.parse_args()
    result = asyncio.run(
        run_nightly_memory_health(args.date, org_id=args.org_id)
    )
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
