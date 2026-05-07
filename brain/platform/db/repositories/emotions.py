"""EmotionRepository — domain queries for emotional snapshots."""
from __future__ import annotations

from typing import Sequence

from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func, select

from brain.platform.db.models.emotion import EmotionalSnapshot
from brain.platform.db.repositories.base import BaseRepository


class EmotionRepository(BaseRepository[EmotionalSnapshot]):
    model = EmotionalSnapshot

    def list_recent(self, *, limit: int = 100) -> Sequence[EmotionalSnapshot]:
        stmt = (
            select(EmotionalSnapshot)
            .order_by(EmotionalSnapshot.timestamp.desc())
            .limit(limit)
        )
        return self._session.scalars(stmt).all()

    def avg_valence_7d(self) -> float | None:
        """7-day average valence."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        stmt = select(func.avg(EmotionalSnapshot.valence)).where(
            EmotionalSnapshot.timestamp >= cutoff
        )
        result = self._session.scalar(stmt)
        return float(result) if result is not None else None

    def count_all(self) -> int:
        """Total snapshot count."""
        stmt = select(func.count(EmotionalSnapshot.id))
        return self._session.scalar(stmt) or 0

    def trajectory_7d(self) -> list[dict]:
        """Return the 7-day emotion trajectory used by the memory CLI index."""
        cutoff = date.today() - timedelta(days=7)
        stmt = (
            select(
                EmotionalSnapshot.session_date,
                func.avg(EmotionalSnapshot.valence),
                func.array_agg(func.distinct(EmotionalSnapshot.label)),
            )
            .where(EmotionalSnapshot.session_date >= cutoff)
            .group_by(EmotionalSnapshot.session_date)
            .order_by(EmotionalSnapshot.session_date.desc())
        )
        rows = self._session.execute(stmt).all()
        return [
            {
                "session_date": row[0],
                "avg_valence": round(float(row[1] or 0), 2),
                "emotions": row[2] or [],
            }
            for row in rows
        ]
