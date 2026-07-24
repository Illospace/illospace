"""Routing marketplace persistence models."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from brain.platform.db.base import Base, CreatedAtMixin

__all__ = [
    "ProviderHealthSnapshot",
    "RoutingDecision",
    "RoutingExperiment",
]


class ProviderHealthSnapshot(Base, CreatedAtMixin):
    """Windowed provider/model health evidence used by the router."""

    __tablename__ = "provider_health_snapshots"
    __table_args__ = (
        Index("ix_provider_health_snapshots_provider_model", "provider", "model"),
        Index("ix_provider_health_snapshots_window_end", "window_end"),
        UniqueConstraint(
            "provider",
            "model",
            "window_start",
            "window_end",
            "source",
            name="uq_provider_health_snapshots_window",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(120), nullable=False)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    p50_latency_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    p95_latency_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    error_rate: Mapped[Optional[float]] = mapped_column(nullable=True)
    auth_fail_rate: Mapped[Optional[float]] = mapped_column(nullable=True)
    rate_limit_rate: Mapped[Optional[float]] = mapped_column(nullable=True)
    sample_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"), default=0)
    source: Mapped[str] = mapped_column(Text, nullable=False)


class RoutingDecision(Base, CreatedAtMixin):
    """Persisted routing decision for shadow or active marketplace routing."""

    __tablename__ = "routing_decisions"
    __table_args__ = (
        Index("ix_routing_decisions_task_family_lane", "task_family", "lane"),
        Index("ix_routing_decisions_created_at", "created_at"),
        UniqueConstraint("run_id", name="uq_routing_decisions_run_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    task_family: Mapped[str] = mapped_column(Text, nullable=False)
    lane: Mapped[str] = mapped_column(Text, nullable=False)
    decision_mode: Mapped[str] = mapped_column(Text, nullable=False)
    selected_provider: Mapped[str] = mapped_column(String(32), nullable=False)
    selected_model: Mapped[str] = mapped_column(String(120), nullable=False)
    inputs: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict)
    candidate_scores: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"), default=list)
    constraints: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict)
    experiment_id: Mapped[Optional[int]] = mapped_column(
        Integer,
        ForeignKey("routing_experiments.id", ondelete="SET NULL"),
        nullable=True,
    )
    applied: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("FALSE"), default=False)
    fallback_used: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("FALSE"), default=False)
    post_run_outcome: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)


class RoutingExperiment(Base, CreatedAtMixin):
    """Routing experiment metadata for staged marketplace rollout."""

    __tablename__ = "routing_experiments"
    __table_args__ = (
        Index("ix_routing_experiments_status", "status"),
        UniqueConstraint("name", name="uq_routing_experiments_name"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    task_family_filter: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)
    allocation_policy: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
