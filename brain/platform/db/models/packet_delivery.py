"""Outbox rows for handoff-packet brief delivery to Slack origin threads.

One row per handoff id (spec: illo-handoff-packets, post-slice-05 hardening).
The row is written inside the mint's savepoint so it commits or rolls back
atomically with the handoff row, the idea stamp, and the run's terminal
status — the Slack post itself happens strictly AFTER that commit (see
``brain.systems.briefing.deliver``). ``idempotency_key`` is deterministic
(``packet-brief:<handoff_id>``); the brief text embeds the handoff id via
its launch URL, which is the in-thread marker crash recovery checks before
ever re-sending.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from brain.platform.db.base import Base, TimestampMixin
from brain.platform.db.constraints import check_in_constraint

UUIDString = UUID(as_uuid=False).with_variant(String, "sqlite")

# pending  → recorded at mint time, not yet attempted (or returned after a
#            definite Slack reject that is worth retrying)
# posting  → claimed by a deliverer; an attempt may be in flight. A stale
#            claim is ambiguous (crash before OR after the send) and must be
#            disambiguated by reading the origin thread before any re-send.
# posted   → the brief is in the thread (send confirmed, or found by the
#            disambiguation read).
# superseded → the handoff was superseded before the brief went out; the
#            obligation moved to the superseding row's delivery.
# failed   → permanent Slack reject or attempt cap reached; loud log, no retry.
PACKET_BRIEF_DELIVERY_STATES = ("pending", "posting", "posted", "superseded", "failed")

__all__ = ["PacketBriefDelivery", "PACKET_BRIEF_DELIVERY_STATES"]


class PacketBriefDelivery(Base, TimestampMixin):
    """Transactional-outbox record for one handoff packet's human brief."""

    __tablename__ = "packet_brief_deliveries"
    __table_args__ = (
        CheckConstraint(
            check_in_constraint("state", PACKET_BRIEF_DELIVERY_STATES),
            name="ck_packet_brief_deliveries_state",
        ),
        UniqueConstraint("handoff_id", name="uq_packet_brief_deliveries_handoff"),
        Index("ix_packet_brief_deliveries_org_state", "org_id", "state"),
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
    handoff_id: Mapped[str] = mapped_column(
        UUIDString,
        ForeignKey("launch_handoffs.id", ondelete="CASCADE"),
        nullable=False,
    )
    state: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="pending", default="pending"
    )
    idempotency_key: Mapped[str] = mapped_column(String(120), nullable=False)
    channel: Mapped[str] = mapped_column(String(40), nullable=False)
    thread_ts: Mapped[str] = mapped_column(String(40), nullable=False)
    # Illo's Slack identity from the origin provenance: crash disambiguation
    # counts only bot-authored thread messages, so a human pasting the same
    # launch URL can never satisfy the "already posted" check.
    bot_user_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    brief: Mapped[str] = mapped_column(Text, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"), default=0)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    posted_message_ts: Mapped[str | None] = mapped_column(String(40), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
