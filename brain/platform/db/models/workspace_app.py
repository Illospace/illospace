"""Generated workspace app persistence models."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from brain.platform.db.base import Base, CreatedAtMixin, TimestampMixin

__all__ = [
    "WorkspaceApp",
    "WorkspaceAppVersion",
    "WorkspaceAppState",
]


class WorkspaceApp(Base, TimestampMixin):
    """An org-wide generated UI application available inside Cortex."""

    __tablename__ = "workspace_apps"
    __table_args__ = (
        UniqueConstraint("org_id", "key", name="uq_workspace_apps_org_key"),
        Index("ix_workspace_apps_org_archived", "org_id", "archived_at"),
        Index("ix_workspace_apps_anchor_user", "anchor_user_id"),
    )

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
        default=lambda: str(uuid.uuid4()),
    )
    org_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    key: Mapped[str] = mapped_column(String(100), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    renderer_key: Mapped[str] = mapped_column(
        String(120), nullable=False, server_default="app-capsule", default="app-capsule"
    )
    visual_spec: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
    app_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
    created_by_user_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    anchor_user_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    archived_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )


class WorkspaceAppVersion(Base, CreatedAtMixin):
    """One generated source snapshot for a workspace app."""

    __tablename__ = "workspace_app_versions"
    __table_args__ = (
        UniqueConstraint("app_id", "version", name="uq_workspace_app_versions_app_version"),
        Index("ix_workspace_app_versions_app_created", "app_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    app_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("workspace_apps.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    renderer_key: Mapped[str] = mapped_column(
        String(120), nullable=False, server_default="app-capsule", default="app-capsule"
    )
    source_kind: Mapped[str] = mapped_column(
        String(40), nullable=False, server_default="html", default="html"
    )
    source_code: Mapped[str] = mapped_column(Text, nullable=False)
    manifest: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
    created_by_user_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class WorkspaceAppState(Base, TimestampMixin):
    """Durable state bucket for a generated workspace app."""

    __tablename__ = "workspace_app_states"
    __table_args__ = (
        UniqueConstraint("app_id", "scope", "key", name="uq_workspace_app_states_app_scope_key"),
        Index("ix_workspace_app_states_org_app", "org_id", "app_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    org_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    app_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("workspace_apps.id", ondelete="CASCADE"), nullable=False, index=True
    )
    scope: Mapped[str] = mapped_column(String(30), nullable=False, server_default="org", default="org")
    key: Mapped[str] = mapped_column(String(120), nullable=False, server_default="default", default="default")
    data: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
    updated_by_user_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
