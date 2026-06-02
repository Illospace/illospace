"""Launch handoffs for opening coding work in a local agent surface."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from brain.contracts.statuses import LAUNCH_HANDOFF_STATUS_VALUES
from brain.platform.db.base import Base, TimestampMixin
from brain.platform.db.constraints import check_in_constraint

JSONVariant = JSONB().with_variant(JSON(), "sqlite")
UUIDString = UUID(as_uuid=False).with_variant(String, "sqlite")

__all__ = ["LaunchHandoff"]


class LaunchHandoff(Base, TimestampMixin):
    """A durable, user-clicked package for launching work in an external agent."""

    __tablename__ = "launch_handoffs"
    __table_args__ = (
        CheckConstraint(
            check_in_constraint("status", LAUNCH_HANDOFF_STATUS_VALUES),
            name="ck_launch_handoffs_status",
        ),
        UniqueConstraint("org_id", "idempotency_key", name="uq_launch_handoffs_org_idempotency"),
        Index("ix_launch_handoffs_org_target_status_created", "org_id", "target_tool", "status", "created_at"),
        Index("ix_launch_handoffs_org_source_created", "org_id", "source_surface", "created_at"),
        Index("ix_launch_handoffs_created_by", "created_by_user_id", "created_at"),
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
    created_by_user_id: Mapped[str | None] = mapped_column(
        UUIDString,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    source_surface: Mapped[str] = mapped_column(String(40), nullable=False, default="illo")
    source_ref: Mapped[dict[str, Any]] = mapped_column(JSONVariant, nullable=False, default=dict)
    target_tool: Mapped[str] = mapped_column(String(40), nullable=False, default="codex")
    title: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    instructions: Mapped[str] = mapped_column(Text, nullable=False)
    acceptance_criteria: Mapped[list[Any]] = mapped_column(JSONVariant, nullable=False, default=list)
    context_parts: Mapped[list[dict[str, Any]]] = mapped_column(JSONVariant, nullable=False, default=list)
    repo_origin_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    branch_hint: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False, server_default="open", default="open")
    launch_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"), default=0)
    last_launched_by_user_id: Mapped[str | None] = mapped_column(
        UUIDString,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    last_launched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(120), nullable=True)
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSONVariant, nullable=False, default=dict)
