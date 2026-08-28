"""Inbound run receipt reconciliation and monitored-channel retry tests."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from brain.platform.db.models.agent_run import (
    AgentRunArtifactRow,
    AgentRunEventRow,
    AgentRunRow,
)
from brain.platform.db.models.inbound import InboundDecisionReceiptRow, InboundEventRow
from brain.platform.db.models.org import User
from brain.systems.inbound.reconciliation import reconcile_inbound_triage_run
from brain.systems.inbound.results import read_inbound_submission_result
from brain.systems.runs.events import run_event
from brain.systems.runs.interactive_reply import INTERACTIVE_TRANSPORT_FALLBACK_MESSAGE
from brain.systems.runs.status import RunStatus
from brain.systems.runs.store import AsyncAgentRunStore

_ORG = str(uuid.uuid4())
_CONN = str(uuid.uuid4())
_RUN_USER = str(uuid.uuid4())
_NOW = datetime(2026, 7, 16, 9, 0, tzinfo=timezone.utc)

_REPLY_RESULT = json.dumps({"operation": "posted", "channel_id": "C0PROD",
                            "message_ts": "1752600001.0"})


@pytest.fixture
async def session(async_sqlite_session_factory, sqlite_postgres_ddl_patch):
    return await async_sqlite_session_factory([
        AgentRunRow.__table__,
        AgentRunEventRow.__table__,
        AgentRunArtifactRow.__table__,
        InboundEventRow.__table__,
        InboundDecisionReceiptRow.__table__,
        User.__table__,
    ])



@pytest.fixture(autouse=True)
def _hermetic_assignment_env(monkeypatch):
    for key in ("ILLO_BUSINESS_OWNER_USER_ID", "ILLO_PRODUCT_OWNER_USER_ID",
                "ILLO_REPO_OWNERS", "ILLO_UNCLAIMED_POOL_USER_ID"):
        monkeypatch.delenv(key, raising=False)



def _slack_envelope() -> dict:
    return {
        "kind": "slack_message",
        "origin": "slack.channel_message",
        "idempotency_key": "slack:T1:C0ALERTS:1752600000.0",
        "summary": "Rollbar: generation pipeline exploding",
        "payload": {
            "origin": "slack.channel_message",
            "event_kind": "channel_message",
            "team_id": "T1",
            "channel_id": "C0ALERTS",
            "channel_type": "channel",
            "thread_ts": "1752600000.0",
            "message_ts": "1752600000.0",
            "slack_user_id": "U0AXEL",
            "bot_user_id": "B0ILLO",
            "text": "Rollbar: generation pipeline exploding",
        },
        "hints": {},
    }


async def _seed_slack_lane(
    session,
    *,
    tool_results,
    run_status="completed",
) -> tuple[str, int]:
    """One slack_teammate_run admission, receipt terminal at admission (the
    production shape written by brain/systems/slack/inbound.py)."""
    event = InboundEventRow(
        org_id=_ORG,
        connection_id=_CONN,
        kind="slack_message",
        origin="slack.channel_message",
        idempotency_key="slack:T1:C0ALERTS:1752600000.0",
        envelope=_slack_envelope(),
        status="processed",
        action_type="slack.run_admitted",
    )
    session.add(event)
    await session.flush()

    run = AgentRunRow(
        org_id=_ORG,
        user_id=_RUN_USER,
        thread_id=f"slack:T1:C0ALERTS:1752600000.0",
        profile="fast",
        recipe="fast",
        status=run_status,
        input_message="monitor triage",
        target_ref={"kind": "slack_message", "slack_thread_id": "slack:T1:C0ALERTS:1752600000.0"},
        metadata_={
            "origin": "slack_channel_monitor",
            "slack_monitor": True,
            "headless": True,
            "inbound_event": {"event_id": str(event.id)},
        },
        source_idempotency_scope="slack",
        source_idempotency_key="slack:T1:C0ALERTS:1752600000.0",
        completed_at=_NOW if run_status == "completed" else None,
        failed_at=_NOW if run_status == "failed" else None,
    )
    session.add(run)
    await session.flush()

    event.action_result = {"operation": "slack_run_admitted", "run_id": run.id,
                           "event_id": str(event.id)}
    session.add(InboundDecisionReceiptRow(
        event_id=str(event.id),
        org_id=_ORG,
        connection_id=_CONN,
        status="processed",
        tool_use={"type": "slack_teammate_run", "run_id": run.id,
                  "slack_thread_id": "slack:T1:C0ALERTS:1752600000.0"},
        target={"kind": "slack_message", "run_id": run.id},
        outcome=dict(event.action_result),
    ))
    for seq, (tool, result) in enumerate(tool_results, start=1):
        session.add(AgentRunEventRow(
            run_id=run.id,
            sequence_no=seq,
            event_type="run.tool_completed",
            payload={"tool_name": tool, "args": {}, "result": result},
        ))
    await session.flush()
    return str(event.id), int(run.id)


@pytest.mark.parametrize(
    "failure_reason",
    [INTERACTIVE_TRANSPORT_FALLBACK_MESSAGE, "server_error"],
    ids=["transport_disconnect", "provider_server_error"],
)
async def test_transient_failed_monitor_run_readmits_once_and_replacement_replies(
    session,
    failure_reason,
):
    event_id, original_run_id = await _seed_slack_lane(
        session,
        tool_results=[],
        run_status="running",
    )

    store = AsyncAgentRunStore(session)
    await store.set_status(
        original_run_id,
        RunStatus.FAILED,
        reason=failure_reason,
    )

    receipt = (await session.scalars(select(InboundDecisionReceiptRow))).one()
    runs = list((await session.scalars(select(AgentRunRow).order_by(AgentRunRow.id.asc()))).all())
    assert len(runs) == 2
    replacement = runs[1]
    assert replacement.source_idempotency_scope == "slack"
    assert replacement.source_idempotency_key == "slack:T1:C0ALERTS:1752600000.0:attempt:1"
    assert replacement.metadata_["retry_attempt"] == 1
    assert replacement.metadata_["inbound_event"]["retry_attempt"] == 1
    assert replacement.metadata_["inbound_event"]["original_run_id"] == original_run_id

    event = await session.get(InboundEventRow, event_id)
    assert event.action_result["run_id"] == replacement.id
    assert event.action_result["original_run_id"] == original_run_id
    assert event.action_result["replacement_run_id"] == replacement.id
    assert event.action_result["retry_attempt"] == 1
    assert event.action_result["retry_lineage"] == [
        {"run_id": original_run_id, "retry_attempt": 0},
        {"run_id": replacement.id, "retry_attempt": 1},
    ]
    assert receipt.tool_use["run_id"] == replacement.id
    assert receipt.target["run_id"] == replacement.id

    # Re-processing the original terminal run before the replacement executes
    # is inert: the receipt already follows attempt 1.
    await reconcile_inbound_triage_run(session, original_run_id)
    assert len(list((await session.scalars(select(AgentRunRow))).all())) == 2

    # The replacement executes the original monitored-channel contract and
    # explicitly posts a Slack reply. Reconciliation must not mint a third run.
    await store.set_status(replacement.id, RunStatus.STARTING)
    await store.set_status(replacement.id, RunStatus.RUNNING)
    await store.append_event(run_event(
        replacement.id,
        "run.tool_completed",
        {"tool_name": "post_slack_reply", "args": {}, "result": _REPLY_RESULT},
        root_run_id=replacement.root_run_id,
    ))
    await store.set_status(replacement.id, RunStatus.COMPLETED)
    await reconcile_inbound_triage_run(session, original_run_id)

    runs = list((await session.scalars(select(AgentRunRow))).all())
    assert len(runs) == 2
    reply_events = list((await session.scalars(
        select(AgentRunEventRow).where(
            AgentRunEventRow.run_id == replacement.id,
            AgentRunEventRow.event_type == "run.tool_completed",
        )
    )).all())
    assert [event.payload["tool_name"] for event in reply_events] == ["post_slack_reply"]


@pytest.mark.parametrize(
    "failure_reason",
    [INTERACTIVE_TRANSPORT_FALLBACK_MESSAGE, "server_error"],
    ids=["transport_disconnect", "provider_server_error"],
)
async def test_second_transient_failure_is_terminal_and_result_shows_retry_lineage(
    session,
    failure_reason,
):
    event_id, original_run_id = await _seed_slack_lane(
        session,
        tool_results=[],
        run_status="running",
    )
    store = AsyncAgentRunStore(session)
    await store.set_status(
        original_run_id,
        RunStatus.FAILED,
        reason=failure_reason,
    )
    replacement = (await session.scalars(
        select(AgentRunRow).where(AgentRunRow.id != original_run_id)
    )).one()
    await store.set_status(
        replacement.id,
        RunStatus.FAILED,
        reason=failure_reason,
    )
    await reconcile_inbound_triage_run(session, replacement.id)

    assert len(list((await session.scalars(select(AgentRunRow))).all())) == 2
    result = await read_inbound_submission_result(
        session,
        org_id=_ORG,
        connection_id=_CONN,
        event_id=event_id,
    )
    assert result.payload is not None
    assert result.payload["run_id"] == replacement.id
    assert result.payload["run_status"] == "failed"
    assert result.payload["retry_attempt"] == 1
    assert result.payload["original_run_id"] == original_run_id
    assert result.payload["replacement_run_id"] == replacement.id
    assert result.payload["retry_lineage"] == [
        {"run_id": original_run_id, "retry_attempt": 0},
        {"run_id": replacement.id, "retry_attempt": 1},
    ]

async def test_non_transient_failed_monitor_run_is_not_readmitted(session):
    event_id, original_run_id = await _seed_slack_lane(
        session,
        tool_results=[],
        run_status="running",
    )

    await AsyncAgentRunStore(session).set_status(
        original_run_id,
        RunStatus.FAILED,
        reason="Refused because the requested operation violates policy",
    )

    assert len(list((await session.scalars(select(AgentRunRow))).all())) == 1
    event = await session.get(InboundEventRow, event_id)
    assert event.action_result["run_id"] == original_run_id
    assert "retry_attempt" not in event.action_result


async def _seed_triage_lane(session, *, receipt_type: str, run_status: str = "completed"):
    """One triage-lane admission: receipt written NON-terminal, awaiting the run."""
    event = InboundEventRow(
        org_id=_ORG,
        connection_id=_CONN,
        origin="webhook.sentry",
        envelope=_slack_envelope(),
        status="review_required",
    )
    session.add(event)
    await session.flush()
    run = AgentRunRow(
        org_id=_ORG,
        user_id=_RUN_USER,
        thread_id="idea:t",
        profile="fast",
        recipe="illo",
        status=run_status,
        input_message="triage",
        metadata_={"inbound_event": {"event_id": str(event.id)}},
        completed_at=_NOW if run_status == "completed" else None,
        failed_at=_NOW if run_status == "failed" else None,
    )
    session.add(run)
    await session.flush()
    session.add(InboundDecisionReceiptRow(
        event_id=str(event.id),
        org_id=_ORG,
        connection_id=_CONN,
        status="review_required",
        tool_use={"type": receipt_type, "run_id": run.id},
        target={"kind": "cortex_idea"},
        outcome={},
    ))
    await session.flush()
    return event, run


@pytest.mark.parametrize(
    ("receipt_type", "outcome_key"),
    [("illo_triage", "triage"), ("illo_submit", "handling")],
)
async def test_completed_run_takes_the_receipt_terminal(session, receipt_type, outcome_key):
    """The core of the lane: a finished run closes the receipt it was admitted on."""
    event, run = await _seed_triage_lane(session, receipt_type=receipt_type)

    receipt = await reconcile_inbound_triage_run(session, run.id)

    assert receipt is not None
    assert receipt.status == "processed"
    assert receipt.outcome[outcome_key]["status"] == "completed"
    assert receipt.tool_use["status"] == "completed"

    event = await session.get(InboundEventRow, str(event.id))
    assert event.status == "processed"
    assert event.processed_at is not None
    assert event.error is None
    assert event.action_result[outcome_key]["run_id"] == run.id


async def test_failed_run_takes_the_receipt_terminal_with_the_failure(session):
    """A failed run must close the receipt too, carrying why — never hang open."""
    event, run = await _seed_triage_lane(
        session, receipt_type="illo_triage", run_status="failed",
    )

    receipt = await reconcile_inbound_triage_run(session, run.id)

    assert receipt is not None
    assert receipt.status == "failed"
    assert receipt.outcome["triage"]["status"] == "failed"
    assert receipt.outcome["triage"]["failure"]["message"]

    event = await session.get(InboundEventRow, str(event.id))
    assert event.status == "failed"
    assert event.error == receipt.outcome["triage"]["failure"]["message"]
