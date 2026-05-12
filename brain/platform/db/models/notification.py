"""Unified notification event models."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    DateTime,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from brain.platform.db.base import Base, TimestampMixin

NOTIFICATION_SOURCE_CHAT = "chat"
NOTIFICATION_SOURCE_WORKSPACE = "workspace"

NOTIFICATION_KIND_CHAT_DM_MESSAGE = "chat.dm_message"
NOTIFICATION_KIND_CHAT_MENTION = "chat.mention"
NOTIFICATION_KIND_CHAT_ROOM_MESSAGE = "chat.room_message"
NOTIFICATION_KIND_WORKSPACE_MENTION = "workspace.mention"
NOTIFICATION_KIND_WORKSPACE_THREAD_ATTENTION = "workspace.thread_attention"

__all__ = [
    "NotificationEvent",
    "NOTIFICATION_SOURCE_CHAT",
    "NOTIFICATION_SOURCE_WORKSPACE",
    "NOTIFICATION_KIND_CHAT_DM_MESSAGE",
    "NOTIFICATION_KIND_CHAT_MENTION",
    "NOTIFICATION_KIND_CHAT_ROOM_MESSAGE",
    "NOTIFICATION_KIND_WORKSPACE_MENTION",
    "NOTIFICATION_KIND_WORKSPACE_THREAD_ATTENTION",
]


class NotificationEvent(Base, TimestampMixin):
    """A durable in-app notification row for the user's inbox."""

    __tablename__ = "notification_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    org_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    source: Mapped[str] = mapped_column(String(30), nullable=False)
    kind: Mapped[str] = mapped_column(String(60), nullable=False)
    actor_user_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    idea_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), ForeignKey("ideas.id", ondelete="CASCADE"), nullable=True
    )
    conversation_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), ForeignKey("chat_conversations.id", ondelete="CASCADE"), nullable=True
    )
    thread_root_message_id: Mapped[Optional[int]] = mapped_column(nullable=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    payload: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    coalesce_key: Mapped[str] = mapped_column(String(255), nullable=False)
    occurrence_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    read_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "source IN ('chat', 'workspace')",
            name="ck_notification_events_source",
        ),
        CheckConstraint(
            "occurrence_count >= 1",
            name="ck_notification_events_occurrence_count_positive",
        ),
        Index(
            "ix_notification_events_user_read_updated",
            "user_id",
            "read_at",
            "updated_at",
        ),
        Index(
            "ix_notification_events_org_user_source",
            "org_id",
            "user_id",
            "source",
        ),
        Index(
            "ix_notification_events_user_coalesce",
            "user_id",
            "coalesce_key",
            "read_at",
        ),
    )
