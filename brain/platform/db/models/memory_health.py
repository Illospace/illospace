"""Memory health models: MemoryHealthLog and RetrievalPoolStats.

MemoryHealthLog tracks integrity/quality checks.
RetrievalPoolStats tracks hit/miss rates per retrieval pool.
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
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from brain.platform.db.base import Base, CreatedAtMixin

__all__ = ["MemoryHealthLog", "RetrievalPoolStats"]


class MemoryHealthLog(Base, CreatedAtMixin):
    """A single memory health check result."""

    __tablename__ = "memory_health_log"
    __table_args__ = (
        Index("ix_memory_health_log_org_created", "org_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    check_type: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    details: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)

    org_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("orgs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )


class RetrievalPoolStats(Base):
    """Windowed hit/miss statistics per retrieval pool."""

    __tablename__ = "retrieval_pool_stats"
    __table_args__ = (
        UniqueConstraint(
            "org_id", "pool_name", "window_start",
            name="uq_pool_stats_org_pool_window",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    pool_name: Mapped[str] = mapped_column(String(20), nullable=False)
    hit_count: Mapped[int] = mapped_column(
        Integer, server_default="0", default=0
    )
    miss_count: Mapped[int] = mapped_column(
        Integer, server_default="0", default=0
    )
    window_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    org_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("orgs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
