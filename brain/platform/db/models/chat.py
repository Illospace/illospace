"""Chat domain models for shared room and direct messages."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    DateTime,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from brain.platform.db.base import Base, CreatedAtMixin, TimestampMixin

CHAT_CONVERSATION_ROOM = "room"
CHAT_CONVERSATION_DM = "dm"

CHAT_VISIBILITY_ORG = "org"
CHAT_VISIBILITY_MEMBERS = "members"

__all__ = [
    "ChatConversation",
    "ChatConversationMember",
    "ChatMessage",
    "ChatMessageMention",
    "ChatNotification",
    "ChatConversationRead",
]


class ChatConversation(Base, TimestampMixin):
    """A chat surface such as the shared team room or a DM."""

    __tablename__ = "chat_conversations"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
        default=lambda: str(uuid.uuid4()),
    )
    org_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False
    )
    type: Mapped[str] = mapped_column(String(20), nullable=False)
    stable_key: Mapped[str] = mapped_column(String(255), nullable=False)
    title: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_by_user_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    visibility: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        server_default=CHAT_VISIBILITY_ORG,
        default=CHAT_VISIBILITY_ORG,
    )
    is_archived: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("FALSE"), default=False
    )
    last_message_seq: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), default=0
    )

    __table_args__ = (
        CheckConstraint(
            "type IN ('room', 'dm')",
            name="ck_chat_conversations_type",
        ),
        CheckConstraint(
            "visibility IN ('org', 'members')",
            name="ck_chat_conversations_visibility",
        ),
        CheckConstraint(
            "last_message_seq >= 0",
            name="ck_chat_conversations_last_message_seq_non_negative",
        ),
        UniqueConstraint(
            "org_id", "stable_key", name="uq_chat_conversations_org_stable_key"
        ),
        Index("ix_chat_conversations_org_type", "org_id", "type"),
    )


class ChatConversationMember(Base):
    """Membership and notification preferences for a conversation."""

    __tablename__ = "chat_conversation_members"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("chat_conversations.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="member", default="member"
    )
    joined_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("NOW()")
    )
    muted_until: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    notification_preference: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="all", default="all"
    )

    __table_args__ = (
        UniqueConstraint(
            "conversation_id",
            "user_id",
            name="uq_chat_conversation_members_conversation_user",
        ),
        Index(
            "ix_chat_conversation_members_user_conversation",
            "user_id",
            "conversation_id",
        ),
    )


class ChatMessage(Base, CreatedAtMixin):
    """A chat message within a room, DM, or room thread."""

    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("chat_conversations.id", ondelete="CASCADE"), nullable=False
    )
    sender_user_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    sender_kind: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="user", default="user"
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)
    body_format: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="markdown", default="markdown"
    )
    client_generated_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    thread_root_message_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("chat_messages.id", ondelete="SET NULL"), nullable=True
    )
    reply_to_message_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("chat_messages.id", ondelete="SET NULL"), nullable=True
    )
    attachments: Mapped[Optional[list]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb"), default=list
    )
    metadata_: Mapped[Optional[dict]] = mapped_column(
        "metadata", JSONB, nullable=True
    )
    conversation_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    reply_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), default=0
    )
    last_reply_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_reply_message_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("chat_messages.id", ondelete="SET NULL"), nullable=True
    )
    edited_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "conversation_seq > 0",
            name="ck_chat_messages_conversation_seq_positive",
        ),
        CheckConstraint(
            "reply_count >= 0",
            name="ck_chat_messages_reply_count_non_negative",
        ),
        CheckConstraint(
            "(thread_root_message_id IS NOT NULL) OR (reply_to_message_id IS NULL)",
            name="ck_chat_messages_reply_target_requires_thread_root",
        ),
        Index(
            "ix_chat_messages_thread_root_seq",
            "thread_root_message_id",
            "conversation_seq",
        ),
        UniqueConstraint(
            "conversation_id",
            "conversation_seq",
            name="uq_chat_messages_conversation_seq",
        ),
    )


class ChatMessageMention(Base, CreatedAtMixin):
    """A structured mention extracted from a chat message."""

    __tablename__ = "chat_message_mentions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    message_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("chat_messages.id", ondelete="CASCADE"), nullable=False
    )
    mentioned_user_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    mentioned_by_user_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    delivered_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    seen_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index(
            "ix_chat_message_mentions_user_seen",
            "mentioned_user_id",
            "seen_at",
        ),
    )


class ChatNotification(Base, CreatedAtMixin):
    """In-app notification rows for chat activity."""

    __tablename__ = "chat_notifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    type: Mapped[str] = mapped_column(String(30), nullable=False)
    conversation_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), ForeignKey("chat_conversations.id", ondelete="CASCADE"), nullable=True
    )
    message_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("chat_messages.id", ondelete="CASCADE"), nullable=True
    )
    actor_user_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    metadata_: Mapped[Optional[dict]] = mapped_column("metadata", JSONB, nullable=True)
    read_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index(
            "ix_chat_notifications_user_read_created",
            "user_id",
            "read_at",
            "created_at",
        ),
    )


class ChatConversationRead(Base):
    """Last-read cursor for a conversation per user."""

    __tablename__ = "chat_conversation_reads"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("chat_conversations.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    last_read_message_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("chat_messages.id", ondelete="SET NULL"), nullable=True
    )
    last_read_conversation_seq: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), default=0
    )
    last_read_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("NOW()")
    )

    __table_args__ = (
        CheckConstraint(
            "last_read_conversation_seq >= 0",
            name="ck_chat_conversation_reads_last_read_seq_non_negative",
        ),
        UniqueConstraint(
            "conversation_id",
            "user_id",
            name="uq_chat_conversation_reads_conversation_user",
        ),
        Index(
            "ix_chat_conversation_reads_user_conversation",
            "user_id",
            "conversation_id",
        ),
    )
