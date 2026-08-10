"""Runtime-editable storage and retention policy revisions."""

from __future__ import annotations

from typing import Optional

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from brain.platform.db.base import Base, CreatedAtMixin

__all__ = ["StoragePolicy"]


class StoragePolicy(Base, CreatedAtMixin):
    """Versioned policy values with exactly one active revision."""

    __tablename__ = "storage_policies"
    __table_args__ = (
        CheckConstraint(
            "finished_workspace_retention_hours > 0",
            name="ck_storage_policies_finished_workspace_retention_positive",
        ),
        CheckConstraint(
            "project_draft_retention_hours > 0",
            name="ck_storage_policies_project_draft_retention_positive",
        ),
        CheckConstraint(
            "canvas_quiet_hours > 0",
            name="ck_storage_policies_canvas_quiet_positive",
        ),
        CheckConstraint(
            "capacity_warn_percent >= 1 AND capacity_warn_percent <= 99",
            name="ck_storage_policies_warn_percent",
        ),
        CheckConstraint(
            "capacity_critical_percent >= 2 AND capacity_critical_percent <= 100",
            name="ck_storage_policies_critical_percent",
        ),
        CheckConstraint(
            "capacity_warn_percent < capacity_critical_percent",
            name="ck_storage_policies_threshold_order",
        ),
        Index(
            "uq_storage_policies_one_active",
            "is_active",
            unique=True,
            postgresql_where=text("is_active"),
            sqlite_where=text("is_active = 1"),
        ),
        Index("ix_storage_policies_created", "created_at", "id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    finished_workspace_retention_hours: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )
    project_draft_retention_hours: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )
    canvas_quiet_hours: Mapped[int] = mapped_column(Integer, nullable=False)
    capacity_warn_percent: Mapped[int] = mapped_column(Integer, nullable=False)
    capacity_critical_percent: Mapped[int] = mapped_column(Integer, nullable=False)
    automatic_reclamation_allowed: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("FALSE"),
        default=False,
    )
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    source_type: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        server_default="system",
        default="system",
    )
    source_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    reverted_from_id: Mapped[Optional[int]] = mapped_column(
        Integer,
        ForeignKey("storage_policies.id", ondelete="SET NULL"),
        nullable=True,
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("TRUE"),
        default=True,
    )
