"""Durable obligations owed in Slack threads."""

from __future__ import annotations

from datetime import datetime

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


OPEN_ASK_STATUSES = ("open", "answered")
OPEN_ASK_OBLIGATION_KINDS = ("human_ask", "run_deferral")
UUIDString = UUID(as_uuid=False).with_variant(String, "sqlite")

__all__ = ["OPEN_ASK_OBLIGATION_KINDS", "OPEN_ASK_STATUSES", "OpenAsk"]


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
        UniqueConstraint(
            "org_id",
            "channel_id",
            "thread_ts",
            "requester_slack_id",
            name="uq_open_asks_slack_requester",
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
        default="human_ask",
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
    requester_slack_id: Mapped[str] = mapped_column(String(80), nullable=False)
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
    notice_conditions: Mapped[str | None] = mapped_column(Text, nullable=True)
