"""Write durable meetbot lifecycle state inside the brain database boundary."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from sqlalchemy.ext.asyncio import AsyncSession

from brain.kernel.common.coercion import coerce_datetime
from brain.platform.db.models.meetbot_session import MeetbotSession


async def create_requested_meetbot_session(
    session: AsyncSession,
    *,
    session_id: str,
    meeting_url: str,
    requesting_run_id: int | None,
    requested_at: datetime | None = None,
) -> MeetbotSession:
    """Add the durable request that must commit before meetbot starts a browser."""

    row = MeetbotSession(
        session_id=session_id,
        meeting_url=meeting_url,
        requesting_run_id=requesting_run_id,
        requested_at=requested_at or datetime.now(timezone.utc),
        outcome="requested",
    )
    session.add(row)
    await session.flush()
    return row


async def record_meetbot_health(
    session: AsyncSession,
    payload: Mapping[str, Any],
) -> MeetbotSession:
    """Apply one active-session callback without undoing a terminal outcome."""

    row = await _get_or_create_callback_row(session, payload)
    status = str(payload.get("status") or "").strip().lower()
    joined_at = coerce_datetime(payload.get("joined_at"))
    if row.outcome in {"requested", "admitted"} and status in {
        "admitted",
        "captions_flowing",
    }:
        row.outcome = "admitted"
        row.admitted_at = row.admitted_at or joined_at
    row.participant_count = _max_observed(
        row.participant_count,
        payload.get("participant_count"),
    )
    row.caption_count = _max_observed(
        row.caption_count,
        payload.get("caption_lines"),
    )
    await session.flush()
    return row


async def record_meetbot_terminal(
    session: AsyncSession,
    payload: Mapping[str, Any],
) -> MeetbotSession:
    """Apply a terminal callback and preserve the reason admission failed."""

    row = await _get_or_create_callback_row(session, payload)
    joined_at = coerce_datetime(payload.get("joined_at"))
    ended_at = coerce_datetime(payload.get("ended_at"))
    end_reason = str(payload.get("end_reason") or "").strip().lower()

    if joined_at is not None:
        row.admitted_at = row.admitted_at or joined_at
    if row.admitted_at is not None:
        row.outcome = "left"
        row.left_at = ended_at
    elif end_reason == "refused":
        row.outcome = "refused"
        row.refusal_text = str(payload.get("error") or "").strip() or None
        row.left_at = ended_at
    else:
        row.outcome = "not_admitted"
        row.left_at = ended_at

    participants = payload.get("participants")
    participant_count = len(participants) if isinstance(participants, list) else None
    row.participant_count = _max_observed(row.participant_count, participant_count)
    row.caption_count = _max_observed(row.caption_count, payload.get("caption_lines"))
    await session.flush()
    return row


async def _get_or_create_callback_row(
    session: AsyncSession,
    payload: Mapping[str, Any],
) -> MeetbotSession:
    session_id = str(payload.get("session_id") or "").strip()
    row = await session.get(MeetbotSession, session_id)
    if row is not None:
        return row
    row = MeetbotSession(
        session_id=session_id,
        meeting_url=str(payload.get("meeting_url") or "").strip(),
        requesting_run_id=None,
        requested_at=(
            coerce_datetime(payload.get("started_at")) or datetime.now(timezone.utc)
        ),
        outcome="requested",
    )
    session.add(row)
    await session.flush()
    return row


def _max_observed(current: int | None, value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return current
    try:
        observed = int(value)
    except (TypeError, ValueError):
        return current
    if observed < 0:
        return current
    return observed if current is None else max(current, observed)


__all__ = [
    "create_requested_meetbot_session",
    "record_meetbot_health",
    "record_meetbot_terminal",
]
