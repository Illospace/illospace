"""Run genome, scoped policy promotion, learning example, and practice-loop models."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from brain.platform.db.base import Base, CreatedAtMixin

__all__ = [
    "RunGenome",
    "LearningExample",
    "LearningSignal",
    "PolicyUpdateCandidate",
    "PolicyPromotion",
    "PracticeRun",
    "TrajectoryEvalCase",
]


class RunGenome(Base, CreatedAtMixin):
    """A compact, fact-derived signature for a run."""

    __tablename__ = "run_genomes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    org_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), ForeignKey("orgs.id", ondelete="SET NULL"), nullable=True
    )
    visibility: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="private", default="private"
    )
    genome_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    task_family: Mapped[str] = mapped_column(String(80), nullable=False)
    target_family: Mapped[str] = mapped_column(String(80), nullable=False)
    context_profile: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
    strategy_name: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    skill_name: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    model_mix: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
    tool_mix: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
    retrieval_profile: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
    verifier_outcome: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
    contract_type: Mapped[str] = mapped_column(String(40), nullable=False)
    token_cost_bucket: Mapped[str] = mapped_column(String(20), nullable=False)
    latency_bucket: Mapped[str] = mapped_column(String(20), nullable=False)
    success: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("FALSE"), default=False
    )
    rework_required: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("FALSE"), default=False
    )
    satisfaction_proxy: Mapped[float] = mapped_column(Float, nullable=False)
    evidence_status: Mapped[str] = mapped_column(
        String(30), nullable=False, server_default="unverified", default="unverified"
    )
    learning_outcome: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="neutral", default="neutral"
    )
    evidence_gate: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )

    __table_args__ = (
        UniqueConstraint("run_id", name="uq_run_genomes_run_id"),
        Index("ix_run_genomes_org_created", "org_id", "created_at"),
        Index("ix_run_genomes_evidence_status", "evidence_status"),
    )


class LearningExample(Base, CreatedAtMixin):
    """A scoped example learned from an episode without promoting it to policy."""

    __tablename__ = "learning_examples"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True
    )
    genome_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("run_genomes.id", ondelete="SET NULL"), nullable=True
    )
    user_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    org_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), ForeignKey("orgs.id", ondelete="SET NULL"), nullable=True
    )
    visibility: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="private", default="private"
    )
    example_type: Mapped[str] = mapped_column(String(30), nullable=False)
    evidence_status: Mapped[str] = mapped_column(String(30), nullable=False)
    task_family: Mapped[str] = mapped_column(String(80), nullable=False)
    target_family: Mapped[str] = mapped_column(String(80), nullable=False)
    skill_name: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    lesson: Mapped[str] = mapped_column(Text, nullable=False)
    evidence: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
    signals: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )

    __table_args__ = (
        Index("ix_learning_examples_org_type_created", "org_id", "example_type", "created_at"),
        Index("ix_learning_examples_run_id", "run_id"),
        Index("ix_learning_examples_skill", "skill_name"),
    )


class PolicyPromotion(Base, CreatedAtMixin):
    """A versioned policy recommendation derived from evidence."""

    __tablename__ = "policy_promotions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    promotion_type: Mapped[str] = mapped_column(String(50), nullable=False)
    source_kind: Mapped[str] = mapped_column(String(50), nullable=False)
    user_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    org_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), ForeignKey("orgs.id", ondelete="SET NULL"), nullable=True
    )
    visibility: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="private", default="private"
    )
    source_refs: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
    policy_payload: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="recommended", default="recommended"
    )
    evidence: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1"), default=1
    )
    shadow_metrics: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
    activated_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    rolled_back_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    demoted_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    demotion_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    explicit_global_promotion: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("FALSE"), default=False
    )
    reviewer_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), ForeignKey("users.id"), nullable=True
    )

    __table_args__ = (
        Index("ix_policy_promotions_type_status_version", "promotion_type", "status", "version"),
        Index("ix_policy_promotions_source_kind", "source_kind"),
        Index("ix_policy_promotions_org_type_status", "org_id", "promotion_type", "status"),
    )


class PracticeRun(Base, CreatedAtMixin):
    """An isolated practice record for a weak skill or policy."""

    __tablename__ = "practice_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    origin_skill_name: Mapped[str] = mapped_column(Text, nullable=False)
    user_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    org_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), ForeignKey("orgs.id", ondelete="SET NULL"), nullable=True
    )
    visibility: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="private", default="private"
    )
    origin_policy_promotion_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("policy_promotions.id", ondelete="SET NULL"), nullable=True
    )
    synthesized_task: Mapped[str] = mapped_column(Text, nullable=False)
    isolation_mode: Mapped[str] = mapped_column(String(30), nullable=False)
    workspace_template: Mapped[str] = mapped_column(Text, nullable=False)
    cost_budget: Mapped[float] = mapped_column(Float, nullable=False)
    run_status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="queued", default="queued"
    )
    run_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True
    )
    outcome: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
    score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    touched_production: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("FALSE"), default=False
    )


class LearningSignal(Base, CreatedAtMixin):
    """Tenant-scoped evidence extracted from a run trajectory or evaluator."""

    __tablename__ = "learning_signals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    signal_digest: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    signal_type: Mapped[str] = mapped_column(String(60), nullable=False)
    status: Mapped[str] = mapped_column(
        String(30), nullable=False, server_default="recorded", default="recorded"
    )
    review_status: Mapped[str] = mapped_column(
        String(30), nullable=False, server_default="unreviewed", default="unreviewed"
    )
    source_run_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True
    )
    trace_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    trajectory_digest: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    context_pack_digest: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    skill_effective_digest: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    user_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    org_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), ForeignKey("orgs.id", ondelete="SET NULL"), nullable=True
    )
    visibility: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="private", default="private"
    )
    outcome_label: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    label_confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    payload: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
    evidence: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
    applied_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    rolled_back_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_learning_signals_org_type_created", "org_id", "signal_type", "created_at"),
        Index("ix_learning_signals_run", "source_run_id"),
        Index("ix_learning_signals_skill_digest", "skill_effective_digest"),
        Index("ix_learning_signals_status_review", "status", "review_status"),
    )


class TrajectoryEvalCase(Base, CreatedAtMixin):
    """A compact, redaction-mode-specific eval case derived from a trajectory."""

    __tablename__ = "trajectory_eval_cases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    eval_digest: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"), default=1)
    redaction_mode: Mapped[str] = mapped_column(String(30), nullable=False, server_default="eval", default="eval")
    status: Mapped[str] = mapped_column(String(30), nullable=False, server_default="active", default="active")
    source_run_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True
    )
    trace_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    trajectory_digest: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    context_pack_digest: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    skill_effective_digest: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    user_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    org_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), ForeignKey("orgs.id", ondelete="SET NULL"), nullable=True
    )
    visibility: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="private", default="private"
    )
    payload: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
    quality: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )

    __table_args__ = (
        Index("ix_trajectory_eval_cases_org_status_created", "org_id", "status", "created_at"),
        Index("ix_trajectory_eval_cases_run", "source_run_id"),
        Index("ix_trajectory_eval_cases_skill_digest", "skill_effective_digest"),
    )


class PolicyUpdateCandidate(Base, CreatedAtMixin):
    """A proposed active-learning policy change that can be reviewed or rolled back."""

    __tablename__ = "policy_update_candidates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    candidate_digest: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    candidate_type: Mapped[str] = mapped_column(String(60), nullable=False)
    status: Mapped[str] = mapped_column(
        String(30), nullable=False, server_default="proposed", default="proposed"
    )
    review_status: Mapped[str] = mapped_column(
        String(30), nullable=False, server_default="unreviewed", default="unreviewed"
    )
    user_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    org_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), ForeignKey("orgs.id", ondelete="SET NULL"), nullable=True
    )
    visibility: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="private", default="private"
    )
    source_signal_ids: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb"), default=list
    )
    policy_payload: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
    evaluation_payload: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
    applied_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    rolled_back_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_policy_update_candidates_org_type_status", "org_id", "candidate_type", "status"),
        Index("ix_policy_update_candidates_review", "review_status"),
    )
