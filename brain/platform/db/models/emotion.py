"""Emotional snapshot model.

Matches: emotional_snapshots table.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from sqlalchemy import DateTime, Date, Double, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from brain.platform.db.base import Base

__all__ = ["EmotionalSnapshot"]


class EmotionalSnapshot(Base):
    """A point-in-time emotional reading."""

    __tablename__ = "emotional_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_date: Mapped[date] = mapped_column(Date, nullable=False)
    timestamp: Mapped[datetime] = mapped_column(
        "timestamp", DateTime(timezone=True), server_default=func.now()
    )
    valence: Mapped[float] = mapped_column(Double, nullable=False)
    arousal: Mapped[float] = mapped_column(Double, nullable=False)
    label: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    trigger_summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    topic_tags: Mapped[Optional[list]] = mapped_column(
        ARRAY(Text), server_default="{}", default=list
    )
    attributed_to: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
