"""Repository for agent-run persistence."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import func, select

from brain.platform.db.models.agent_run import (
    AgentRunArtifactRow,
    AgentRunEventRow,
    AgentRunRow,
)
from brain.platform.db.repositories.base import BaseRepository


class AgentRunRepository(BaseRepository[AgentRunRow]):
    model = AgentRunRow

    async def list_by_thread(self, thread_id: str, *, limit: int = 50) -> Sequence[AgentRunRow]:
        stmt = (
            select(AgentRunRow)
            .where(AgentRunRow.thread_id == thread_id)
            .order_by(AgentRunRow.created_at.desc())
            .limit(limit)
        )
        return (await self._session.scalars(stmt)).all()

    async def list_claimable(self, *, limit: int = 25) -> Sequence[AgentRunRow]:
        stmt = (
            select(AgentRunRow)
            .where(AgentRunRow.status == "queued")
            .order_by(AgentRunRow.created_at.asc())
            .limit(limit)
        )
        return (await self._session.scalars(stmt)).all()

    async def next_sequence(self, run_id: int) -> int:
        value = await self._session.scalar(
            select(func.coalesce(func.max(AgentRunEventRow.sequence_no), 0)).where(
                AgentRunEventRow.run_id == run_id
            )
        )
        return int(value or 0) + 1


class AgentRunEventRepository(BaseRepository[AgentRunEventRow]):
    model = AgentRunEventRow

    async def list_after(self, *, cursor: int = 0, limit: int = 100) -> Sequence[AgentRunEventRow]:
        stmt = (
            select(AgentRunEventRow)
            .where(AgentRunEventRow.id > cursor)
            .order_by(AgentRunEventRow.id.asc())
            .limit(limit)
        )
        return (await self._session.scalars(stmt)).all()

    async def list_for_run(self, run_id: int, *, limit: int = 200) -> Sequence[AgentRunEventRow]:
        stmt = (
            select(AgentRunEventRow)
            .where(AgentRunEventRow.run_id == run_id)
            .order_by(AgentRunEventRow.sequence_no.asc())
            .limit(limit)
        )
        return (await self._session.scalars(stmt)).all()


class AgentRunArtifactRepository(BaseRepository[AgentRunArtifactRow]):
    model = AgentRunArtifactRow

    async def list_for_run(self, run_id: int) -> Sequence[AgentRunArtifactRow]:
        stmt = (
            select(AgentRunArtifactRow)
            .where(AgentRunArtifactRow.run_id == run_id)
            .order_by(AgentRunArtifactRow.created_at.asc(), AgentRunArtifactRow.id.asc())
        )
        return (await self._session.scalars(stmt)).all()
