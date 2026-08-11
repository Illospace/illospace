from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select

from brain.platform.db.models.meetbot_session import MeetbotSession
from brain.systems.meetings.session_record import (
    MeetbotHealthUpdate,
    MeetbotTerminalUpdate,
    create_requested_meetbot_session,
    record_meetbot_health,
    record_meetbot_terminal,
)
from meetbot.models import MeetbotSessionOutcome


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
        MeetbotTerminalUpdate(
            session_id="joined-session",
            outcome=MeetbotSessionOutcome.LEFT,
            joined_at=datetime(2026, 8, 11, 14, 0, 10, tzinfo=timezone.utc),
            ended_at=datetime(2026, 8, 11, 15, 0, tzinfo=timezone.utc),
            participant_count=2,
            caption_count=12,
        ),
    )
    await record_meetbot_terminal(
        session,
        MeetbotTerminalUpdate(
            session_id="refused-session",
            outcome=MeetbotSessionOutcome.REFUSED,
            joined_at=None,
            ended_at=datetime(2026, 8, 11, 14, 0, 10, tzinfo=timezone.utc),
            participant_count=0,
            caption_count=0,
            refusal_text="Your request to join was denied",
        ),
    )
    await record_meetbot_terminal(
        session,
        MeetbotTerminalUpdate(
            session_id="timeout-session",
            outcome=MeetbotSessionOutcome.NOT_ADMITTED,
            joined_at=None,
            ended_at=datetime(2026, 8, 11, 14, 10, tzinfo=timezone.utc),
            participant_count=0,
            caption_count=0,
        ),
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
        MeetbotHealthUpdate(
            session_id="admitted-session",
            status="admitted",
            joined_at=datetime(2026, 8, 11, 14, 0, 10, tzinfo=timezone.utc),
            participant_count=2,
            caption_count=4,
        ),
    )

    row = await session.get(MeetbotSession, "admitted-session")
    assert row.outcome == "admitted"
    assert row.admitted_at is not None
    assert row.participant_count == 2
    assert row.caption_count == 4


async def test_callback_does_not_fabricate_a_missing_request_row(
    async_sqlite_session_factory,
):
    session = await async_sqlite_session_factory([MeetbotSession.__table__])

    result = await record_meetbot_terminal(
        session,
        MeetbotTerminalUpdate(
            session_id="missing-request",
            outcome=MeetbotSessionOutcome.NOT_ADMITTED,
            joined_at=None,
            ended_at=datetime(2026, 8, 11, 14, 10, tzinfo=timezone.utc),
            participant_count=0,
            caption_count=0,
        ),
    )

    assert result is None
    assert await session.get(MeetbotSession, "missing-request") is None
