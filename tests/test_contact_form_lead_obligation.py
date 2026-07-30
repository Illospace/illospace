from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.dialects.sqlite.base import SQLiteDDLCompiler, SQLiteTypeCompiler

from brain.platform.db.models.agent_run import AgentRunEventRow, AgentRunRow
from brain.platform.db.models.open_ask import ObligationNotice, OpenAsk
from brain.platform.db.models.org import Org, User


ORG_ID = "11111111-1111-4111-8111-111111111111"
USER_ID = "22222222-2222-4222-8222-222222222222"
THREAD_TS = "1785149400.000200"


def _patch_sqlite_for_models() -> None:
    if not hasattr(SQLiteTypeCompiler, "visit_JSONB"):
        SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "TEXT"
    SQLiteTypeCompiler.visit_UUID = lambda self, type_, **kw: "TEXT"
    SQLiteTypeCompiler.visit_BIGINT = lambda self, type_, **kw: "INTEGER"
    original = SQLiteDDLCompiler.get_column_default_string
    if getattr(original, "_contact_lead_patch", False):
        return

    def patched(self, column, **kw):
        result = original(self, column, **kw)
        if result:
            result = result.replace("::jsonb", "")
            result = result.replace("NOW()", "CURRENT_TIMESTAMP")
            result = result.replace("TRUE", "1").replace("FALSE", "0")
        return result

    patched._contact_lead_patch = True
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


def _lead_admission_event():
    from brain.systems.runs.work_intake import WorkIntakeEvent
    from brain.systems.slack.triggers import build_slack_work_intake_payload

    trigger = build_slack_work_intake_payload(
        org_id=ORG_ID,
        authority_user_id=USER_ID,
        idempotency_key=f"slack:T789:CALERTS:{THREAD_TS}",
        payload={
            "origin": "contact_form_lead",
            "event_kind": "contact_form_lead",
            "team_id": "T789",
            "channel_id": "CALERTS",
            "channel_name": "alerts",
            "channel_type": "channel",
            "message_ts": THREAD_TS,
            "thread_ts": THREAD_TS,
            "slack_user_id": "",
            "bot_user_id": "BILLO",
            "text": "New Contact Form Submission",
            "permalink": (
                "https://uwear.slack.com/archives/CALERTS/"
                "p1785149400000200"
            ),
            "response_target": {
                "channel_id": "CALERTS",
                "thread_ts": THREAD_TS,
                "visibility": "public",
            },
            "obligation_requester": {
                "name": "Aline Athaydes",
                "slack_user_id": "B_CONTACT_FORM",
                "user_id": None,
            },
            "contact_form_lead": {
                "name": "Aline Athaydes",
                "email": "aline@madamedusk.com",
                "company_website": "www.madamedusk.com",
                "phone": None,
                "message": "Can a consistent model carry across multiple products?",
                "owner": {
                    "name": "Reda",
                    "slack_user_id": "UREDA",
                    "user_id": USER_ID,
                },
            },
        },
    )
    return WorkIntakeEvent.from_trigger_payload(trigger)


@pytest.mark.asyncio
async def test_contact_form_intake_run_cannot_settle_its_open_ask(
    monkeypatch,
):
    from brain.systems.runs.execution_context import bind_agent_context
    from brain.systems.runs.open_asks import DeliveredSlackReplyCounts
    from brain.systems.runs.tool_catalog.handlers.slack import (
        _handle_post_slack_reply,
    )

    class _SlackClient:
        async def post_message(self, **kwargs):
            return {
                "ok": True,
                "ts": "1785149500.000300",
                "channel": kwargs["channel"],
            }

    monkeypatch.setattr(
        "brain.systems.runs.tool_catalog.handlers.slack._slack_client_from_runtime",
        AsyncMock(return_value=_SlackClient()),
    )
    recorder = AsyncMock(return_value=DeliveredSlackReplyCounts.empty())
    monkeypatch.setattr(
        "brain.systems.runs.slack_delivery.persist_delivered_slack_answer",
        recorder,
    )
    context = {
        "run_id": 9,
        "org_id": ORG_ID,
        "execution_metadata": {
            "run_id": 9,
            "org_id": ORG_ID,
            "illo_trigger": {
                "event_type": "contact_form_lead",
            },
        },
        "slack_trigger": {
            "response_target": {
                "channel_id": "CALERTS",
                "thread_ts": THREAD_TS,
                "visibility": "public",
            }
        },
    }

    with bind_agent_context(context):
        result = json.loads(
            await _handle_post_slack_reply(
                body="Verified lead assessment.",
                answers_open_ask=True,
            )
        )

    assert result["answers_open_ask"] is False
    assert result["answered_open_asks"] == 0
    recorder.assert_awaited_once()
    delivered = recorder.await_args.args[0]
    assert delivered.is_answer is False


@pytest.mark.asyncio
async def test_unanswered_contact_form_lead_resurfaces_once_to_owner_after_24h(
    session,
):
    from brain.systems.runs.obligation_notices import (
        deliver_pending_obligation_notices,
    )
    from brain.systems.runs.obligation_specs import (
        obligation_spec_from_metadata,
    )
    from brain.systems.runs.work_intake import admit_work

    admitted = await admit_work(session, _lead_admission_event())
    assert admitted.ok is True
    run = await session.get(AgentRunRow, admitted.run_id)
    assert run.target_ref["headless"] is True
    assert run.target_ref["required_response_tool"] == "post_slack_reply"
    assert run.target_ref["final_answer_target_surface"] == "headless"
    obligation = (await session.scalars(select(OpenAsk))).one()
    notice = (await session.scalars(select(ObligationNotice))).one()
    opened_at = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)
    obligation.opened_at = opened_at
    await session.commit()

    spec = obligation_spec_from_metadata(run.metadata_["obligation_spec"])
    assert spec is not None
    assert spec.answerer.name == "Reda"
    assert spec.answerer.slack_user_id == "UREDA"
    assert obligation.requester_name == "Aline Athaydes"
    assert obligation.requester_slack_id == "B_CONTACT_FORM"
    assert obligation.status == "open"
    assert notice.condition == spec.condition
    assert notice.state == "pending"

    class _SessionLease:
        async def __aenter__(self):
            return session

        async def __aexit__(self, *_exc):
            return False

    posts: list[dict] = []

    async def poster(**kwargs):
        posts.append(kwargs)
        return {"ok": True, "ts": "1785235801.000100"}

    too_early = await deliver_pending_obligation_notices(
        org_id=ORG_ID,
        session_factory=lambda: _SessionLease(),
        poster=poster,
        now=opened_at + timedelta(hours=23, minutes=59),
    )
    assert too_early["selected"] == 0
    assert posts == []

    due = await deliver_pending_obligation_notices(
        org_id=ORG_ID,
        session_factory=lambda: _SessionLease(),
        poster=poster,
        now=opened_at + timedelta(hours=24, seconds=1),
    )
    assert due["delivered"] == 1
    assert len(posts) == 1
    assert posts[0]["thread_ts"] == THREAD_TS
    assert "<@UREDA>" in posts[0]["text"]
    assert "still unanswered after 24h" in posts[0]["text"]
    assert "Next action:" in posts[0]["text"]

    repeated = await deliver_pending_obligation_notices(
        org_id=ORG_ID,
        session_factory=lambda: _SessionLease(),
        poster=poster,
        now=opened_at + timedelta(hours=25),
    )
    assert repeated["selected"] == 0
    assert len(posts) == 1


@pytest.mark.asyncio
async def test_owner_reply_answers_contact_lead_and_suppresses_24h_resurface(
    session,
):
    from brain.systems.runs.obligation_notices import (
        deliver_pending_obligation_notices,
    )
    from brain.systems.runs.open_asks import (
        record_inbound_slack_obligation_answer,
    )
    from brain.systems.runs.work_intake import admit_work

    admitted = await admit_work(session, _lead_admission_event())
    assert admitted.ok is True
    obligation = (await session.scalars(select(OpenAsk))).one()
    opened_at = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)
    obligation.opened_at = opened_at

    ignored = await record_inbound_slack_obligation_answer(
        session,
        org_id=ORG_ID,
        channel_id="CALERTS",
        thread_ts=THREAD_TS,
        slack_user_id="B_CONTACT_FORM",
        message_ts="1785150200.000250",
        answer_text="The requester posted another source message.",
        now=opened_at + timedelta(minutes=10),
    )
    assert ignored == 0
    assert obligation.status == "open"

    answered = await record_inbound_slack_obligation_answer(
        session,
        org_id=ORG_ID,
        channel_id="CALERTS",
        thread_ts=THREAD_TS,
        slack_user_id="UREDA",
        message_ts="1785150300.000300",
        answer_text="I replied to Aline with the verified capability answers.",
        now=opened_at + timedelta(minutes=15),
    )
    await session.commit()

    assert answered == 1
    assert obligation.status == "answered"
    notice = (await session.scalars(select(ObligationNotice))).one()
    assert notice.state == "superseded"

    class _SessionLease:
        async def __aenter__(self):
            return session

        async def __aexit__(self, *_exc):
            return False

    posts: list[dict] = []

    async def poster(**kwargs):
        posts.append(kwargs)
        return {"ok": True, "ts": "unexpected"}

    result = await deliver_pending_obligation_notices(
        org_id=ORG_ID,
        session_factory=lambda: _SessionLease(),
        poster=poster,
        now=opened_at + timedelta(hours=25),
    )
    assert result["selected"] == 0
    assert posts == []
