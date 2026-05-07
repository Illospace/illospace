"""Quality tracking models.

Matches: delegation_quality, critic_reviews, execution_outcomes tables.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from brain.platform.db.base import Base, CreatedAtMixin

__all__ = ["DelegationQuality", "CriticReview", "ExecutionOutcome"]


class DelegationQuality(Base, CreatedAtMixin):
    """Quality log for a delegated task."""

    __tablename__ = "delegation_quality"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_key: Mapped[str] = mapped_column(Text, nullable=False)
    original_ask: Mapped[str] = mapped_column(Text, nullable=False)
    task_delegated: Mapped[str] = mapped_column(Text, nullable=False)
    sub_agent_output: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    quality_score: Mapped[float] = mapped_column(
        Float, server_default=text("0.0"), default=0.0
    )
    rounds_needed: Mapped[int] = mapped_column(
        Integer, server_default=text("1"), default=1
    )


class CriticReview(Base, CreatedAtMixin):
    """A critic review of a skill execution."""

    __tablename__ = "critic_reviews"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    execution_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("skill_executions.id"), nullable=False
    )
    critic_skill_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("skills.id"), nullable=True
    )
    findings: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'"), default=list
    )
    scores: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'"), default=dict
    )
    verdict: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="approve", default="approve"
    )


class ExecutionOutcome(Base, CreatedAtMixin):
    """Ground-truth outcome for a skill execution."""

    __tablename__ = "execution_outcomes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    execution_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("skill_executions.id"), nullable=False
    )
    critic_review_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("critic_reviews.id"), nullable=True
    )
    outcome: Mapped[str] = mapped_column(String(20), nullable=False)
    outcome_source: Mapped[str] = mapped_column(
        String(50), nullable=False, server_default="user_feedback", default="user_feedback"
    )
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
