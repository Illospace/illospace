"""Durable acknowledgement/throttle state for provider-alert signatures."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from brain.platform.db.base import Base, TimestampMixin


UUIDString = UUID(as_uuid=False).with_variant(String, "sqlite")

__all__ = [
    "ProviderAlertLedger",
    "ProviderAlertOccurrence",
    "ProviderAlertSurge",
]


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


class ProviderAlertOccurrence(Base, TimestampMixin):
    """One idempotent Rollbar message observed on a monitored Slack channel."""

    __tablename__ = "provider_alert_occurrences"
    __table_args__ = (
        UniqueConstraint(
            "org_id",
            "channel_id",
            "slack_message_ts",
            name="uq_provider_alert_occurrence_message",
        ),
        Index(
            "ix_provider_alert_occurrence_window",
            "org_id",
            "channel_id",
            "service",
            "occurred_at",
        ),
        Index(
            "ix_provider_alert_occurrence_signature",
            "org_id",
            "service",
            "subsystem",
            "signature",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    org_id: Mapped[str] = mapped_column(
        UUIDString,
        ForeignKey("orgs.id", ondelete="CASCADE"),
        nullable=False,
    )
    channel_id: Mapped[str] = mapped_column(String(80), nullable=False)
    slack_message_ts: Mapped[str] = mapped_column(String(40), nullable=False)
    service: Mapped[str] = mapped_column(String(120), nullable=False)
    subsystem: Mapped[str] = mapped_column(String(120), nullable=False)
    external_id: Mapped[str] = mapped_column(String(180), nullable=False)
    signature: Mapped[str] = mapped_column(String(64), nullable=False)
    signature_title: Mapped[str] = mapped_column(Text, nullable=False)
    occurrence_milestone: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_new_error: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("false"),
        default=False,
    )
    is_new_signature: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("false"),
        default=False,
    )
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ProviderAlertSurge(Base, TimestampMixin):
    """Current material-surge state for one service on one source channel."""

    __tablename__ = "provider_alert_surges"
    __table_args__ = (
        UniqueConstraint(
            "org_id",
            "source_channel_id",
            "service",
            name="uq_provider_alert_surge_service",
        ),
        Index(
            "ix_provider_alert_surge_open",
            "org_id",
            "last_seen_at",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    org_id: Mapped[str] = mapped_column(
        UUIDString,
        ForeignKey("orgs.id", ondelete="CASCADE"),
        nullable=False,
    )
    source_channel_id: Mapped[str] = mapped_column(String(80), nullable=False)
    service: Mapped[str] = mapped_column(String(120), nullable=False)
    subsystem: Mapped[str] = mapped_column(String(120), nullable=False)
    window_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    trigger_reason: Mapped[str] = mapped_column(String(80), nullable=False)
    message_count: Mapped[int] = mapped_column(Integer, nullable=False)
    signatures_json: Mapped[str] = mapped_column(Text, nullable=False)
    external_ids_json: Mapped[str] = mapped_column(Text, nullable=False)
    owner: Mapped[str] = mapped_column(String(180), nullable=False)
    next_action: Mapped[str] = mapped_column(Text, nullable=False)
    material_channel: Mapped[str] = mapped_column(String(80), nullable=False)
    material_message_ts: Mapped[str | None] = mapped_column(String(40), nullable=True)
    material_post_claimed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    material_posted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
