"""Durable scheduler-global state for overdue-freeze alert deduplication."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from brain.platform.db.models.scheduler import SchedulerAlertLatch


SCHEDULER_OVERDUE_FREEZE_ALERT_KEY = "scheduler_overdue_freeze"


def _alert_latch_insert(session: AsyncSession):
    if session.get_bind().dialect.name == "sqlite":
        return sqlite_insert(SchedulerAlertLatch)
    return postgresql_insert(SchedulerAlertLatch)


async def try_claim_scheduler_alert(
    session: AsyncSession,
    *,
    alert_key: str,
    alerted_at: datetime,
) -> bool:
    """Atomically claim one alert key without committing the caller's session."""
    claim = (
        _alert_latch_insert(session)
        .values(alert_key=alert_key, alerted_at=alerted_at)
        .on_conflict_do_nothing(index_elements=[SchedulerAlertLatch.alert_key])
        .returning(SchedulerAlertLatch.alert_key)
    )
    result = await session.execute(claim)
    return result.scalar_one_or_none() == alert_key


async def release_scheduler_alert(
    session: AsyncSession,
    *,
    alert_key: str,
) -> None:
    """Release one alert key without committing the caller's session."""
    await session.execute(
        delete(SchedulerAlertLatch).where(
            SchedulerAlertLatch.alert_key == alert_key
        )
    )


async def claim_scheduler_overdue_alert(*, alerted_at: datetime) -> bool:
    """Claim and commit the overdue-freeze alert in one short transaction."""
    from brain.platform.db.repositories.unit_of_work import UnitOfWork

    async with UnitOfWork() as uow:
        return await try_claim_scheduler_alert(
            uow.session,
            alert_key=SCHEDULER_OVERDUE_FREEZE_ALERT_KEY,
            alerted_at=alerted_at,
        )


async def release_scheduler_overdue_alert() -> None:
    """Release and commit the overdue-freeze alert in one short transaction."""
    from brain.platform.db.repositories.unit_of_work import UnitOfWork

    async with UnitOfWork() as uow:
        await release_scheduler_alert(
            uow.session,
            alert_key=SCHEDULER_OVERDUE_FREEZE_ALERT_KEY,
        )


__all__ = [
    "SCHEDULER_OVERDUE_FREEZE_ALERT_KEY",
    "claim_scheduler_overdue_alert",
    "release_scheduler_alert",
    "release_scheduler_overdue_alert",
    "try_claim_scheduler_alert",
]
