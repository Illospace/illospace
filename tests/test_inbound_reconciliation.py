"""Reconciliation → packet-mint lanes (the gate that never fired in prod).

Production reality this file pins down (diagnosed 2026-07-16): actionable
inbound runs complete as ``slack_teammate_run`` (and ``illo_submit``) —
``illo_triage`` receipts stopped occurring on 2026-07-09, before packets
shipped. The mint hook was gated on the dormant lane, so zero packets were
ever minted. The regression contract:

- a ``slack_teammate_run``-filed GitHub issue produces a ``launch_handoffs``
  row with ``source_surface='inbound_triage'`` and a PENDING brief-delivery
  outbox row when the provenance is a public channel — the Slack post itself
  happens strictly post-commit (``brain.systems.briefing.deliver``), so a
  failed terminal-status commit can never leave a live Slack message
  pointing at rolled-back state;
- runs that only replied in Slack never mint;
- the slack lane's already-terminal receipt and event are never rewritten;
- every mint decision leaves a log line (a dormant gate can't be silent);
- the triage lane's unconditional mint and receipt transition are unchanged.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from brain.platform.db.models.agent_run import (
    AgentRunArtifactRow,
    AgentRunEventRow,
    AgentRunRow,
)
from brain.platform.db.models.idea import Idea
from brain.platform.db.models.inbound import InboundDecisionReceiptRow, InboundEventRow
from brain.platform.db.models.launch_handoff import LaunchHandoff
from brain.platform.db.models.org import User
from brain.platform.db.models.packet_delivery import PacketBriefDelivery
from brain.systems.briefing.deliver import deliver_pending_briefs
from brain.systems.briefing.gather import SlackThreadRead
from brain.systems.inbound.reconciliation import reconcile_inbound_triage_run
from brain.systems.runs.events import run_event
from brain.systems.runs.interactive_reply import INTERACTIVE_TRANSPORT_FALLBACK_MESSAGE
from brain.systems.runs.status import RunStatus
from brain.systems.runs.store import AsyncAgentRunStore

_ORG = str(uuid.uuid4())
_CONN = str(uuid.uuid4())
_RUN_USER = str(uuid.uuid4())
_NOW = datetime(2026, 7, 16, 9, 0, tzinfo=timezone.utc)

_ISSUE_RESULT = json.dumps({
    "repo": "uwear-ai/uwear-backend",
    "issue": {"type": "issue", "number": 616, "title": "Rollbar: boom",
              "html_url": "https://github.com/uwear-ai/uwear-backend/issues/616"},
    "token_source": "github_app",
})
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
        Idea.__table__,
        LaunchHandoff.__table__,
        PacketBriefDelivery.__table__,
        User.__table__,
    ])


class _SessionLease:
    """A factory shim handing the deliverer the test's live sqlite session
    (in-memory sqlite is per-connection, so a real second session would see
    an empty database). Close is a no-op; commits are real."""

    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *_exc):
        return False


def _lease_factory(session):
    return lambda: _SessionLease(session)


async def _deliveries(session) -> list[PacketBriefDelivery]:
    rows = list((await session.scalars(select(PacketBriefDelivery))).all())
    for row in rows:
        await session.refresh(row)  # the deliverer's CAS bypasses the ORM
    return rows


@pytest.fixture(autouse=True)
def _hermetic_assignment_env(monkeypatch):
    for key in ("ILLO_BUSINESS_OWNER_USER_ID", "ILLO_PRODUCT_OWNER_USER_ID",
                "ILLO_REPO_OWNERS", "ILLO_UNCLAIMED_POOL_USER_ID",
                "ILLO_MEMBER_AGENT_TARGETS"):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture(autouse=True)
def _fake_readers(monkeypatch):
    """Gather must do no live I/O in tests: swap the default readers the mint
    constructs (mint builds them by the names it imported)."""

    class FakeSlackReader:
        def __init__(self, client=None):
            pass

        async def read_thread(self, *, channel, thread_ts, limit):
            return SlackThreadRead(
                messages=({"ts": thread_ts, "user": "U0AXEL", "text": "alert body"},),
                total=1,
                channel=channel,
            )

    class FakeGithubReader:
        def __init__(self, org_id=None, user_id=None):
            pass

        async def read_ref(self, *, repo_slug, number):
            return {"kind": "issue", "title": f"{repo_slug}#{number}", "body": "…",
                    "state": "open", "body_total_chars": 1}

    import brain.systems.briefing.mint as mint_module

    monkeypatch.setattr(mint_module, "DefaultSlackReader", FakeSlackReader)
    monkeypatch.setattr(mint_module, "DefaultGithubReader", FakeGithubReader)


@pytest.fixture(autouse=True)
def posts(monkeypatch):
    recorded: list[dict] = []

    class FakeClient:
        async def post_message(self, *, channel, text, thread_ts=None):
            recorded.append({"channel": channel, "text": text, "thread_ts": thread_ts})
            return {"ok": True}

    import brain.systems.slack.client as slack_client

    async def fake_from_runtime(**_kwargs):
        return FakeClient()

    monkeypatch.setattr(slack_client, "slack_web_client_from_runtime", fake_from_runtime)
    return recorded


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


async def _handoffs(session) -> list[LaunchHandoff]:
    return list((await session.scalars(select(LaunchHandoff))).all())


async def _ideas(session) -> list[Idea]:
    return list((await session.scalars(select(Idea))).all())


async def test_slack_filed_issue_mints_packet_silently(session, posts, caplog):
    event_id, run_id = await _seed_slack_lane(
        session, tool_results=[("create_github_issue", _ISSUE_RESULT),
                               ("post_slack_reply", _REPLY_RESULT)],
    )
    with caplog.at_level(logging.INFO, logger="brain.systems.inbound.reconciliation"):
        receipt = await reconcile_inbound_triage_run(session, run_id)

    assert receipt is None  # slack receipts are never rewritten

    rows = await _handoffs(session)
    assert len(rows) == 1
    row = rows[0]
    assert row.source_surface == "inbound_triage"
    assert row.source_ref.get("inbound_event_id") == event_id
    assert row.status == "open"

    ideas = await _ideas(session)
    assert len(ideas) == 1
    idea = ideas[0]
    assert idea.origin_ref == f"inbound_event:{event_id}"
    assert idea.agent_details["packet"]["handoff_id"] == str(row.id)
    assert idea.agent_details["assignment"]["owner_id"] == _RUN_USER
    assert "uwear-ai/uwear-backend#616" in (idea.description or "")
    assert (row.metadata_ or {}).get("job_ref") == f"idea:{idea.id}"

    # Briefs retired: no Slack post, no outbox obligation — the ticket IS
    # the handoff.
    assert posts == []
    assert await _deliveries(session) == []

    mint_lines = [r for r in caplog.records if "packet mint:" in r.getMessage()]
    assert len(mint_lines) == 1
    assert "lane=slack_teammate_run" in mint_lines[0].getMessage()
    assert "ok=True" in mint_lines[0].getMessage()
    assert "delivery=skipped:briefs_retired" in mint_lines[0].getMessage()

    # The admission receipt and event stay exactly as the slack lane wrote them.
    stored = (await session.scalars(select(InboundDecisionReceiptRow))).one()
    assert stored.status == "processed"
    assert "reconciled_at" not in dict(stored.tool_use)
    event = await session.get(InboundEventRow, event_id)
    assert event.status == "processed"
    assert "triage" not in dict(event.action_result or {})

    # The drain sweep still runs for legacy rows and finds nothing new.
    summary = await deliver_pending_briefs(
        org_id=_ORG, session_factory=_lease_factory(session)
    )
    assert summary["selected"] == 0
    assert posts == []


async def test_slack_reply_only_run_skips_with_log_line(session, posts, caplog):
    _, run_id = await _seed_slack_lane(
        session, tool_results=[("post_slack_reply", _REPLY_RESULT)],
    )
    with caplog.at_level(logging.INFO, logger="brain.systems.inbound.reconciliation"):
        await reconcile_inbound_triage_run(session, run_id)

    assert await _handoffs(session) == []
    assert await _ideas(session) == []
    assert await _deliveries(session) == []
    assert posts == []
    mint_lines = [r for r in caplog.records if "packet mint:" in r.getMessage()]
    assert len(mint_lines) == 1
    assert "reason=no durable work created by run" in mint_lines[0].getMessage()


async def test_slack_lane_mint_is_one_shot(session, posts, caplog):
    _, run_id = await _seed_slack_lane(
        session, tool_results=[("create_github_issue", _ISSUE_RESULT)],
    )
    await reconcile_inbound_triage_run(session, run_id)
    with caplog.at_level(logging.INFO, logger="brain.systems.inbound.reconciliation"):
        await reconcile_inbound_triage_run(session, run_id)  # duplicate reconcile

    assert len(await _handoffs(session)) == 1
    assert await _deliveries(session) == []  # briefs retired: none, ever
    second = [r for r in caplog.records if "packet mint:" in r.getMessage()]
    assert len(second) == 1
    assert "reason=packet already minted for event" in second[0].getMessage()


async def test_failed_slack_run_never_mints(session, posts, caplog):
    _, run_id = await _seed_slack_lane(
        session, tool_results=[("create_github_issue", _ISSUE_RESULT)],
        run_status="failed",
    )
    with caplog.at_level(logging.INFO, logger="brain.systems.inbound.reconciliation"):
        await reconcile_inbound_triage_run(session, run_id)

    assert await _handoffs(session) == []
    assert posts == []
    skipped = [r for r in caplog.records if "packet mint skipped" in r.getMessage()]
    assert len(skipped) == 1
    assert "run_status=failed" in skipped[0].getMessage()


@pytest.mark.parametrize(
    "failure_reason",
    [INTERACTIVE_TRANSPORT_FALLBACK_MESSAGE, "server_error"],
    ids=["transport_disconnect", "provider_server_error"],
)
async def test_transient_failed_monitor_run_readmits_once_and_replacement_replies(
    session,
    posts,
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
    posts,
    failure_reason,
):
    from brain.systems.inbound.results import read_inbound_submission_result

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
    assert result.payload["run_id"] == replacement.id
    assert result.payload["run_status"] == "failed"
    assert result.payload["retry_attempt"] == 1
    assert result.payload["original_run_id"] == original_run_id
    assert result.payload["replacement_run_id"] == replacement.id
    assert result.payload["retry_lineage"] == [
        {"run_id": original_run_id, "retry_attempt": 0},
        {"run_id": replacement.id, "retry_attempt": 1},
    ]


async def test_non_transient_failed_monitor_run_is_not_readmitted(session, posts):
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


async def test_submit_lane_mints_on_durable_work_without_post(session, posts):
    event = InboundEventRow(
        org_id=_ORG,
        connection_id=_CONN,
        origin="mcp.submission",
        envelope={"kind": "submission", "summary": "please file the melting bug"},
        status="review_required",
    )
    session.add(event)
    await session.flush()
    run = AgentRunRow(
        org_id=_ORG, user_id=_RUN_USER, thread_id=f"inbound:{_CONN}:{event.id}",
        profile="fast", recipe="illo", status="completed",
        input_message="submission",
        metadata_={"inbound_event": {"event_id": str(event.id)}},
        completed_at=_NOW,
    )
    session.add(run)
    await session.flush()
    event.action_result = {"handling": {"status": "queued", "run_id": run.id}}
    session.add(InboundDecisionReceiptRow(
        event_id=str(event.id), org_id=_ORG, connection_id=_CONN,
        status="review_required",
        tool_use={"type": "illo_submit", "status": "queued", "run_id": run.id},
        target={"kind": "inbound_submission", "event_id": str(event.id)},
        outcome=dict(event.action_result),
    ))
    session.add(AgentRunEventRow(
        run_id=run.id, sequence_no=1, event_type="run.tool_completed",
        payload={"tool_name": "create_github_issue", "args": {}, "result": _ISSUE_RESULT},
    ))
    await session.flush()

    receipt = await reconcile_inbound_triage_run(session, run.id)

    assert receipt is not None and receipt.status == "processed"  # loop still closes
    rows = await _handoffs(session)
    assert len(rows) == 1
    assert rows[0].source_surface == "inbound_triage"
    assert posts == []
    assert await _deliveries(session) == []  # no slack provenance → nothing owed

    # Re-reconcile (illo_get_result polls do this): terminal receipt, no re-mint.
    await reconcile_inbound_triage_run(session, run.id)
    assert len(await _handoffs(session)) == 1


async def test_triage_lane_still_mints_unconditionally(session, posts):
    event = InboundEventRow(
        org_id=_ORG,
        connection_id=_CONN,
        origin="webhook.sentry",
        envelope=_slack_envelope(),  # public provenance so the post fires too
        status="review_required",
    )
    session.add(event)
    await session.flush()
    run = AgentRunRow(
        org_id=_ORG, user_id=_RUN_USER, thread_id="idea:t", profile="fast",
        recipe="illo", status="completed", input_message="triage",
        metadata_={"inbound_event": {"event_id": str(event.id)}},
        completed_at=_NOW,
    )
    session.add(run)
    await session.flush()
    # The idea _queue_illo_triage would have created before the run.
    session.add(Idea(
        title="Inbound signal needs Illo triage: webhook.sentry",
        description="triage home",
        status="emerged",
        origin="inbound_signal",
        origin_ref=f"inbound_event:{event.id}",
        user_id=_RUN_USER,
        org_id=_ORG,
        agent_details={
            "inbound_triage": {"event_id": str(event.id)},
            "task_domain": "engineering",
            "assignment": {"owner_id": _RUN_USER, "basis": "connection", "unclaimed": False},
        },
    ))
    session.add(InboundDecisionReceiptRow(
        event_id=str(event.id), org_id=_ORG, connection_id=_CONN,
        status="review_required",
        tool_use={"type": "illo_triage", "run_id": run.id},
        target={"kind": "cortex_idea"},
        outcome={},
    ))
    # NO tool events at all: triage completion must mint regardless.
    await session.flush()

    receipt = await reconcile_inbound_triage_run(session, run.id)

    assert receipt is not None and receipt.status == "processed"
    rows = await _handoffs(session)
    assert len(rows) == 1
    assert rows[0].source_surface == "inbound_triage"
    assert posts == []
    assert await _deliveries(session) == []  # briefs retired


async def test_submit_mint_failure_is_retried_by_next_poll(session, posts, monkeypatch):
    """Cross-family review finding (2026-07-16): the receipt transitions to
    processed even when the contained mint fails — a later illo_get_result
    poll must retry the mint instead of the packet being lost forever."""
    import brain.systems.briefing.mint as mint_module

    event = InboundEventRow(
        org_id=_ORG, connection_id=_CONN, origin="mcp.submission",
        envelope={"kind": "submission", "summary": "file the melting bug"},
        status="review_required",
    )
    session.add(event)
    await session.flush()
    run = AgentRunRow(
        org_id=_ORG, user_id=_RUN_USER, thread_id=f"inbound:{_CONN}:{event.id}",
        profile="fast", recipe="illo", status="completed", input_message="s",
        metadata_={"inbound_event": {"event_id": str(event.id)}}, completed_at=_NOW,
    )
    session.add(run)
    await session.flush()
    session.add(InboundDecisionReceiptRow(
        event_id=str(event.id), org_id=_ORG, connection_id=_CONN,
        status="review_required",
        tool_use={"type": "illo_submit", "status": "queued", "run_id": run.id},
        target={"kind": "inbound_submission"}, outcome={},
    ))
    session.add(AgentRunEventRow(
        run_id=run.id, sequence_no=1, event_type="run.tool_completed",
        payload={"tool_name": "create_github_issue", "args": {}, "result": _ISSUE_RESULT},
    ))
    await session.flush()

    real_build = mint_module.build_packet_for_job
    calls = {"n": 0}

    async def flaky_build(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("gather source down")
        return await real_build(*args, **kwargs)

    monkeypatch.setattr(mint_module, "build_packet_for_job", flaky_build)

    receipt = await reconcile_inbound_triage_run(session, run.id)
    assert receipt is not None and receipt.status == "processed"  # loop closed anyway
    assert await _handoffs(session) == []  # mint failed, contained

    await reconcile_inbound_triage_run(session, run.id)  # the retrying poll
    rows = await _handoffs(session)
    assert len(rows) == 1
    ideas = await _ideas(session)
    assert len(ideas) == 1  # the failed attempt's job home was reused
    assert ideas[0].agent_details["packet"]["handoff_id"] == str(rows[0].id)


async def test_mint_arms_no_fast_path_and_commit_dispatches_nothing(session, posts, monkeypatch):
    """Briefs retired (Reda, 2026-07-30): the mint no longer arms the
    after-commit dispatch, so committing the reconcile transaction must
    trigger NO delivery attempt — the ticket is the handoff."""
    import asyncio

    import brain.systems.briefing.deliver as deliver_module

    dispatched: list[dict] = []

    async def fake_deliver(**kwargs):
        dispatched.append(kwargs)
        return {"selected": 1, "posted": 1}

    monkeypatch.setattr(deliver_module, "deliver_pending_briefs", fake_deliver)

    _, run_id = await _seed_slack_lane(
        session, tool_results=[("create_github_issue", _ISSUE_RESULT)],
    )
    await reconcile_inbound_triage_run(session, run_id)
    rows = await _handoffs(session)
    assert len(rows) == 1  # the packet still mints

    await session.commit()
    for _ in range(5):  # give after_commit → call_soon → task turns to fire
        await asyncio.sleep(0)
    assert dispatched == []
    assert posts == []
