"""SQLAlchemy models for memory graph: Memory, Edge, Tag.

Memory is the core knowledge unit with pgvector embeddings.
Edge models relationships between memories.
Tag provides a normalised many-to-one tag index.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Double,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship as sa_relationship

from pgvector.sqlalchemy import Vector

from brain.kernel.config import MEMORY_SEMANTIC_EMBEDDING_DIM
from brain.platform.db.base import ArchivableMixin, Base, CreatedAtMixin


class Memory(Base, CreatedAtMixin, ArchivableMixin):
    """Core knowledge unit stored in the brain."""

    __tablename__ = "memories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    memory_type: Mapped[str] = mapped_column(String(20), nullable=False)

    # pgvector embeddings
    semantic_embedding: Mapped[Optional[list]] = mapped_column(
        Vector(MEMORY_SEMANTIC_EMBEDDING_DIM), nullable=True
    )

    # Retrieval salience
    salience: Mapped[float] = mapped_column(
        Double, server_default="5.0", default=5.0
    )

    # Provenance
    source: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    source_session: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)

    # Array columns (TEXT[] and INTEGER[])
    tags: Mapped[Optional[list]] = mapped_column(
        ARRAY(Text), server_default="{}", default=list
    )
    source_memory_ids: Mapped[Optional[list]] = mapped_column(
        ARRAY(Integer), server_default="{}", default=list
    )

    # Access tracking
    last_accessed: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), default=datetime.utcnow
    )
    access_count: Mapped[int] = mapped_column(
        Integer, server_default="0", default=0
    )
    decay_eligible: Mapped[bool] = mapped_column(
        Boolean, server_default="true", default=True
    )

    # Supersession (self-referential FK)
    superseded_by: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("memories.id"), nullable=True
    )

    # Tier & consolidation
    memory_tier: Mapped[str] = mapped_column(
        String(20), server_default="episodic", default="episodic"
    )
    consolidated: Mapped[bool] = mapped_column(
        Boolean, server_default="false", default=False
    )

    # Scope
    scope: Mapped[str] = mapped_column(
        String(20), server_default="personal", default="personal"
    )

    # Ownership
    user_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("users.id"), nullable=False
    )
    visibility: Mapped[str] = mapped_column(
        String(20), server_default="private", default="private", nullable=False
    )

    # org_id defined directly (ON DELETE SET NULL differs from OrgScopedMixin)
    org_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("orgs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # Harvest metadata (memory-DAG)
    harvest_type: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    harvest_confidence: Mapped[Optional[float]] = mapped_column(Double, nullable=True)
    topic_tags: Mapped[Optional[list]] = mapped_column(
        ARRAY(Text), nullable=True
    )

    # Promotion metadata
    promoted_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    promoted_by: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    # Truth-maintenance metadata
    truth_status: Mapped[str] = mapped_column(
        String(20), server_default="unknown", default="unknown"
    )
    review_status: Mapped[str] = mapped_column(
        String(20), server_default="unreviewed", default="unreviewed"
    )
    confidence: Mapped[float] = mapped_column(
        Double, server_default="0.5", default=0.5
    )
    freshness_score: Mapped[float] = mapped_column(
        Double, server_default="0.5", default=0.5
    )
    observed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    staleness_score: Mapped[Optional[float]] = mapped_column(Double, nullable=True)
    source_type: Mapped[Optional[str]] = mapped_column(
        String(30), server_default="direct", default="direct", nullable=True
    )
    source_kind: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    source_ref: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    source_digest: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    subject_type: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    subject_ref: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    valid_from: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    valid_until: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    policy_kind: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    policy_scope: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    reviewed_by: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    demoted_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    demotion_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Relationships
    edges_out: Mapped[list["Edge"]] = sa_relationship(
        "Edge",
        foreign_keys="Edge.source_id",
        back_populates="source_memory",
        lazy="noload",
    )
    edges_in: Mapped[list["Edge"]] = sa_relationship(
        "Edge",
        foreign_keys="Edge.target_id",
        back_populates="target_memory",
        lazy="noload",
    )
    contradictions_left: Mapped[list["MemoryContradiction"]] = sa_relationship(
        "MemoryContradiction",
        foreign_keys="MemoryContradiction.left_memory_id",
        back_populates="left_memory",
        lazy="noload",
    )
    contradictions_right: Mapped[list["MemoryContradiction"]] = sa_relationship(
        "MemoryContradiction",
        foreign_keys="MemoryContradiction.right_memory_id",
        back_populates="right_memory",
        lazy="noload",
    )
    reviews: Mapped[list["MemoryReview"]] = sa_relationship(
        "MemoryReview",
        back_populates="memory",
        lazy="noload",
    )
    tag_rows: Mapped[list["Tag"]] = sa_relationship(
        "Tag",
        back_populates="memory",
        lazy="noload",
    )
    superseded_by_memory: Mapped[Optional["Memory"]] = sa_relationship(
        "Memory",
        foreign_keys=[superseded_by],
        remote_side="Memory.id",
        lazy="noload",
    )


class Edge(Base, CreatedAtMixin):
    """Directed relationship between two memories."""

    __tablename__ = "edges"
    __table_args__ = (
        UniqueConstraint("source_id", "target_id", "relationship", name="uq_edge_src_tgt_rel"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("memories.id", ondelete="CASCADE"), nullable=False
    )
    target_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("memories.id", ondelete="CASCADE"), nullable=False
    )
    relationship: Mapped[str] = mapped_column(String(30), nullable=False)
    weight: Mapped[float] = mapped_column(
        Double, server_default="1.0", default=1.0
    )
    auto_generated: Mapped[bool] = mapped_column(
        Boolean, server_default="false", default=False
    )
    last_activated: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), default=datetime.utcnow
    )
    activation_count: Mapped[int] = mapped_column(
        Integer, server_default="0", default=0
    )

    # Relationships
    source_memory: Mapped["Memory"] = sa_relationship(
        "Memory",
        foreign_keys=[source_id],
        back_populates="edges_out",
        lazy="noload",
    )
    target_memory: Mapped["Memory"] = sa_relationship(
        "Memory",
        foreign_keys=[target_id],
        back_populates="edges_in",
        lazy="noload",
    )


class MemoryContradiction(Base, CreatedAtMixin):
    """Structured contradiction record for memory truth maintenance."""

    __tablename__ = "memory_contradictions"
    __table_args__ = (
        UniqueConstraint(
            "left_memory_id",
            "right_memory_id",
            "contradiction_type",
            name="uq_memory_contradictions_pair_type",
        ),
        Index("ix_memory_contradictions_left_status", "left_memory_id", "status"),
        Index("ix_memory_contradictions_right_status", "right_memory_id", "status"),
        Index("ix_memory_contradictions_status_created", "status", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    left_memory_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("memories.id", ondelete="CASCADE"), nullable=False
    )
    right_memory_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("memories.id", ondelete="CASCADE"), nullable=False
    )
    detected_by: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    contradiction_type: Mapped[str] = mapped_column(String(50), nullable=False)
    evidence: Mapped[Optional[dict]] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), default=dict, nullable=False
    )
    severity: Mapped[float] = mapped_column(
        Double, server_default="0.5", default=0.5
    )
    status: Mapped[str] = mapped_column(
        String(20), server_default="open", default="open"
    )
    resolution_memory_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("memories.id", ondelete="SET NULL"), nullable=True
    )
    resolved_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    resolved_by: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    left_memory: Mapped["Memory"] = sa_relationship(
        "Memory",
        foreign_keys=[left_memory_id],
        back_populates="contradictions_left",
        lazy="noload",
    )
    right_memory: Mapped["Memory"] = sa_relationship(
        "Memory",
        foreign_keys=[right_memory_id],
        back_populates="contradictions_right",
        lazy="noload",
    )
    resolution_memory: Mapped[Optional["Memory"]] = sa_relationship(
        "Memory",
        foreign_keys=[resolution_memory_id],
        lazy="noload",
    )


class MemoryReview(Base, CreatedAtMixin):
    """Structured review record for memory promotions and demotions."""

    __tablename__ = "memory_reviews"
    __table_args__ = (
        Index("ix_memory_reviews_memory_created", "memory_id", "created_at"),
        Index("ix_memory_reviews_action", "action"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    memory_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("memories.id", ondelete="CASCADE"), nullable=False
    )
    action: Mapped[str] = mapped_column(String(30), nullable=False)
    from_tier: Mapped[str] = mapped_column(String(20), nullable=False)
    to_tier: Mapped[str] = mapped_column(String(20), nullable=False)
    reviewer_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    rationale: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    evidence: Mapped[Optional[dict]] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), default=dict, nullable=False
    )

    memory: Mapped["Memory"] = sa_relationship(
        "Memory",
        back_populates="reviews",
        lazy="noload",
    )


class Tag(Base):
    """Normalised tag index for memories."""

    __tablename__ = "tags"
    __table_args__ = (
        UniqueConstraint("memory_id", "tag", name="uq_tag_memory_tag"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    memory_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("memories.id"), nullable=False
    )
    tag: Mapped[str] = mapped_column(Text, nullable=False)

    # Relationships
    memory: Mapped["Memory"] = sa_relationship(
        "Memory",
        back_populates="tag_rows",
        lazy="noload",
    )
