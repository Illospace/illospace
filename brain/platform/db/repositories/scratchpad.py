"""Repository for session scratchpad entries."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from brain.platform.db.models.scratchpad import SessionScratchpad


class ScratchpadRepository:
    """CRUD for session scratchpad entries."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def write(
        self,
        run_id: str,
        section: str,
        value: str,
        key: str | None = None,
        worker_name: str = "coordinator",
    ) -> SessionScratchpad:
        entry = SessionScratchpad(
            run_id=run_id,
            section=section,
            key=key,
            value=value,
            worker_name=worker_name,
        )
        self._session.add(entry)
        await self._session.flush()
        return entry

    async def read(
        self,
        run_id: str,
        section: str | None = None,
        key: str | None = None,
    ) -> list[dict]:
        stmt = select(SessionScratchpad).where(SessionScratchpad.run_id == run_id)
        if section:
            stmt = stmt.where(SessionScratchpad.section == section)
        if key:
            stmt = stmt.where(SessionScratchpad.key == key)
        stmt = stmt.order_by(SessionScratchpad.created_at)
        rows = (await self._session.execute(stmt)).scalars().all()
        return [
            {
                "id": r.id,
                "section": r.section,
                "key": r.key,
                "value": r.value,
                "worker_name": r.worker_name,
                "created_at": str(r.created_at),
            }
            for r in rows
        ]

    async def list_sections(self, run_id: str) -> list[str]:
        from sqlalchemy import distinct
        stmt = select(distinct(SessionScratchpad.section)).where(
            SessionScratchpad.run_id == run_id
        )
        return [row[0] for row in (await self._session.execute(stmt)).all()]

    async def promote(self, run_id: str) -> dict:
        """Gather all scratchpad entries for an AgentRun, formatted for review.

        Returns a structured summary organized by section. The parent run
        decides which entries are worth encoding as long-term brain memories.
        """
        entries = await self.read(run_id=run_id)
        if not entries:
            return {"run_id": run_id, "sections": {}, "total_entries": 0}

        by_section: dict[str, list[dict]] = {}
        for e in entries:
            by_section.setdefault(e["section"], []).append({
                "key": e["key"],
                "value": e["value"],
                "worker": e["worker_name"],
            })

        return {
            "run_id": run_id,
            "sections": by_section,
            "total_entries": len(entries),
        }

    async def close(self, run_id: str) -> int:
        """Mark all entries for a run as closed (sets closed_at timestamp).

        Closed entries are kept for 24h for debugging, then eligible for cleanup.
        Returns the number of entries closed.
        """
        now = datetime.now(timezone.utc)
        stmt = (
            update(SessionScratchpad)
            .where(SessionScratchpad.run_id == run_id)
            .where(SessionScratchpad.closed_at.is_(None))
            .values(closed_at=now)
        )
        result = (await self._session.execute(stmt))
        await self._session.flush()
        return result.rowcount

    async def cleanup_expired(self, hours: int = 24) -> int:
        """Delete entries that have been closed for more than `hours` hours.

        Returns the number of entries deleted.
        """
        from sqlalchemy import delete
        cutoff = datetime.now(timezone.utc) - __import__("datetime").timedelta(hours=hours)
        stmt = (
            delete(SessionScratchpad)
            .where(SessionScratchpad.closed_at.isnot(None))
            .where(SessionScratchpad.closed_at < cutoff)
        )
        result = (await self._session.execute(stmt))
        await self._session.flush()
        return result.rowcount
