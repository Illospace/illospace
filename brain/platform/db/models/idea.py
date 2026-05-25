"""Idea graph models.

Matches: ideas, idea_state_log, idea_connections, idea_threads tables.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
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
from pgvector.sqlalchemy import Vector

from brain.kernel.config import IDEA_EMBEDDING_DIM
from brain.platform.db.base import Base, CreatedAtMixin

__all__ = [
    "Idea",
    "IdeaStateLog",
    "IdeaConnection",
    "IdeaThread",
    "UserMention",
    "VisualBlock",
    "ThreadContextSubmission",
    "ThreadDiscussionComment",
    "ProjectProfile",
    "ProjectProfileAccess",
    "IdeaProjectAttachment",
]


class Idea(Base):
    """A cortex idea (UUID PK)."""

    __tablename__ = "ideas"
    __table_args__ = (
        CheckConstraint(
            "status IN ('emerged', 'queued', 'active', 'working', 'needs_input', "
            "'unread_reply', 'blocked', 'failed', 'resolved', 'stale', 'paused', "
            "'done', 'archived', 'exploring', 'building', 'testing')",
            name="ck_ideas_status",
        ),
        Index("ix_ideas_archived_updated", "archived_at", "updated_at"),
        Index("ix_ideas_org_archived_updated", "org_id", "archived_at", "updated_at"),
        Index("ix_ideas_status_archived_updated", "status", "archived_at", "updated_at"),
    )

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
        default=lambda: str(uuid.uuid4()),
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="emerged", default="emerged"
    )
    origin: Mapped[str] = mapped_column(
        String(30), nullable=False, server_default="user_created", default="user_created"
    )
    origin_ref: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    salience_score: Mapped[Optional[float]] = mapped_column(
        Float, server_default=text("5.0"), default=5.0
    )
    position_x: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    position_y: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    position_sticky: Mapped[bool] = mapped_column(
        Boolean, server_default=text("FALSE"), default=False
    )
    orbit_anchor_type: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    orbit_anchor_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    parent_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), ForeignKey("ideas.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("NOW()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("NOW()")
    )
    archived_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    encoded_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    user_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("users.id"), nullable=False
    )
    org_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), ForeignKey("orgs.id"), nullable=True
    )
    # Cortex UI columns
    display_title: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    read_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    working_memory: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    active_agents: Mapped[int] = mapped_column(
        Integer, server_default=text("0"), default=0
    )
    agent_details: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    attachments: Mapped[Optional[list]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb"), default=list
    )
    # Intentionally fixed: legacy Cortex idea embeddings were produced in an
    # OpenAI-specific 1536-dimensional space and require a dedicated re-embed
    # migration before joining the shared semantic embedding registry.
    embedding: Mapped[Optional[object]] = mapped_column(
        Vector(IDEA_EMBEDDING_DIM), nullable=True
    )


class IdeaStateLog(Base):
    """State transition log for an idea."""

    __tablename__ = "idea_state_log"
    __table_args__ = (
        Index("ix_idea_state_log_idea_changed", "idea_id", "changed_at", "id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    idea_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), ForeignKey("ideas.id", ondelete="CASCADE"), nullable=True
    )
    from_state: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    to_state: Mapped[str] = mapped_column(String(20), nullable=False)
    changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("NOW()")
    )
    trigger: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)


class IdeaConnection(Base, CreatedAtMixin):
    """Weighted edge between two ideas."""

    __tablename__ = "idea_connections"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
        default=lambda: str(uuid.uuid4()),
    )
    source_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), ForeignKey("ideas.id", ondelete="CASCADE"), nullable=True
    )
    target_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), ForeignKey("ideas.id", ondelete="CASCADE"), nullable=True
    )
    type: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="manual", default="manual"
    )
    weight: Mapped[float] = mapped_column(
        Float, server_default=text("1.0"), default=1.0
    )
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("source_id", "target_id", name="uq_idea_connections_src_tgt"),
    )


class IdeaThread(Base, CreatedAtMixin):
    """A conversation thread message on an idea."""

    __tablename__ = "idea_threads"
    __table_args__ = (
        Index("ix_idea_threads_idea_created", "idea_id", "created_at", "id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    idea_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), ForeignKey("ideas.id", ondelete="CASCADE"), nullable=True
    )
    role: Mapped[str] = mapped_column(String(10), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    attachments: Mapped[Optional[list]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb"), default=list
    )
    metadata_: Mapped[Optional[dict]] = mapped_column(
        "metadata", JSONB, nullable=True
    )
    message_type: Mapped[Optional[str]] = mapped_column(
        String(20), server_default="message", default="message"
    )
    user_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), ForeignKey("users.id"), nullable=True
    )


class ThreadContextSubmission(Base, CreatedAtMixin):
    """Immutable context submitted by a personal agent and attached to a Thread."""

    __tablename__ = "thread_context_submissions"
    __table_args__ = (
        Index("ix_thread_context_submissions_thread_created", "thread_id", "created_at", "id"),
        Index("ix_thread_context_submissions_inbound_event", "inbound_event_id"),
    )

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
        default=lambda: str(uuid.uuid4()),
    )
    thread_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("ideas.id", ondelete="CASCADE"), nullable=False
    )
    org_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False
    )
    source_connection_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), ForeignKey("external_agent_connections.id", ondelete="SET NULL"), nullable=True
    )
    submitted_by_user_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    inbound_event_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), ForeignKey("inbound_events.id", ondelete="SET NULL"), nullable=True
    )
    intent: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    source: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'::jsonb"), default=dict)
    constraints: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'::jsonb"), default=dict)
    correlation: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'::jsonb"), default=dict)
    parts: Mapped[list] = mapped_column(JSONB, server_default=text("'[]'::jsonb"), default=list)
    routing_result: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'::jsonb"), default=dict)


class ThreadDiscussionComment(Base, CreatedAtMixin):
    """Attached team discussion comment for a Thread."""

    __tablename__ = "thread_discussion_comments"
    __table_args__ = (
        Index("ix_thread_discussion_comments_thread_created", "thread_id", "created_at", "id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    thread_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("ideas.id", ondelete="CASCADE"), nullable=False
    )
    org_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False
    )
    author_user_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    author_kind: Mapped[str] = mapped_column(String(20), nullable=False, server_default="user", default="user")
    body: Mapped[str] = mapped_column(Text, nullable=False)
    attachments: Mapped[list] = mapped_column(JSONB, server_default=text("'[]'::jsonb"), default=list)
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, server_default=text("'{}'::jsonb"), default=dict)


class UserMention(Base, CreatedAtMixin):
    """A structured @mention inside a cortex idea thread."""

    __tablename__ = "user_mentions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    idea_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("ideas.id", ondelete="CASCADE"), nullable=False
    )
    mentioned_by: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("users.id"), nullable=False
    )
    thread_message_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("idea_threads.id"), nullable=True
    )
    seen_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "idea_id",
            "thread_message_id",
            name="uq_user_mentions_user_idea_thread",
        ),
        Index("ix_user_mentions_user_seen", "user_id", "seen_at"),
    )


class VisualBlock(Base, CreatedAtMixin):
    """A visual content block associated with a cortex idea thread."""

    __tablename__ = "visual_blocks"
    __table_args__ = (
        Index("ix_visual_blocks_idea_created", "idea_id", "created_at", "id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    idea_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("ideas.id", ondelete="CASCADE"), nullable=False
    )
    content_type: Mapped[str] = mapped_column(String(30), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    display_mode: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="inline", default="inline"
    )
    position_after: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("idea_threads.id", ondelete="SET NULL"), nullable=True
    )
    run_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)


class ProjectProfile(Base, CreatedAtMixin):
    """Durable reusable Project Context profile owned by an org/user."""

    __tablename__ = "project_profiles"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
        default=lambda: str(uuid.uuid4()),
    )
    org_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), ForeignKey("orgs.id", ondelete="CASCADE"), nullable=True
    )
    user_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    slug: Mapped[str] = mapped_column(String(120), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    project_context: Mapped[dict] = mapped_column(JSONB, nullable=False)
    visibility: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="private", default="private"
    )
    default_environment_binding_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("environment_bindings.id", ondelete="SET NULL"), nullable=True
    )
    active: Mapped[bool] = mapped_column(Boolean, server_default=text("TRUE"), default=True)
    metadata_: Mapped[Optional[dict]] = mapped_column(
        "metadata", JSONB, server_default=text("'{}'::jsonb"), default=dict
    )

    __table_args__ = (
        UniqueConstraint("org_id", "slug", name="uq_project_profiles_org_slug"),
        CheckConstraint("visibility IN ('private', 'public')", name="ck_project_profiles_visibility"),
        Index("ix_project_profiles_org_active", "org_id", "active"),
        Index("ix_project_profiles_org_visibility", "org_id", "visibility"),
    )


class ProjectProfileAccess(Base, CreatedAtMixin):
    """User access grant for private Project Context profiles."""

    __tablename__ = "project_profile_access"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_profile_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("project_profiles.id", ondelete="CASCADE"), nullable=False
    )
    shared_with_user_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    shared_by_user_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    __table_args__ = (
        UniqueConstraint(
            "project_profile_id",
            "shared_with_user_id",
            name="uq_project_profile_access_profile_user",
        ),
        Index("ix_project_profile_access_user", "shared_with_user_id"),
    )


class IdeaProjectAttachment(Base, CreatedAtMixin):
    """A Project Context snapshot attached to a thought/idea."""

    __tablename__ = "idea_project_attachments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    idea_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("ideas.id", ondelete="CASCADE"), nullable=False
    )
    project_profile_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), ForeignKey("project_profiles.id", ondelete="SET NULL"), nullable=True
    )
    attached_by: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    permission_scope: Mapped[Optional[dict]] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), default=dict
    )
    status: Mapped[str] = mapped_column(String(30), nullable=False, server_default="validated", default="validated")
    validation_errors: Mapped[Optional[list]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb"), default=list
    )
    environment_binding_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("environment_bindings.id", ondelete="SET NULL"), nullable=True
    )
    metadata_: Mapped[Optional[dict]] = mapped_column(
        "metadata", JSONB, server_default=text("'{}'::jsonb"), default=dict
    )

    __table_args__ = (
        Index("ix_idea_project_attachments_idea_status", "idea_id", "status"),
    )
