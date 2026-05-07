"""Generic Cortex UI event replay rows."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from brain.platform.db.base import Base


class CortexEvent(Base):
    __tablename__ = "cortex_events"
    __table_args__ = (
        Index("ix_cortex_events_created", "created_at"),
        Index("ix_cortex_events_idea_created", "idea_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_type: Mapped[str] = mapped_column(String(120), nullable=False)
    idea_id: Mapped[Optional[str]] = mapped_column(UUID(as_uuid=False), ForeignKey("ideas.id", ondelete="CASCADE"), nullable=True)
    target_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    session_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("NOW()"))


__all__ = ["CortexEvent"]
