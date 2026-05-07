"""Resource lease and warm pool registry models."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import ForeignKey, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from brain.platform.db.base import Base, CreatedAtMixin

__all__ = [
    "ResourceLease",
    "WorkspacePoolEntry",
    "BrowserPoolEntry",
]


class ResourceLease(Base, CreatedAtMixin):
    """A renewable lease for a tracked runtime resource."""

    __tablename__ = "resource_leases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    resource_type: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    resource_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    owner_run_id: Mapped[Optional[int]] = mapped_column(
        Integer,
        ForeignKey("agent_runs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    owner_worker_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True, index=True)
    lease_token: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        unique=True,
        index=True,
        server_default=text("gen_random_uuid()::text"),
        default=lambda: uuid.uuid4().hex,
    )
    heartbeat_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    expires_at: Mapped[Optional[datetime]] = mapped_column(nullable=True, index=True)
    released_at: Mapped[Optional[datetime]] = mapped_column(nullable=True, index=True)
    release_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class WorkspacePoolEntry(Base, CreatedAtMixin):
    """A warm workspace snapshot or delta base candidate."""

    __tablename__ = "workspace_pool_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    repo_root: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    base_commit: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    runtime_fingerprint: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    pool_key: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        server_default="ready",
        default="ready",
        index=True,
    )
    base_path: Mapped[str] = mapped_column(Text, nullable=False)
    mode: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        server_default="cold",
        default="cold",
        index=True,
    )
    last_used_at: Mapped[Optional[datetime]] = mapped_column(nullable=True, index=True)
    ttl_expires_at: Mapped[Optional[datetime]] = mapped_column(nullable=True, index=True)
    health: Mapped[Optional[dict]] = mapped_column(
        JSONB,
        nullable=True,
        server_default=text("'{}'::jsonb"),
        default=dict,
    )


class BrowserPoolEntry(Base, CreatedAtMixin):
    """A clean browser context candidate."""

    __tablename__ = "browser_pool_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    profile_key: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    browser_version: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        server_default="ready",
        default="ready",
        index=True,
    )
    context_mode: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        server_default="fresh",
        default="fresh",
        index=True,
    )
    last_used_at: Mapped[Optional[datetime]] = mapped_column(nullable=True, index=True)
    ttl_expires_at: Mapped[Optional[datetime]] = mapped_column(nullable=True, index=True)
    health: Mapped[Optional[dict]] = mapped_column(
        JSONB,
        nullable=True,
        server_default=text("'{}'::jsonb"),
        default=dict,
    )
