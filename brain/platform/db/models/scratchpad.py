"""Session scratchpad model.

Provides within-session shared state between AgentRun workers.
Each entry is scoped to a run_id and organized by section.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Index, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from brain.platform.db.base import Base, CreatedAtMixin

__all__ = ["SessionScratchpad"]


class SessionScratchpad(Base, CreatedAtMixin):
    """A single scratchpad entry written by a worker during an AgentRun."""

    __tablename__ = "session_scratchpad"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    worker_name: Mapped[str] = mapped_column(String(100), nullable=False, server_default="coordinator")
    section: Mapped[str] = mapped_column(
        String(50), nullable=False,
        comment="findings | decisions | open_questions | resources | handoffs",
    )
    key: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    closed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None,
        comment="Set when the run completes; entries expire 24h after this",
    )

    __table_args__ = (
        Index("idx_scratchpad_run_section", "run_id", "section"),
        Index("idx_scratchpad_run_key", "run_id", "key"),
    )
