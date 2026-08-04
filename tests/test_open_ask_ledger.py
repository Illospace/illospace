from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite.base import SQLiteDDLCompiler, SQLiteTypeCompiler

from brain.platform.db.models.agent_run import AgentRunEventRow, AgentRunRow
from brain.platform.db.models.open_ask import ObligationNotice, OpenAsk
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
            ObligationNotice.__table__,
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


async def _create_slack_run(session):
    from brain.systems.runs.domain import AgentRunRequest
    from brain.systems.runs.store import AsyncAgentRunStore

    created = await AsyncAgentRunStore(session).create_run(
        AgentRunRequest(
            org_id=ORG_ID,
            user_id=USER_ID,
            thread_id=ORIGIN_REF,
            message=ASK_TEXT,
            target_ref={
                "kind": "slack_message",
                "slack_trigger": {
                    "team_id": "T789",
                    "channel_id": "CALERTS",
                    "channel_type": "channel",
                    "message_ts": "1784741786.046759",
                    "thread_ts": "1784741786.046759",
                    "slack_user_id": "UREDA",
                    "bot_user_id": "BILLO",
                    "text": ASK_TEXT,
                    "permalink": THREAD_URL,
                    "response_target": {
                        "channel_id": "CALERTS",
                        "thread_ts": "1784741786.046759",
                        "visibility": "public",
                    },
                },
            },
            metadata={"final_answer_target_surface": "slack"},
        )
    )
    return await session.get(AgentRunRow, created.id)


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
    assert ask.obligation_kind == "human_ask"
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
async def test_admission_rejects_fabricated_open_ask_timestamp(
    session,
    caplog,
):
    from brain.systems.runs.work_intake import admit_work

    event = _slack_admission_event()
    fabricated_ts = "meeting-1eebef49-253a-4c59-830a-0b18f33f417d"
    event.payload["metadata"]["slack_trigger"]["message_ts"] = fabricated_ts
    event.payload["metadata"]["slack_trigger"]["thread_ts"] = fabricated_ts

    with caplog.at_level("WARNING", logger="brain.systems.runs.open_asks"):
        result = await admit_work(session, event)

    assert result.ok is True
    assert await session.scalar(select(func.count()).select_from(OpenAsk)) == 0
    assert "Ignoring invalid Slack thread_ts for open-ask anchoring" in caplog.text
    assert "Ignoring invalid Slack message_ts for open-ask anchoring" in caplog.text


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
    assert "Under each obligation owner's recap" in message
    assert "Reda — unanswered for 2h 7m" in message
    assert ASK_TEXT in message
    assert THREAD_URL in message


@pytest.mark.asyncio
async def test_run_deferral_is_durable_deduplicated_and_closed_only_by_delivery(
    session,
    monkeypatch,
):
    from brain.systems.runs.failures import terminal_run_notice_condition
    from brain.systems.runs.open_asks import list_open_ask_stragglers
    from brain.systems.runs.obligation_notices import (
        deliver_pending_obligation_notices,
    )
    from brain.systems.runs.slack_delivery import post_slack_run_message

    run = await _create_slack_run(session)
    calls = []

    class FakeSlackClient:
        async def post_message(self, **kwargs):
            calls.append(kwargs)
            return {
                "ok": True,
                "channel": kwargs["channel"],
                "ts": f"1784743141.000{len(calls)}",
            }

        async def set_assistant_status(self, **_kwargs):
            return {"ok": True}

    monkeypatch.setattr(
        "brain.systems.runs.slack_delivery.slack_client_for_run",
        AsyncMock(return_value=FakeSlackClient()),
    )
    monkeypatch.setattr(
        "brain.systems.runs.obligation_notices.schedule_post_commit_notice_delivery",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        "brain.systems.slack.thread_mute.read_thread_post_mute",
        AsyncMock(return_value=None),
    )

    for _ in range(3):
        result = await post_slack_run_message(
            session,
            run=run,
            text=(
                f"I was interrupted by a system restart at 17:55 UTC (run {run.id}); "
                "I've re-queued it and will reply here when it finishes."
            ),
            deferral_condition="interruption:requeued",
        )

    rows = list((await session.scalars(select(OpenAsk))).all())
    notices = list((await session.scalars(select(ObligationNotice))).all())
    assert len(rows) == 1
    assert len(notices) == 1
    obligation = rows[0]
    interruption_notice = notices[0]
    assert obligation.obligation_kind == "run_deferral"
    assert obligation.origin_run_id == run.id
    assert obligation.requester_slack_id is None
    assert obligation.owner_label == f"Illo run {run.id}"
    assert obligation.channel_id == "CALERTS"
    assert obligation.thread_ts == "1784741786.046759"
    assert obligation.status == "open"
    assert result["suppressed"] is True
    assert result["reason"] == "duplicate_run_deferral"
    assert interruption_notice.condition == "interruption:requeued"
    assert interruption_notice.state == "pending"
    assert calls == []

    class _SessionLease:
        async def __aenter__(self):
            return session

        async def __aexit__(self, *_exc):
            return False

    async def poster(*, channel, text, thread_ts, idempotency_key):
        assert idempotency_key.startswith(
            f"obligation-notice:{obligation.id}:"
        )
        return await FakeSlackClient().post_message(
            channel=channel,
            text=text,
            thread_ts=thread_ts,
        )

    await session.commit()
    summary = await deliver_pending_obligation_notices(
        org_id=ORG_ID,
        session_factory=lambda: _SessionLease(),
        poster=poster,
    )
    await session.refresh(interruption_notice)
    assert summary["delivered"] == 1
    assert interruption_notice.state == "delivered"
    assert interruption_notice.delivered_message_ts == "1784743141.0001"
    assert len(calls) == 1

    run.status = "failed"
    failure_condition = terminal_run_notice_condition("failed", "internal")
    await post_slack_run_message(
        session,
        run=run,
        text="I failed on this and it is still open — I will come back.",
        deferral_condition=failure_condition,
    )
    await post_slack_run_message(
        session,
        run=run,
        text="I failed on this and it is still open — I will come back.",
        deferral_condition=failure_condition,
    )

    await session.refresh(obligation)
    notices = list(
        (
            await session.scalars(
                select(ObligationNotice).order_by(ObligationNotice.id)
            )
        ).all()
    )
    assert obligation.status == "open"
    assert [notice.condition for notice in notices] == [
        "interruption:requeued",
        "terminal:failed:internal",
    ]
    assert [notice.state for notice in notices] == ["delivered", "pending"]
    assert len(calls) == 1
    await session.commit()
    summary = await deliver_pending_obligation_notices(
        org_id=ORG_ID,
        session_factory=lambda: _SessionLease(),
        poster=poster,
    )
    assert summary["delivered"] == 1
    assert len(calls) == 2
    observed_at = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)
    obligation.opened_at = observed_at - timedelta(hours=2, minutes=7)
    stragglers = await list_open_ask_stragglers(
        session,
        org_id=ORG_ID,
        now=observed_at,
    )
    assert stragglers[0]["obligation_kind"] == "run_deferral"
    assert stragglers[0]["age"] == "2h 7m"

    run.status = "completed"
    await post_slack_run_message(
        session,
        run=run,
        text="Done. The customer ticket is now answered.",
    )

    await session.refresh(obligation)
    assert obligation.status == "answered"
    assert obligation.answer_text == "Done. The customer ticket is now answered."
    assert obligation.answered_by_run_id == run.id
    assert obligation.delivered_message_ts == "1784743141.0003"
    assert len(calls) == 3

    # Answered is terminal: a later unseen condition cannot resurrect the row
    # or create a contradictory "I will come back" notice.
    await post_slack_run_message(
        session,
        run=run,
        text="A late failure retry must stay silent.",
        deferral_condition="terminal:failed:verification",
    )
    await session.refresh(obligation)
    assert obligation.status == "answered"
    assert len(list((await session.scalars(select(ObligationNotice))).all())) == 2


@pytest.mark.asyncio
async def test_uncommitted_deferral_never_reaches_slack_when_outer_transaction_fails(
    session,
    monkeypatch,
):
    from brain.systems.runs.slack_delivery import post_slack_run_message

    run = await _create_slack_run(session)
    slack_post = AsyncMock(side_effect=AssertionError("Slack must be post-commit"))
    monkeypatch.setattr(
        "brain.systems.runs.slack_delivery.slack_client_for_run",
        AsyncMock(
            return_value=SimpleNamespace(
                post_message=slack_post,
            )
        ),
    )
    monkeypatch.setattr(
        "brain.systems.runs.obligation_notices.schedule_post_commit_notice_delivery",
        lambda *_args, **_kwargs: False,
    )

    result = await post_slack_run_message(
        session,
        run=run,
        text="I was interrupted; I will come back.",
        deferral_condition="interruption:requeued",
    )
    assert result["queued"] is True
    assert slack_post.await_count == 0

    # Model the enclosing unit-of-work commit failure.
    await session.rollback()
    assert list((await session.scalars(select(OpenAsk))).all()) == []
    assert list((await session.scalars(select(ObligationNotice))).all()) == []
    assert slack_post.await_count == 0


def test_delivered_slack_transition_commands_have_disjoint_fields():
    from brain.systems.runs.open_asks import (
        DeliveredSlackAnswer,
        DeliveredSlackRoute,
    )

    answer_fields = set(DeliveredSlackAnswer.__dataclass_fields__)
    route_fields = set(DeliveredSlackRoute.__dataclass_fields__)
    assert "is_answer" not in answer_fields | route_fields
    assert {"routed_to_name", "routed_to_slack_id"}.isdisjoint(answer_fields)
    assert {"answer_text", "artifact_kind", "artifact_ref"}.isdisjoint(route_fields)


@pytest.mark.asyncio
async def test_stale_notice_claim_is_disambiguated_before_any_resend(session):
    from brain.systems.runs.open_asks import record_run_deferral
    from brain.systems.runs.obligation_notices import (
        STALE_NOTICE_POSTING_GRACE,
        deliver_pending_obligation_notices,
    )

    run = await _create_slack_run(session)
    _obligation, notice, _created = await record_run_deferral(
        session,
        run=run,
        channel_id="CALERTS",
        thread_ts="1784741786.046759",
        trigger=run.target_ref["slack_trigger"],
        deferral_text="I was interrupted; I will come back.",
        notice_condition="interruption:requeued",
        post_thread_ts="1784741786.046759",
    )
    now = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)
    notice.state = "posting"
    notice.attempts = 1
    notice.claimed_at = now - STALE_NOTICE_POSTING_GRACE - timedelta(minutes=1)
    await session.commit()

    class _SessionLease:
        async def __aenter__(self):
            return session

        async def __aexit__(self, *_exc):
            return False

    poster = AsyncMock()
    reader = AsyncMock(
        return_value={
            "messages": [
                {
                    "user": "BILLO",
                    "text": notice.notice_text,
                    "ts": "1784743141.000100",
                    "metadata": {
                        "event_type": "illo_obligation_notice",
                        "event_payload": {
                            "idempotency_key": notice.idempotency_key,
                        },
                    },
                }
            ],
            "complete": True,
        }
    )
    summary = await deliver_pending_obligation_notices(
        org_id=ORG_ID,
        session_factory=lambda: _SessionLease(),
        poster=poster,
        destination_reader=reader,
        now=now,
    )

    await session.refresh(notice)
    assert summary["already_delivered"] == 1
    assert notice.state == "delivered"
    assert notice.delivered_message_ts == "1784743141.000100"
    reader.assert_awaited_once()
    poster.assert_not_awaited()


@pytest.mark.asyncio
async def test_answer_supersedes_a_stale_notice_proven_absent(session):
    from brain.systems.runs.open_asks import (
        DeliveredSlackAnswer,
        record_delivered_slack_answer,
        record_run_deferral,
    )
    from brain.systems.runs.obligation_notices import (
        STALE_NOTICE_POSTING_GRACE,
        deliver_pending_obligation_notices,
    )

    run = await _create_slack_run(session)
    _obligation, notice, _created = await record_run_deferral(
        session,
        run=run,
        channel_id="CALERTS",
        thread_ts="1784741786.046759",
        trigger=run.target_ref["slack_trigger"],
        deferral_text="I was interrupted; I will come back.",
        notice_condition="interruption:requeued",
        post_thread_ts="1784741786.046759",
    )
    now = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)
    notice.state = "posting"
    notice.attempts = 1
    notice.claimed_at = now - STALE_NOTICE_POSTING_GRACE - timedelta(minutes=1)
    await record_delivered_slack_answer(
        session,
        DeliveredSlackAnswer(
            org_id=ORG_ID,
            channel_id="CALERTS",
            thread_ts="1784741786.046759",
            answering_run_id=run.id,
            slack_message_ts="1784743141.000100",
            answer_text="Done.",
        ),
    )
    await session.commit()

    class _SessionLease:
        async def __aenter__(self):
            return session

        async def __aexit__(self, *_exc):
            return False

    poster = AsyncMock()
    summary = await deliver_pending_obligation_notices(
        org_id=ORG_ID,
        session_factory=lambda: _SessionLease(),
        poster=poster,
        destination_reader=AsyncMock(
            return_value={"messages": [], "complete": True}
        ),
        now=now,
    )

    await session.refresh(notice)
    assert summary["superseded"] == 1
    assert notice.state == "superseded"
    poster.assert_not_awaited()


@pytest.mark.asyncio
async def test_delivered_answer_returns_kind_counts_and_closes_all_matching_kinds(
    session,
):
    from brain.platform.db.models.open_ask import ObligationKind
    from brain.systems.runs.open_asks import (
        DeliveredSlackAnswer,
        record_delivered_slack_answer,
        record_run_deferral,
    )

    run_id, human_ask = await _admit_originating_ask(session)
    run = await session.get(AgentRunRow, run_id)
    deferral, notice, _created = await record_run_deferral(
        session,
        run=run,
        channel_id="CALERTS",
        thread_ts="1784741786.046759",
        trigger=run.target_ref["slack_trigger"],
        deferral_text="I will come back.",
        notice_condition="terminal:failed:internal",
        post_thread_ts="1784741786.046759",
    )

    counts = await record_delivered_slack_answer(
        session,
        DeliveredSlackAnswer(
            org_id=ORG_ID,
            channel_id="CALERTS",
            thread_ts="1784741786.046759",
            answering_run_id=run_id,
            slack_message_ts="1784743141.000100",
            answer_text="Issue #1221 is filed.",
        ),
    )

    assert counts.by_kind == {
        ObligationKind.HUMAN_ASK: 1,
        ObligationKind.RUN_DEFERRAL: 1,
    }
    assert counts.answered_open_asks == 1
    assert counts.total == 2
    assert human_ask.status == "answered"
    assert deferral.status == "answered"
    assert notice.state == "superseded"
