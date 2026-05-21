"""Org, User, and credential models.

Matches the SQL schema exactly:
  orgs, users, org_api_keys, user_codex_connections
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    DateTime,
    Boolean,
    ForeignKey,
    Integer,
    JSON,
    LargeBinary,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from brain.platform.db.base import Base, CreatedAtMixin

__all__ = ["Org", "User", "OrgApiKey", "UserCodexConnection"]


class Org(Base, CreatedAtMixin):
    """An organisation — top-level tenant."""

    __tablename__ = "orgs"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
        default=lambda: str(uuid.uuid4()),
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    slug: Mapped[str] = mapped_column(String(50), nullable=False, unique=True)

    # Memory-DAG configuration
    memory_model_config: Mapped[Optional[dict]] = mapped_column(
        JSONB().with_variant(JSON, "sqlite"),
        nullable=True,
    )
    memory_token_budget: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)


class User(Base, CreatedAtMixin):
    """A user belonging to an org."""

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
        default=lambda: str(uuid.uuid4()),
    )
    org_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("orgs.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    email: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    color: Mapped[str] = mapped_column(
        String(7), nullable=False, server_default="#6366f1", default="#6366f1"
    )
    role: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="member", default="member"
    )
    password_hash: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    vault_salt: Mapped[Optional[bytes]] = mapped_column(LargeBinary, nullable=True)
    attribution_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("TRUE"), default=True
    )
    approved: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("FALSE"), default=False
    )
    notification_sound_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("TRUE"), default=True
    )
    message_notifications_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("TRUE"), default=True
    )

    __table_args__ = (
        # role CHECK constraint is expressed in the DB; SQLAlchemy models
        # don't enforce it at the Python level but it matches the schema.
    )


class OrgApiKey(Base, CreatedAtMixin):
    """An org-level provider API key shared across the workspace."""

    __tablename__ = "org_api_keys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    org_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False
    )
    provider: Mapped[str] = mapped_column(
        String(50), nullable=False
    )
    encrypted_key: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    label: Mapped[str] = mapped_column(
        String(100), server_default="main", default="main"
    )
    last_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    total_tokens_used: Mapped[int] = mapped_column(
        Integer, server_default=text("0"), default=0
    )
    estimated_cost_usd: Mapped[float] = mapped_column(
        Numeric(10, 4), server_default=text("0"), default=0
    )

    __table_args__ = (
        UniqueConstraint("org_id", "provider", name="uq_org_api_keys_org_provider"),
    )


class UserCodexConnection(Base, CreatedAtMixin):
    """The only user-owned credential: a user's Codex/ChatGPT subscription."""

    __tablename__ = "user_codex_connections"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    encrypted_credential: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    label: Mapped[str] = mapped_column(
        String(100), server_default="Codex / ChatGPT", default="Codex / ChatGPT"
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("TRUE"), default=True
    )
    last_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    total_tokens_used: Mapped[int] = mapped_column(
        Integer, server_default=text("0"), default=0
    )
    estimated_cost_usd: Mapped[float] = mapped_column(
        Numeric(10, 4), server_default=text("0"), default=0
    )

    __table_args__ = (
        UniqueConstraint("user_id", name="uq_user_codex_connections_user"),
    )
