"""Habit compiler and execution models.

Habits are narrower than skills: they compile repeated successful run
trajectories into versioned execution artifacts with explicit fallback
semantics.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from brain.platform.db.base import Base, CreatedAtMixin

__all__ = [
    "RunHabit",
    "HabitVersion",
    "HabitExecution",
]


class RunHabit(Base, CreatedAtMixin):
    """A narrow habit family compiled from repeated successful runs."""

    __tablename__ = "run_habits"
    __table_args__ = (
        UniqueConstraint(
            "task_family",
            "signature_hash",
            name="uq_run_habits_task_family_signature_hash",
        ),
        Index("ix_run_habits_task_family", "task_family"),
        Index("ix_run_habits_signature_hash", "signature_hash"),
        Index("ix_run_habits_status", "status"),
        Index("ix_run_habits_active_version_id", "active_version_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_family: Mapped[str] = mapped_column(Text, nullable=False)
    signature_hash: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        Text, server_default="draft", default="draft",
    )
    source_skill: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    active_version_id: Mapped[Optional[int]] = mapped_column(
        Integer,
        ForeignKey("habit_versions.id", ondelete="SET NULL", use_alter=True),
        nullable=True,
    )
    eligibility_metrics: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
    verifier_profile: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )


class HabitVersion(Base, CreatedAtMixin):
    """Immutable compiled version for a habit family."""

    __tablename__ = "habit_versions"
    __table_args__ = (
        UniqueConstraint(
            "habit_id",
            "version",
            name="uq_habit_versions_habit_version",
        ),
        Index("ix_habit_versions_habit_id", "habit_id"),
        Index("ix_habit_versions_version", "version"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    habit_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("run_habits.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    matcher: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
    preconditions: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
    step_graph: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb"), default=list
    )
    expected_artifacts: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
    fallback_policy: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
    source_run_ids: Mapped[list[int]] = mapped_column(
        ARRAY(Integer), nullable=False, server_default=text("'{}'"), default=list
    )
    shadow_stats: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )


class HabitExecution(Base, CreatedAtMixin):
    """Per-run habit evaluation or execution record."""

    __tablename__ = "habit_executions"
    __table_args__ = (
        UniqueConstraint(
            "run_id",
            "habit_version_id",
            name="uq_habit_executions_run_version",
        ),
        Index("ix_habit_executions_run_id", "run_id"),
        Index("ix_habit_executions_habit_id", "habit_id"),
        Index("ix_habit_executions_status", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False
    )
    habit_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("run_habits.id", ondelete="CASCADE"), nullable=False
    )
    habit_version_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("habit_versions.id", ondelete="CASCADE"), nullable=False
    )
    match_confidence: Mapped[float] = mapped_column(
        Float, nullable=False, server_default=text("0"), default=0.0
    )
    guard_result: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
    status: Mapped[str] = mapped_column(Text, nullable=False)
    fallback_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    executed_steps: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb"), default=list
    )
    verifier_result: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    cost: Mapped[Optional[float]] = mapped_column(Numeric(10, 6), nullable=True)
