"""System repositories — daily metrics, consolidation, retrieval."""
from __future__ import annotations

from typing import Sequence

from sqlalchemy import select

from brain.platform.db.models.system import (
    ConsolidationRun,
    DailyMetrics,
    RetrievalLog,
)
from brain.platform.db.repositories.base import BaseRepository


class DailyMetricsRepository(BaseRepository[DailyMetrics]):
    model = DailyMetrics

    async def a_list_recent(self, *, limit: int = 30) -> Sequence[DailyMetrics]:
        stmt = (
            select(DailyMetrics)
            .order_by(DailyMetrics.metric_date.desc())
            .limit(limit)
        )
        result = await self._session.scalars(stmt)
        return result.all()


class ConsolidationRunRepository(BaseRepository[ConsolidationRun]):
    model = ConsolidationRun

    async def a_list_recent(self, *, limit: int = 20) -> Sequence[ConsolidationRun]:
        stmt = (
            select(ConsolidationRun)
            .order_by(ConsolidationRun.started_at.desc())
            .limit(limit)
        )
        result = await self._session.scalars(stmt)
        return result.all()


class RetrievalLogRepository(BaseRepository[RetrievalLog]):
    model = RetrievalLog

    async def a_list_recent(self, *, limit: int = 100) -> Sequence[RetrievalLog]:
        stmt = (
            select(RetrievalLog)
            .order_by(RetrievalLog.timestamp.desc())
            .limit(limit)
        )
        result = await self._session.scalars(stmt)
        return result.all()
