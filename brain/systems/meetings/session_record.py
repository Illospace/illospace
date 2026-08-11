"""Write durable meetbot lifecycle state inside the brain database boundary."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import logging

from sqlalchemy.ext.asyncio import AsyncSession

from brain.platform.db.models.meetbot_session import MeetbotSession
from meetbot.models import MeetbotSessionOutcome, SessionStatus

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class MeetbotHealthUpdate:
    """Validated active-session values accepted by the persistence boundary."""

    session_id: str
    status: SessionStatus
    joined_at: datetime | None
    participant_count: int
    caption_count: int


@dataclass(frozen=True, slots=True)
class MeetbotTerminalUpdate:
    """Validated terminal-session values accepted by the persistence boundary."""

    session_id: str
    outcome: MeetbotSessionOutcome
    joined_at: datetime | None
    ended_at: datetime
    participant_count: int
    caption_count: int
    refusal_text: str | None = None


async def create_requested_meetbot_session(
    session: AsyncSession,
    *,
    session_id: str,
    meeting_url: str,
    requesting_run_id: int | None,
    requested_at: datetime | None = None,
) -> None:
    """Add the durable request that must commit before meetbot starts a browser."""

    session.add(
        MeetbotSession(
            session_id=session_id,
            meeting_url=meeting_url,
            requesting_run_id=requesting_run_id,
            requested_at=requested_at or datetime.now(timezone.utc),
            outcome=MeetbotSessionOutcome.REQUESTED.value,
        )
    )
    await session.flush()


async def record_meetbot_health(
    session: AsyncSession,
    update: MeetbotHealthUpdate,
) -> None:
    """Apply one validated active callback without undoing a terminal outcome."""

    row = await _requested_row(session, update.session_id)
    if row is None:
        return
    if row.outcome in {
        MeetbotSessionOutcome.REQUESTED.value,
        MeetbotSessionOutcome.ADMITTED.value,
    } and update.status in {"admitted", "captions_flowing"}:
        row.outcome = MeetbotSessionOutcome.ADMITTED.value
        row.admitted_at = row.admitted_at or update.joined_at
    row.participant_count = _max_observed(
        row.participant_count,
        update.participant_count,
    )
    row.caption_count = _max_observed(row.caption_count, update.caption_count)
    await session.flush()


async def record_meetbot_terminal(
    session: AsyncSession,
    update: MeetbotTerminalUpdate,
) -> None:
    """Apply one validated terminal callback to its brain-issued request."""

    if update.outcome not in {
        MeetbotSessionOutcome.LEFT,
        MeetbotSessionOutcome.REFUSED,
        MeetbotSessionOutcome.NOT_ADMITTED,
    }:
        raise ValueError(f"Meetbot terminal outcome is not terminal: {update.outcome}")

    row = await _requested_row(session, update.session_id)
    if row is None:
        return
    row.admitted_at = row.admitted_at or update.joined_at
    row.outcome = update.outcome.value
    row.refusal_text = (
        update.refusal_text
        if update.outcome is MeetbotSessionOutcome.REFUSED
        else None
    )
    row.left_at = update.ended_at
    row.participant_count = _max_observed(
        row.participant_count,
        update.participant_count,
    )
    row.caption_count = _max_observed(row.caption_count, update.caption_count)
    await session.flush()


async def _requested_row(
    session: AsyncSession,
    session_id: str,
) -> MeetbotSession | None:
    row = await session.get(MeetbotSession, session_id)
    if row is None:
        logger.warning(
            "Meetbot callback for %s has no brain-issued request row; skipping audit update",
            session_id,
        )
    return row


def _max_observed(current: int | None, observed: int) -> int:
    return observed if current is None else max(current, observed)


__all__ = [
    "MeetbotHealthUpdate",
    "MeetbotTerminalUpdate",
    "create_requested_meetbot_session",
    "record_meetbot_health",
    "record_meetbot_terminal",
]
