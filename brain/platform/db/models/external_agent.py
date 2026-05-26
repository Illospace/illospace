"""External personal-agent connection and task models."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.types import JSON
from sqlalchemy.orm import Mapped, mapped_column

from brain.platform.db.base import Base, TimestampMixin
from brain.platform.db.constraints import check_in_constraint
from brain.contracts.statuses import EXTERNAL_AGENT_TASK_STATUS_VALUES

JSONVariant = JSONB().with_variant(JSON(), "sqlite")
UUIDString = UUID(as_uuid=False).with_variant(String, "sqlite")

__all__ = [
    "ExternalAgentConnectionRow",
    "ExternalAgentConnectionTokenRow",
    "ExternalAgentTaskArtifactRow",
    "ExternalAgentTaskEventRow",
    "ExternalAgentTaskRow",
]


class ExternalAgentConnectionRow(Base, TimestampMixin):
    """A personal agent connection owned by a user inside an org."""

    __tablename__ = "external_agent_connections"
    __table_args__ = (
        Index("ix_external_agent_connections_org_owner_status", "org_id", "owner_user_id", "status"),
        Index("ix_external_agent_connections_org_kind", "org_id", "agent_kind"),
        Index("ix_external_agent_connections_status_seen", "status", "last_seen_at"),
    )

    id: Mapped[str] = mapped_column(
        UUIDString,
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
    )
    org_id: Mapped[str] = mapped_column(
        UUIDString,
        ForeignKey("orgs.id", ondelete="CASCADE"),
        nullable=False,
    )
    owner_user_id: Mapped[str] = mapped_column(
        UUIDString,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    agent_kind: Mapped[str] = mapped_column(String(40), nullable=False)
    transport: Mapped[str] = mapped_column(String(60), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="pending")
    endpoint_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    remote_agent_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    remote_session_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    remote_agent_card: Mapped[dict[str, Any]] = mapped_column(JSONVariant, nullable=False, default=dict)
    capabilities: Mapped[dict[str, Any]] = mapped_column(JSONVariant, nullable=False, default=dict)
    auth_secret_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    auth_metadata: Mapped[dict[str, Any]] = mapped_column(JSONVariant, nullable=False, default=dict)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_tested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSONVariant, nullable=False, default=dict)
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ExternalAgentConnectionTokenRow(Base):
    """Scoped bridge token for one external-agent connection."""

    __tablename__ = "external_agent_connection_tokens"
    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_external_agent_connection_tokens_hash"),
        Index("ix_external_agent_connection_tokens_connection", "connection_id"),
        Index("ix_external_agent_connection_tokens_org_owner", "org_id", "owner_user_id"),
    )

    id: Mapped[str] = mapped_column(
        UUIDString,
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
    )
    connection_id: Mapped[str] = mapped_column(
        UUIDString,
        ForeignKey("external_agent_connections.id", ondelete="CASCADE"),
        nullable=False,
    )
    org_id: Mapped[str] = mapped_column(
        UUIDString,
        ForeignKey("orgs.id", ondelete="CASCADE"),
        nullable=False,
    )
    owner_user_id: Mapped[str] = mapped_column(
        UUIDString,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    token_hash: Mapped[str] = mapped_column(String(96), nullable=False)
    token_prefix: Mapped[str] = mapped_column(String(24), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    scopes: Mapped[list[str]] = mapped_column(JSONVariant, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ExternalAgentTaskRow(Base, TimestampMixin):
    """A task delegated to, or created by, a personal external agent."""

    __tablename__ = "external_agent_tasks"
    __table_args__ = (
        CheckConstraint(
            check_in_constraint("status", EXTERNAL_AGENT_TASK_STATUS_VALUES),
            name="ck_external_agent_tasks_status",
        ),
        UniqueConstraint("connection_id", "idempotency_key", name="uq_external_agent_tasks_connection_idempotency"),
        Index("ix_external_agent_tasks_org_status_created", "org_id", "status", "created_at"),
        Index("ix_external_agent_tasks_connection_status_created", "connection_id", "status", "created_at"),
        Index("ix_external_agent_tasks_source_idea_created", "source_idea_id", "created_at"),
        Index("ix_external_agent_tasks_remote_task", "remote_task_id"),
        Index("ix_external_agent_tasks_illo_run", "illo_run_id"),
    )

    id: Mapped[str] = mapped_column(
        UUIDString,
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
    )
    org_id: Mapped[str] = mapped_column(
        UUIDString,
        ForeignKey("orgs.id", ondelete="CASCADE"),
        nullable=False,
    )
    connection_id: Mapped[str] = mapped_column(
        UUIDString,
        ForeignKey("external_agent_connections.id", ondelete="CASCADE"),
        nullable=False,
    )
    created_by_user_id: Mapped[str | None] = mapped_column(
        UUIDString,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    source_surface: Mapped[str] = mapped_column(String(40), nullable=False)
    source_idea_id: Mapped[str | None] = mapped_column(
        UUIDString,
        ForeignKey("ideas.id", ondelete="SET NULL"),
        nullable=True,
    )
    source_thread_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_chat_conversation_id: Mapped[str | None] = mapped_column(
        UUIDString,
        ForeignKey("chat_conversations.id", ondelete="SET NULL"),
        nullable=True,
    )
    source_chat_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    instructions: Mapped[str] = mapped_column(Text, nullable=False)
    input_parts: Mapped[list[dict[str, Any]]] = mapped_column(JSONVariant, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="queued")
    remote_task_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    remote_run_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    remote_session_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    illo_run_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(80), nullable=False)
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    result_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSONVariant, nullable=False, default=dict)


class ExternalAgentTaskEventRow(Base):
    """Durable event log for external-agent task progress."""

    __tablename__ = "external_agent_task_events"
    __table_args__ = (
        UniqueConstraint("task_id", "sequence_no", name="uq_external_agent_task_events_task_sequence"),
        Index("ix_external_agent_task_events_task_created", "task_id", "created_at"),
        Index("ix_external_agent_task_events_org_created", "org_id", "created_at"),
        Index("ix_external_agent_task_events_connection_created", "connection_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    task_id: Mapped[str] = mapped_column(
        UUIDString,
        ForeignKey("external_agent_tasks.id", ondelete="CASCADE"),
        nullable=False,
    )
    org_id: Mapped[str] = mapped_column(UUIDString, ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False)
    connection_id: Mapped[str] = mapped_column(
        UUIDString,
        ForeignKey("external_agent_connections.id", ondelete="CASCADE"),
        nullable=False,
    )
    sequence_no: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONVariant, nullable=False, default=dict)
    remote_event_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    producer: Mapped[str] = mapped_column(String(80), nullable=False)
    visibility: Mapped[str] = mapped_column(String(30), nullable=False, default="public")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class ExternalAgentTaskArtifactRow(Base):
    """Artifact returned by an external personal agent."""

    __tablename__ = "external_agent_task_artifacts"
    __table_args__ = (
        Index("ix_external_agent_task_artifacts_task_created", "task_id", "created_at"),
        Index("ix_external_agent_task_artifacts_org_created", "org_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(
        UUIDString,
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
    )
    task_id: Mapped[str] = mapped_column(
        UUIDString,
        ForeignKey("external_agent_tasks.id", ondelete="CASCADE"),
        nullable=False,
    )
    org_id: Mapped[str] = mapped_column(UUIDString, ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False)
    connection_id: Mapped[str] = mapped_column(
        UUIDString,
        ForeignKey("external_agent_connections.id", ondelete="CASCADE"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    mime_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_json: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant, nullable=True)
    uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    upload_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSONVariant, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
