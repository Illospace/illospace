#!/usr/bin/env python3
"""Move quiet emerged thoughts from the live canvas into history."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from brain.platform.db.models.idea import Idea
from brain.platform.db.repositories.unit_of_work import UnitOfWork
from brain.systems.cortex.thought_lifecycle import ThoughtStatusCommand, transition_thought_status

log = logging.getLogger(__name__)

CANVAS_QUIET_HOURS = 24
ARCHIVE_TRIGGER = "canvas_occupancy_24h_quiet"


async def archive_dormant_emerged(
    session: AsyncSession,
    *,
    now: datetime | None = None,
    quiet_hours: int = CANVAS_QUIET_HOURS,
    batch_size: int = 250,
) -> int:
    """Archive emerged thoughts after a bounded quiet period.

    The canonical lifecycle service sets ``archived_at`` and records the
    transition in ``idea_state_log``. Other states are never changed here.
    """
    changed_at = now or datetime.now(timezone.utc)
    cutoff = changed_at - timedelta(hours=quiet_hours)
    archived = 0

    while True:
        ideas = list(
            (
                await session.scalars(
                    select(Idea)
                    .where(
                        Idea.status == "emerged",
                        Idea.archived_at.is_(None),
                        Idea.updated_at < cutoff,
                    )
                    .order_by(Idea.updated_at, Idea.id)
                    .limit(batch_size)
                    .with_for_update(skip_locked=True)
                )
            ).all()
        )
        if not ideas:
            break

        for idea in ideas:
            await transition_thought_status(
                session,
                idea=idea,
                command=ThoughtStatusCommand(
                    to_status="archived",
                    trigger=ARCHIVE_TRIGGER,
                    changed_at=changed_at,
                ),
            )
        archived += len(ideas)

    return archived


async def run() -> dict[str, int]:
    async with UnitOfWork() as uow:
        archived = await archive_dormant_emerged(uow.session)  # type: ignore[arg-type]
    result = {"archived": archived, "quiet_hours": CANVAS_QUIET_HOURS}
    log.info("Cortex occupancy maintenance complete: %s", result)
    return result


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [cortex_occupancy] %(message)s")
    print(json.dumps(asyncio.run(run()), sort_keys=True))


if __name__ == "__main__":
    main()
