"""Durable scheduler alert-latch persistence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, case, delete, or_, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from brain.platform.db.models.scheduler import SchedulerAlertLatch


SCHEDULER_OVERDUE_FREEZE_ALERT_KEY = "scheduler_overdue_freeze"
SCHEDULER_SELF_HEAL_ALERT_KEY = "scheduler_self_heal"


@dataclass(frozen=True, slots=True)
class SchedulerOverdueAlertState:
    alerted_at: datetime
    freeze_started_at: datetime | None
    next_alert_at: datetime | None
    self_heal_attempts: int = 0


@dataclass(frozen=True, slots=True)
class SchedulerSelfHealState:
    attempt: int | None
    attempts: int
    exhausted: bool
    freeze_started_at: datetime | None = None


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


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
        attempts_result = await uow.session.execute(
            select(SchedulerAlertLatch.attempt_count).where(
                SchedulerAlertLatch.alert_key == SCHEDULER_SELF_HEAL_ALERT_KEY,
                SchedulerAlertLatch.freeze_started_at == row.freeze_started_at,
            )
        )
        return SchedulerOverdueAlertState(
            alerted_at=row.alerted_at,
            freeze_started_at=row.freeze_started_at,
            next_alert_at=row.next_alert_at,
            self_heal_attempts=attempts_result.scalar_one_or_none() or 0,
        )


async def try_claim_scheduler_self_heal(
    session: AsyncSession,
    *,
    attempted_at: datetime,
    freeze_started_at: datetime,
    next_attempt_at: datetime,
    max_attempts: int,
) -> SchedulerSelfHealState:
    """Atomically claim one bounded restart attempt for a freeze episode."""
    claim = (
        _alert_latch_insert(session)
        .values(
            alert_key=SCHEDULER_SELF_HEAL_ALERT_KEY,
            alerted_at=attempted_at,
            freeze_started_at=freeze_started_at,
            next_alert_at=next_attempt_at,
            attempt_count=1,
        )
        .on_conflict_do_update(
            index_elements=[SchedulerAlertLatch.alert_key],
            set_={
                "alerted_at": attempted_at,
                "freeze_started_at": freeze_started_at,
                "next_alert_at": next_attempt_at,
                "attempt_count": case(
                    (
                        or_(
                            SchedulerAlertLatch.freeze_started_at.is_(None),
                            SchedulerAlertLatch.freeze_started_at
                            != freeze_started_at,
                        ),
                        1,
                    ),
                    else_=SchedulerAlertLatch.attempt_count + 1,
                ),
            },
            where=or_(
                SchedulerAlertLatch.freeze_started_at.is_(None),
                SchedulerAlertLatch.freeze_started_at != freeze_started_at,
                and_(
                    SchedulerAlertLatch.attempt_count < max_attempts,
                    or_(
                        SchedulerAlertLatch.next_alert_at.is_(None),
                        SchedulerAlertLatch.next_alert_at <= attempted_at,
                    ),
                ),
            ),
        )
        .returning(SchedulerAlertLatch.attempt_count)
    )
    claim_result = await session.execute(claim)
    attempt = claim_result.scalar_one_or_none()
    if attempt is None:
        state_result = await session.execute(
            select(SchedulerAlertLatch.attempt_count).where(
                SchedulerAlertLatch.alert_key == SCHEDULER_SELF_HEAL_ALERT_KEY,
                SchedulerAlertLatch.freeze_started_at == freeze_started_at,
            )
        )
        attempts = state_result.scalar_one_or_none() or 0
        return SchedulerSelfHealState(
            attempt=None,
            attempts=attempts,
            exhausted=attempts >= max_attempts,
            freeze_started_at=freeze_started_at,
        )
    return SchedulerSelfHealState(
        attempt=attempt,
        attempts=attempt,
        exhausted=False,
        freeze_started_at=freeze_started_at,
    )


async def claim_scheduler_self_heal(
    *,
    attempted_at: datetime,
    heal_after: timedelta,
    max_attempts: int,
) -> SchedulerSelfHealState:
    """Claim and commit one self-heal attempt in a short transaction."""
    from brain.platform.db.repositories.unit_of_work import UnitOfWork

    async with UnitOfWork() as uow:
        freeze_result = await uow.session.execute(
            select(SchedulerAlertLatch.freeze_started_at).where(
                SchedulerAlertLatch.alert_key
                == SCHEDULER_OVERDUE_FREEZE_ALERT_KEY
            )
        )
        freeze_started_at = freeze_result.scalar_one_or_none()
        if freeze_started_at is None:
            return SchedulerSelfHealState(
                attempt=None,
                attempts=0,
                exhausted=False,
            )
        freeze_started_at = _as_utc(freeze_started_at)
        if _as_utc(attempted_at) < freeze_started_at + heal_after:
            state = await _scheduler_self_heal_state(
                uow.session,
                freeze_started_at=freeze_started_at,
                max_attempts=max_attempts,
            )
            return state
        return await try_claim_scheduler_self_heal(
            uow.session,
            attempted_at=attempted_at,
            freeze_started_at=freeze_started_at,
            next_attempt_at=attempted_at + heal_after,
            max_attempts=max_attempts,
        )


async def _scheduler_self_heal_state(
    session: AsyncSession,
    *,
    freeze_started_at: datetime,
    max_attempts: int,
) -> SchedulerSelfHealState:
    result = await session.execute(
        select(SchedulerAlertLatch.attempt_count).where(
            SchedulerAlertLatch.alert_key == SCHEDULER_SELF_HEAL_ALERT_KEY,
            SchedulerAlertLatch.freeze_started_at == freeze_started_at,
        )
    )
    attempts = result.scalar_one_or_none() or 0
    return SchedulerSelfHealState(
        attempt=None,
        attempts=attempts,
        exhausted=attempts >= max_attempts,
        freeze_started_at=freeze_started_at,
    )


async def release_scheduler_self_heal_claim(
    *,
    attempted_at: datetime,
    freeze_started_at: datetime,
    attempt: int,
) -> None:
    """Give back a claim when the runtime-services queue was already busy."""
    from brain.platform.db.repositories.unit_of_work import UnitOfWork

    async with UnitOfWork() as uow:
        await uow.session.execute(
            update(SchedulerAlertLatch)
            .where(
                SchedulerAlertLatch.alert_key
                == SCHEDULER_SELF_HEAL_ALERT_KEY,
                SchedulerAlertLatch.alerted_at == attempted_at,
                SchedulerAlertLatch.freeze_started_at == freeze_started_at,
                SchedulerAlertLatch.attempt_count == attempt,
            )
            .values(
                attempt_count=max(0, attempt - 1),
                next_alert_at=attempted_at,
            )
        )


__all__ = [
    "SCHEDULER_OVERDUE_FREEZE_ALERT_KEY",
    "SCHEDULER_SELF_HEAL_ALERT_KEY",
    "SchedulerOverdueAlertState",
    "SchedulerSelfHealState",
    "claim_scheduler_self_heal",
    "claim_scheduler_overdue_alert",
    "release_scheduler_alert",
    "release_scheduler_overdue_alert",
    "release_scheduler_self_heal_claim",
    "try_claim_scheduler_overdue_alert",
    "try_claim_scheduler_self_heal",
    "try_claim_scheduler_alert",
]
