"""Narrative models: ProjectNarrative and NarrativeSession.

ProjectNarrative tracks evolving topic arcs across sessions.
NarrativeSession links individual sessions to their narrative.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

try:
    from pgvector.sqlalchemy import Vector
except ImportError:  # pragma: no cover — pgvector optional in test env
    from sqlalchemy import PickleType as Vector  # type: ignore[assignment]

from brain.kernel.config import NARRATIVE_SEMANTIC_EMBEDDING_DIM
from brain.platform.db.base import Base, CreatedAtMixin, TimestampMixin

__all__ = ["ProjectNarrative", "NarrativeSession"]


class ProjectNarrative(Base, TimestampMixin):
    """A topic-level narrative arc built from session summaries."""

    __tablename__ = "project_narratives"
    __table_args__ = (
        Index(
            "uq_project_narratives_scope_topic",
            "org_id",
            "user_id",
            "visibility",
            "topic_slug",
            unique=True,
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    topic_slug: Mapped[str] = mapped_column(String(100), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    arc_summary: Mapped[str] = mapped_column(Text, nullable=False)
    semantic_embedding: Mapped[Optional[list]] = mapped_column(
        Vector(NARRATIVE_SEMANTIC_EMBEDDING_DIM), nullable=True
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


class NarrativeSession(Base, CreatedAtMixin):
    """A single session's contribution to a narrative arc."""

    __tablename__ = "narrative_sessions"
    __table_args__ = (
        Index("ix_narrative_sessions_narrative_date", "narrative_id", "session_date"),
        Index("ix_narrative_sessions_session_id", "session_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    narrative_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("project_narratives.id", ondelete="CASCADE"),
        nullable=False,
    )
    session_id: Mapped[str] = mapped_column(String(100), nullable=False)
    session_date: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    summary: Mapped[str] = mapped_column(Text, nullable=False)
