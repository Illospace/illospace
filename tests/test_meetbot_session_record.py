from __future__ import annotations

from sqlalchemy import select

from brain.platform.db.models.meetbot_session import MeetbotSession
from brain.systems.meetings.session_record import (
    create_requested_meetbot_session,
    record_meetbot_health,
    record_meetbot_terminal,
)


async def test_terminal_meetbot_session_outcomes_remain_distinguishable(
    async_sqlite_session_factory,
):
    session = await async_sqlite_session_factory([MeetbotSession.__table__])
    meeting_url = "https://meet.google.com/abc-defg-hij"
    for session_id in ("joined-session", "refused-session", "timeout-session"):
        await create_requested_meetbot_session(
            session,
            session_id=session_id,
            meeting_url=meeting_url,
            requesting_run_id=None,
        )

    await record_meetbot_terminal(
        session,
        {
            "session_id": "joined-session",
            "meeting_url": meeting_url,
            "joined_at": "2026-08-11T14:00:10Z",
            "ended_at": "2026-08-11T15:00:00Z",
            "end_reason": "leave_requested",
            "participants": ["Reda", "Illo"],
            "caption_lines": 12,
        },
    )
    await record_meetbot_terminal(
        session,
        {
            "session_id": "refused-session",
            "meeting_url": meeting_url,
            "joined_at": None,
            "ended_at": "2026-08-11T14:00:10Z",
            "end_reason": "refused",
            "error": "Your request to join was denied",
            "participants": [],
            "caption_lines": 0,
        },
    )
    await record_meetbot_terminal(
        session,
        {
            "session_id": "timeout-session",
            "meeting_url": meeting_url,
            "joined_at": None,
            "ended_at": "2026-08-11T14:10:00Z",
            "end_reason": "not_admitted",
            "error": "Nobody admitted the bot within 10 minutes.",
            "participants": [],
            "caption_lines": 0,
        },
    )

    rows = {
        row.session_id: row
        for row in (await session.scalars(select(MeetbotSession))).all()
    }
    assert rows["joined-session"].outcome == "left"
    assert rows["joined-session"].admitted_at is not None
    assert rows["refused-session"].outcome == "refused"
    assert rows["refused-session"].refusal_text == "Your request to join was denied"
    assert rows["timeout-session"].outcome == "not_admitted"
    assert rows["timeout-session"].refusal_text is None


async def test_admission_status_promotes_requested_session(
    async_sqlite_session_factory,
):
    session = await async_sqlite_session_factory([MeetbotSession.__table__])
    meeting_url = "https://meet.google.com/abc-defg-hij"
    await create_requested_meetbot_session(
        session,
        session_id="admitted-session",
        meeting_url=meeting_url,
        requesting_run_id=None,
    )

    await record_meetbot_health(
        session,
        {
            "session_id": "admitted-session",
            "meeting_url": meeting_url,
            "status": "admitted",
            "joined_at": "2026-08-11T14:00:10Z",
            "participant_count": 2,
            "caption_lines": 4,
        },
    )

    row = await session.get(MeetbotSession, "admitted-session")
    assert row.outcome == "admitted"
    assert row.admitted_at is not None
    assert row.participant_count == 2
    assert row.caption_count == 4
