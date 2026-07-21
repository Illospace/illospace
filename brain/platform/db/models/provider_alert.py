"""Durable acknowledgement/throttle state for provider-alert signatures."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from brain.platform.db.base import Base, TimestampMixin


UUIDString = UUID(as_uuid=False).with_variant(String, "sqlite")

__all__ = ["ProviderAlertLedger"]


class ProviderAlertLedger(Base, TimestampMixin):
    """One durable row per org/channel/typed alert signature."""

    __tablename__ = "provider_alert_ledger"
    __table_args__ = (
        UniqueConstraint(
            "org_id",
            "channel_id",
            "signature",
            name="uq_provider_alert_ledger_signature",
        ),
        Index(
            "ix_provider_alert_ledger_recent",
            "org_id",
            "channel_id",
            "last_seen_at",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    org_id: Mapped[str] = mapped_column(
        UUIDString,
        ForeignKey("orgs.id", ondelete="CASCADE"),
        nullable=False,
    )
    channel_id: Mapped[str] = mapped_column(String(80), nullable=False)
    signature: Mapped[str] = mapped_column(String(64), nullable=False)
    classification: Mapped[str] = mapped_column(String(80), nullable=False)
    severity: Mapped[str] = mapped_column(String(20), nullable=False)
    rule_id: Mapped[str] = mapped_column(String(120), nullable=False)
    occurrence_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("1"),
        default=1,
    )
    occurrences_at_last_post: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("0"),
        default=0,
    )
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    slack_thread_ts: Mapped[str | None] = mapped_column(String(40), nullable=True)
    slack_message_ts: Mapped[str | None] = mapped_column(String(40), nullable=True)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    acknowledged_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    acknowledgement: Mapped[str | None] = mapped_column(Text, nullable=True)
