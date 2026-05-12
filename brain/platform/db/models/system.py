"""System/infrastructure models."""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Double,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
    func,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from brain.platform.db.base import Base, CreatedAtMixin, TimestampMixin

__all__ = [
    "ConsolidationRun",
    "DailyMetrics",
    "OrgProviderModelMapping",
    "RetrievalLog",
    "RetrievalDecision",
    "RetrievalItemFeedback",
    "ErrorPipelineRun",
]


class ConsolidationRun(Base):
    """A nightly consolidation/dream/reflect run."""

    __tablename__ = "consolidation_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_date: Mapped[date] = mapped_column(Date, nullable=False)
    phase: Mapped[str] = mapped_column(String(20), nullable=False)
    started_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        server_default=text("NOW()"), nullable=True
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    memories_created: Mapped[int] = mapped_column(
        Integer, server_default=text("0"), default=0
    )
    memories_decayed: Mapped[int] = mapped_column(
        Integer, server_default=text("0"), default=0
    )
    edges_created: Mapped[int] = mapped_column(
        Integer, server_default=text("0"), default=0
    )
    patterns_detected: Mapped[int] = mapped_column(
        Integer, server_default=text("0"), default=0
    )
    summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), server_default="running", default="running"
    )
    user_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), ForeignKey("users.id"), nullable=True
    )
    org_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), ForeignKey("orgs.id"), nullable=True
    )


class DailyMetrics(Base):
    """Daily aggregate metrics."""

    __tablename__ = "daily_metrics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    metric_date: Mapped[date] = mapped_column(Date, nullable=False, unique=True)
    retrieval_attempts: Mapped[int] = mapped_column(
        Integer, server_default=text("0"), default=0
    )
    retrieval_hits: Mapped[int] = mapped_column(
        Integer, server_default=text("0"), default=0
    )
    retrieval_misses: Mapped[int] = mapped_column(
        Integer, server_default=text("0"), default=0
    )
    mistakes_total: Mapped[int] = mapped_column(
        Integer, server_default=text("0"), default=0
    )
    mistakes_known: Mapped[int] = mapped_column(
        Integer, server_default=text("0"), default=0
    )
    mistakes_new: Mapped[int] = mapped_column(
        Integer, server_default=text("0"), default=0
    )
    corrections_by_operator: Mapped[int] = mapped_column(
        Integer, server_default=text("0"), default=0
    )
    competence_architecture: Mapped[Optional[float]] = mapped_column(
        Double, nullable=True
    )
    competence_debugging: Mapped[Optional[float]] = mapped_column(
        Double, nullable=True
    )
    competence_frontend: Mapped[Optional[float]] = mapped_column(
        Double, nullable=True
    )
    competence_provider_apis: Mapped[Optional[float]] = mapped_column(
        Double, nullable=True
    )
    competence_communication: Mapped[Optional[float]] = mapped_column(
        Double, nullable=True
    )
    competence_proactivity: Mapped[Optional[float]] = mapped_column(
        Double, nullable=True
    )
    total_exchanges: Mapped[int] = mapped_column(
        Integer, server_default=text("0"), default=0
    )
    reflection_notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    behavioral_adjustments: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class OrgProviderModelMapping(Base, CreatedAtMixin):
    """Org-level mapping from intelligence tier to concrete provider model."""

    __tablename__ = "org_provider_model_mappings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    org_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("orgs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    intelligence_level: Mapped[str] = mapped_column(String(20), nullable=False)
    model_name: Mapped[str] = mapped_column(String(120), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "org_id",
            "provider",
            "intelligence_level",
            name="uq_org_provider_model_mappings_org_provider_level",
        ),
    )


class RetrievalLog(Base):
    """Log of memory retrieval attempts."""

    __tablename__ = "retrieval_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime] = mapped_column(
        "timestamp", DateTime(timezone=True), server_default=func.now()
    )
    query_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    results_returned: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    top_result_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("memories.id"), nullable=True
    )
    top_score: Mapped[Optional[float]] = mapped_column(Double, nullable=True)
    was_relevant: Mapped[Optional[bool]] = mapped_column(nullable=True)
    feedback: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    org_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("orgs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )


class RetrievalDecision(Base, CreatedAtMixin):
    """Shadow/observe decision log for retrieval and frame assembly."""

    __tablename__ = "retrieval_decisions"
    __table_args__ = (
        Index("ix_retrieval_decisions_run_stage", "run_id", "stage"),
        Index("ix_retrieval_decisions_stage_created", "stage", "created_at"),
        Index("ix_retrieval_decisions_org_user_created", "org_id", "user_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True
    )
    stage: Mapped[str] = mapped_column(String(50), nullable=False)
    query_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    query_fingerprint: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    policy_version: Mapped[str] = mapped_column(String(40), nullable=False)
    mode: Mapped[str] = mapped_column(String(20), nullable=False)
    user_id: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    org_id: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    preload_budget_tokens: Mapped[int] = mapped_column(
        Integer, server_default=text("0"), default=0
    )
    lazy_budget_tokens: Mapped[int] = mapped_column(
        Integer, server_default=text("0"), default=0
    )
    selected_item_ids: Mapped[Optional[list]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb"), default=list
    )
    suppressed_item_ids: Mapped[Optional[list]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb"), default=list
    )
    omission_risk_score: Mapped[Optional[float]] = mapped_column(Double, nullable=True)
    contradiction_risk_score: Mapped[Optional[float]] = mapped_column(Double, nullable=True)
    candidate_count: Mapped[int] = mapped_column(
        Integer, server_default=text("0"), default=0
    )
    decision_debug: Mapped[Optional[dict]] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), default=dict, nullable=False
    )


class RetrievalItemFeedback(Base):
    """Per-candidate usefulness and decision attribution for retrieval."""

    __tablename__ = "retrieval_item_feedback"
    __table_args__ = (
        Index("ix_retrieval_item_feedback_decision_memory", "retrieval_decision_id", "memory_id"),
        Index("ix_retrieval_item_feedback_decision_summary", "retrieval_decision_id", "summary_id"),
        Index("ix_retrieval_item_feedback_org_user", "org_id", "user_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    retrieval_decision_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("retrieval_decisions.id", ondelete="CASCADE"),
        nullable=False,
    )
    memory_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("memories.id", ondelete="CASCADE"), nullable=True
    )
    summary_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("memory_summaries.id", ondelete="CASCADE"), nullable=True
    )
    user_id: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    org_id: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    candidate_source: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    semantic_score: Mapped[Optional[float]] = mapped_column(Double, nullable=True)
    stage_score: Mapped[Optional[float]] = mapped_column(Double, nullable=True)
    freshness_score: Mapped[Optional[float]] = mapped_column(Double, nullable=True)
    novelty_score: Mapped[Optional[float]] = mapped_column(Double, nullable=True)
    prior_usefulness_score: Mapped[Optional[float]] = mapped_column(Double, nullable=True)
    contradiction_score: Mapped[Optional[float]] = mapped_column(Double, nullable=True)
    omission_risk_score: Mapped[Optional[float]] = mapped_column(Double, nullable=True)
    preload_decision: Mapped[bool] = mapped_column(
        Boolean, server_default=text("FALSE"), default=False
    )
    lazy_load_eligible: Mapped[bool] = mapped_column(
        Boolean, server_default=text("FALSE"), default=False
    )
    lazy_loaded: Mapped[bool] = mapped_column(
        Boolean, server_default=text("FALSE"), default=False
    )
    actually_used: Mapped[bool] = mapped_column(
        Boolean, server_default=text("FALSE"), default=False
    )
    cited_in_output: Mapped[bool] = mapped_column(
        Boolean, server_default=text("FALSE"), default=False
    )
    correlated_with_success: Mapped[bool] = mapped_column(
        Boolean, server_default=text("FALSE"), default=False
    )
    retry_delta: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    verifier_helped: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    user_feedback_signal: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    feedback_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class ErrorPipelineRun(Base, TimestampMixin):
    """Legacy Rollbar/Sentry pipeline residue retained for schema compatibility.

    Current run health visibility uses harness alert models in
    ``brain.platform.db.models.run``. Do not add new product behavior to this table;
    dropping it requires a later data/export review and migration.
    """

    __tablename__ = "error_pipeline_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    rollbar_counter_id: Mapped[Optional[int]] = mapped_column(
        Integer, unique=True, nullable=True
    )
    rollbar_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    error_title: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    github_issue_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    github_pr_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    slack_channel_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    slack_message_ts: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        Text, server_default="detected", default="detected"
    )
    repo: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    branch_name: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
