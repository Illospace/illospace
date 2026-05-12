"""Skill-related SQLAlchemy models.

Matches the SQL schema exactly:
  skills, skill_dependencies, skill_executions, skill_versions, skill_heuristics
"""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from sqlalchemy import (
    DateTime,
    ARRAY,
    Boolean,
    Double,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from brain.platform.db.base import ArchivableMixin, Base, CreatedAtMixin, TimestampMixin

try:
    from pgvector.sqlalchemy import Vector
except ImportError:  # pragma: no cover — pgvector optional in test env
    from sqlalchemy import PickleType as Vector  # type: ignore[assignment]

from brain.kernel.config import (
    SKILL_SEMANTIC_EMBEDDING_DIM,
    SKILL_TASK_CENTROID_EMBEDDING_DIM,
)

__all__ = [
    "Skill",
    "SkillDependency",
    "SkillExecution",
    "SkillVersion",
    "SkillHeuristic",
]


class Skill(Base, TimestampMixin, ArchivableMixin):
    """A skill — the core unit of reusable procedural knowledge."""

    __tablename__ = "skills"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(50), nullable=False, unique=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    procedure: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[int] = mapped_column(
        Integer, server_default=text("1"), default=1
    )
    parent_skill_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("skills.id"), nullable=True
    )
    level: Mapped[str] = mapped_column(
        String(20), server_default="cognitive", default="cognitive"
    )
    skill_type: Mapped[str] = mapped_column(
        String(20), server_default="skill", default="skill"
    )
    maturity: Mapped[str] = mapped_column(
        String(20), server_default="emerging", default="emerging"
    )
    confidence: Mapped[float] = mapped_column(
        Double, server_default=text("0.3"), default=0.3
    )
    use_count: Mapped[int] = mapped_column(
        Integer, server_default=text("0"), default=0
    )
    success_count: Mapped[int] = mapped_column(
        Integer, server_default=text("0"), default=0
    )
    failure_count: Mapped[int] = mapped_column(
        Integer, server_default=text("0"), default=0
    )
    partial_count: Mapped[int] = mapped_column(
        Integer, server_default=text("0"), default=0
    )
    avg_duration_sec: Mapped[Optional[float]] = mapped_column(Double, nullable=True)
    last_used: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    pitfalls: Mapped[Optional[list]] = mapped_column(
        JSONB, server_default=text("'[]'"), default=list
    )
    refinements: Mapped[Optional[list]] = mapped_column(
        JSONB, server_default=text("'[]'"), default=list
    )
    triggers: Mapped[Optional[list]] = mapped_column(
        JSONB, server_default=text("'[]'"), default=list
    )
    guardrails: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'"), default=list
    )
    embedding: Mapped[Optional[object]] = mapped_column(
        Vector(SKILL_SEMANTIC_EMBEDDING_DIM), nullable=True
    )
    task_centroid: Mapped[Optional[object]] = mapped_column(
        Vector(SKILL_TASK_CENTROID_EMBEDDING_DIM), nullable=True
    )
    centroid_count: Mapped[int] = mapped_column(
        Integer, server_default=text("0"), default=0
    )
    auto_emerged: Mapped[bool] = mapped_column(
        Boolean, server_default=text("FALSE"), default=False
    )
    provisional: Mapped[bool] = mapped_column(
        Boolean, server_default=text("FALSE"), default=False
    )
    builtin: Mapped[bool] = mapped_column(
        Boolean, server_default=text("FALSE"), default=False
    )
    model_tier: Mapped[str] = mapped_column(
        String(20), server_default="medium", default="medium"
    )
    thinking_tier: Mapped[str] = mapped_column(
        String(20), server_default="medium", default="medium"
    )
    generation: Mapped[int] = mapped_column(
        Integer, server_default=text("1"), default=1
    )
    procedure_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    fitness_score: Mapped[float] = mapped_column(
        Float, server_default=text("0.5"), default=0.5
    )
    graduated_steps: Mapped[Optional[list]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb"), default=list
    )
    last_distilled_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    heuristic_count: Mapped[int] = mapped_column(
        Integer, server_default=text("0"), default=0
    )
    skill_installation_id: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True
    )
    bundle_version_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("skill_bundle_versions.id"), nullable=True
    )
    bundle_digest: Mapped[Optional[str]] = mapped_column(String(96), nullable=True)
    overlay_revision: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    effective_digest: Mapped[Optional[str]] = mapped_column(String(96), nullable=True)
    source_kind: Mapped[str] = mapped_column(
        String(30), server_default="legacy_db", default="legacy_db"
    )
    trust_level: Mapped[str] = mapped_column(
        String(30), server_default="private_local", default="private_local"
    )

    # Relationships
    executions: Mapped[List["SkillExecution"]] = relationship(
        "SkillExecution", back_populates="skill", lazy="noload"
    )
    children: Mapped[List["SkillDependency"]] = relationship(
        "SkillDependency",
        foreign_keys="SkillDependency.parent_id",
        lazy="noload",
    )
    versions: Mapped[List["SkillVersion"]] = relationship(
        "SkillVersion", back_populates="skill", lazy="noload"
    )

    @property
    def success_rate(self) -> float:
        """Fraction of executions that succeeded."""
        return self.success_count / self.use_count if self.use_count > 0 else 0.0


class SkillDependency(Base):
    """Directed dependency edge between two skills."""

    __tablename__ = "skill_dependencies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    parent_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("skills.id", ondelete="CASCADE"), nullable=False
    )
    child_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("skills.id", ondelete="CASCADE"), nullable=False
    )
    relationship: Mapped[str] = mapped_column(String(20), nullable=False)
    execution_order: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    strength: Mapped[float] = mapped_column(
        Double, server_default=text("1.0"), default=1.0
    )
    learned_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        server_default=text("NOW()"), nullable=True
    )

    __table_args__ = (
        UniqueConstraint("parent_id", "child_id", name="uq_skill_dependencies_parent_child"),
    )


class SkillExecution(Base):
    """A single recorded execution of a skill."""

    __tablename__ = "skill_executions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    skill_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("skills.id"), nullable=False
    )
    task_description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    task_type: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    complexity: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        server_default=text("NOW()"), nullable=True
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_sec: Mapped[Optional[float]] = mapped_column(Double, nullable=True)
    memory_ids_used: Mapped[Optional[list]] = mapped_column(
        ARRAY(Integer), server_default=text("'{}'"), default=list
    )
    context_summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    outcome: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    outcome_details: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    error_analysis: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    operator_feedback: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    operator_emotion: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    tests_passed: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    needed_rework: Mapped[bool] = mapped_column(
        Boolean, server_default=text("FALSE"), default=False
    )
    rework_rounds: Mapped[int] = mapped_column(
        Integer, server_default=text("0"), default=0
    )
    refinement_proposed: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    new_pitfall: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    lesson_memory_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("memories.id"), nullable=True
    )
    flagged: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("FALSE"), default=False
    )
    user_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), ForeignKey("users.id"), nullable=True
    )

    # Relationships
    skill: Mapped["Skill"] = relationship(
        "Skill", back_populates="executions", lazy="noload"
    )


class SkillVersion(Base, CreatedAtMixin):
    """A historical snapshot of a skill's procedure."""

    __tablename__ = "skill_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    skill_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("skills.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    procedure: Mapped[str] = mapped_column(Text, nullable=False)
    pitfalls: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)
    refinements: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)
    changed_by: Mapped[str] = mapped_column(
        String(50), server_default="system", default="system"
    )

    # Relationships
    skill: Mapped["Skill"] = relationship(
        "Skill", back_populates="versions", lazy="noload"
    )


class SkillHeuristic(Base, TimestampMixin):
    """A learned heuristic rule extracted from skill executions."""

    __tablename__ = "skill_heuristics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    skill_name: Mapped[str] = mapped_column(Text, nullable=False)
    condition: Mapped[str] = mapped_column(Text, nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float] = mapped_column(
        Float, server_default=text("0.5"), default=0.5
    )
    source_count: Mapped[int] = mapped_column(
        Integer, server_default=text("1"), default=1
    )
    validated_count: Mapped[int] = mapped_column(
        Integer, server_default=text("0"), default=0
    )
    violated_count: Mapped[int] = mapped_column(
        Integer, server_default=text("0"), default=0
    )
    last_validated: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_violated: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    active: Mapped[bool] = mapped_column(
        Boolean, server_default=text("TRUE"), default=True
    )
    graduated: Mapped[bool] = mapped_column(
        Boolean, server_default=text("FALSE"), default=False
    )
    graduated_at: Mapped[Optional[datetime]] = mapped_column(
        "graduated_at", DateTime(timezone=True), nullable=True
    )
    demoted_at: Mapped[Optional[datetime]] = mapped_column(
        "demoted_at", DateTime(timezone=True), nullable=True
    )
