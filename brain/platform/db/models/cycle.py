"""Cycle scheduling and durable Cycle memory models."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Boolean, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from brain.platform.db.base import Base, CreatedAtMixin, TimestampMixin

__all__ = [
    "Cycle",
    "CycleRevision",
    "CycleGuidance",
    "CycleOutputTarget",
    "CycleRun",
    "CycleRunEvaluation",
]


class Cycle(Base, TimestampMixin):
    """A workspace-owned recurring mission."""

    __tablename__ = "cycles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    org_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), ForeignKey("orgs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    creator_type: Mapped[str] = mapped_column(
        String(30), nullable=False, server_default="user", default="user"
    )
    creator_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    maintainer_type: Mapped[str] = mapped_column(
        String(30), nullable=False, server_default="user", default="user"
    )
    maintainer_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    schedule_expr: Mapped[str] = mapped_column(Text, nullable=False)
    timezone: Mapped[str] = mapped_column(
        String(64), nullable=False, server_default="UTC", default="UTC"
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("TRUE"), default=True
    )
    max_concurrency: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1"), default=1
    )
    timeout_seconds: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    retry_policy: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
    model_override: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    thinking_override: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    execution_mode: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        server_default="reuse_same_idea",
        default="reuse_same_idea",
    )
    target_idea_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), ForeignKey("ideas.id", ondelete="SET NULL"), nullable=True, index=True
    )
    reopen_archived: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("TRUE"), default=True
    )
    next_run_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    last_run_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_status: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    degradation_state: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
    exception_ping_state: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
    deleted_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )


class CycleRevision(Base, CreatedAtMixin):
    """Immutable mission/configuration revision for a Cycle."""

    __tablename__ = "cycle_revisions"
    __table_args__ = (
        UniqueConstraint("cycle_id", "revision_number", name="uq_cycle_revisions_cycle_number"),
        Index("ix_cycle_revisions_cycle_created", "cycle_id", "created_at", "id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    cycle_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("cycles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    revision_number: Mapped[int] = mapped_column(Integer, nullable=False)
    source_type: Mapped[str] = mapped_column(
        String(30), nullable=False, server_default="system", default="system"
    )
    source_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    rationale: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    schedule_expr: Mapped[str] = mapped_column(Text, nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    model_override: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    thinking_override: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    target_idea_id: Mapped[Optional[str]] = mapped_column(UUID(as_uuid=False), nullable=True)
    context_policy: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )


class CycleGuidance(Base, CreatedAtMixin):
    """Durable guidance that orients future Cycle runs."""

    __tablename__ = "cycle_guidance"
    __table_args__ = (
        Index("ix_cycle_guidance_cycle_active", "cycle_id", "is_active", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    cycle_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("cycles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    revision_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("cycle_revisions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    source_type: Mapped[str] = mapped_column(
        String(30), nullable=False, server_default="user", default="user"
    )
    source_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    guidance: Mapped[str] = mapped_column(Text, nullable=False)
    rationale: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("TRUE"), default=True
    )


class CycleOutputTarget(Base, TimestampMixin):
    """A destination a CycleRun may publish to or repair/adapt."""

    __tablename__ = "cycle_output_targets"
    __table_args__ = (
        Index("ix_cycle_output_targets_cycle_active", "cycle_id", "is_active", "target_type"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    cycle_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("cycles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    revision_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("cycle_revisions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    target_type: Mapped[str] = mapped_column(String(50), nullable=False)
    target_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    label: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    config: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
    source_type: Mapped[str] = mapped_column(
        String(30), nullable=False, server_default="user", default="user"
    )
    source_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    rationale: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("TRUE"), default=True
    )


class CycleRun(Base, CreatedAtMixin):
    """A single triggered cycle run."""

    __tablename__ = "cycle_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    cycle_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("cycles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    revision_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("cycle_revisions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    scheduled_for: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="queued", default="queued"
    )
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    skip_reason: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    idea_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), ForeignKey("ideas.id", ondelete="SET NULL"), nullable=True, index=True
    )
    run_id: Mapped[Optional[int]] = mapped_column(
        Integer,
        ForeignKey("agent_runs.id", ondelete="SET NULL"),
        nullable=True,
        unique=True,
    )
    prompt_snapshot: Mapped[str] = mapped_column(Text, nullable=False)
    guidance_snapshot: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb"), default=list
    )
    output_targets_snapshot: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb"), default=list
    )
    context_snapshot: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
    self_review_summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class CycleRunEvaluation(Base, CreatedAtMixin):
    """Ledger entry recording how a CycleRun evaluated itself or was settled."""

    __tablename__ = "cycle_run_evaluations"
    __table_args__ = (
        Index("ix_cycle_run_evaluations_cycle_created", "cycle_id", "created_at", "id"),
        Index("ix_cycle_run_evaluations_run_created", "cycle_run_id", "created_at", "id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    cycle_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("cycles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    cycle_run_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("cycle_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    evaluator_type: Mapped[str] = mapped_column(
        String(30), nullable=False, server_default="system", default="system"
    )
    evaluator_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    score: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    details: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
