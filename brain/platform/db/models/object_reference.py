"""Durable object references extracted from user-visible surfaces."""
from __future__ import annotations

from typing import Optional

from sqlalchemy import ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from brain.platform.db.base import Base, CreatedAtMixin

__all__ = ["ObjectReference"]


class ObjectReference(Base, CreatedAtMixin):
    """A normalized reference from chat/thread text to a product object."""

    __tablename__ = "object_references"
    __table_args__ = (
        UniqueConstraint(
            "source_type",
            "source_id",
            "object_type",
            "object_id",
            "original_ref",
            name="uq_object_references_source_object_ref",
        ),
        Index("ix_object_references_source", "source_type", "source_id"),
        Index("ix_object_references_object", "object_type", "object_id"),
        Index("ix_object_references_org_created", "org_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    org_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False
    )
    source_type: Mapped[str] = mapped_column(String(60), nullable=False)
    source_id: Mapped[str] = mapped_column(String(120), nullable=False)
    object_type: Mapped[str] = mapped_column(String(60), nullable=False)
    object_id: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    original_ref: Mapped[str] = mapped_column(Text, nullable=False)
    canonical_ref: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False, server_default="available", default="available")
    reference_payload: Mapped[Optional[dict]] = mapped_column(
        JSONB, nullable=True, server_default=text("'{}'::jsonb"), default=dict
    )
