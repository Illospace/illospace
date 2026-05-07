"""MemorySummaryRepository - domain queries for the memory DAG."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Sequence

from sqlalchemy import select, update

from brain.platform.db.models.memory import Memory
from brain.platform.db.models.memory_dag import MemorySummary, SummaryLineage
from brain.platform.db.repositories.base import BaseRepository


class MemorySummaryRepository(BaseRepository[MemorySummary]):
    """CRUD + domain queries for MemorySummary and SummaryLineage."""

    model = MemorySummary

    # ------------------------------------------------------------------
    # Listing by depth
    # ------------------------------------------------------------------

    def list_by_depth(
        self,
        depth: int,
        org_id: str | None = None,
        limit: int | None = None,
        *,
        user_id: str | None = None,
        visibility: str | None = None,
        include_stale: bool = False,
    ) -> Sequence[MemorySummary]:
        """Return active summaries at a given depth, optionally scoped."""
        stmt = select(MemorySummary).where(MemorySummary.depth == depth)
        stmt = self._apply_scope(
            stmt,
            org_id=org_id,
            user_id=user_id,
            visibility=visibility,
        )
        if not include_stale:
            stmt = stmt.where(MemorySummary.stale_at.is_(None))
        stmt = stmt.order_by(MemorySummary.id)
        if limit is not None:
            stmt = stmt.limit(limit)
        return self._session.scalars(stmt).all()

    def list_by_depth_min_count(
        self,
        depth: int,
        min_count: int,
        org_id: str | None = None,
        *,
        user_id: str | None = None,
        visibility: str | None = None,
        include_stale: bool = False,
    ) -> Sequence[MemorySummary]:
        """Return summaries at depth only if total count >= min_count, else empty."""
        results = self.list_by_depth(
            depth,
            org_id=org_id,
            user_id=user_id,
            visibility=visibility,
            include_stale=include_stale,
        )
        if len(results) < min_count:
            return []
        return results

    # ------------------------------------------------------------------
    # Lineage management
    # ------------------------------------------------------------------

    def add_child_memory(self, summary_id: int, memory_id: int) -> SummaryLineage:
        """Link a raw memory as a child of a summary."""
        lineage = SummaryLineage(
            summary_id=summary_id,
            child_memory_id=memory_id,
        )
        self._session.add(lineage)
        self._session.flush()
        return lineage

    def add_child_summary(
        self, summary_id: int, child_summary_id: int
    ) -> SummaryLineage:
        """Link a lower-level summary as a child of a higher summary."""
        lineage = SummaryLineage(
            summary_id=summary_id,
            child_summary_id=child_summary_id,
        )
        self._session.add(lineage)
        self._session.flush()
        return lineage

    def get_children(self, summary_id: int) -> Sequence[SummaryLineage]:
        """Return all lineage edges for a given summary."""
        stmt = (
            select(SummaryLineage)
            .where(SummaryLineage.summary_id == summary_id)
            .order_by(SummaryLineage.id)
        )
        return self._session.scalars(stmt).all()

    def get_parent_of_memory(self, memory_id: int) -> SummaryLineage | None:
        """Return the lineage edge linking a memory to its parent summary, or None."""
        stmt = select(SummaryLineage).where(
            SummaryLineage.child_memory_id == memory_id
        )
        return self._session.scalars(stmt).first()

    # ------------------------------------------------------------------
    # Staleness
    # ------------------------------------------------------------------

    def mark_stale_for_memory(
        self,
        memory_id: int,
        reason: str,
        *,
        stale_at: datetime | None = None,
    ) -> int:
        """Mark every active summary depending on ``memory_id`` as stale."""
        return self.mark_stale_for_memories([memory_id], reason, stale_at=stale_at)

    def mark_stale_for_memories(
        self,
        memory_ids: Sequence[int],
        reason: str,
        *,
        stale_at: datetime | None = None,
    ) -> int:
        """Mark direct and transitive parent summaries for source memories stale."""
        source_ids = [int(memory_id) for memory_id in memory_ids if memory_id is not None]
        if not source_ids:
            return 0

        summary_ids = self._dependent_summary_ids(source_ids)
        if not summary_ids:
            return 0

        now = stale_at or datetime.now(timezone.utc)
        result = self._session.execute(
            update(MemorySummary)
            .where(MemorySummary.id.in_(summary_ids))
            .where(MemorySummary.stale_at.is_(None))
            .values(stale_at=now, stale_reason=str(reason or "source memory changed"))
        )
        self._session.flush()
        return int(result.rowcount or 0)

    def mark_stale_for_contradiction(
        self,
        left_memory_id: int,
        right_memory_id: int,
        reason: str = "source memory contradicted",
        *,
        stale_at: datetime | None = None,
    ) -> int:
        """Mark summaries stale when either side of a contradiction is a source."""
        return self.mark_stale_for_memories(
            [left_memory_id, right_memory_id],
            reason,
            stale_at=stale_at,
        )

    # ------------------------------------------------------------------
    # Expansion
    # ------------------------------------------------------------------

    def expand_breadcrumb(
        self, summary_id: int, source_ids: list[int]
    ) -> Sequence[Memory]:
        """Load full Memory objects for the given source IDs (breadcrumb drill-down)."""
        if not source_ids:
            return []
        stmt = (
            select(Memory)
            .where(Memory.id.in_(source_ids))
            .order_by(Memory.id)
        )
        return self._session.scalars(stmt).all()

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
            stmt = stmt.where(MemorySummary.org_id == org_id)
        if user_id is not None:
            stmt = stmt.where(MemorySummary.user_id == user_id)
        if visibility is not None:
            stmt = stmt.where(MemorySummary.visibility == visibility)
        return stmt

    def _dependent_summary_ids(self, memory_ids: Sequence[int]) -> set[int]:
        direct_stmt = select(SummaryLineage.summary_id).where(
            SummaryLineage.child_memory_id.in_(memory_ids)
        )
        affected = set(self._session.scalars(direct_stmt).all())
        frontier = set(affected)

        while frontier:
            parent_stmt = select(SummaryLineage.summary_id).where(
                SummaryLineage.child_summary_id.in_(frontier)
            )
            parents = set(self._session.scalars(parent_stmt).all())
            new_parents = parents - affected
            if not new_parents:
                break
            affected.update(new_parents)
            frontier = new_parents

        return affected
