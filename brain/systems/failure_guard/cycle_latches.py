"""Cycle-scoped persistence for durable alert latches."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from brain.platform.db.models.cycle import CycleFailureGuardLatch
from brain.systems.failure_guard.core import (
    FailureGuardLatch,
    FailureGuardTriggerKind,
)


def _latch_insert(session: AsyncSession):
    if session.get_bind().dialect.name == "sqlite":
        return sqlite_insert(CycleFailureGuardLatch)
    return postgresql_insert(CycleFailureGuardLatch)


async def async_try_claim_cycle_alert_latch(
    session: AsyncSession,
    *,
    cycle_id: int,
    trigger_kind: FailureGuardTriggerKind,
    alerted_at: datetime,
) -> bool:
    """Atomically claim one cycle alert edge across concurrent replicas."""
    claim = (
        _latch_insert(session)
        .values(
            cycle_id=cycle_id,
            trigger_kind=str(trigger_kind),
            alerted_at=alerted_at,
        )
        .on_conflict_do_nothing(
            index_elements=[
                CycleFailureGuardLatch.cycle_id,
                CycleFailureGuardLatch.trigger_kind,
            ]
        )
        .returning(CycleFailureGuardLatch.cycle_id)
    )
    result = await session.execute(claim)
    return result.scalar_one_or_none() == cycle_id


async def async_release_cycle_alert_latch(
    session: AsyncSession,
    *,
    cycle_id: int,
    trigger_kind: FailureGuardTriggerKind,
    alerted_at: datetime | None = None,
) -> None:
    """Release one cycle alert edge, optionally fencing an old delivery."""
    statement = delete(CycleFailureGuardLatch).where(
        CycleFailureGuardLatch.cycle_id == cycle_id,
        CycleFailureGuardLatch.trigger_kind == str(trigger_kind),
    )
    if alerted_at is not None:
        statement = statement.where(
            CycleFailureGuardLatch.alerted_at == alerted_at,
        )
    await session.execute(statement)


@dataclass(frozen=True)
class CycleAlertLatchStore:
    """Persist alert latches shared by cycle-scoped policy owners."""

    session: AsyncSession
    cycle_id: int

    async def load_latches(
        self,
    ) -> dict[FailureGuardTriggerKind, CycleFailureGuardLatch]:
        result = await self.session.scalars(
            select(CycleFailureGuardLatch).where(
                CycleFailureGuardLatch.cycle_id == self.cycle_id,
            )
        )
        return {
            FailureGuardTriggerKind(latch.trigger_kind): latch
            for latch in result.all()
        }

    async def create_latch(
        self,
        trigger_kind: FailureGuardTriggerKind,
        alerted_at: datetime,
    ) -> FailureGuardLatch:
        latch = CycleFailureGuardLatch(
            cycle_id=self.cycle_id,
            trigger_kind=str(trigger_kind),
            alerted_at=alerted_at,
        )
        self.session.add(latch)
        return latch

    async def delete_latch(
        self,
        trigger_kind: FailureGuardTriggerKind,
    ) -> None:
        await self.session.execute(
            delete(CycleFailureGuardLatch).where(
                CycleFailureGuardLatch.cycle_id == self.cycle_id,
                CycleFailureGuardLatch.trigger_kind == str(trigger_kind),
            )
        )
