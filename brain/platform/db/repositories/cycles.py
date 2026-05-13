"""Cycle repositories."""
from __future__ import annotations

from typing import Sequence

from sqlalchemy import select

from brain.platform.db.models.cycle import Cycle, CycleRun
from brain.platform.db.repositories.base import BaseRepository


class CycleRepository(BaseRepository[Cycle]):
    model = Cycle

    async def list_for_user(self, user_id: str) -> Sequence[Cycle]:
        stmt = (
            select(Cycle)
            .where(Cycle.user_id == user_id, Cycle.deleted_at.is_(None))
            .order_by(Cycle.created_at.desc())
        )
        return (await self._session.scalars(stmt)).all()

    async def get_for_user(self, cycle_id: int, user_id: str) -> Cycle | None:
        stmt = select(Cycle).where(
            Cycle.id == cycle_id,
            Cycle.user_id == user_id,
            Cycle.deleted_at.is_(None),
        )
        return (await self._session.scalars(stmt)).first()


class CycleRunRepository(BaseRepository[CycleRun]):
    model = CycleRun

    async def list_for_cycle(self, cycle_id: int, *, limit: int = 25) -> Sequence[CycleRun]:
        stmt = (
            select(CycleRun)
            .where(CycleRun.cycle_id == cycle_id)
            .order_by(CycleRun.created_at.desc())
            .limit(limit)
        )
        return (await self._session.scalars(stmt)).all()
