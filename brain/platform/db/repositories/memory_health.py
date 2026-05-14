"""MemoryHealthRepository & RetrievalPoolStatsRepository."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select

from brain.platform.db.models.memory_health import MemoryHealthLog, RetrievalPoolStats
from brain.platform.db.repositories.base import BaseRepository


class MemoryHealthRepository(BaseRepository[MemoryHealthLog]):
    """CRUD + domain queries for MemoryHealthLog."""

    model = MemoryHealthLog

    async def a_log_check(
        self,
        check_type: str,
        status: str,
        details: dict | None = None,
        org_id: str | None = None,
    ) -> MemoryHealthLog:
        """Record a health check result."""
        entry = MemoryHealthLog(
            check_type=check_type,
            status=status,
            details=details,
            org_id=org_id,
        )
        self._session.add(entry)
        await self._session.flush()
        return entry


class RetrievalPoolStatsRepository(BaseRepository[RetrievalPoolStats]):
    """CRUD + domain queries for RetrievalPoolStats."""

    model = RetrievalPoolStats

    # Default pool ratios when no data exists
    _DEFAULT_RATIOS = {"recency": 0.60, "semantic": 0.25, "narrative": 0.15}
    _FLOOR = 0.10

    async def a_record_outcome(
        self,
        pool_name: str,
        hit: bool,
        org_id: str | None = None,
    ) -> RetrievalPoolStats:
        """Upsert hit/miss count for the current hour window."""
        now = datetime.now(timezone.utc)
        window_start = now.replace(minute=0, second=0, microsecond=0)

        stmt = select(RetrievalPoolStats).where(
            RetrievalPoolStats.pool_name == pool_name,
            RetrievalPoolStats.window_start == window_start,
        )
        if org_id is not None:
            stmt = stmt.where(RetrievalPoolStats.org_id == org_id)
        else:
            stmt = stmt.where(RetrievalPoolStats.org_id.is_(None))

        row = (await self._session.scalars(stmt)).first()
        if row is None:
            row = RetrievalPoolStats(
                pool_name=pool_name,
                hit_count=0,
                miss_count=0,
                window_start=window_start,
                org_id=org_id,
            )
            self._session.add(row)

        if hit:
            row.hit_count = (row.hit_count or 0) + 1
        else:
            row.miss_count = (row.miss_count or 0) + 1

        await self._session.flush()
        return row

    async def a_get_pool_ratios(
        self, org_id: str | None = None
    ) -> dict[str, float]:
        """Compute adaptive pool ratios from recent stats."""
        stmt = select(RetrievalPoolStats)
        if org_id is not None:
            stmt = stmt.where(RetrievalPoolStats.org_id == org_id)
        else:
            stmt = stmt.where(RetrievalPoolStats.org_id.is_(None))

        rows = (await self._session.scalars(stmt)).all()
        if not rows:
            return dict(self._DEFAULT_RATIOS)

        pool_hits: dict[str, int] = {}
        pool_total: dict[str, int] = {}
        for row in rows:
            h = row.hit_count or 0
            m = row.miss_count or 0
            total = h + m
            if total == 0:
                continue
            pool_hits[row.pool_name] = pool_hits.get(row.pool_name, 0) + h
            pool_total[row.pool_name] = pool_total.get(row.pool_name, 0) + total

        if not pool_total:
            return dict(self._DEFAULT_RATIOS)

        raw: dict[str, float] = {}
        for name in self._DEFAULT_RATIOS:
            if name in pool_total and pool_total[name] > 0:
                raw[name] = pool_hits.get(name, 0) / pool_total[name]
            else:
                raw[name] = self._DEFAULT_RATIOS[name]

        for name in raw:
            raw[name] = max(raw[name], self._FLOOR)

        total_raw = sum(raw.values())
        if total_raw == 0:
            return dict(self._DEFAULT_RATIOS)

        return {name: round(val / total_raw, 4) for name, val in raw.items()}
