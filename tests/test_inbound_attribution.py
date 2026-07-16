"""Attribution: durable-ref extraction + the packet-mint work predicate.

The gap under test (2026-07-16 fix): a run whose entire outcome is a filed
GitHub issue reported NO mutated refs — the connector payload carries
``{"repo", "issue": {"number"}}``, not ``*_id`` keys — which left the
packet-mint predicate blind to the most common actionable outcome.
"""

from __future__ import annotations

import json

import pytest

from brain.platform.db.models.agent_run import AgentRunEventRow, AgentRunRow
from brain.systems.inbound.attribution import (
    WORK_ITEM_REF_KINDS,
    durable_work_refs,
    summarize_inbound_run_attribution,
)
from brain.systems.runs.status import RunStatus


@pytest.fixture
async def session(async_sqlite_session_factory, sqlite_postgres_ddl_patch):
    return await async_sqlite_session_factory([
        AgentRunRow.__table__,
        AgentRunEventRow.__table__,
    ])


def _tool_event(run_id: int, seq: int, tool: str, result: dict) -> AgentRunEventRow:
    return AgentRunEventRow(
        run_id=run_id,
        sequence_no=seq,
        event_type="run.tool_completed",
        payload={"tool_name": tool, "args": {}, "result": json.dumps(result)},
    )


async def _seed_run(session) -> int:
    run = AgentRunRow(
        org_id=None,
        thread_id="t-1",
        profile="fast",
        recipe="illo",
        status="completed",
        input_message="x",
    )
    session.add(run)
    await session.flush()
    return int(run.id)


async def test_created_github_issue_becomes_a_mutated_ref(session):
    run_id = await _seed_run(session)
    session.add(_tool_event(run_id, 1, "create_github_issue", {
        "repo": "uwear-ai/uwear-backend",
        "issue": {"type": "issue", "number": 616, "title": "Rollbar: boom",
                  "html_url": "https://github.com/uwear-ai/uwear-backend/issues/616"},
        "token_source": "github_app",
    }))
    await session.flush()

    attribution = await summarize_inbound_run_attribution(
        session, run_id=run_id, status=RunStatus.COMPLETED
    )
    refs = attribution["mutated_target_refs"]
    assert {"kind": "github_issue", "id": "uwear-ai/uwear-backend#616",
            "source": "create_github_issue"} in refs
    assert durable_work_refs(attribution) == [
        {"kind": "github_issue", "id": "uwear-ai/uwear-backend#616",
         "source": "create_github_issue"}
    ]


async def test_created_pull_request_ref_kind(session):
    run_id = await _seed_run(session)
    session.add(_tool_event(run_id, 1, "create_github_issue", {
        "repo": "uwear-ai/uwear-website",
        "issue": {"type": "pull_request", "number": 88},
    }))
    await session.flush()

    attribution = await summarize_inbound_run_attribution(
        session, run_id=run_id, status=RunStatus.COMPLETED
    )
    kinds = {ref["kind"] for ref in attribution["mutated_target_refs"]}
    assert "github_pull_request" in kinds


async def test_error_payload_with_repo_yields_no_artifact_ref(session):
    """The handler's failure shape carries ``repo`` but no ``issue`` — it must
    never read as created work."""
    run_id = await _seed_run(session)
    session.add(_tool_event(run_id, 1, "create_github_issue", {
        "error": "No GitHub App identity is connected",
        "no_write_token": True,
        "repo": "uwear-ai/uwear-backend",
    }))
    await session.flush()

    attribution = await summarize_inbound_run_attribution(
        session, run_id=run_id, status=RunStatus.COMPLETED
    )
    assert durable_work_refs(attribution) == []


async def test_slack_reply_never_counts_as_durable_work(session):
    run_id = await _seed_run(session)
    session.add(_tool_event(run_id, 1, "post_slack_reply", {
        "operation": "posted",
        "channel_id": "C123",
        "message_ts": "1752600000.0",
    }))
    await session.flush()

    attribution = await summarize_inbound_run_attribution(
        session, run_id=run_id, status=RunStatus.COMPLETED
    )
    assert durable_work_refs(attribution) == []


def test_predicate_filters_kinds_and_chat_sources():
    attribution = {
        "mutated_target_refs": [
            {"kind": "github_issue", "id": "a/b#1", "source": "create_github_issue"},
            {"kind": "idea", "id": "i-1", "source": "manage_idea"},
            {"kind": "message", "id": "m-1", "source": "post_chat_message"},
            # work-item kind, but produced by a conversational tool:
            {"kind": "thread", "id": "t-1", "source": "post_slack_reply"},
            {"kind": "memory_source", "id": "s-1", "source": "brain_encode"},
        ]
    }
    refs = durable_work_refs(attribution)
    assert [ref["kind"] for ref in refs] == ["github_issue", "idea"]
    assert durable_work_refs(None) == []
    assert durable_work_refs({}) == []


def test_work_item_vocabulary_is_the_expected_set():
    assert WORK_ITEM_REF_KINDS == {
        "github_issue", "github_pull_request", "idea", "domain_record",
        "agent_run", "launch_handoff", "thread",
    }


async def test_truncated_result_preview_recovers_refs_from_result_refs(session):
    """Run events store a 1000-char result PREVIEW; the executor extracts
    refs from the FULL result into payload.result_refs first. A truncated
    (unparseable) preview must not blind attribution to what the tool
    created (live illo-dev finding, 2026-07-16: tracker record invisible
    to the packet-mint predicate)."""
    run_id = await _seed_run(session)
    big_result = json.dumps({"record": {"id": 1823, "domain_id": 30, "pad": "x" * 5000}})
    session.add(AgentRunEventRow(
        run_id=run_id,
        sequence_no=1,
        event_type="run.tool_completed",
        payload={
            "tool_name": "manage_domain",
            "args": {"action": "create_record"},
            "result": big_result[:1000],  # what tools.py persists
            "result_refs": [
                {"kind": "domain_record", "id": "1823", "source": "manage_domain"},
                {"kind": "domain", "id": "30", "source": "manage_domain"},
            ],
        },
    ))
    await session.flush()

    attribution = await summarize_inbound_run_attribution(
        session, run_id=run_id, status=RunStatus.COMPLETED
    )
    assert {"kind": "domain_record", "id": "1823", "source": "manage_domain"} in (
        attribution["mutated_target_refs"]
    )
    durable = durable_work_refs(attribution)
    assert any(ref["kind"] == "domain_record" and ref["id"] == "1823" for ref in durable)


def test_event_payload_carries_full_result_refs_beside_preview():
    from brain.systems.runs.tools import _event_payload

    big_result = json.dumps({"record": {"id": 99, "pad": "y" * 3000}})
    payload = _event_payload("manage_domain", {"action": "create_record"}, result=big_result)
    assert len(payload["result"]) == 1000  # bounded preview
    assert {"kind": "domain_record", "id": "99", "source": "manage_domain"} in payload["result_refs"]

    small = _event_payload("post_slack_reply", {}, result=json.dumps({"ok": True, "channel_id": "C1"}))
    assert "result_refs" not in small  # no refs → no key


def test_oversized_ref_ids_are_dropped_never_truncated():
    """A clipped id is a wrong ref, and unbounded ids would balloon the
    persisted result_refs payload — drop anything id-shaped that is really
    content (cross-family review finding, 2026-07-16)."""
    from brain.systems.inbound.attribution import collect_result_refs

    refs = collect_result_refs(
        json.dumps({"record_id": "z" * 1_000_000, "idea_id": "idea-ok"}),
        source="s" * 500,
    )
    assert refs == [{"kind": "idea", "id": "idea-ok", "source": "s" * 80}]
