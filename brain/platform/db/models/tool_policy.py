"""Organization-scoped tool policies and append-only change history."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    JSON,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from brain.platform.db.base import Base

__all__ = ["ToolPolicy", "ToolPolicyChangeAudit"]


class ToolPolicy(Base):
    __tablename__ = "tool_policies"
    __table_args__ = (
        UniqueConstraint(
            "org_id", "scope_type", "scope_key", name="uq_tool_policy_scope"
        ),
        CheckConstraint(
            "(scope_type = 'organization' AND scope_key = '') OR "
            "(scope_type = 'skill' AND scope_key <> '')",
            name="ck_tool_policy_scope",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    org_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("orgs.id", ondelete="CASCADE")
    )
    scope_type: Mapped[str] = mapped_column(String(20))
    scope_key: Mapped[str] = mapped_column(String(50))
    disabled_tools: Mapped[list] = mapped_column(
        JSONB().with_variant(JSON, "sqlite"), default=list
    )
    version: Mapped[int] = mapped_column(Integer)
    updated_by: Mapped[str] = mapped_column(UUID(as_uuid=False), ForeignKey("users.id"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ToolPolicyChangeAudit(Base):
    __tablename__ = "tool_policy_change_audits"
    __table_args__ = (
        UniqueConstraint(
            "org_id",
            "scope_type",
            "scope_key",
            "version",
            name="uq_tool_policy_audit_version",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    org_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("orgs.id", ondelete="CASCADE")
    )
    scope_type: Mapped[str] = mapped_column(String(20))
    scope_key: Mapped[str] = mapped_column(String(50))
    version: Mapped[int] = mapped_column(Integer)
    actor_id: Mapped[str] = mapped_column(String(100))
    actor_type: Mapped[str] = mapped_column(String(30))
    before_disabled_tools: Mapped[list] = mapped_column(
        JSONB().with_variant(JSON, "sqlite")
    )
    after_disabled_tools: Mapped[list] = mapped_column(
        JSONB().with_variant(JSON, "sqlite")
    )
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
