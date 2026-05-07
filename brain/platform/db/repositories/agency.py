"""Agency repositories."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Sequence

from sqlalchemy import select

from brain.platform.db.models.agency import (
    AgencyApproval,
    AgencyBudget,
    AgencyBudgetEvent,
    AgencyCandidate,
    AgencyDecision,
)
from brain.platform.db.repositories.base import BaseRepository


class AgencyRepository(BaseRepository[AgencyCandidate]):
    """Repository for candidate review and budget operations."""

    model = AgencyCandidate

    def get_by_candidate_key(self, candidate_key: str) -> AgencyCandidate | None:
        stmt = select(AgencyCandidate).where(AgencyCandidate.candidate_key == candidate_key)
        return self._session.scalars(stmt).first()

    def list_recent_candidates(
        self,
        *,
        limit: int = 50,
        status: str | None = None,
        org_id: str | None = None,
        user_id: str | None = None,
    ) -> Sequence[AgencyCandidate]:
        stmt = select(AgencyCandidate)
        if status is not None:
            stmt = stmt.where(AgencyCandidate.status == status)
        if org_id is not None:
            stmt = stmt.where(AgencyCandidate.org_id == org_id)
        if user_id is not None:
            stmt = stmt.where(AgencyCandidate.user_id == user_id)
        stmt = stmt.order_by(AgencyCandidate.created_at.desc()).limit(limit)
        return self._session.scalars(stmt).all()

    def list_recent_decisions(self, *, limit: int = 50) -> Sequence[AgencyDecision]:
        stmt = select(AgencyDecision).order_by(AgencyDecision.created_at.desc()).limit(limit)
        return self._session.scalars(stmt).all()

    def list_recent_approvals(
        self,
        *,
        candidate_id: int | None = None,
        limit: int = 50,
    ) -> Sequence[AgencyApproval]:
        stmt = select(AgencyApproval)
        if candidate_id is not None:
            stmt = stmt.where(AgencyApproval.candidate_id == candidate_id)
        stmt = stmt.order_by(AgencyApproval.created_at.desc()).limit(limit)
        return self._session.scalars(stmt).all()

    def list_budget_events(
        self,
        *,
        candidate_id: int | None = None,
        budget_id: int | None = None,
        limit: int = 100,
    ) -> Sequence[AgencyBudgetEvent]:
        stmt = select(AgencyBudgetEvent)
        if candidate_id is not None:
            stmt = stmt.where(AgencyBudgetEvent.candidate_id == candidate_id)
        if budget_id is not None:
            stmt = stmt.where(AgencyBudgetEvent.budget_id == budget_id)
        stmt = stmt.order_by(AgencyBudgetEvent.created_at.desc()).limit(limit)
        return self._session.scalars(stmt).all()

    def list_active_budgets(
        self,
        *,
        scope_type: str | None = None,
        scope_id: str | None = None,
        drive_type: str | None = None,
        now: datetime | None = None,
    ) -> Sequence[AgencyBudget]:
        now = now or datetime.now(timezone.utc)
        stmt = select(AgencyBudget).where(AgencyBudget.active.is_(True))
        if scope_type is not None:
            stmt = stmt.where(AgencyBudget.scope_type == scope_type)
        if scope_id is not None:
            stmt = stmt.where(AgencyBudget.scope_id == scope_id)
        if drive_type is not None:
            stmt = stmt.where(
                (AgencyBudget.drive_type == drive_type) | (AgencyBudget.drive_type.is_(None))
            )
        stmt = stmt.where(AgencyBudget.window_start <= now, AgencyBudget.window_end >= now)
        stmt = stmt.order_by(
            AgencyBudget.drive_type.is_(None),
            AgencyBudget.window_start.desc(),
            AgencyBudget.id.desc(),
        )
        return self._session.scalars(stmt).all()

    def create_candidate(self, **kwargs: Any) -> AgencyCandidate:
        candidate = AgencyCandidate(**kwargs)
        self._session.add(candidate)
        return candidate

    def create_decision(self, **kwargs: Any) -> AgencyDecision:
        decision = AgencyDecision(**kwargs)
        self._session.add(decision)
        return decision

    def create_approval(self, **kwargs: Any) -> AgencyApproval:
        approval = AgencyApproval(**kwargs)
        self._session.add(approval)
        return approval

    def create_budget(self, **kwargs: Any) -> AgencyBudget:
        budget = AgencyBudget(**kwargs)
        self._session.add(budget)
        return budget

    def create_budget_event(self, **kwargs: Any) -> AgencyBudgetEvent:
        event = AgencyBudgetEvent(**kwargs)
        self._session.add(event)
        return event
