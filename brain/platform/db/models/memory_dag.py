"""Memory DAG models: MemorySummary (nodes) and SummaryLineage (edges).

MemorySummary stores compressed summaries at increasing depths.
SummaryLineage tracks which memories/summaries roll up into each summary.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

try:
    from pgvector.sqlalchemy import Vector
except ImportError:  # pragma: no cover — pgvector optional in test env
    from sqlalchemy import PickleType as Vector  # type: ignore[assignment]

from brain.kernel.config import SUMMARY_SEMANTIC_EMBEDDING_DIM
from brain.platform.db.base import Base, CreatedAtMixin

__all__ = ["MemorySummary", "SummaryLineage"]


class MemorySummary(Base, CreatedAtMixin):
    """DAG node: a compressed summary of memories or lower-level summaries."""

    __tablename__ = "memory_summaries"
    __table_args__ = (
        Index("ix_memory_summaries_org_depth", "org_id", "depth"),
        Index(
            "ix_memory_summaries_scope_depth_active",
            "org_id",
            "user_id",
            "visibility",
            "depth",
            "stale_at",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    depth: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    breadcrumbs: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    semantic_embedding: Mapped[Optional[list]] = mapped_column(
        Vector(SUMMARY_SEMANTIC_EMBEDDING_DIM), nullable=True
    )
    token_count: Mapped[int] = mapped_column(Integer, nullable=False)
    earliest_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    latest_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    descendant_count: Mapped[int] = mapped_column(
        Integer, server_default="0", default=0
    )
    visibility: Mapped[str] = mapped_column(
        String(20), server_default="private", default="private", nullable=False
    )
    stale_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    stale_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Ownership
    user_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("users.id"), nullable=False
    )
    org_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("orgs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    created_by_model: Mapped[Optional[str]] = mapped_column(
        String(100), nullable=True
    )


class SummaryLineage(Base, CreatedAtMixin):
    """DAG edge: links a summary to its child memories or child summaries."""

    __tablename__ = "summary_lineage"
    __table_args__ = (
        CheckConstraint(
            "(child_memory_id IS NOT NULL AND child_summary_id IS NULL) "
            "OR (child_memory_id IS NULL AND child_summary_id IS NOT NULL)",
            name="ck_summary_lineage_exactly_one_child",
        ),
        Index("ix_summary_lineage_summary_id", "summary_id"),
        Index("ix_summary_lineage_child_memory_id", "child_memory_id"),
        Index("ix_summary_lineage_child_summary_id", "child_summary_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    summary_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("memory_summaries.id", ondelete="CASCADE"),
        nullable=False,
    )
    child_memory_id: Mapped[Optional[int]] = mapped_column(
        Integer,
        ForeignKey("memories.id", ondelete="CASCADE"),
        nullable=True,
    )
    child_summary_id: Mapped[Optional[int]] = mapped_column(
        Integer,
        ForeignKey("memory_summaries.id", ondelete="CASCADE"),
        nullable=True,
    )
