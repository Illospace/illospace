"""Reconciliation → packet-mint lanes (the gate that never fired in prod).

Production reality this file pins down (diagnosed 2026-07-16): actionable
inbound runs complete as ``slack_teammate_run`` (and ``illo_submit``) —
``illo_triage`` receipts stopped occurring on 2026-07-09, before packets
shipped. The mint hook was gated on the dormant lane, so zero packets were
ever minted. The regression contract:

- a ``slack_teammate_run``-filed GitHub issue produces a ``launch_handoffs``
  row with ``source_surface='inbound_triage'`` and a Slack thread post when
  the provenance is a public channel;
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
from brain.systems.briefing.gather import SlackThreadRead
from brain.systems.inbound.reconciliation import reconcile_inbound_triage_run

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
        User.__table__,
    ])


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

    monkeypatch.setattr(slack_client, "slack_web_client_from_env", lambda: FakeClient())
    return recorded


def _slack_envelope() -> dict:
    return {
        "kind": "slack_message",
        "summary": "Rollbar: generation pipeline exploding",
        "payload": {
            "channel_id": "C0ALERTS",
            "channel_type": "channel",
            "thread_ts": "1752600000.0",
            "message_ts": "1752600000.0",
            "bot_user_id": "B0ILLO",
        },
        "hints": {},
    }


async def _seed_slack_lane(session, *, tool_results, run_status="completed") -> tuple[str, int]:
    """One slack_teammate_run admission, receipt terminal at admission (the
    production shape written by brain/systems/slack/inbound.py)."""
    event = InboundEventRow(
        org_id=_ORG,
        connection_id=_CONN,
        origin="slack.channel_message",
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
        recipe="illo",
        status=run_status,
        input_message="monitor triage",
        metadata_={"inbound_event": {"event_id": str(event.id)}},
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


async def test_slack_filed_issue_mints_packet_and_posts(session, posts, caplog):
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

    assert len(posts) == 1
    assert posts[0]["channel"] == "C0ALERTS"
    assert posts[0]["thread_ts"] == "1752600000.0"

    mint_lines = [r for r in caplog.records if "packet mint:" in r.getMessage()]
    assert len(mint_lines) == 1
    assert "lane=slack_teammate_run" in mint_lines[0].getMessage()
    assert "ok=True" in mint_lines[0].getMessage()

    # The admission receipt and event stay exactly as the slack lane wrote them.
    stored = (await session.scalars(select(InboundDecisionReceiptRow))).one()
    assert stored.status == "processed"
    assert "reconciled_at" not in dict(stored.tool_use)
    event = await session.get(InboundEventRow, event_id)
    assert event.status == "processed"
    assert "triage" not in dict(event.action_result or {})


async def test_slack_reply_only_run_skips_with_log_line(session, posts, caplog):
    _, run_id = await _seed_slack_lane(
        session, tool_results=[("post_slack_reply", _REPLY_RESULT)],
    )
    with caplog.at_level(logging.INFO, logger="brain.systems.inbound.reconciliation"):
        await reconcile_inbound_triage_run(session, run_id)

    assert await _handoffs(session) == []
    assert await _ideas(session) == []
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
        await reconcile_inbound_triage_run(session, run_id)  # duplicate delivery

    assert len(await _handoffs(session)) == 1
    assert len(posts) == 1
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
    assert posts == []  # no slack provenance → persistence without a post

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
    assert len(posts) == 1


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
