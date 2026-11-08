"""Organization-owned MCP configuration and metadata-only audit history."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from brain.platform.db.base import Base

__all__ = ["ExternalMcpServer", "ExternalMcpAuditEvent"]


class ExternalMcpServer(Base):
    __tablename__ = "external_mcp_servers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    org_id: Mapped[str] = mapped_column(UUID(as_uuid=False), ForeignKey("orgs.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(32))
    url: Mapped[str] = mapped_column(Text)
    transport: Mapped[str] = mapped_column(String(24), default="streamable_http", server_default="streamable_http")
    auth_mode: Mapped[str] = mapped_column(String(24), default="none", server_default="none")
    bearer_token_key: Mapped[str | None] = mapped_column(String(128))
    token_url: Mapped[str | None] = mapped_column(Text)
    client_id_key: Mapped[str | None] = mapped_column(String(128))
    client_secret_key: Mapped[str | None] = mapped_column(String(128))
    scope: Mapped[str | None] = mapped_column(Text)
    resource: Mapped[str | None] = mapped_column(Text)
    allowed_tools: Mapped[list] = mapped_column(JSONB, default=list, server_default=text("'[]'::jsonb"))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=30, server_default=text("30"))
    created_by: Mapped[str | None] = mapped_column(UUID(as_uuid=False), ForeignKey("users.id", ondelete="SET NULL"))
    updated_by: Mapped[str | None] = mapped_column(UUID(as_uuid=False), ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("NOW()"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("NOW()"))

    __table_args__ = (
        UniqueConstraint("org_id", "name", name="uq_external_mcp_servers_org_name"),
        CheckConstraint("timeout_seconds BETWEEN 1 AND 120", name="ck_external_mcp_timeout"),
        CheckConstraint("transport = 'streamable_http'", name="ck_external_mcp_transport"),
        CheckConstraint("auth_mode IN ('none', 'bearer', 'client_credentials')", name="ck_external_mcp_auth"),
    )


class ExternalMcpAuditEvent(Base):
    __tablename__ = "external_mcp_audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    org_id: Mapped[str] = mapped_column(UUID(as_uuid=False), ForeignKey("orgs.id", ondelete="CASCADE"), index=True)
    event_type: Mapped[str] = mapped_column(String(32))
    server_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("external_mcp_servers.id", ondelete="SET NULL"))
    server_name: Mapped[str] = mapped_column(String(32))
    actor_user_id: Mapped[str | None] = mapped_column(UUID(as_uuid=False), ForeignKey("users.id", ondelete="SET NULL"))
    run_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("agent_runs.id", ondelete="SET NULL"))
    tool_name: Mapped[str | None] = mapped_column(String(128))
    outcome: Mapped[str] = mapped_column(String(32))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    changed_fields: Mapped[list | None] = mapped_column(JSONB)
    error_kind: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("NOW()"))
