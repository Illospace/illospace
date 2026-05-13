"""NarrativeRepository - domain queries for project narratives."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Sequence

from sqlalchemy import select, update

from brain.platform.db.models.memory import Memory
from brain.platform.db.models.narrative import NarrativeSession, ProjectNarrative
from brain.platform.db.repositories.base import BaseRepository


class NarrativeRepository(BaseRepository[ProjectNarrative]):
    """CRUD + domain queries for ProjectNarrative and NarrativeSession."""

    model = ProjectNarrative

    # ------------------------------------------------------------------
    # Lookup
    # ------------------------------------------------------------------

    async def a_get_by_slug(
        self,
        slug: str,
        org_id: str | None = None,
        *,
        user_id: str | None = None,
        visibility: str | None = None,
        include_stale: bool = False,
    ) -> ProjectNarrative | None:
        """Return a narrative by topic_slug, optionally scoped to org/user/visibility."""
        stmt = select(ProjectNarrative).where(ProjectNarrative.topic_slug == slug)
        stmt = self._apply_scope(
            stmt,
            org_id=org_id,
            user_id=user_id,
            visibility=visibility,
        )
        if not include_stale:
            stmt = stmt.where(ProjectNarrative.stale_at.is_(None))
        return (await self._session.scalars(stmt)).first()

    async def a_find_by_topic_fuzzy(
        self,
        topic: str,
        org_id: str | None = None,
        *,
        user_id: str | None = None,
        visibility: str | None = None,
        include_stale: bool = False,
    ) -> Sequence[ProjectNarrative]:
        """Return narratives whose title or topic_slug contains the topic string."""
        pattern = f"%{topic}%"
        stmt = select(ProjectNarrative).where(
            ProjectNarrative.title.ilike(pattern)
            | ProjectNarrative.topic_slug.ilike(pattern)
        )
        stmt = self._apply_scope(
            stmt,
            org_id=org_id,
            user_id=user_id,
            visibility=visibility,
        )
        if not include_stale:
            stmt = stmt.where(ProjectNarrative.stale_at.is_(None))
        stmt = stmt.order_by(ProjectNarrative.updated_at.desc())
        return (await self._session.scalars(stmt)).all()

    async def a_list_active(
        self,
        org_id: str | None = None,
        limit: int = 20,
        *,
        user_id: str | None = None,
        visibility: str | None = None,
        include_stale: bool = False,
    ) -> Sequence[ProjectNarrative]:
        """Return narratives ordered by most recently updated."""
        stmt = select(ProjectNarrative)
        stmt = self._apply_scope(
            stmt,
            org_id=org_id,
            user_id=user_id,
            visibility=visibility,
        )
        if not include_stale:
            stmt = stmt.where(ProjectNarrative.stale_at.is_(None))
        stmt = stmt.order_by(ProjectNarrative.updated_at.desc()).limit(limit)
        return (await self._session.scalars(stmt)).all()

    async def a_mark_stale(
        self,
        narrative_id: int,
        reason: str,
        *,
        stale_at: datetime | None = None,
    ) -> bool:
        """Mark a narrative stale without deleting its historical sessions."""
        result = await self._session.execute(
            update(ProjectNarrative)
            .where(ProjectNarrative.id == narrative_id)
            .where(ProjectNarrative.stale_at.is_(None))
            .values(
                stale_at=stale_at or datetime.now(timezone.utc),
                stale_reason=str(reason or "narrative source changed"),
            )
        )
        await self._session.flush()
        return bool(result.rowcount)

    async def a_mark_stale_for_memory(
        self,
        memory_id: int,
        reason: str,
        *,
        stale_at: datetime | None = None,
    ) -> int:
        """Mark narratives stale when a source session contains ``memory_id``."""
        return await self.a_mark_stale_for_memories([memory_id], reason, stale_at=stale_at)

    async def a_mark_stale_for_memories(
        self,
        memory_ids: Sequence[int],
        reason: str,
        *,
        stale_at: datetime | None = None,
    ) -> int:
        """Mark narratives stale through memory.source_session -> narrative session."""
        source_ids = [int(memory_id) for memory_id in memory_ids if memory_id is not None]
        if not source_ids:
            return 0

        narrative_stmt = (
            select(NarrativeSession.narrative_id)
            .join(Memory, Memory.source_session == NarrativeSession.session_id)
            .where(Memory.id.in_(source_ids))
        )
        narrative_ids = set((await self._session.scalars(narrative_stmt)).all())
        if not narrative_ids:
            return 0

        result = await self._session.execute(
            update(ProjectNarrative)
            .where(ProjectNarrative.id.in_(narrative_ids))
            .where(ProjectNarrative.stale_at.is_(None))
            .values(
                stale_at=stale_at or datetime.now(timezone.utc),
                stale_reason=str(reason or "narrative source memory changed"),
            )
        )
        await self._session.flush()
        return int(result.rowcount or 0)

    # ------------------------------------------------------------------
    # Session entries
    # ------------------------------------------------------------------

    async def a_add_session_entry(
        self,
        narrative_id: int,
        session_id: str,
        session_date,
        summary: str,
    ) -> NarrativeSession:
        """Append a session entry to a narrative."""
        entry = NarrativeSession(
            narrative_id=narrative_id,
            session_id=session_id,
            session_date=session_date,
            summary=summary,
        )
        self._session.add(entry)
        await self._session.flush()
        return entry

    async def a_get_session_entries(
        self, narrative_id: int
    ) -> Sequence[NarrativeSession]:
        """Return session entries for a narrative, ordered by date ascending."""
        stmt = (
            select(NarrativeSession)
            .where(NarrativeSession.narrative_id == narrative_id)
            .order_by(NarrativeSession.session_date.asc())
        )
        return (await self._session.scalars(stmt)).all()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _apply_scope(
        self,
        stmt,
        *,
        org_id: str | None = None,
        user_id: str | None = None,
        visibility: str | None = None,
    ):
        if org_id is not None:
            stmt = stmt.where(ProjectNarrative.org_id == org_id)
        if user_id is not None:
            stmt = stmt.where(ProjectNarrative.user_id == user_id)
        if visibility is not None:
            stmt = stmt.where(ProjectNarrative.visibility == visibility)
        return stmt
