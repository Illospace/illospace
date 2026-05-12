"""SQLAlchemy declarative base and mixins.

Every model inherits from Base. Mixins add common columns:
- CreatedAtMixin: created_at (most tables)
- TimestampMixin: created_at + updated_at (tables with both)
- OrgScopedMixin: org_id FK for multi-tenancy
- ArchivableMixin: soft-delete via archived flag
"""
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """All models inherit from this."""

    def __repr__(self) -> str:
        pk = getattr(self, "id", None)
        if pk is None:
            pk = getattr(self, "session_id", None)
        if pk is None:
            pk = getattr(self, "key", None)
        return f"<{type(self).__name__} {pk!r}>"


class CreatedAtMixin:
    """created_at only. For tables that have created_at but no updated_at."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class TimestampMixin(CreatedAtMixin):
    """created_at + updated_at. Only for tables that have BOTH columns."""

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class OrgScopedMixin:
    """org_id FK for multi-tenant isolation."""

    org_id: Mapped[str | None] = mapped_column(
        ForeignKey("orgs.id"), index=True
    )


class ArchivableMixin:
    """Soft-delete via archived flag."""

    archived: Mapped[bool] = mapped_column(server_default="false", default=False)
