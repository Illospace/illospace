"""Reconstructive memory persistence models.

These tables are the clean-slate replacement for flat memory rows.  The
core unit is a source-backed graph: immutable sources are segmented into spans,
spans support nodes and assertions, and reconstruction runs record the active
memory-search trajectory that produced an evidence pack.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Double,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    text as sql_text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

try:
    from pgvector.sqlalchemy import Vector
except ImportError:  # pragma: no cover - pgvector optional in test env
    from sqlalchemy import PickleType as Vector  # type: ignore[assignment]

from brain.kernel.config import MEMORY_SEMANTIC_EMBEDDING_DIM
from brain.platform.db.base import Base, CreatedAtMixin, TimestampMixin

__all__ = [
    "MemoryAssertionNode",
    "MemoryEdgeNode",
    "MemoryNode",
    "MemoryNodeEmbedding",
    "MemorySource",
    "MemorySpan",
    "ReconstructionEvidence",
    "ReconstructionFeedback",
    "ReconstructionRun",
    "ReconstructionStep",
]


def _uuid_fk(target: str, *, nullable: bool = True, ondelete: str = "SET NULL"):
    return mapped_column(
        UUID(as_uuid=False).with_variant(String, "sqlite"),
        ForeignKey(target, ondelete=ondelete),
        nullable=nullable,
        index=True,
    )


def _json_column(default_sql: str = "'{}'", default_factory=dict, *, nullable: bool = False):
    return mapped_column(
        JSONB().with_variant(JSON, "sqlite"),
        nullable=nullable,
        server_default=sql_text(default_sql),
        default=default_factory,
    )


class MemorySource(Base, TimestampMixin):
    """Immutable source material from which reconstructive memory is derived."""

    __tablename__ = "memory_sources"
    __table_args__ = (
        Index("ix_memory_sources_org_kind_created", "org_id", "source_kind", "created_at"),
        Index("ix_memory_sources_source_ref", "source_ref"),
        UniqueConstraint("org_id", "source_kind", "source_ref", "content_digest", name="uq_memory_sources_identity"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    org_id: Mapped[str | None] = _uuid_fk("orgs.id")
    user_id: Mapped[str | None] = _uuid_fk("users.id")
    visibility: Mapped[str] = mapped_column(String(20), nullable=False, server_default=sql_text("'private'"), default="private")
    source_kind: Mapped[str] = mapped_column(String(60), nullable=False)
    source_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_digest: Mapped[str] = mapped_column(String(128), nullable=False)
    raw_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    structured_payload: Mapped[dict[str, Any]] = _json_column()
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    authority_principal: Mapped[str | None] = mapped_column(Text, nullable=True)
    sensitivity: Mapped[str] = mapped_column(String(30), nullable=False, server_default=sql_text("'low'"), default="low")
    retention_policy: Mapped[str | None] = mapped_column(Text, nullable=True)


class MemorySpan(Base, CreatedAtMixin):
    """Addressable source span used as evidence for nodes and assertions."""

    __tablename__ = "memory_spans"
    __table_args__ = (
        Index("ix_memory_spans_source", "source_id"),
        Index("ix_memory_spans_digest", "content_digest"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_id: Mapped[int] = mapped_column(Integer, ForeignKey("memory_sources.id", ondelete="CASCADE"), nullable=False)
    span_kind: Mapped[str] = mapped_column(String(40), nullable=False, server_default=sql_text("'text'"), default="text")
    locator: Mapped[dict[str, Any]] = _json_column()
    text: Mapped[str] = mapped_column(Text, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sql_text("0"), default=0)
    content_digest: Mapped[str] = mapped_column(String(128), nullable=False)


class MemoryNode(Base, TimestampMixin):
    """Unified graph node: cue, tag, content, summary, procedure, policy, etc."""

    __tablename__ = "memory_nodes"
    __table_args__ = (
        Index("ix_memory_nodes_org_kind_key", "org_id", "node_kind", "normalized_key"),
        Index("ix_memory_nodes_visibility", "org_id", "user_id", "visibility"),
        Index("ix_memory_nodes_truth_freshness", "truth_status", "freshness_status"),
        UniqueConstraint("org_id", "node_kind", "scope_key", "normalized_key", name="uq_memory_nodes_scope_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    node_kind: Mapped[str] = mapped_column(String(40), nullable=False)
    content_kind: Mapped[str | None] = mapped_column(String(40), nullable=True)
    canonical_label: Mapped[str] = mapped_column(Text, nullable=False)
    text: Mapped[str | None] = mapped_column(Text, nullable=True)
    normalized_key: Mapped[str] = mapped_column(Text, nullable=False)
    scope_key: Mapped[str] = mapped_column(Text, nullable=False, server_default=sql_text("'default'"), default="default")
    org_id: Mapped[str | None] = _uuid_fk("orgs.id")
    user_id: Mapped[str | None] = _uuid_fk("users.id")
    visibility: Mapped[str] = mapped_column(String(20), nullable=False, server_default=sql_text("'private'"), default="private")
    sensitivity: Mapped[str] = mapped_column(String(30), nullable=False, server_default=sql_text("'low'"), default="low")
    confidence: Mapped[float] = mapped_column(Double, nullable=False, server_default=sql_text("0.5"), default=0.5)
    truth_status: Mapped[str] = mapped_column(String(30), nullable=False, server_default=sql_text("'unknown'"), default="unknown")
    freshness_status: Mapped[str] = mapped_column(String(30), nullable=False, server_default=sql_text("'unknown'"), default="unknown")
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class MemoryNodeEmbedding(Base, CreatedAtMixin):
    """Embeddings are separated from node identity for model upgrades."""

    __tablename__ = "memory_node_embeddings"
    __table_args__ = (
        Index("ix_memory_node_embeddings_node_kind", "node_id", "embedding_kind"),
        UniqueConstraint("node_id", "embedding_kind", "model", "content_digest", name="uq_memory_node_embeddings_digest"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    node_id: Mapped[int] = mapped_column(Integer, ForeignKey("memory_nodes.id", ondelete="CASCADE"), nullable=False)
    embedding_kind: Mapped[str] = mapped_column(String(40), nullable=False)
    model: Mapped[str] = mapped_column(String(120), nullable=False)
    dimension: Mapped[int] = mapped_column(Integer, nullable=False)
    embedding: Mapped[list | None] = mapped_column(Vector(MEMORY_SEMANTIC_EMBEDDING_DIM), nullable=True)
    content_digest: Mapped[str] = mapped_column(String(128), nullable=False)


class MemoryEdgeNode(Base, CreatedAtMixin):
    """Typed relationship between reconstructive memory nodes."""

    __tablename__ = "memory_edges"
    __table_args__ = (
        Index("ix_memory_edges_source_kind", "source_node_id", "edge_kind"),
        Index("ix_memory_edges_target_kind", "target_node_id", "edge_kind"),
        UniqueConstraint("source_node_id", "target_node_id", "edge_kind", name="uq_memory_edges_src_tgt_kind"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_node_id: Mapped[int] = mapped_column(Integer, ForeignKey("memory_nodes.id", ondelete="CASCADE"), nullable=False)
    target_node_id: Mapped[int] = mapped_column(Integer, ForeignKey("memory_nodes.id", ondelete="CASCADE"), nullable=False)
    edge_kind: Mapped[str] = mapped_column(String(60), nullable=False)
    weight: Mapped[float] = mapped_column(Double, nullable=False, server_default=sql_text("1.0"), default=1.0)
    confidence: Mapped[float] = mapped_column(Double, nullable=False, server_default=sql_text("0.5"), default=0.5)
    directionality: Mapped[str] = mapped_column(String(20), nullable=False, server_default=sql_text("'directed'"), default="directed")
    org_id: Mapped[str | None] = _uuid_fk("orgs.id")
    visibility: Mapped[str] = mapped_column(String(20), nullable=False, server_default=sql_text("'private'"), default="private")
    evidence_span_ids: Mapped[list[int]] = _json_column("'[]'", list)
    created_by: Mapped[str] = mapped_column(String(40), nullable=False, server_default=sql_text("'extractor'"), default="extractor")
    last_activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    activation_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sql_text("0"), default=0)


class MemoryAssertionNode(Base, TimestampMixin):
    """Truth-bearing claim separated from graph-node display text."""

    __tablename__ = "memory_assertions"
    __table_args__ = (
        Index("ix_memory_assertions_node", "node_id"),
        Index("ix_memory_assertions_subject_predicate", "subject_node_id", "predicate"),
        Index("ix_memory_assertions_truth", "truth_status", "review_status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    node_id: Mapped[int] = mapped_column(Integer, ForeignKey("memory_nodes.id", ondelete="CASCADE"), nullable=False)
    claim_text: Mapped[str] = mapped_column(Text, nullable=False)
    subject_node_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("memory_nodes.id", ondelete="SET NULL"), nullable=True)
    predicate: Mapped[str | None] = mapped_column(String(80), nullable=True)
    object_node_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("memory_nodes.id", ondelete="SET NULL"), nullable=True)
    object_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    polarity: Mapped[str] = mapped_column(String(20), nullable=False, server_default=sql_text("'positive'"), default="positive")
    confidence: Mapped[float] = mapped_column(Double, nullable=False, server_default=sql_text("0.5"), default=0.5)
    truth_status: Mapped[str] = mapped_column(String(30), nullable=False, server_default=sql_text("'unknown'"), default="unknown")
    review_status: Mapped[str] = mapped_column(String(30), nullable=False, server_default=sql_text("'unreviewed'"), default="unreviewed")
    source_span_ids: Mapped[list[int]] = _json_column("'[]'", list)
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ReconstructionRun(Base, CreatedAtMixin):
    """One active memory-reasoning episode."""

    __tablename__ = "reconstruction_runs"
    __table_args__ = (
        Index("ix_reconstruction_runs_agent_run", "run_id"),
        Index("ix_reconstruction_runs_thread", "thread_id"),
        Index("ix_reconstruction_runs_org_user_created", "org_id", "user_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True)
    thread_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    query_text: Mapped[str] = mapped_column(Text, nullable=False)
    query_kind: Mapped[str] = mapped_column(String(50), nullable=False, server_default=sql_text("'fact_lookup'"), default="fact_lookup")
    org_id: Mapped[str | None] = _uuid_fk("orgs.id")
    user_id: Mapped[str | None] = _uuid_fk("users.id")
    visibility_context: Mapped[dict[str, Any]] = _json_column()
    budget_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sql_text("0"), default=0)
    budget_steps: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sql_text("0"), default=0)
    policy_version: Mapped[str] = mapped_column(String(80), nullable=False, server_default=sql_text("'deterministic-v1'"), default="deterministic-v1")
    model: Mapped[str | None] = mapped_column(String(120), nullable=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False, server_default=sql_text("'running'"), default="running")
    final_confidence: Mapped[float | None] = mapped_column(Double, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ReconstructionStep(Base, CreatedAtMixin):
    """One action in a reconstruction run."""

    __tablename__ = "reconstruction_steps"
    __table_args__ = (
        Index("ix_reconstruction_steps_run_index", "reconstruction_run_id", "step_index"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    reconstruction_run_id: Mapped[int] = mapped_column(Integer, ForeignKey("reconstruction_runs.id", ondelete="CASCADE"), nullable=False)
    step_index: Mapped[int] = mapped_column(Integer, nullable=False)
    state_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    action_kind: Mapped[str] = mapped_column(String(60), nullable=False)
    action_input: Mapped[dict[str, Any]] = _json_column()
    action_output: Mapped[dict[str, Any]] = _json_column()
    selected_node_ids: Mapped[list[int]] = _json_column("'[]'", list)
    rejected_node_ids: Mapped[list[int]] = _json_column("'[]'", list)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    cost_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sql_text("0"), default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sql_text("0"), default=0)


class ReconstructionEvidence(Base, CreatedAtMixin):
    """Final support set selected by a reconstruction run."""

    __tablename__ = "reconstruction_evidence"
    __table_args__ = (
        Index("ix_reconstruction_evidence_run_rank", "reconstruction_run_id", "rank"),
        Index("ix_reconstruction_evidence_node", "node_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    reconstruction_run_id: Mapped[int] = mapped_column(Integer, ForeignKey("reconstruction_runs.id", ondelete="CASCADE"), nullable=False)
    node_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("memory_nodes.id", ondelete="SET NULL"), nullable=True)
    assertion_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("memory_assertions.id", ondelete="SET NULL"), nullable=True)
    source_span_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("memory_spans.id", ondelete="SET NULL"), nullable=True)
    role: Mapped[str] = mapped_column(String(40), nullable=False, server_default=sql_text("'supports_answer'"), default="supports_answer")
    confidence: Mapped[float] = mapped_column(Double, nullable=False, server_default=sql_text("0.5"), default=0.5)
    rank: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sql_text("0"), default=0)


class ReconstructionFeedback(Base, CreatedAtMixin):
    """Learning signal attached to a reconstruction trajectory."""

    __tablename__ = "reconstruction_feedback"
    __table_args__ = (
        Index("ix_reconstruction_feedback_run", "reconstruction_run_id"),
        Index("ix_reconstruction_feedback_signal", "signal_kind"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    reconstruction_run_id: Mapped[int] = mapped_column(Integer, ForeignKey("reconstruction_runs.id", ondelete="CASCADE"), nullable=False)
    signal_kind: Mapped[str] = mapped_column(String(60), nullable=False)
    target_step_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("reconstruction_steps.id", ondelete="SET NULL"), nullable=True)
    target_node_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("memory_nodes.id", ondelete="SET NULL"), nullable=True)
    target_edge_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("memory_edges.id", ondelete="SET NULL"), nullable=True)
    details: Mapped[dict[str, Any]] = _json_column()
