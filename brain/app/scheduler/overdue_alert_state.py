"""Durable scheduler alert-latch persistence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import and_, delete, or_
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from brain.platform.db.models.scheduler import SchedulerAlertLatch


SCHEDULER_OVERDUE_FREEZE_ALERT_KEY = "scheduler_overdue_freeze"


@dataclass(frozen=True, slots=True)
class SchedulerOverdueAlertState:
    alerted_at: datetime
    freeze_started_at: datetime | None
    next_alert_at: datetime | None


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


async def try_claim_scheduler_overdue_alert(
    session: AsyncSession,
    *,
    alerted_at: datetime,
    freeze_started_at: datetime,
    next_alert_at: datetime,
) -> bool:
    """Atomically claim the first overdue alert or one due escalation."""
    claim = (
        _alert_latch_insert(session)
        .values(
            alert_key=SCHEDULER_OVERDUE_FREEZE_ALERT_KEY,
            alerted_at=alerted_at,
            freeze_started_at=freeze_started_at,
            next_alert_at=next_alert_at,
        )
        .on_conflict_do_update(
            index_elements=[SchedulerAlertLatch.alert_key],
            set_={
                "alerted_at": alerted_at,
                "freeze_started_at": freeze_started_at,
                "next_alert_at": next_alert_at,
            },
            where=or_(
                SchedulerAlertLatch.freeze_started_at.is_(None),
                and_(
                    SchedulerAlertLatch.freeze_started_at == freeze_started_at,
                    or_(
                        SchedulerAlertLatch.next_alert_at.is_(None),
                        SchedulerAlertLatch.next_alert_at <= alerted_at,
                    ),
                ),
            ),
        )
        .returning(SchedulerAlertLatch.alert_key)
    )
    result = await session.execute(claim)
    return result.scalar_one_or_none() == SCHEDULER_OVERDUE_FREEZE_ALERT_KEY


async def claim_scheduler_overdue_alert(
    *,
    alerted_at: datetime,
    freeze_started_at: datetime,
    next_alert_at: datetime,
) -> bool:
    """Claim and commit one overdue escalation in a short transaction."""
    from brain.platform.db.repositories.unit_of_work import UnitOfWork

    async with UnitOfWork() as uow:
        return await try_claim_scheduler_overdue_alert(
            uow.session,
            alerted_at=alerted_at,
            freeze_started_at=freeze_started_at,
            next_alert_at=next_alert_at,
        )


async def release_scheduler_overdue_alert() -> SchedulerOverdueAlertState | None:
    """Release and return the overdue latch in one short transaction."""
    from brain.platform.db.repositories.unit_of_work import UnitOfWork

    async with UnitOfWork() as uow:
        released = await uow.session.execute(
            delete(SchedulerAlertLatch)
            .where(
                SchedulerAlertLatch.alert_key
                == SCHEDULER_OVERDUE_FREEZE_ALERT_KEY
            )
            .returning(
                SchedulerAlertLatch.alerted_at,
                SchedulerAlertLatch.freeze_started_at,
                SchedulerAlertLatch.next_alert_at,
            )
        )
        row = released.one_or_none()
        if row is None:
            return None
        return SchedulerOverdueAlertState(
            alerted_at=row.alerted_at,
            freeze_started_at=row.freeze_started_at,
            next_alert_at=row.next_alert_at,
        )


__all__ = [
    "SCHEDULER_OVERDUE_FREEZE_ALERT_KEY",
    "SchedulerOverdueAlertState",
    "claim_scheduler_overdue_alert",
    "release_scheduler_alert",
    "release_scheduler_overdue_alert",
    "try_claim_scheduler_overdue_alert",
    "try_claim_scheduler_alert",
]
