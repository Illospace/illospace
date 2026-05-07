"""Skill quality evidence models."""
from __future__ import annotations

from typing import Optional

from sqlalchemy import ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from brain.platform.db.base import Base, CreatedAtMixin

__all__ = ["SkillRunEvidence"]


class SkillRunEvidence(Base, CreatedAtMixin):
    """Per-run evidence for later skill quality scoring."""

    __tablename__ = "skill_run_evidence"
    __table_args__ = (
        UniqueConstraint(
            "run_id",
            "skill_effective_digest",
            name="uq_skill_run_evidence_run_digest",
        ),
        Index("ix_skill_run_evidence_skill_digest", "skill_effective_digest"),
        Index("ix_skill_run_evidence_skill_name", "skill_name"),
        Index("ix_skill_run_evidence_bundle_identity", "bundle_namespace", "bundle_name"),
        Index("ix_skill_run_evidence_org_user", "org_id", "user_id"),
        Index("ix_skill_run_evidence_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    skill_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("skills.id", ondelete="SET NULL"), nullable=True
    )
    skill_name: Mapped[str] = mapped_column(String(80), nullable=False)
    skill_effective_digest: Mapped[str] = mapped_column(String(96), nullable=False)

    bundle_namespace: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    bundle_name: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    bundle_version: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    bundle_digest: Mapped[Optional[str]] = mapped_column(String(96), nullable=True)

    run_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True
    )
    trace_id: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    task_class: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)

    outcome_label: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    verifier_status: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    user_feedback: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)

    token_bucket: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    total_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    cost_bucket: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    cost_usd: Mapped[Optional[float]] = mapped_column(Numeric(12, 6), nullable=True)
    runtime_bucket: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    runtime_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    tool_risk_class: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    action_risk_class: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    evidence_source: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    org_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), ForeignKey("orgs.id", ondelete="SET NULL"), nullable=True
    )
    user_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
