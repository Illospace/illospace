from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from brain.contracts.statuses import (
    ACTIVE_OPEN_ASK_STATUS_VALUES,
    OPEN_ASK_STATUS_VALUES,
    TERMINAL_OPEN_ASK_STATUS_VALUES,
    OpenAskStatus,
)
from brain.platform.db.models.agent_run import AgentRunEventRow, AgentRunRow
from brain.platform.db.models.open_ask import (
    ObligationKind,
    ObligationNotice,
    OpenAsk,
)
from brain.platform.db.models.org import Org, User
from tests.conftest import requires_db
from tests.test_contact_form_lead_obligation import (
    ORG_ID,
    THREAD_TS,
    USER_ID,
    _lead_admission_event,
    _patch_sqlite_for_models,
)


@pytest.fixture
async def terminal_session(async_sqlite_session_factory):
    _patch_sqlite_for_models()
    session = await async_sqlite_session_factory(
        [
            Org.__table__,
            User.__table__,
            AgentRunRow.__table__,
            AgentRunEventRow.__table__,
            OpenAsk.__table__,
            ObligationNotice.__table__,
        ]
    )
    session.add(Org(id=ORG_ID, name="Test Org", slug="test-org"))
    session.add(
        User(
            id=USER_ID,
            org_id=ORG_ID,
            name="Reda",
            email="reda@example.com",
        )
    )
    await session.flush()
    return session


def test_open_ask_contract_and_model_declare_only_supported_statuses():
    assert OPEN_ASK_STATUS_VALUES == ("open", "answered", "routed", "expired")
    assert ACTIVE_OPEN_ASK_STATUS_VALUES == ("open", "routed")
    assert TERMINAL_OPEN_ASK_STATUS_VALUES == ("answered", "expired")
    assert OPEN_ASK_STATUS_VALUES == tuple(status.value for status in OpenAskStatus)
    constraint = next(
        item
        for item in OpenAsk.__table__.constraints
        if item.name == "ck_open_asks_status"
    )
    expression = str(constraint.sqltext)
    for status in OPEN_ASK_STATUS_VALUES:
        assert repr(status) in expression
    assert "unknown" not in expression


@requires_db
async def test_open_ask_database_constraint_accepts_supported_statuses_only(
    db_session,
):
    org_id = str(uuid4())
    db_session.add(Org(id=org_id, name="Status Test Org", slug=f"status-{uuid4()}"))
    await db_session.flush()

    statement = text(
        """
        INSERT INTO open_asks (
            obligation_kind, org_id, channel_id, channel_type, team_id,
            thread_ts, thread_permalink, requester_slack_id, requester_name,
            ask_text, origin_ref, status, opened_at
        ) VALUES (
            'human_ask', :org_id, 'CSTATUS', 'channel', 'TSTATUS',
            :thread_ts, :permalink, :requester_slack_id, 'Requester',
            'Can you answer this?', :origin_ref, :status, :opened_at
        )
        """
    )
    opened_at = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)
    for index, status in enumerate(OPEN_ASK_STATUS_VALUES):
        await db_session.execute(
            statement,
            {
                "org_id": org_id,
                "thread_ts": f"1785850000.000{index}",
                "permalink": f"https://example.com/thread/{index}",
                "requester_slack_id": f"U{index}",
                "origin_ref": f"status:{index}",
                "status": status,
                "opened_at": opened_at,
            },
        )
    await db_session.flush()

    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.execute(
                statement,
                {
                    "org_id": org_id,
                    "thread_ts": "1785850000.0099",
                    "permalink": "https://example.com/thread/invalid",
                    "requester_slack_id": "UINVALID",
                    "origin_ref": "status:invalid",
                    "status": "unknown",
                    "opened_at": opened_at,
                },
            )


@pytest.mark.asyncio
async def test_contact_form_acknowledgement_routes_then_owner_reply_answers(
    terminal_session,
    monkeypatch,
):
    from brain.systems.runs.execution_context import bind_agent_context
    from brain.systems.runs.open_ask_settlement import (
        record_inbound_slack_obligation_answer,
    )
    from brain.systems.runs.tool_catalog.handlers.slack import _handle_post_slack_reply
    from brain.systems.runs.work_intake import admit_work

    admitted = await admit_work(terminal_session, _lead_admission_event())
    assert admitted.ok is True
    run = await terminal_session.get(AgentRunRow, admitted.run_id)
    obligation = (await terminal_session.scalars(select(OpenAsk))).one()
    original_permalink = obligation.thread_permalink

    class _SlackClient:
        async def post_message(self, **kwargs):
            return {
                "ok": True,
                "ts": "1785149500.000300",
                "channel": kwargs["channel"],
            }

    class _SameSessionUnitOfWork:
        def __init__(self, *_args, **_kwargs):
            self.session = terminal_session

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, _exc, _tb):
            if exc_type is None:
                await terminal_session.flush()
            return False

    monkeypatch.setattr(
        "brain.systems.runs.tool_catalog.handlers.slack._slack_client_from_runtime",
        lambda: _async_value(_SlackClient()),
    )
    monkeypatch.setattr(
        "brain.platform.db.repositories.unit_of_work.UnitOfWork",
        _SameSessionUnitOfWork,
    )
    context = {
        "run_id": run.id,
        "org_id": ORG_ID,
        "execution_metadata": {
            **dict(run.metadata_),
            "run_id": run.id,
            "org_id": ORG_ID,
        },
        "slack_trigger": dict(run.target_ref["slack_trigger"]),
    }

    with bind_agent_context(context):
        result = json.loads(
            await _handle_post_slack_reply(
                body=(
                    "Strong apparel fit. <@UREDA> owns the human response. "
                    "Next action: confirm the recommendation with Aline."
                ),
                answers_open_ask=True,
            )
        )

    await terminal_session.refresh(obligation)
    assert result["answers_open_ask"] is False
    assert result["routed_open_asks"] == 1
    assert obligation.status == "routed"
    assert obligation.routed_to_name == "Reda"
    assert obligation.routed_to_slack_id == "UREDA"
    assert obligation.routed_at is not None
    assert obligation.thread_permalink == original_permalink

    answered = await record_inbound_slack_obligation_answer(
        terminal_session,
        org_id=ORG_ID,
        channel_id="CALERTS",
        thread_ts=THREAD_TS,
        slack_user_id="UREDA",
        message_ts="1785150300.000300",
        answer_text="I sent Aline the verified recommendation.",
    )

    assert answered == 1
    assert obligation.status == "answered"


@pytest.mark.asyncio
async def test_scheduled_straggler_collection_expires_only_old_terminal_deferrals(
    terminal_session,
):
    from brain.systems.cycles.service import _async_attach_open_ask_stragglers
    from brain.systems.runs.open_ask_digest import (
        RUN_DEFERRAL_EXPIRY_AFTER,
        list_open_ask_stragglers,
    )

    now = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)
    runs = []
    for index in range(2):
        run = AgentRunRow(
            trace_id=f"trace-{index}",
            org_id=ORG_ID,
            user_id=USER_ID,
            thread_id=f"slack:T789:CEXP:{index}",
            profile="fast",
            recipe="fast",
            status="failed",
            input_message=f"Deferred work {index}",
            target_ref={},
            workspace_ref={},
            model_policy={},
            metadata_={},
        )
        terminal_session.add(run)
        runs.append(run)
    await terminal_session.flush()

    old = OpenAsk(
        obligation_kind=ObligationKind.RUN_DEFERRAL,
        org_id=ORG_ID,
        channel_id="CEXP",
        channel_type="channel",
        team_id="T789",
        thread_ts="1785850000.000100",
        thread_permalink="https://example.com/thread/old",
        requester_slack_id=None,
        ask_text="Old deferred work",
        origin_ref="run-deferral:old",
        origin_run_id=runs[0].id,
        status="open",
        opened_at=now - RUN_DEFERRAL_EXPIRY_AFTER - timedelta(minutes=1),
    )
    inside_bound = OpenAsk(
        obligation_kind=ObligationKind.RUN_DEFERRAL,
        org_id=ORG_ID,
        channel_id="CEXP",
        channel_type="channel",
        team_id="T789",
        thread_ts="1785850000.000200",
        thread_permalink="https://example.com/thread/recent",
        requester_slack_id=None,
        ask_text="Recent deferred work",
        origin_ref="run-deferral:recent",
        origin_run_id=runs[1].id,
        status="open",
        opened_at=now - RUN_DEFERRAL_EXPIRY_AFTER + timedelta(minutes=1),
    )
    routed = OpenAsk(
        obligation_kind=ObligationKind.HUMAN_ASK,
        org_id=ORG_ID,
        channel_id="CEXP",
        channel_type="channel",
        team_id="T789",
        thread_ts="1785850000.000300",
        thread_permalink="https://example.com/thread/routed",
        requester_slack_id="ULEAD",
        requester_name="Lead",
        ask_text="Routed human work",
        origin_ref="human-ask:routed",
        origin_run_id=runs[1].id,
        status="routed",
        opened_at=now - timedelta(hours=100),
        routed_to_name="Reda",
        routed_to_slack_id="UREDA",
        routed_at=now - timedelta(hours=2),
    )
    terminal_session.add_all([old, inside_bound, routed])
    await terminal_session.flush()

    unswept_stragglers = await list_open_ask_stragglers(
        terminal_session,
        org_id=ORG_ID,
        now=now,
    )
    assert old.status == "open"
    assert [row["id"] for row in unswept_stragglers] == [
        routed.id,
        old.id,
        inside_bound.id,
    ]

    cycle = SimpleNamespace(
        org_id=ORG_ID,
        name="Uwear Ticket Coordinator Check-ins",
    )
    cycle_run = SimpleNamespace(
        scheduled_for=now,
        context_snapshot={
            "launch_context": {
                "origin": "scheduled_cycle",
                "run_kind": "scheduled_digest",
            }
        },
    )
    stragglers = await _async_attach_open_ask_stragglers(
        terminal_session,
        cycle,
        cycle_run,
    )

    assert old.status == "expired"
    assert old.expired_at == now
    assert "terminal (failed)" in old.status_reason
    assert "72h" in old.status_reason
    assert inside_bound.status == "open"
    assert [row["id"] for row in stragglers] == [routed.id, inside_bound.id]
    assert stragglers[0]["owner_label"] == "Reda"
    assert stragglers[0]["age"] == "2h"


@pytest.mark.asyncio
async def test_failed_scheduled_sweep_rolls_back_its_savepoint(
    terminal_session,
    monkeypatch,
):
    from brain.systems.cycles.service import _async_attach_open_ask_stragglers
    from brain.systems.runs import open_ask_digest

    async def fail_during_sweep(session, **_kwargs):
        session.add(Org(id=ORG_ID, name="Duplicate", slug="duplicate"))
        await session.flush()

    monkeypatch.setattr(
        open_ask_digest,
        "expire_stale_run_deferrals",
        fail_during_sweep,
    )
    cycle = SimpleNamespace(
        org_id=ORG_ID,
        name="Uwear Ticket Coordinator Check-ins",
    )
    cycle_run = SimpleNamespace(
        scheduled_for=datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc),
        context_snapshot={
            "launch_context": {
                "origin": "scheduled_cycle",
                "run_kind": "scheduled_digest",
            }
        },
    )

    assert await _async_attach_open_ask_stragglers(
        terminal_session,
        cycle,
        cycle_run,
    ) == []
    assert "open_ask_ledger_error" in cycle_run.context_snapshot
    assert await terminal_session.scalar(select(func.count()).select_from(Org)) == 1
    user = await terminal_session.get(User, USER_ID)
    user.name = "Session survived"
    await terminal_session.flush()


def test_open_ask_instruction_splits_illo_and_human_ownership():
    from brain.systems.cycles.prompts import _open_ask_instruction

    rendered = _open_ask_instruction(
        [
            {
                "status": "open",
                "owner_label": "Nicolas",
                "ask_text": "Tell me what is best for us",
                "age": "96h 41m",
                "thread_permalink": "https://example.com/open",
            },
            {
                "status": "routed",
                "owner_label": "Reda",
                "ask_text": "Confirm the recommendation",
                "age": "96h 40m",
                "thread_permalink": "https://example.com/routed",
            },
            {
                "status": "expired",
                "owner_label": "Illo run 5",
                "ask_text": "Dead promise",
                "age": "176h",
                "thread_permalink": "https://example.com/expired",
            },
        ]
    )

    illo_section, human_section = rendered.split(
        "- MANDATORY WAITING-ON-HUMAN LEDGER:",
        maxsplit=1,
    )
    assert "still owned by Illo" in illo_section
    assert "Nicolas — unanswered for 96h 41m" in illo_section
    assert "Reda" not in illo_section
    assert "Waiting on Reda for 96h 40m" in human_section
    assert "age is time waiting on that person" in human_section
    assert "Dead promise" not in rendered
    assert "https://example.com/expired" not in rendered


async def _async_value(value):
    return value
