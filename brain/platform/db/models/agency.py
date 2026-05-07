"""Agency control-plane models."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from brain.platform.db.base import Base, CreatedAtMixin, TimestampMixin

CANDIDATE_STATE_PROPOSED = "proposed"
CANDIDATE_STATE_APPROVED = "approved"
CANDIDATE_STATE_REJECTED = "rejected"
CANDIDATE_STATE_EXPIRED = "expired"
CANDIDATE_STATE_SUPPRESSED = "suppressed"
CANDIDATE_STATE_AUTO_EXECUTED = "auto_executed"
CANDIDATE_STATES = frozenset(
    {
        CANDIDATE_STATE_PROPOSED,
        CANDIDATE_STATE_APPROVED,
        CANDIDATE_STATE_REJECTED,
        CANDIDATE_STATE_EXPIRED,
        CANDIDATE_STATE_SUPPRESSED,
        CANDIDATE_STATE_AUTO_EXECUTED,
    }
)

__all__ = [
    "CANDIDATE_STATE_PROPOSED",
    "CANDIDATE_STATE_APPROVED",
    "CANDIDATE_STATE_REJECTED",
    "CANDIDATE_STATE_EXPIRED",
    "CANDIDATE_STATE_SUPPRESSED",
    "CANDIDATE_STATE_AUTO_EXECUTED",
    "CANDIDATE_STATES",
    "AgencyCandidate",
    "AgencyDecision",
    "AgencyApproval",
    "AgencyBudget",
    "AgencyBudgetEvent",
]


class AgencyCandidate(Base, TimestampMixin):
    """A bounded agency recommendation candidate."""

    __tablename__ = "agency_candidates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    candidate_key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    drive_type: Mapped[str] = mapped_column(String(40), nullable=False)
    source_type: Mapped[str] = mapped_column(String(80), nullable=False)
    source_refs: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb"), default=list
    )
    org_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), ForeignKey("orgs.id", ondelete="CASCADE"), nullable=True, index=True
    )
    user_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    target_binding_id: Mapped[Optional[str]] = mapped_column(
        String(120), nullable=True, index=True
    )
    proposal_kind: Mapped[str] = mapped_column(String(80), nullable=False)
    proposed_run_payload: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
    risk_class: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("'low'"), default="low"
    )
    reversibility_class: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("'read_only'"),
        default="read_only",
    )
    expected_value: Mapped[float] = mapped_column(
        Float, nullable=False, server_default=text("0.0"), default=0.0
    )
    novelty_score: Mapped[float] = mapped_column(
        Float, nullable=False, server_default=text("0.0"), default=0.0
    )
    urgency_score: Mapped[float] = mapped_column(
        Float, nullable=False, server_default=text("0.0"), default=0.0
    )
    estimated_cost: Mapped[float] = mapped_column(
        Float, nullable=False, server_default=text("0.0"), default=0.0
    )
    estimated_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), default=0
    )
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, server_default=text("'proposed'"),
        default=CANDIDATE_STATE_PROPOSED,
    )
    suppression_until: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        UniqueConstraint("candidate_key", name="uq_agency_candidates_candidate_key"),
        CheckConstraint(
            "status IN ('proposed', 'approved', 'rejected', 'expired', 'suppressed', 'auto_executed')",
            name="ck_agency_candidates_status",
        ),
    )


class AgencyDecision(Base, CreatedAtMixin):
    """A review decision over an agency candidate."""

    __tablename__ = "agency_decisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    candidate_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("agency_candidates.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    decision: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_type: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'system'"), default="system"
    )
    actor_id: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    reason_code: Mapped[str] = mapped_column(String(80), nullable=False)
    reason_text: Mapped[str] = mapped_column(Text, nullable=False)
    policy_snapshot: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
    budget_snapshot: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
    scheduler_run_id: Mapped[Optional[int]] = mapped_column(
        Integer,
        ForeignKey("scheduler_runs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    run_id: Mapped[Optional[int]] = mapped_column(
        Integer,
        ForeignKey("agent_runs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )


class AgencyApproval(Base, CreatedAtMixin):
    """Explicit approval grant for an agency candidate."""

    __tablename__ = "agency_approvals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    candidate_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("agency_candidates.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    actor_id: Mapped[str] = mapped_column(String(120), nullable=False)
    actor_role: Mapped[str] = mapped_column(String(40), nullable=False)
    approval_kind: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'manual'"), default="manual"
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("TRUE"), default=True
    )


class AgencyBudget(Base):
    """Budget window for a bounded agency scope."""

    __tablename__ = "agency_budgets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scope_type: Mapped[str] = mapped_column(String(20), nullable=False)
    scope_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    drive_type: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    max_candidates: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), default=0
    )
    max_auto_exec: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), default=0
    )
    max_estimated_cost: Mapped[float] = mapped_column(
        Float, nullable=False, server_default=text("0.0"), default=0.0
    )
    max_estimated_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), default=0
    )
    require_review_above_risk: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("'medium'"),
        default="medium",
    )
    auto_execute_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("FALSE"), default=False
    )
    cooldown_hours: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("24"), default=24
    )
    consumed_candidates: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), default=0
    )
    consumed_auto_exec: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), default=0
    )
    consumed_cost: Mapped[float] = mapped_column(
        Float, nullable=False, server_default=text("0.0"), default=0.0
    )
    consumed_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), default=0
    )
    active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("TRUE"), default=True
    )

    __table_args__ = (
        UniqueConstraint(
            "scope_type", "scope_id", "drive_type", "window_start",
            name="uq_agency_budgets_scope_window",
        ),
    )


class AgencyBudgetEvent(Base, CreatedAtMixin):
    """Append-only event ledger for budget reservations and releases."""

    __tablename__ = "agency_budget_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    budget_id: Mapped[Optional[int]] = mapped_column(
        Integer,
        ForeignKey("agency_budgets.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    candidate_id: Mapped[Optional[int]] = mapped_column(
        Integer,
        ForeignKey("agency_candidates.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    decision_id: Mapped[Optional[int]] = mapped_column(
        Integer,
        ForeignKey("agency_decisions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    event_type: Mapped[str] = mapped_column(String(40), nullable=False)
    scope_type: Mapped[str] = mapped_column(String(20), nullable=False)
    scope_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    drive_type: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    actor_type: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'system'"), default="system"
    )
    actor_id: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    reason_code: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    delta_candidates: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), default=0
    )
    delta_auto_exec: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), default=0
    )
    delta_cost: Mapped[float] = mapped_column(
        Float, nullable=False, server_default=text("0.0"), default=0.0
    )
    delta_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), default=0
    )
    before_snapshot: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
    after_snapshot: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
    event_metadata: Mapped[dict] = mapped_column(
        "metadata",
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
        default=dict,
    )
