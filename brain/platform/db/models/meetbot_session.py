"""Durable lifecycle records for meetbot join requests."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from brain.platform.db.base import Base
from brain.platform.db.constraints import check_in_constraint

__all__ = [
    "MEETBOT_SESSION_OUTCOMES",
    "MeetbotSession",
    "MeetbotSessionOutcome",
]


class MeetbotSessionOutcome(StrEnum):
    """Lifecycle outcomes accepted by the brain persistence boundary."""

    REQUESTED = "requested"
    ADMITTED = "admitted"
    REFUSED = "refused"
    NOT_ADMITTED = "not_admitted"
    LEFT = "left"


MEETBOT_SESSION_OUTCOMES = tuple(outcome.value for outcome in MeetbotSessionOutcome)


class MeetbotSession(Base):
    """One durable meetbot join request and its observed outcome."""

    __tablename__ = "meetbot_sessions"
    __table_args__ = (
        CheckConstraint(
            check_in_constraint("outcome", MEETBOT_SESSION_OUTCOMES),
            name="ck_meetbot_sessions_outcome",
        ),
        CheckConstraint(
            "participant_count IS NULL OR participant_count >= 0",
            name="ck_meetbot_sessions_participant_count_nonnegative",
        ),
        CheckConstraint(
            "caption_count IS NULL OR caption_count >= 0",
            name="ck_meetbot_sessions_caption_count_nonnegative",
        ),
        Index(
            "ix_meetbot_sessions_meeting_requested",
            "meeting_url",
            "requested_at",
        ),
        Index("ix_meetbot_sessions_requesting_run_id", "requesting_run_id"),
    )

    session_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    meeting_url: Mapped[str] = mapped_column(Text, nullable=False)
    requesting_run_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("agent_runs.id", ondelete="SET NULL"),
        nullable=True,
    )
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    outcome: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=MeetbotSessionOutcome.REQUESTED.value,
        server_default=MeetbotSessionOutcome.REQUESTED.value,
    )
    refusal_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    admitted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    left_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    participant_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    caption_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
