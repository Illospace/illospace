"""Browser session models for live thought browsing."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from brain.platform.db.base import Base, CreatedAtMixin

__all__ = ["BrowserSession"]


class BrowserSession(Base, CreatedAtMixin):
    """Persisted metadata for a server-side browser session."""

    __tablename__ = "browser_sessions"

    id: Mapped[str] = mapped_column(
        String,
        primary_key=True,
        server_default=text("gen_random_uuid()::text"),
        default=lambda: str(uuid.uuid4()),
    )
    idea_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("ideas.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), ForeignKey("users.id"), nullable=True, index=True
    )
    run_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="starting", default="starting", index=True
    )
    current_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    page_title: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    viewport_width: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1280"), default=1280
    )
    viewport_height: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("800"), default=800
    )
    storage_mode: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="ephemeral", default="ephemeral"
    )
    allow_downloads: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("FALSE"), default=False
    )
    allow_file_uploads: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("TRUE"), default=True
    )
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("TRUE"), default=True
    )
    last_frame_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    closed_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
