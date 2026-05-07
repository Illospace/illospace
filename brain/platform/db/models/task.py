"""Task and scheduler run-log models.

Agent worker execution truth lives in child ``agent_runs`` rows and
``agent_run_artifacts``.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Computed,
    Double,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from brain.platform.db.base import Base, CreatedAtMixin

try:
    from pgvector.sqlalchemy import Vector
except ImportError:  # pragma: no cover
    from sqlalchemy import PickleType as Vector  # type: ignore[assignment]

__all__ = ["Task", "RunLog"]


class Task(Base, CreatedAtMixin):
    """A high-level task with embedding."""

    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    task_type: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    complexity_estimate: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    strategy_chosen: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    plan: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    similar_past_tasks: Mapped[Optional[list]] = mapped_column(
        ARRAY(Integer), server_default=text("'{}'"), default=list
    )
    memory_ids_recalled: Mapped[Optional[list]] = mapped_column(
        ARRAY(Integer), server_default=text("'{}'"), default=list
    )
    guardrails: Mapped[Optional[list]] = mapped_column(
        ARRAY(Text), server_default=text("'{}'"), default=list
    )
    skills_used: Mapped[Optional[list]] = mapped_column(
        ARRAY(Integer), server_default=text("'{}'"), default=list
    )
    started_at: Mapped[Optional[datetime]] = mapped_column(
        server_default=text("NOW()"), nullable=True
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    duration_sec: Mapped[Optional[float]] = mapped_column(Double, nullable=True)
    outcome: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    outcome_details: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    operator_satisfaction: Mapped[Optional[float]] = mapped_column(Double, nullable=True)
    feedback_notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    embedding: Mapped[Optional[object]] = mapped_column(
        Vector(2000), nullable=True
    )


class RunLog(Base):
    """Log entry for each runed task."""

    __tablename__ = "run_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_summary: Mapped[str] = mapped_column(Text, nullable=False)
    task_type: Mapped[str] = mapped_column(String(50), nullable=False)
    template_used: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    skill_name: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    memories_injected: Mapped[Optional[list]] = mapped_column(
        ARRAY(Integer), server_default=text("'{}'"), default=list
    )
    guardrails_injected: Mapped[Optional[list]] = mapped_column(
        ARRAY(Text), server_default=text("'{}'"), default=list
    )
    similar_past_ids: Mapped[Optional[list]] = mapped_column(
        ARRAY(Integer), server_default=text("'{}'"), default=list
    )
    session_key: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    model: Mapped[str] = mapped_column(
        String(50), nullable=False, server_default="medium", default="medium"
    )
    thinking_level: Mapped[Optional[str]] = mapped_column(
        String(20), server_default="low", default="low"
    )
    runed_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=text("NOW()")
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    outcome: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    outcome_notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    duration_s: Mapped[Optional[int]] = mapped_column(
        Integer,
        Computed("EXTRACT(EPOCH FROM (completed_at - runed_at))::INTEGER"),
        nullable=True,
    )
    prompt_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    payload_json: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
