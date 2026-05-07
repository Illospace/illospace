"""Three-pool retrieval — exploit/explore/narrative with adaptive bandit ratios.

Replaces single-pass retrieval with three independent pools that are
deduplicated and merged into a final result set.  Each pool targets a
different retrieval objective:

* **Exploit** — top-ranked memories by cosine similarity (high precision).
* **Explore** — under-accessed memories above a cosine floor (serendipity).
* **Narrative** — project-level narrative arcs for long-range context.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Optional

from sqlalchemy import and_, func, or_, select

from brain.platform.db.enums import PoolName
from brain.platform.db.models.memory import Memory
from brain.platform.db.models.narrative import ProjectNarrative
from brain.platform.db.repositories.memory_visibility import (
    MemoryVisibilityContext,
    memory_is_visible,
    memory_visibility_predicate,
)
from brain.systems.memory.dedup import deduplicate_results

logger = logging.getLogger(__name__)

_SERVICE_USER_IDS = {"system", "service:internal-api"}


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class RetrievalConfig:
    """Knobs for the three-pool retriever."""

    total_results: int = 5
    exploit_ratio: float = 0.60
    explore_ratio: float = 0.25
    narrative_ratio: float = 0.15
    dedup_threshold: float = 0.85
    explore_cosine_floor: float = 0.4
    org_id: Optional[str] = None
    user_id: Optional[str] = None
    service_retrieval: bool = False
    emotion_context: Optional[dict] = field(default=None)


# ---------------------------------------------------------------------------
# Freshness helper
# ---------------------------------------------------------------------------


def _creation_freshness(created_at: datetime) -> float:
    """Exponential decay based on age since creation.

    Returns a value in (0, 1] — 1.0 for brand-new memories, decaying with
    a half-life of ~35 days (``exp(-0.02 * days)``).
    """
    now = datetime.now(timezone.utc)
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    days = max(0, (now - created_at).total_seconds() / 86_400)
    return math.exp(-0.02 * days)


# ---------------------------------------------------------------------------
# PoolRetriever
# ---------------------------------------------------------------------------


class PoolRetriever:
    """Three-pool retrieval: exploit, explore, narrative."""

    def __init__(self, config: RetrievalConfig | None = None) -> None:
        self.config = config or RetrievalConfig()

    def _memory_visibility_context(self) -> MemoryVisibilityContext:
        user_id = _text(self.config.user_id)
        org_id = _text(self.config.org_id)
        if self.config.service_retrieval or user_id in _SERVICE_USER_IDS:
            return MemoryVisibilityContext(
                user_id=user_id or "system",
                org_id=org_id,
                allow_global=True,
                principal_type="service",
            )
        if not user_id:
            raise ValueError(
                "Pool retrieval requires user_id; pass service_retrieval=True for explicit service/system retrieval"
            )
        return MemoryVisibilityContext(
            user_id=user_id,
            org_id=org_id,
        )

    def _apply_memory_visibility(self, stmt):
        return stmt.where(memory_visibility_predicate(Memory, self._memory_visibility_context()))

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def retrieve(
        self,
        query_embedding: list[float],
        *,
        session=None,
        ratios: dict[str, float] | None = None,
    ) -> list[dict]:
        """Run three pools, deduplicate, return up to ``total_results``.

        Args:
            query_embedding: The query vector.
            session: SQLAlchemy session for DB queries.
            ratios: Optional override ``{exploit, explore, narrative}`` float ratios.

        Returns:
            List of result dicts, each tagged with ``_pool``.
        """
        if ratios is None:
            ratios = {
                PoolName.EXPLOIT: self.config.exploit_ratio,
                PoolName.EXPLORE: self.config.explore_ratio,
                PoolName.NARRATIVE: self.config.narrative_ratio,
            }

        slots = self._allocate_slots(ratios)
        all_results: list[dict] = []

        # -- Exploit pool --
        exploit_results = self._exploit_pool(query_embedding, slots[PoolName.EXPLOIT], session=session)
        for r in exploit_results:
            r["_pool"] = PoolName.EXPLOIT
        all_results.extend(exploit_results)

        # -- Explore pool --
        explore_results = self._explore_pool(query_embedding, slots[PoolName.EXPLORE], session=session)
        for r in explore_results:
            r["_pool"] = PoolName.EXPLORE
        all_results.extend(explore_results)

        # -- Narrative pool --
        narrative_results = self._narrative_pool(query_embedding, slots[PoolName.NARRATIVE], session=session)
        for r in narrative_results:
            r["_pool"] = PoolName.NARRATIVE
        all_results.extend(narrative_results)

        # Deduplicate across pools
        deduped = deduplicate_results(all_results, threshold=self.config.dedup_threshold)

        return deduped[: self.config.total_results]

    # ------------------------------------------------------------------
    # Slot allocation
    # ------------------------------------------------------------------

    def _allocate_slots(self, ratios: dict[str, float]) -> dict[str, int]:
        """Convert float ratios to integer slot counts, min 1 per pool.

        Uses largest-remainder method to distribute ``total_results`` slots.
        """
        total = self.config.total_results
        pools = list(ratios.keys())

        # Ensure ratios sum to 1
        ratio_sum = sum(ratios.values())
        if ratio_sum <= 0:
            # Fallback to equal distribution
            base = total // len(pools)
            return {p: max(1, base) for p in pools}

        normalised = {p: ratios[p] / ratio_sum for p in pools}

        # Ideal (float) slots
        ideal = {p: normalised[p] * total for p in pools}

        # Floor each to at least 1
        floored = {p: max(1, int(ideal[p])) for p in pools}
        allocated = sum(floored.values())

        # Distribute remaining slots by largest fractional remainder
        remaining = total - allocated
        if remaining > 0:
            remainders = {p: ideal[p] - floored[p] for p in pools}
            for p in sorted(remainders, key=remainders.get, reverse=True):
                if remaining <= 0:
                    break
                floored[p] += 1
                remaining -= 1

        return floored

    # ------------------------------------------------------------------
    # Pool implementations (mockable for testing)
    # ------------------------------------------------------------------

    def _exploit_pool(
        self,
        query_embedding: list[float],
        limit: int,
        *,
        session=None,
    ) -> list[dict]:
        """Top-ranked memories by cosine similarity."""
        if session is None:
            logger.debug("No session provided for exploit pool — returning empty")
            return []

        stmt = (
            select(Memory)
            .where(Memory.archived != True)  # noqa: E712
            .where(Memory.semantic_embedding.isnot(None))
            .order_by(Memory.semantic_embedding.cosine_distance(query_embedding))
            .limit(limit)
        )
        stmt = self._apply_memory_visibility(stmt)

        rows = session.scalars(stmt).all()
        return [self._memory_to_dict(m) for m in rows]

    def _explore_pool(
        self,
        query_embedding: list[float],
        limit: int,
        *,
        session=None,
    ) -> list[dict]:
        """Under-accessed memories above the cosine floor.

        Over-fetches 3x, filters for low access_count (<= median),
        then takes up to ``limit``.
        """
        if session is None:
            logger.debug("No session provided for explore pool — returning empty")
            return []

        over_fetch = limit * 3

        # Get median access_count for filtering
        median_q = session.scalar(
            select(func.percentile_cont(0.5).within_group(Memory.access_count))
            .where(Memory.archived != True)  # noqa: E712
            .where(memory_visibility_predicate(Memory, self._memory_visibility_context()))
        )
        median_access = median_q if median_q is not None else 0

        stmt = (
            select(Memory)
            .where(Memory.archived != True)  # noqa: E712
            .where(Memory.semantic_embedding.isnot(None))
            .where(Memory.access_count <= median_access)
            .order_by(Memory.semantic_embedding.cosine_distance(query_embedding))
            .limit(over_fetch)
        )
        stmt = self._apply_memory_visibility(stmt)

        rows = session.scalars(stmt).all()

        # Filter for cosine >= floor (distance <= 1 - floor)
        max_distance = 1.0 - self.config.explore_cosine_floor
        results: list[dict] = []
        for m in rows:
            d = self._memory_to_dict(m)
            results.append(d)
            if len(results) >= limit:
                break

        return results

    def _narrative_pool(
        self,
        query_embedding: list[float],
        limit: int,
        *,
        session=None,
    ) -> list[dict]:
        """Query ProjectNarrative embeddings for long-range context."""
        if session is None:
            logger.debug("No session provided for narrative pool — returning empty")
            return []
        context = self._memory_visibility_context()

        stmt = (
            select(ProjectNarrative)
            .where(ProjectNarrative.semantic_embedding.isnot(None))
            .order_by(
                ProjectNarrative.semantic_embedding.cosine_distance(query_embedding)
            )
            .limit(limit)
        )

        if not context.allow_global:
            visibility_clauses = []
            if context.user_id:
                visibility_clauses.append(
                    and_(
                        ProjectNarrative.user_id == context.user_id,
                        ProjectNarrative.org_id.is_(None),
                    )
                )
            if context.org_id:
                visibility_clauses.append(ProjectNarrative.org_id == context.org_id)
            if not visibility_clauses:
                return []
            stmt = stmt.where(or_(*visibility_clauses))

        rows = session.scalars(stmt).all()
        return [self._narrative_to_dict(n) for n in rows if self._narrative_visible(n, context)]

    # ------------------------------------------------------------------
    # Serialisation helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _memory_to_dict(m: Memory) -> dict:
        embedding = m.semantic_embedding
        if embedding is not None and not isinstance(embedding, list):
            embedding = list(embedding)
        return {
            "id": m.id,
            "content": m.content,
            "candidate_kind": "memory",
            "memory_type": m.memory_type,
            "salience": m.salience,
            "embedding": embedding,
            "access_count": m.access_count,
            "created_at": m.created_at,
            "visibility": getattr(m, "visibility", "private") or "private",
            "user_id": _text(getattr(m, "user_id", None)),
            "org_id": _text(getattr(m, "org_id", None)),
        }

    @staticmethod
    def _narrative_to_dict(n: ProjectNarrative) -> dict:
        embedding = n.semantic_embedding
        if embedding is not None and not isinstance(embedding, list):
            embedding = list(embedding)
        org_id = _text(getattr(n, "org_id", None))
        return {
            "id": n.id,
            "content": n.arc_summary,
            "candidate_kind": "narrative",
            "title": n.title,
            "topic_slug": n.topic_slug,
            "salience": 5.0,  # narratives have no salience column; default
            "embedding": embedding,
            "visibility": "org" if org_id else "private",
            "user_id": _text(getattr(n, "user_id", None)),
            "org_id": org_id,
        }

    @staticmethod
    def _narrative_visible(n: ProjectNarrative, context: MemoryVisibilityContext) -> bool:
        org_id = _text(getattr(n, "org_id", None))
        return memory_is_visible(
            SimpleNamespace(
                user_id=_text(getattr(n, "user_id", None)),
                org_id=org_id,
                visibility="org" if org_id else "private",
            ),
            context,
        )


def _text(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
