"""Org, User, and API key models.

Matches the SQL schema exactly:
  orgs, users, user_api_keys, api_key_shares, org_api_keys
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

__all__ = ["Org", "User", "UserApiKey", "ApiKeyShare", "OrgApiKey"]


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
    default_provider: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    default_api_key_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
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


class UserApiKey(Base, CreatedAtMixin):
    """A personal API key owned by a user."""

    __tablename__ = "user_api_keys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    provider: Mapped[str] = mapped_column(
        String(50), nullable=False
    )
    encrypted_key: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    label: Mapped[str] = mapped_column(
        String(100), server_default="", default=""
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
        UniqueConstraint("user_id", "provider", "label", name="uq_user_api_keys_user_provider_label"),
    )


class ApiKeyShare(Base, CreatedAtMixin):
    """A shared user API key grant."""

    __tablename__ = "api_key_shares"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    api_key_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("user_api_keys.id", ondelete="CASCADE"), nullable=False
    )
    shared_with_user_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("users.id"), nullable=False
    )
    shared_by_user_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("users.id"), nullable=False
    )
    shared_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("api_key_id", "shared_with_user_id", name="uq_api_key_shares_key_user"),
    )


class OrgApiKey(Base, CreatedAtMixin):
    """An org-level API key (shared across the org)."""

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
