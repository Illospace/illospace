"""Cycle-scoped persistence for durable alert latches."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from brain.platform.db.models.cycle import CycleFailureGuardLatch
from brain.systems.failure_guard.core import (
    FailureGuardLatch,
    FailureGuardTriggerKind,
)


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
