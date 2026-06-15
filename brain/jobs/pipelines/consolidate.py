#!/usr/bin/env python3
"""Reconstructive memory consolidation entrypoint.

Reconstructive memory keeps sources, spans, nodes, edges, assertions, and
reconstruction traces in one graph. This job records a consolidation pass for
operators without manufacturing derived memory rows.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import date, datetime, timedelta

from sqlalchemy import func, select, text

from brain.platform.db.models.reconstructive_memory import MemoryNode
from brain.platform.db.repositories.unit_of_work import UnitOfWork


async def phase_consolidation(target_date: date, org_id: str | None = None) -> dict:
    """Record a completed reconstructive-memory consolidation pass."""
    async with UnitOfWork() as uow:
        result = await uow.session.execute(
            text("""
                INSERT INTO consolidation_runs (
                    run_date, phase, org_id, status, completed_at,
                    memories_created, edges_created, memories_decayed, summary
                )
                VALUES (
                    :run_date, 'reconstructive_consolidation', :org_id, 'completed', NOW(),
                    0, 0, 0, :summary
                )
                RETURNING id
            """),
            {
                "run_date": target_date,
                "org_id": org_id,
                "summary": "Reconstructive graph is source-backed; no derived memory rows were manufactured.",
            },
        )
        run_id = result.mappings().first()["id"]
        active_nodes = await uow.session.scalar(
            select(func.count(MemoryNode.id)).where(MemoryNode.archived_at.is_(None))
        ) or 0
    return {
        "run_id": run_id,
        "phase": "reconstructive_consolidation",
        "active_memory_nodes": int(active_nodes),
        "memory_system": "reconstructive",
    }


async def phase_reflection(target_date: date, org_id: str | None = None) -> dict:
    del target_date, org_id
    return {"phase": "reflection", "retired": True, "memory_system": "reconstructive"}


async def phase_synthesis(target_date: date, org_id: str | None = None) -> dict:
    del target_date, org_id
    return {"phase": "synthesis", "retired": True, "memory_system": "reconstructive"}


async def generate_index(path: str | None = None) -> dict:
    del path
    async with UnitOfWork() as uow:
        rows = (
            await uow.session.execute(
                select(MemoryNode.content_kind, func.count(MemoryNode.id))
                .where(MemoryNode.archived_at.is_(None))
                .group_by(MemoryNode.content_kind)
            )
        ).all()
    return {
        "memory_system": "reconstructive",
        "nodes_by_kind": {str(kind or "unknown"): int(count) for kind, count in rows},
    }


async def _async_main(args) -> None:
    target = (
        datetime.strptime(args.date, "%Y-%m-%d").date()
        if args.date
        else (datetime.now() - timedelta(days=1)).date()
    )
    results = []
    if args.phase in ("all", "consolidate"):
        results.append(await phase_consolidation(target, org_id=args.org_id))
    if args.phase in ("all", "reflect"):
        results.append(await phase_reflection(target, org_id=args.org_id))
    if args.phase in ("all", "synthesize"):
        results.append(await phase_synthesis(target, org_id=args.org_id))
    if args.phase in ("all", "index"):
        results.append(await generate_index())
    for result in results:
        print(result)


def main() -> None:
    parser = argparse.ArgumentParser(description="Reconstructive memory consolidation")
    parser.add_argument("--phase", default="all", choices=["all", "consolidate", "reflect", "synthesize", "index"])
    parser.add_argument("--date", help="Target date (YYYY-MM-DD), default yesterday")
    parser.add_argument("--org-id")
    parser.add_argument("--user-id")
    parser.add_argument("--all-users", action="store_true")
    args = parser.parse_args()
    asyncio.run(_async_main(args))


if __name__ == "__main__":
    main()
