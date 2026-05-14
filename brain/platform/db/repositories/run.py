"""Repositories for the AgentRun runtime."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select

from brain.platform.db.models.agent_run import AgentRunRow
from brain.platform.db.repositories.base import BaseRepository


class RunRepository(BaseRepository[AgentRunRow]):
    model = AgentRunRow

    async def list_pending(self) -> Sequence[AgentRunRow]:
        stmt = (
            select(AgentRunRow)
            .where(AgentRunRow.status == "queued")
            .order_by(AgentRunRow.created_at.asc(), AgentRunRow.id.asc())
        )
        return (await self._session.scalars(stmt)).all()

    async def list_by_thread(self, thread_id: str) -> Sequence[AgentRunRow]:
        stmt = (
            select(AgentRunRow)
            .where(AgentRunRow.thread_id == thread_id)
            .order_by(AgentRunRow.created_at.desc(), AgentRunRow.id.desc())
        )
        return (await self._session.scalars(stmt)).all()

    async def list_recent(self, *, limit: int = 50) -> Sequence[AgentRunRow]:
        stmt = (
            select(AgentRunRow)
            .order_by(AgentRunRow.created_at.desc(), AgentRunRow.id.desc())
            .limit(limit)
        )
        return (await self._session.scalars(stmt)).all()


__all__ = ["RunRepository"]
