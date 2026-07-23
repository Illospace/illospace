from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.dialects.sqlite.base import SQLiteDDLCompiler, SQLiteTypeCompiler

from brain.platform.db.models.agent_run import AgentRunEventRow, AgentRunRow
from brain.platform.db.models.open_ask import OpenAsk
from brain.platform.db.models.org import Org, User


ORG_ID = "11111111-1111-4111-8111-111111111111"
USER_ID = "22222222-2222-4222-8222-222222222222"
ORIGIN_REF = "slack:T789:CALERTS:1784741786.046759"
ASK_TEXT = (
    "@Illo we may have a bug, email from a customer, assign ticket to me. "
    "The customer's generations reach 99%, disappear, and still consume credits."
)
THREAD_URL = "https://uwear.slack.com/archives/CALERTS/p1784741786046759"


def _patch_sqlite_for_models() -> None:
    if not hasattr(SQLiteTypeCompiler, "visit_JSONB"):
        SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "TEXT"
    SQLiteTypeCompiler.visit_UUID = lambda self, type_, **kw: "TEXT"
    SQLiteTypeCompiler.visit_BIGINT = lambda self, type_, **kw: "INTEGER"
    original = SQLiteDDLCompiler.get_column_default_string
    if getattr(original, "_open_ask_patch", False):
        return

    def patched(self, column, **kw):
        result = original(self, column, **kw)
        if result:
            result = result.replace("::jsonb", "")
            result = result.replace("NOW()", "CURRENT_TIMESTAMP")
            result = result.replace("TRUE", "1").replace("FALSE", "0")
        return result

    patched._open_ask_patch = True
    SQLiteDDLCompiler.get_column_default_string = patched


@pytest.fixture
async def session(async_sqlite_session_factory):
    _patch_sqlite_for_models()
    db = await async_sqlite_session_factory(
        [
            Org.__table__,
            User.__table__,
            AgentRunRow.__table__,
            AgentRunEventRow.__table__,
            OpenAsk.__table__,
        ],
    )
    db.add(Org(id=ORG_ID, name="Test Org", slug="test-org"))
    db.add(User(id=USER_ID, org_id=ORG_ID, name="Reda", email="reda@example.com"))
    await db.flush()
    return db


def _slack_admission_event():
    from brain.systems.runs.work_intake import WorkIntakeEvent
    from brain.systems.slack.triggers import build_slack_work_intake_payload

    payload = build_slack_work_intake_payload(
        org_id=ORG_ID,
        authority_user_id=USER_ID,
        idempotency_key=ORIGIN_REF,
        payload={
            "origin": "slack.app_mention",
            "team_id": "T789",
            "channel_id": "CALERTS",
            "channel_type": "channel",
            "message_ts": "1784741786.046759",
            "thread_ts": "1784741786.046759",
            "slack_user_id": "UREDA",
            "bot_user_id": "BILLO",
            "text": ASK_TEXT,
            "permalink": THREAD_URL,
        },
    )
    return WorkIntakeEvent.from_trigger_payload(payload)


async def _admit_originating_ask(session) -> tuple[int, OpenAsk]:
    from brain.systems.runs.work_intake import admit_work

    result = await admit_work(session, _slack_admission_event())
    assert result.ok is True
    row = (await session.scalars(select(OpenAsk))).one()
    return int(result.run_id), row


@pytest.mark.asyncio
async def test_2026_07_22_replay_failing_run_then_other_run_files_issue(
    session,
    monkeypatch,
):
    """Keystone replay: later-lane artifact answers the original human thread."""

    from brain.systems.runs.domain import AgentRunRequest
    from brain.systems.runs.failures import safe_terminal_run_message
    from brain.systems.runs.slack_delivery import (
        OpenAskArtifact,
        post_open_ask_artifact_reply,
    )
    from brain.systems.runs.store import AsyncAgentRunStore

    origin_run_id, ask = await _admit_originating_ask(session)
    assert ask.origin_ref == ORIGIN_REF
    assert ask.ask_text == ASK_TEXT
    assert ask.origin_run_id == origin_run_id
    assert ask.status == "open"

    origin_run = await session.get(AgentRunRow, origin_run_id)
    origin_run.status = "failed"
    origin_run.failed_at = datetime.now(timezone.utc)
    await session.flush()

    failure_message = safe_terminal_run_message("failed")
    assert failure_message == "I failed on this and it is still open — I will come back."
    assert "please retry" not in failure_message.lower()
    assert ask.status == "open"
    assert ask.answered_at is None

    later_run = await AsyncAgentRunStore(session).create_run(
        AgentRunRequest(
            org_id=ORG_ID,
            user_id=USER_ID,
            thread_id="different-lane",
            message="File the diagnosed customer issue",
        )
    )
    fake_client = SimpleNamespace(
        post_message=AsyncMock(
            return_value={
                "ok": True,
                "ts": "1784743141.000100",
                "message": {"text": "stored"},
            }
        )
    )
    monkeypatch.setattr(
        "brain.systems.slack.thread_mute.read_thread_post_mute",
        AsyncMock(return_value=None),
    )

    delivery = await post_open_ask_artifact_reply(
        session,
        origin_ref=ORIGIN_REF,
        artifact=OpenAskArtifact(
            kind="GitHub issue",
            reference="uwear-ai/uwear-backend#1221",
            title="Terminalize rejected generations and refund exactly once",
            url="https://github.com/uwear-ai/uwear-backend/issues/1221",
        ),
        answering_run_id=later_run.id,
        client=fake_client,
    )
    await session.flush()

    assert delivery["matched"] == 1
    assert delivery["delivered"] == 1
    fake_client.post_message.assert_awaited_once()
    posted = fake_client.post_message.await_args.kwargs
    assert posted["channel"] == "CALERTS"
    assert posted["thread_ts"] == "1784741786.046759"
    assert ASK_TEXT in posted["text"]
    assert "Reda (<@UREDA>)" in posted["text"]
    assert "*Mechanism:* GitHub issue uwear-ai/uwear-backend#1221" in posted["text"]
    assert "https://github.com/uwear-ai/uwear-backend/issues/1221" in posted["text"]

    await session.refresh(ask)
    assert ask.status == "answered"
    assert ask.answered_by_run_id == later_run.id
    assert ask.answer_artifact_kind == "GitHub issue"
    assert ask.answer_artifact_ref == (
        "https://github.com/uwear-ai/uwear-backend/issues/1221"
    )
    assert ask.delivered_message_ts == "1784743141.000100"


@pytest.mark.asyncio
async def test_failure_or_failed_thread_delivery_never_closes_open_ask(
    session,
    monkeypatch,
):
    from brain.systems.runs.slack_delivery import (
        OpenAskArtifact,
        post_open_ask_artifact_reply,
    )

    origin_run_id, ask = await _admit_originating_ask(session)
    origin_run = await session.get(AgentRunRow, origin_run_id)
    origin_run.status = "failed"
    await session.flush()
    assert ask.status == "open"

    monkeypatch.setattr(
        "brain.systems.slack.thread_mute.read_thread_post_mute",
        AsyncMock(return_value=None),
    )
    failed_client = SimpleNamespace(
        post_message=AsyncMock(side_effect=RuntimeError("Slack unavailable"))
    )
    delivery = await post_open_ask_artifact_reply(
        session,
        origin_ref=ORIGIN_REF,
        artifact=OpenAskArtifact(
            kind="GitHub issue",
            reference="uwear-ai/uwear-backend#1221",
            url="https://github.com/uwear-ai/uwear-backend/issues/1221",
        ),
        answering_run_id=origin_run_id,
        client=failed_client,
    )
    await session.flush()

    assert delivery["delivered"] == 0
    await session.refresh(ask)
    assert ask.status == "open"
    assert ask.answered_at is None
    assert ask.answer_artifact_ref is None


@pytest.mark.asyncio
async def test_admission_preserves_verbatim_ask_and_answer_requires_delivery_timestamp(
    session,
):
    from brain.systems.runs.open_asks import mark_open_ask_answered
    from brain.systems.runs.work_intake import admit_work

    event = _slack_admission_event()
    verbatim_ask = f"  {ASK_TEXT}\n"
    event.payload["metadata"]["slack_trigger"]["text"] = verbatim_ask

    result = await admit_work(session, event)
    assert result.ok is True
    ask = (await session.scalars(select(OpenAsk))).one()
    assert ask.ask_text == verbatim_ask

    with pytest.raises(
        ValueError,
        match="confirmed Slack delivery timestamp",
    ):
        mark_open_ask_answered(
            ask,
            answer_text="Created the issue.",
            answered_by_run_id=result.run_id,
            slack_response={"ok": True},
        )
    assert ask.status == "open"
    assert ask.answered_at is None


@pytest.mark.asyncio
async def test_overdue_open_ask_is_mandatory_in_next_scheduled_person_recap(
    session,
):
    from brain.systems.cycles.prompts import cycle_run_message
    from brain.systems.cycles.service import _async_attach_open_ask_stragglers

    _, ask = await _admit_originating_ask(session)
    scheduled_for = datetime(2026, 7, 23, 17, 0, tzinfo=timezone.utc)
    ask.opened_at = scheduled_for - timedelta(hours=2, minutes=7)
    await session.flush()

    cycle = SimpleNamespace(
        id=2,
        org_id=ORG_ID,
        user_id=USER_ID,
        name="Uwear Ticket Coordinator Check-ins",
        prompt="Publish the engineering digest with a Per-person recap.",
        timezone="America/Toronto",
    )
    cycle_run = SimpleNamespace(
        id=88,
        revision_id=None,
        scheduled_for=scheduled_for,
        guidance_snapshot=[],
        output_targets_snapshot=[],
        context_snapshot={
            "launch_context": {
                "origin": "scheduled_cycle",
                "source": "cycle_scheduler",
                "run_kind": "scheduled_digest",
            }
        },
    )

    stragglers = await _async_attach_open_ask_stragglers(
        session,
        cycle,
        cycle_run,
    )
    message = cycle_run_message(
        SimpleNamespace(id="digest-thread", title="Engineering digest"),
        cycle,
        cycle_run,
    )

    assert stragglers == cycle_run.context_snapshot["open_ask_stragglers"]
    assert stragglers[0]["age"] == "2h 7m"
    assert "MANDATORY OPEN-ASK LEDGER" in message
    assert "Under each requester's Per-person recap" in message
    assert "Reda — unanswered for 2h 7m" in message
    assert ASK_TEXT in message
    assert THREAD_URL in message
