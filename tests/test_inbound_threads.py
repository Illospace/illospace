"""Inbound thread reads use real SQLite storage and the shared visibility rule."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from brain.app.api.routers.agent_mcp import _tool_read
from brain.platform.db.models.agent_run import AgentRunArtifactRow, AgentRunRow
from brain.platform.db.models.idea import Idea
from brain.platform.db.models.inbound import InboundEventRow
from brain.systems.external_agents import service
from tests.test_inbound_reconciliation import session


pytestmark = pytest.mark.asyncio
ORG = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa1"
CONNECTION = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbb1"
EVENT = "cccccccc-cccc-cccc-cccc-ccccccccccc1"
THREAD = f"inbound:{CONNECTION}:{EVENT}"
NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)


@pytest.fixture
def principal():
    return service.AgentBridgePrincipal(
        connection_id=CONNECTION, org_id=ORG, owner_user_id=CONNECTION,
        token_id=CONNECTION, scopes=frozenset(service.DEFAULT_BRIDGE_SCOPES),
        connection_display_name="Codex", agent_kind="codex",
    )


@pytest.fixture
async def inbound(session):
    session.add(InboundEventRow(
        id=EVENT, org_id=ORG, connection_id=CONNECTION, kind="agent_signal",
        origin="codex", status="processed", created_at=NOW,
        normalized_payload={"message": "Please preserve this decision", "task_title": "Decision"},
        action_result={},
    ))
    run = AgentRunRow(
        org_id=ORG, thread_id=THREAD, profile="fast", recipe="fast",
        status="completed", input_message="Please preserve this decision", created_at=NOW,
    )
    session.add(run)
    await session.flush()
    session.add(AgentRunArtifactRow(
        run_id=run.id, artifact_type="final_answer", text="Decision preserved",
        visibility="public", created_at=NOW + timedelta(seconds=2),
    ))
    await session.flush()
    return run


async def test_inbound_thread_contains_submission_and_final_answer(session, principal, inbound):
    result = await _tool_read(session, principal, {
        "capability": "thread.get", "arguments": {"idea_id": THREAD},
    })
    assert set(result) == {"idea", "thread_reference", "messages"}
    assert result["idea"]["id"] == result["idea"]["thread_id"] == THREAD
    assert result["idea"]["title"] == "Decision"
    assert [(m["role"], m["content"]) for m in result["messages"]] == [
        ("user", "Please preserve this decision"), ("assistant", "Decision preserved"),
    ]


async def test_inbound_thread_other_connection_is_not_visible(principal):
    session = AsyncMock()
    caller = replace(principal, connection_id="dddddddd-dddd-dddd-dddd-ddddddddddd1")
    assert await service.get_thread(session, caller, idea_id=THREAD) == {
        "event_id": EVENT, "state": "not_visible_to_connection", "owned_by_another_connection": True,
    }
    assert session.mock_calls == []


async def test_inbound_thread_other_org_is_not_found(session, principal, inbound):
    caller = replace(principal, org_id="eeeeeeee-eeee-eeee-eeee-eeeeeeeeeee1")
    with pytest.raises(ValueError, match="^Inbound event not found$"):
        await service.get_thread(session, caller, idea_id=THREAD)


async def test_inbound_thread_forged_connection_is_not_visible(principal):
    session = AsyncMock()
    forged = f"inbound:dddddddd-dddd-dddd-dddd-ddddddddddd1:{EVENT}"
    result = await service.get_thread(session, principal, idea_id=forged)
    assert result == {
        "event_id": EVENT, "state": "not_visible_to_connection", "owned_by_another_connection": True,
    }
    assert session.mock_calls == []


@pytest.mark.parametrize("thread_id", [
    "inbound:not-a-uuid:x", "headless-worker:1:abc", "garbage",
    "headless:scan:aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa1", "inbound:missing",
])
async def test_invalid_thread_id_never_queries_database(principal, thread_id):
    db = AsyncMock()
    with pytest.raises(ValueError, match="^Thread id must be a UUID or inbound:<connection_id>:<event_id>$"):
        await service.get_thread(db, principal, idea_id=thread_id)
    assert db.mock_calls == []


async def test_shared_idea_guard_rejects_raw_ids_before_query():
    db = AsyncMock()
    with pytest.raises(ValueError, match="Thread id must be a UUID"):
        await service.require_idea_for_org(db, idea_id="garbage", org_id=ORG)
    assert db.mock_calls == []


async def test_uuid_thread_still_reads_idea_and_messages(principal, monkeypatch):
    db = AsyncMock()
    idea = Idea(id=EVENT, org_id=ORG, title="Normal thread", status="new")
    rows = MagicMock()
    rows.first.return_value = idea
    db.scalars.return_value = rows
    reference = {"thread_id": EVENT}
    messages = [{"role": "user", "content": "Normal message"}]
    monkeypatch.setattr(service, "thread_reference_payload", AsyncMock(return_value=reference))
    monkeypatch.setattr(service, "_thread_context", AsyncMock(return_value=messages))
    result = await service.get_thread(db, principal, idea_id=EVENT)
    assert result["idea"]["id"] == EVENT
    assert result["thread_reference"] == reference
    assert result["messages"] == messages
    assert EVENT in db.scalars.call_args.args[0].compile().params.values()


async def test_inbound_thread_returns_artifact_links_and_retry_answers_in_order(session, principal, inbound):
    retry = AgentRunRow(
        org_id=ORG, thread_id=THREAD, profile="fast", recipe="fast",
        status="completed", input_message="Retry", created_at=NOW + timedelta(seconds=3),
    )
    foreign = AgentRunRow(
        org_id="eeeeeeee-eeee-eeee-eeee-eeeeeeeeeee1", thread_id=THREAD,
        profile="fast", recipe="fast", status="completed", input_message="Foreign",
    )
    session.add_all([retry, foreign])
    await session.flush()
    session.add_all([
        AgentRunArtifactRow(
            run_id=inbound.id, artifact_type="file_observation", title="Report",
            uri="https://assets.example/report.pdf", payload={"url": "https://assets.example/report.pdf"},
            visibility="public", created_at=NOW + timedelta(seconds=1),
        ),
        AgentRunArtifactRow(
            run_id=retry.id, artifact_type="final_answer", text="Retry completed",
            visibility="public", created_at=NOW + timedelta(seconds=4),
        ),
        AgentRunArtifactRow(
            run_id=inbound.id, artifact_type="context_pack", text="Private context",
            visibility="internal", created_at=NOW + timedelta(seconds=5),
        ),
        AgentRunArtifactRow(
            run_id=foreign.id, artifact_type="final_answer", text="Foreign answer",
            visibility="public", created_at=NOW + timedelta(seconds=6),
        ),
    ])
    await session.flush()
    result = await service.get_thread(session, principal, idea_id=THREAD)
    assert [m["content"] for m in result["messages"]] == [
        "Please preserve this decision", "Report", "Decision preserved", "Retry completed",
    ]
    assert result["messages"][1]["attachments"][0]["uri"] == "https://assets.example/report.pdf"
    limited = await service.get_thread(session, principal, idea_id=THREAD, limit=1)
    assert [m["content"] for m in limited["messages"]] == ["Retry completed"]
