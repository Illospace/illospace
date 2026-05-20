"""Vault models: org-owned secrets, config, sessions, agent grants, project bindings, access log, missing requests.

Matches: secrets, vault_config, vault_sessions, vault_agent_grants, vault_project_bindings, vault_access_log,
         vault_missing_requests tables.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    DateTime,
    Boolean,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from brain.platform.db.base import Base, CreatedAtMixin

__all__ = [
    "Secret",
    "VaultConfig",
    "VaultSession",
    "VaultAgentGrant",
    "VaultProjectBinding",
    "VaultAccessLog",
    "VaultMissingRequest",
]


class Secret(Base):
    """An encrypted secret stored in the vault."""

    __tablename__ = "secrets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key_name: Mapped[str] = mapped_column(String(128), nullable=False)
    encrypted_value: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    description: Mapped[str] = mapped_column(
        Text, server_default="", default=""
    )
    category: Mapped[str] = mapped_column(
        String(64), server_default="general", default="general"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("NOW()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("NOW()")
    )
    last_accessed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    access_count: Mapped[int] = mapped_column(
        Integer, server_default=text("0"), default=0
    )
    agent_access_level: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="ask", default="ask"
    )
    org_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_by_user_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    updated_by_user_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    __table_args__ = (
        UniqueConstraint("org_id", "key_name", name="secrets_org_key_unique"),
    )


class VaultConfig(Base):
    """Key-value vault configuration."""

    __tablename__ = "vault_config"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("NOW()")
    )


class VaultSession(Base):
    """Short-lived unlock session for an org vault."""

    __tablename__ = "vault_sessions"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    org_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    actor_user_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("NOW()")
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_seen_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class VaultAgentGrant(Base):
    """Task-scoped capability that lets an agent read one vault secret."""

    __tablename__ = "vault_agent_grants"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key_name: Mapped[str] = mapped_column(String(128), nullable=False)
    org_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    requested_by_user_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    run_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=True
    )
    requested_by: Mapped[str] = mapped_column(String(128), server_default="agent", default="agent")
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(20), server_default="pending", default="pending")
    approved_by_user_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("NOW()")
    )
    decided_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    read_count: Mapped[int] = mapped_column(Integer, server_default=text("0"), default=0)
    max_reads: Mapped[int] = mapped_column(Integer, server_default=text("1"), default=1)


class VaultProjectBinding(Base):
    """Project/env token availability for an org-owned secret."""

    __tablename__ = "vault_project_bindings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    secret_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("secrets.id", ondelete="CASCADE"), nullable=False
    )
    org_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_by_user_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    target_registry_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("target_registry.id", ondelete="SET NULL"), nullable=True
    )
    project_slug: Mapped[str] = mapped_column(String(120), nullable=False)
    env_name: Mapped[str] = mapped_column(String(128), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, server_default=text("TRUE"), default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("NOW()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("NOW()")
    )

    __table_args__ = (
        UniqueConstraint(
            "org_id",
            "project_slug",
            "env_name",
            name="uq_vault_project_bindings_org_project_env",
        ),
    )


class VaultAccessLog(Base):
    """Audit log for vault access."""

    __tablename__ = "vault_access_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    org_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    actor_user_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    secret_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("secrets.id", ondelete="SET NULL"), nullable=True
    )
    key_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    action: Mapped[str] = mapped_column(String(20), nullable=False)
    accessed_by: Mapped[str] = mapped_column(
        String(50), server_default="user", default="user"
    )
    accessed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("NOW()")
    )


class VaultMissingRequest(Base):
    """A request for a secret that doesn't exist yet."""

    __tablename__ = "vault_missing_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key_name: Mapped[str] = mapped_column(String(128), nullable=False)
    actor_user_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    org_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    request_count: Mapped[int] = mapped_column(
        Integer, server_default=text("1"), default=1
    )
    first_requested: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("NOW()")
    )
    last_requested: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("NOW()")
    )
    resolved: Mapped[bool] = mapped_column(
        Boolean, server_default=text("FALSE"), default=False
    )
