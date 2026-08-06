from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import func, select

from brain.platform.db.models.agent_run import AgentRunEventRow, AgentRunRow
from brain.platform.db.models.external_agent import ExternalAgentConnectionRow
from brain.platform.db.models.inbound import InboundDecisionReceiptRow, InboundEventRow
from brain.platform.db.models.open_ask import OpenAsk
from brain.platform.db.models.org import Org, User


ORG_ID = "11111111-1111-4111-8111-111111111111"
USER_ID = "22222222-2222-4222-8222-222222222222"
MEETBOT_CONNECTION_ID = "33333333-3333-4333-8333-333333333333"
SLACK_CONNECTION_ID = "44444444-4444-4444-8444-444444444444"


@pytest.fixture
async def session(async_sqlite_session_factory, sqlite_postgres_ddl_patch):
    return await async_sqlite_session_factory(
        [
            Org.__table__,
            User.__table__,
            ExternalAgentConnectionRow.__table__,
            AgentRunRow.__table__,
            AgentRunEventRow.__table__,
            OpenAsk.__table__,
            InboundEventRow.__table__,
            InboundDecisionReceiptRow.__table__,
        ]
    )


async def _seed(session, *, include_slack: bool = True):
    session.add(Org(id=ORG_ID, name="Test Org", slug="test-org"))
    session.add(User(id=USER_ID, org_id=ORG_ID, name="Reda", email="reda@example.com"))
    meetbot = ExternalAgentConnectionRow(
        id=MEETBOT_CONNECTION_ID,
        org_id=ORG_ID,
        owner_user_id=USER_ID,
        display_name="Meetbot",
        agent_kind="meetbot",
        transport="webhook",
        status="online",
        remote_agent_card={},
        capabilities={"meeting_transcript": True},
        auth_metadata={},
        metadata_={},
    )
    session.add(meetbot)
    if include_slack:
        session.add(
            ExternalAgentConnectionRow(
                id=SLACK_CONNECTION_ID,
                org_id=ORG_ID,
                owner_user_id=USER_ID,
                display_name="Slack",
                agent_kind="slack",
                transport="slack_socket_mode",
                status="online",
                remote_agent_id="T-team",
                remote_agent_card={},
                capabilities={},
                auth_metadata={},
                metadata_={"slack": {"team_id": "T-team", "bot_user_id": "B-illo"}},
            )
        )
    await session.flush()
    return meetbot


def _ended_payload(upload_root: Path, *, session_id: str = "session-ended") -> dict:
    session_dir = upload_root / session_id
    session_dir.mkdir(parents=True)
    (session_dir / "transcript.jsonl").write_text(
        '{"ts":"00:01","speaker":"Reda","text":"Ship it"}\n',
        encoding="utf-8",
    )
    (session_dir / "transcript.md").write_text(
        "**Reda**: Ship it\n\n**Axel**: I own the follow-up.",
        encoding="utf-8",
    )
    return {
        "session_id": session_id,
        "meeting_url": "https://meet.google.com/abc-defg-hij",
        "status": "ended",
        "transcript_path": str(session_dir / "transcript.jsonl"),
        "transcript_md_path": str(session_dir / "transcript.md"),
        "started_at": "2026-08-03T15:00:00Z",
        "ended_at": "2026-08-03T15:30:00Z",
        "caption_lines": 2,
        "participants": ["Reda", "Axel"],
        "origin": {"channel": "C-meetings", "thread_ts": "1722700000.001"},
        "requested_by": "U-reda",
    }


def _health_payload(
    *,
    session_id: str = "session-active",
    warning: str | None = None,
) -> dict:
    payload = {
        "session_id": session_id,
        "meeting_url": "https://meet.google.com/abc-defg-hij",
        "status": "lobby",
        "started_at": "2026-08-05T13:52:49Z",
        "joined_at": None,
        "observed_at": "2026-08-05T13:55:49Z",
        "caption_lines": 0,
        "participant_count": 0,
        "origin": {"channel": "C-meetings", "thread_ts": "1722700000.001"},
        "requested_by": "U-reda",
    }
    if warning:
        payload["warning"] = warning
    return payload


@pytest.mark.asyncio
async def test_meeting_transcript_admits_same_thread_run_and_is_idempotent(
    session,
    tmp_path,
    monkeypatch,
):
    from brain.systems.inbound.service import submit_inbound_envelope
    from brain.systems.meetings import inbound as meeting_inbound

    meetbot = await _seed(session)
    upload_root = tmp_path / "meetings"
    monkeypatch.setattr(meeting_inbound, "MEETING_UPLOAD_ROOT", upload_root)
    payload = _ended_payload(upload_root)
    envelope = {
        "kind": "meeting_transcript",
        "origin": "meetbot.session_complete",
        "payload": payload,
        "summary": "Meeting transcript ready",
        "idempotency_key": "meeting-session-ended",
    }

    first = await submit_inbound_envelope(session, connection=meetbot, envelope=envelope)
    second = await submit_inbound_envelope(session, connection=meetbot, envelope=envelope)

    run = (await session.scalars(select(AgentRunRow))).one()
    event = (await session.scalars(select(InboundEventRow))).one()
    receipt = (await session.scalars(select(InboundDecisionReceiptRow))).one()
    assert first["status"] == "processed"
    assert first["ilo_outcome"]["operation"] == "meeting_run_admitted"
    assert first["ilo_outcome"]["routing"] == "slack_origin"
    assert second["idempotent_replay"] is True
    assert await session.scalar(select(func.count()).select_from(AgentRunRow)) == 1
    assert await session.scalar(select(func.count()).select_from(OpenAsk)) == 0
    assert run.thread_id == "slack:T-team:C-meetings:1722700000.001"
    assert run.target_ref["slack_trigger"]["response_target"] == {
        "channel_id": "C-meetings",
        "thread_ts": "1722700000.001",
        "visibility": "public",
    }
    assert run.metadata_["meeting"]["session_id"] == "session-ended"
    assert "**Reda**: Ship it" in run.input_message
    assert "1. Post a concise meeting summary" in run.input_message
    assert event.action_type == "meeting.run_admitted"
    assert receipt.tool_use["type"] == "meeting_transcript_intake"
    assert receipt.target["thread_ts"] == "1722700000.001"


@pytest.mark.asyncio
async def test_unthreaded_dm_meeting_admits_without_open_ask(
    session,
    tmp_path,
    monkeypatch,
):
    from brain.systems.inbound.service import submit_inbound_envelope
    from brain.systems.meetings import inbound as meeting_inbound

    meetbot = await _seed(session)
    upload_root = tmp_path / "meetings"
    monkeypatch.setattr(meeting_inbound, "MEETING_UPLOAD_ROOT", upload_root)
    session_id = "1eebef49-253a-4c59-830a-0b18f33f417d"
    payload = _ended_payload(upload_root, session_id=session_id)
    payload["origin"] = {"channel": "D-meeting-dm", "thread_ts": ""}
    payload["requested_by"] = "U04R1A6MZST"

    result = await submit_inbound_envelope(
        session,
        connection=meetbot,
        envelope={
            "kind": "meeting_transcript",
            "origin": "meetbot.session_complete",
            "payload": payload,
            "idempotency_key": f"meeting-{session_id}",
        },
    )

    run = (await session.scalars(select(AgentRunRow))).one()
    assert result["status"] == "processed"
    assert result["ilo_outcome"]["operation"] == "meeting_run_admitted"
    assert await session.scalar(select(func.count()).select_from(OpenAsk)) == 0
    assert run.metadata_["slack_trigger"]["message_ts"] is None
    assert run.metadata_["slack_trigger"]["response_target"] == {
        "channel_id": "D-meeting-dm",
        "thread_ts": None,
        "visibility": "public",
    }


@pytest.mark.asyncio
async def test_failed_meeting_admits_short_failure_report_run(session):
    from brain.systems.inbound.service import submit_inbound_envelope

    meetbot = await _seed(session)
    payload = {
        "session_id": "session-failed",
        "meeting_url": "https://meet.google.com/abc-defg-hij",
        "status": "failed",
        "transcript_path": None,
        "transcript_md_path": None,
        "started_at": "2026-08-03T15:00:00Z",
        "ended_at": "2026-08-03T15:01:00Z",
        "caption_lines": 0,
        "participants": [],
        "origin": {"channel": "C-meetings", "thread_ts": "1722700000.002"},
        "error": "Host denied admission",
    }

    result = await submit_inbound_envelope(
        session,
        connection=meetbot,
        envelope={
            "kind": "meeting_transcript",
            "origin": "meetbot.session_complete",
            "payload": payload,
            "idempotency_key": "meeting-session-failed",
        },
    )

    run = (await session.scalars(select(AgentRunRow))).one()
    assert result["status"] == "processed"
    assert result["ilo_outcome"]["meeting_status"] == "failed"
    assert "Host denied admission" in run.input_message
    assert "Post a short failure report" in run.input_message
    assert "Required sequence" not in run.input_message
    assert "create_github_issue" not in run.input_message


@pytest.mark.asyncio
async def test_zero_caption_terminal_reports_degraded_capture(session, tmp_path, monkeypatch):
    from brain.systems.inbound.service import submit_inbound_envelope
    from brain.systems.meetings import inbound as meeting_inbound

    meetbot = await _seed(session)
    upload_root = tmp_path / "meetings"
    monkeypatch.setattr(meeting_inbound, "MEETING_UPLOAD_ROOT", upload_root)
    payload = _ended_payload(upload_root, session_id="session-empty")
    payload["caption_lines"] = 0
    payload["participants"] = []
    Path(payload["transcript_path"]).write_text("", encoding="utf-8")
    Path(payload["transcript_md_path"]).write_text("", encoding="utf-8")

    result = await submit_inbound_envelope(
        session,
        connection=meetbot,
        envelope={
            "kind": "meeting_transcript",
            "origin": "meetbot.session_complete",
            "payload": payload,
            "idempotency_key": "meeting-session-empty",
        },
    )

    run = (await session.scalars(select(AgentRunRow))).one()
    assert result["ilo_outcome"]["meeting_status"] == "degraded"
    assert run.metadata_["evidence_health"]["status"] == "degraded"
    assert run.metadata_["meeting_capture"]["status"] == "degraded"
    assert "capture is degraded" in run.input_message
    assert "Post a capture-failure report" in run.input_message
    assert "Ask clarifying questions" not in run.input_message


@pytest.mark.asyncio
async def test_healthy_session_health_is_persisted_without_admitting_a_run(session):
    from brain.systems.inbound.service import submit_inbound_envelope

    meetbot = await _seed(session)
    result = await submit_inbound_envelope(
        session,
        connection=meetbot,
        envelope={
            "kind": "meeting_session_health",
            "origin": "meetbot",
            "payload": _health_payload(),
            "idempotency_key": "meeting-health-session-active-1",
        },
    )

    event = (await session.scalars(select(InboundEventRow))).one()
    receipt = (await session.scalars(select(InboundDecisionReceiptRow))).one()
    assert result["status"] == "processed"
    assert result["ilo_outcome"]["operation"] == "meeting_health_observed"
    assert "run_id" not in result["ilo_outcome"]
    assert event.kind == "meeting_session_health"
    assert event.raw_payload["participant_count"] == 0
    assert receipt.tool_use["type"] == "meeting_session_health_intake"
    assert await session.scalar(select(func.count()).select_from(AgentRunRow)) == 0


@pytest.mark.asyncio
async def test_session_health_rejects_invalid_payload_with_meeting_discipline(session):
    from brain.systems.inbound.service import submit_inbound_envelope

    meetbot = await _seed(session)
    payload = _health_payload()
    payload.pop("observed_at")

    result = await submit_inbound_envelope(
        session,
        connection=meetbot,
        envelope={
            "kind": "meeting_session_health",
            "origin": "meetbot",
            "payload": payload,
            "idempotency_key": "meeting-health-invalid",
        },
    )

    assert result["status"] == "failed"
    assert result["ilo_outcome"] == {
        "operation": "meeting_health_payload_rejected",
        "reason": "invalid_meeting_session_health_payload",
        "event_id": result["event_id"],
    }


@pytest.mark.asyncio
async def test_stale_health_warning_admits_run_to_inviting_slack_thread(session):
    from brain.systems.inbound.service import submit_inbound_envelope

    meetbot = await _seed(session)
    warning = (
        "Session stale: wrong meeting, bot was never admitted, or captions off."
    )
    result = await submit_inbound_envelope(
        session,
        connection=meetbot,
        envelope={
            "kind": "meeting_session_health",
            "origin": "meetbot",
            "payload": _health_payload(warning=warning),
            "idempotency_key": "meeting-health-session-active-3",
        },
    )

    run = (await session.scalars(select(AgentRunRow))).one()
    assert result["ilo_outcome"]["routing"] == "slack_origin"
    assert run.thread_id == "slack:T-team:C-meetings:1722700000.001"
    assert run.target_ref["slack_trigger"]["response_target"] == {
        "channel_id": "C-meetings",
        "thread_ts": "1722700000.001",
        "visibility": "public",
    }
    assert "https://meet.google.com/abc-defg-hij" in run.input_message
    assert "Participants observed: 0" in run.input_message
    assert "Caption lines: 0" in run.input_message
    assert "wrong meeting" in run.input_message
    assert "never admitted" in run.input_message
    assert "captions are off" in run.input_message


@pytest.mark.asyncio
async def test_replay_shape_persists_health_rows_between_join_and_terminal(
    session,
    tmp_path,
    monkeypatch,
):
    from brain.systems.inbound.service import submit_inbound_envelope
    from brain.systems.meetings import inbound as meeting_inbound

    meetbot = await _seed(session)
    for sequence, observed_at in enumerate(
        ("2026-08-05T13:53:49Z", "2026-08-05T13:54:49Z"),
        start=1,
    ):
        payload = _health_payload()
        payload["observed_at"] = observed_at
        await submit_inbound_envelope(
            session,
            connection=meetbot,
            envelope={
                "kind": "meeting_session_health",
                "origin": "meetbot",
                "payload": payload,
                "idempotency_key": f"meeting-health-session-active-{sequence}",
            },
        )

    upload_root = tmp_path / "meetings"
    monkeypatch.setattr(meeting_inbound, "MEETING_UPLOAD_ROOT", upload_root)
    terminal = _ended_payload(upload_root, session_id="session-active")
    terminal.update(
        {
            "started_at": "2026-08-05T13:52:49Z",
            "ended_at": "2026-08-05T15:53:25Z",
            "caption_lines": 0,
            "participants": [],
        }
    )
    Path(terminal["transcript_path"]).write_text("", encoding="utf-8")
    Path(terminal["transcript_md_path"]).write_text("", encoding="utf-8")
    await submit_inbound_envelope(
        session,
        connection=meetbot,
        envelope={
            "kind": "meeting_transcript",
            "origin": "meetbot.session_complete",
            "payload": terminal,
            "idempotency_key": "meeting-session-active",
        },
    )

    rows = list((await session.scalars(select(InboundEventRow))).all())
    health_rows = [row for row in rows if row.kind == "meeting_session_health"]
    terminal_rows = [row for row in rows if row.kind == "meeting_transcript"]
    assert sorted(row.raw_payload["observed_at"] for row in health_rows) == [
        "2026-08-05T13:53:49Z",
        "2026-08-05T13:54:49Z",
    ]
    assert len(terminal_rows) == 1
    assert terminal_rows[0].raw_payload["ended_at"] == "2026-08-05T15:53:25Z"


@pytest.mark.asyncio
async def test_missing_slack_connection_routes_meeting_to_run_inbox(
    session,
    tmp_path,
    monkeypatch,
):
    from brain.systems.inbound.service import submit_inbound_envelope
    from brain.systems.meetings import inbound as meeting_inbound

    meetbot = await _seed(session, include_slack=False)
    upload_root = tmp_path / "meetings"
    monkeypatch.setattr(meeting_inbound, "MEETING_UPLOAD_ROOT", upload_root)
    payload = _ended_payload(upload_root, session_id="session-inbox")

    result = await submit_inbound_envelope(
        session,
        connection=meetbot,
        envelope={
            "kind": "meeting_transcript",
            "origin": "meetbot.session_complete",
            "payload": payload,
            "idempotency_key": "meeting-session-inbox",
        },
    )

    run = (await session.scalars(select(AgentRunRow))).one()
    assert result["ilo_outcome"]["routing"] == "run_inbox"
    assert run.thread_id == "meeting:session-inbox"
    assert "slack_trigger" not in run.metadata_


@pytest.mark.asyncio
async def test_missing_origin_uses_existing_slack_alerts_fallback(
    session,
    tmp_path,
    monkeypatch,
):
    from brain.systems.inbound.service import submit_inbound_envelope
    from brain.systems.meetings import inbound as meeting_inbound

    meetbot = await _seed(session)
    upload_root = tmp_path / "meetings"
    monkeypatch.setattr(meeting_inbound, "MEETING_UPLOAD_ROOT", upload_root)
    payload = _ended_payload(upload_root, session_id="session-alerts")
    payload["origin"] = {}

    result = await submit_inbound_envelope(
        session,
        connection=meetbot,
        envelope={
            "kind": "meeting_transcript",
            "origin": "meetbot.session_complete",
            "payload": payload,
            "idempotency_key": "meeting-session-alerts",
        },
    )

    run = (await session.scalars(select(AgentRunRow))).one()
    assert result["ilo_outcome"]["routing"] == "slack_alerts"
    assert run.metadata_["slack_trigger"]["response_target"] == {
        "channel_id": "#alerts",
        "thread_ts": None,
        "visibility": "public",
    }
