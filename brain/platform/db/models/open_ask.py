"""Durable obligations owed in Slack threads."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from brain.platform.db.base import Base, TimestampMixin
from brain.platform.db.constraints import check_in_constraint


OPEN_ASK_STATUSES = ("open", "answered", "routed", "expired")
OBLIGATION_NOTICE_STATES = ("pending", "posting", "delivered", "superseded")
UUIDString = UUID(as_uuid=False).with_variant(String, "sqlite")


class ObligationKind(StrEnum):
    HUMAN_ASK = "human_ask"
    RUN_DEFERRAL = "run_deferral"


OPEN_ASK_OBLIGATION_KINDS = tuple(kind.value for kind in ObligationKind)

__all__ = [
    "OBLIGATION_NOTICE_STATES",
    "OPEN_ASK_OBLIGATION_KINDS",
    "OPEN_ASK_STATUSES",
    "ObligationKind",
    "ObligationNotice",
    "OpenAsk",
]


class OpenAsk(Base, TimestampMixin):
    """One still-owed answer per Slack thread and obligation source."""

    __tablename__ = "open_asks"
    __table_args__ = (
        CheckConstraint(
            check_in_constraint("status", OPEN_ASK_STATUSES),
            name="ck_open_asks_status",
        ),
        CheckConstraint(
            check_in_constraint("obligation_kind", OPEN_ASK_OBLIGATION_KINDS),
            name="ck_open_asks_obligation_kind",
        ),
        CheckConstraint(
            "obligation_kind != 'human_ask' OR requester_slack_id IS NOT NULL",
            name="ck_open_asks_human_requester",
        ),
        CheckConstraint(
            "obligation_kind != 'run_deferral' OR origin_run_id IS NOT NULL",
            name="ck_open_asks_run_origin",
        ),
        Index(
            "uq_open_asks_slack_requester",
            "org_id",
            "channel_id",
            "thread_ts",
            "requester_slack_id",
            unique=True,
            postgresql_where=text("obligation_kind = 'human_ask'"),
            sqlite_where=text("obligation_kind = 'human_ask'"),
        ),
        Index(
            "ix_open_asks_org_status_opened",
            "org_id",
            "status",
            "opened_at",
        ),
        Index("ix_open_asks_origin_ref", "org_id", "origin_ref"),
        Index("ix_open_asks_origin_run", "origin_run_id"),
        Index(
            "uq_open_asks_run_deferral",
            "org_id",
            "channel_id",
            "thread_ts",
            "origin_run_id",
            unique=True,
            postgresql_where=text("obligation_kind = 'run_deferral'"),
            sqlite_where=text("obligation_kind = 'run_deferral'"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    obligation_kind: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        server_default=text("'human_ask'"),
        default=ObligationKind.HUMAN_ASK,
    )
    org_id: Mapped[str] = mapped_column(
        UUIDString,
        ForeignKey("orgs.id", ondelete="CASCADE"),
        nullable=False,
    )
    channel_id: Mapped[str] = mapped_column(String(80), nullable=False)
    channel_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    team_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    thread_ts: Mapped[str] = mapped_column(String(40), nullable=False)
    thread_permalink: Mapped[str] = mapped_column(Text, nullable=False)
    requester_slack_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    requester_user_id: Mapped[str | None] = mapped_column(
        UUIDString,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    requester_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    bot_user_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    ask_text: Mapped[str] = mapped_column(Text, nullable=False)
    origin_ref: Mapped[str] = mapped_column(String(500), nullable=False)
    origin_run_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("agent_runs.id", ondelete="SET NULL"),
        nullable=True,
    )
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        server_default=text("'open'"),
        default="open",
    )
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    answer_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    answer_artifact_kind: Mapped[str | None] = mapped_column(String(80), nullable=True)
    answer_artifact_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    answered_by_run_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("agent_runs.id", ondelete="SET NULL"),
        nullable=True,
    )
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delivered_message_ts: Mapped[str | None] = mapped_column(String(40), nullable=True)
    routed_to_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    routed_to_slack_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    routed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    @property
    def owner_label(self) -> str:
        """Display ownership without pretending every obligation has a requester."""

        if self.status == "routed" and self.routed_to_name:
            return str(self.routed_to_name)
        if self.status == "routed" and self.routed_to_slack_id:
            return f"<@{self.routed_to_slack_id}>"
        if self.obligation_kind == ObligationKind.HUMAN_ASK:
            if self.requester_name:
                return str(self.requester_name)
            if self.requester_slack_id:
                return f"<@{self.requester_slack_id}>"
        if self.obligation_kind == ObligationKind.RUN_DEFERRAL and self.origin_run_id:
            return f"Illo run {int(self.origin_run_id)}"
        return f"Slack obligation {int(self.id)}"


class ObligationNotice(Base, TimestampMixin):
    """Transactional outbox for one public condition on an obligation."""

    __tablename__ = "obligation_notices"
    __table_args__ = (
        CheckConstraint(
            check_in_constraint("state", OBLIGATION_NOTICE_STATES),
            name="ck_obligation_notices_state",
        ),
        UniqueConstraint(
            "obligation_id",
            "condition",
            name="uq_obligation_notices_obligation_condition",
        ),
        UniqueConstraint(
            "idempotency_key",
            name="uq_obligation_notices_idempotency_key",
        ),
        Index("ix_obligation_notices_org_state", "org_id", "state"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    obligation_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("open_asks.id", ondelete="CASCADE"),
        nullable=False,
    )
    org_id: Mapped[str] = mapped_column(
        UUIDString,
        ForeignKey("orgs.id", ondelete="CASCADE"),
        nullable=False,
    )
    condition: Mapped[str] = mapped_column(String(160), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(240), nullable=False)
    state: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        server_default=text("'pending'"),
        default="pending",
    )
    channel_id: Mapped[str] = mapped_column(String(80), nullable=False)
    # Ledger thread identity is always present. post_thread_ts is the actual
    # Slack API target and is nullable for DMs/top-level replies.
    thread_ts: Mapped[str] = mapped_column(String(40), nullable=False)
    post_thread_ts: Mapped[str | None] = mapped_column(String(40), nullable=True)
    bot_user_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    notice_text: Mapped[str] = mapped_column(Text, nullable=False)
    attempts: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("0"),
        default=0,
    )
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delivered_message_ts: Mapped[str | None] = mapped_column(String(40), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
