"""Scheduler control-plane models."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from brain.platform.db.base import Base, CreatedAtMixin

OWNER_MODE_CRON = "cron"
OWNER_MODE_MIRROR = "mirror"
OWNER_MODE_SCHEDULER = "scheduler"

__all__ = [
    "OWNER_MODE_CRON",
    "OWNER_MODE_MIRROR",
    "OWNER_MODE_SCHEDULER",
    "SchedulerJob",
    "SchedulerRun",
    "SchedulerLease",
    "SchedulerRunStep",
]


class SchedulerJob(Base, CreatedAtMixin):
    """A scheduler-visible background job family."""

    __tablename__ = "scheduler_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_key: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    family: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    program_key: Mapped[str] = mapped_column(String(120), nullable=False)
    handler_kind: Mapped[str] = mapped_column(String(40), nullable=False)
    handler_ref: Mapped[str] = mapped_column(Text, nullable=False)
    cron_expr: Mapped[str] = mapped_column(String(120), nullable=False)
    timezone: Mapped[str] = mapped_column(String(80), nullable=False)
    enabled: Mapped[bool] = mapped_column(
        Boolean, server_default=text("TRUE"), default=True
    )
    owner_mode: Mapped[str] = mapped_column(
        String(20), server_default=text("'scheduler'"), default=OWNER_MODE_SCHEDULER
    )
    priority: Mapped[int] = mapped_column(
        Integer, server_default=text("100"), default=100
    )
    max_concurrency: Mapped[int] = mapped_column(
        Integer, server_default=text("1"), default=1
    )
    timeout_seconds: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    retry_policy: Mapped[Optional[dict]] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), default=dict
    )
    misfire_policy: Mapped[str] = mapped_column(
        String(20), server_default=text("'record'"), default="record"
    )
    load_shed_policy: Mapped[Optional[dict]] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), default=dict
    )
    default_payload: Mapped[Optional[dict]] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), default=dict
    )
    target_binding_selector: Mapped[Optional[dict]] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), default=dict
    )
    task_contract: Mapped[Optional[dict]] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), default=dict
    )
    next_run_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    pause_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    last_started_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_finished_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    failure_signature: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    consecutive_failure_count: Mapped[int] = mapped_column(
        Integer, server_default=text("0"), default=0
    )
    failure_alerted_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_failure_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("job_key", name="uq_scheduler_jobs_job_key"),
        UniqueConstraint("family", name="uq_scheduler_jobs_family"),
        CheckConstraint(
            f"owner_mode IN ('{OWNER_MODE_CRON}', '{OWNER_MODE_MIRROR}', '{OWNER_MODE_SCHEDULER}')",
            name="ck_scheduler_jobs_owner_mode",
        ),
        CheckConstraint(
            "misfire_policy IN ('record', 'skip', 'catch_up')",
            name="ck_scheduler_jobs_misfire_policy",
        ),
    )


class SchedulerRun(Base, CreatedAtMixin):
    """A materialized due run for a scheduler job."""

    __tablename__ = "scheduler_runs"
    __table_args__ = (
        Index("ix_scheduler_runs_trace_id", "trace_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("scheduler_jobs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    scheduled_for: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    window_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    window_end: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(20), server_default=text("'recorded'"), default="recorded"
    )
    attempt: Mapped[int] = mapped_column(
        Integer, server_default=text("1"), default=1
    )
    idempotency_key: Mapped[str] = mapped_column(
        String(255), nullable=False, unique=True
    )
    payload: Mapped[Optional[dict]] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), default=dict
    )
    result_summary: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    error_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    task_contract: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    lease_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("scheduler_leases.id", ondelete="SET NULL"), nullable=True
    )
    agent_run_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    trace_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    parent_run_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("scheduler_runs.id", ondelete="SET NULL"), nullable=True
    )
    started_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class SchedulerLease(Base):
    """A lease over a scheduler run."""

    __tablename__ = "scheduler_leases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("scheduler_runs.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    owner_id: Mapped[str] = mapped_column(String(120), nullable=False)
    owner_host: Mapped[str] = mapped_column(String(255), nullable=False)
    owner_pid: Mapped[int] = mapped_column(Integer, nullable=False)
    acquired_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("NOW()")
    )
    heartbeat_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("NOW()")
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    released_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    release_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class SchedulerRunStep(Base):
    """A persisted step inside a multi-step scheduler run."""

    __tablename__ = "scheduler_run_steps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("scheduler_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    step_key: Mapped[str] = mapped_column(String(120), nullable=False)
    sequence_no: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), server_default=text("'pending'"), default="pending"
    )
    attempt: Mapped[int] = mapped_column(
        Integer, server_default=text("1"), default=1
    )
    agent_run_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    trace_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    result_summary: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    error_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("ix_scheduler_run_steps_trace_id", "trace_id"),
        UniqueConstraint("run_id", "step_key", name="uq_scheduler_run_steps_run_key"),
        UniqueConstraint(
            "run_id", "sequence_no", name="uq_scheduler_run_steps_run_sequence"
        ),
    )
