from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite.base import SQLiteDDLCompiler, SQLiteTypeCompiler

from brain.app.api.deps import get_db, rate_limit
from brain.app.api.main import app
from brain.platform.db.models.agent_run import AgentRunArtifactRow, AgentRunEventRow, AgentRunRow
from brain.platform.db.models.domain import (
    Domain,
    DomainEvent,
    DomainFieldDefinition,
    DomainObjectType,
    DomainRecord,
    DomainRelation,
    DomainRelationType,
)
from brain.platform.db.models.external_agent import (
    ExternalAgentConnectionRow,
    ExternalAgentConnectionTokenRow,
)
from brain.platform.db.models.idea import Idea, IdeaStateLog, IdeaThread
from brain.platform.db.models.inbound import (
    InboundDecisionReceiptRow,
    InboundDomainProjectionKeyRow,
    InboundDomainProjectionRow,
    InboundEventRow,
    InboundSourcePolicyRow,
)
from brain.platform.db.models.org import Org, User
from brain.systems.external_agents import service as external_agents
from brain.systems.inbound import admin as inbound_admin
from brain.systems.inbound import service as inbound
from brain.systems.runs.events import run_event
from brain.systems.runs.status import RunStatus
from brain.systems.runs.store import AsyncAgentRunStore
from brain.systems.user_domains.service import AsyncDomainService


pytestmark = pytest.mark.asyncio

ORG_ID = "11111111-1111-4111-8111-111111111111"
USER_ID = "22222222-2222-4222-8222-222222222222"
CONNECTION_ID = "33333333-3333-4333-8333-333333333333"
TOKEN_ID = "44444444-4444-4444-8444-444444444444"
RAW_TOKEN = "illo_conn_test_webhook_token"


def _patch_sqlite_for_pg_types():
    if not hasattr(SQLiteTypeCompiler, "visit_JSONB"):
        SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "TEXT"
    SQLiteTypeCompiler.visit_UUID = lambda self, type_, **kw: "TEXT"
    SQLiteTypeCompiler.visit_BIGINT = lambda self, type_, **kw: "INTEGER"
    for name in ("visit_VECTOR", "visit_Vector"):
        if not hasattr(SQLiteTypeCompiler, name):
            setattr(SQLiteTypeCompiler, name, lambda self, type_, **kw: "TEXT")

    original = SQLiteDDLCompiler.get_column_default_string
    if getattr(original, "_inbound_webhooks_patch", False):
        return

    def patched(self, column, **kw):
        result = original(self, column, **kw)
        if result:
            result = re.sub(r"::jsonb", "", result)
            result = result.replace("NOW()", "CURRENT_TIMESTAMP")
            result = result.replace("TRUE", "1").replace("FALSE", "0")
        return result

    patched._inbound_webhooks_patch = True
    SQLiteDDLCompiler.get_column_default_string = patched


@pytest.fixture
async def session(async_sqlite_session_factory):
    _patch_sqlite_for_pg_types()
    return await async_sqlite_session_factory(
        [
            Org.__table__,
            User.__table__,
            ExternalAgentConnectionRow.__table__,
            ExternalAgentConnectionTokenRow.__table__,
            Domain.__table__,
            DomainObjectType.__table__,
            DomainFieldDefinition.__table__,
            DomainRelationType.__table__,
            DomainRecord.__table__,
            DomainRelation.__table__,
            DomainEvent.__table__,
            Idea.__table__,
            IdeaThread.__table__,
            IdeaStateLog.__table__,
            AgentRunRow.__table__,
            AgentRunEventRow.__table__,
            AgentRunArtifactRow.__table__,
            InboundSourcePolicyRow.__table__,
            InboundDomainProjectionRow.__table__,
            InboundDomainProjectionKeyRow.__table__,
            InboundEventRow.__table__,
            InboundDecisionReceiptRow.__table__,
        ]
    )


async def _seed_connection(
    session,
    *,
    connection_id: str = CONNECTION_ID,
    token_id: str = TOKEN_ID,
    raw_token: str = RAW_TOKEN,
    scopes: list[str] | None = None,
    status: str = "online",
) -> external_agents.AgentBridgePrincipal:
    if await session.get(Org, ORG_ID) is None:
        session.add(Org(id=ORG_ID, name="Test Org", slug="test-org"))
    if await session.get(User, USER_ID) is None:
        session.add(User(id=USER_ID, org_id=ORG_ID, name="Reda", email="reda@example.com"))
    connection = ExternalAgentConnectionRow(
        id=connection_id,
        org_id=ORG_ID,
        owner_user_id=USER_ID,
        display_name="Jira webhook",
        agent_kind="jira",
        transport="webhook",
        status=status,
        remote_agent_card={},
        capabilities={"submit_signals": True},
        auth_metadata={},
        metadata_={},
    )
    token = ExternalAgentConnectionTokenRow(
        id=token_id,
        connection_id=connection_id,
        org_id=ORG_ID,
        owner_user_id=USER_ID,
        token_hash=external_agents.hash_connection_token(raw_token),
        token_prefix=external_agents.token_prefix(raw_token),
        name="Webhook token",
        scopes=scopes or [external_agents.SCOPE_SIGNAL_SUBMIT],
    )
    session.add_all([connection, token])
    await session.flush()
    return external_agents.AgentBridgePrincipal(
        connection_id=connection_id,
        org_id=ORG_ID,
        owner_user_id=USER_ID,
        token_id=token_id,
        scopes=frozenset(scopes or [external_agents.SCOPE_SIGNAL_SUBMIT]),
        connection_display_name="Jira webhook",
        agent_kind="jira",
    )


async def _post_webhook(session, *, headers: dict[str, str] | None = None, json: dict):
    overrides = dict(app.dependency_overrides)
    app.dependency_overrides[get_db] = lambda: session
    app.dependency_overrides[rate_limit] = lambda: None
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post("/webhooks", headers=headers or {}, json=json)
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(overrides)


async def _post_mcp(session, *, headers: dict[str, str] | None = None, json_body: dict):
    overrides = dict(app.dependency_overrides)
    app.dependency_overrides[get_db] = lambda: session
    app.dependency_overrides[rate_limit] = lambda: None
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post("/mcp", headers=headers or {}, json=json_body)
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(overrides)


async def _post_mcp_raw(session, *, headers: dict[str, str] | None = None, content: str):
    overrides = dict(app.dependency_overrides)
    app.dependency_overrides[get_db] = lambda: session
    app.dependency_overrides[rate_limit] = lambda: None
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post("/mcp", headers=headers or {}, content=content)
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(overrides)


async def _assert_queued_triage(session, outcome: dict, *, reason: str) -> dict:
    assert outcome["reason"] == reason
    triage = outcome["triage"]
    assert triage["status"] == "queued"
    assert triage["event_id"]
    assert triage["idea_id"]
    assert triage["thread_message_id"]
    assert triage["run_id"]

    idea = await session.get(Idea, triage["idea_id"])
    thread_message = await session.get(IdeaThread, triage["thread_message_id"])
    run = await session.get(AgentRunRow, triage["run_id"])
    assert idea is not None
    assert idea.origin == "inbound_signal"
    assert idea.origin_ref == f"inbound_event:{triage['event_id']}"
    assert idea.status == "working"
    assert thread_message is not None
    assert thread_message.idea_id == triage["idea_id"]
    assert thread_message.role == "user"
    assert "An inbound signal needs Illo triage." in thread_message.content
    assert run is not None
    assert run.thread_id == triage["idea_id"]
    assert run.status == "queued"
    assert run.metadata_["producer"] == "inbound"
    assert run.metadata_["inbound_event"]["event_id"] == triage["event_id"]
    return triage


async def _queue_unmatched_webhook_triage(
    session,
    *,
    origin: str,
    issue_key: str,
    idempotency_key: str,
) -> dict:
    await _seed_connection(session)
    response = await _post_webhook(
        session,
        headers={"Authorization": f"Bearer {RAW_TOKEN}"},
        json={
            "origin": origin,
            "payload": {"issue": {"key": issue_key}},
            "idempotency_key": idempotency_key,
        },
    )
    return await _assert_queued_triage(
        session,
        response.json()["ilo_outcome"],
        reason="no_matching_source_policy",
    )


async def _finish_triage_run(
    session,
    triage: dict,
    *,
    status: RunStatus,
    final_answer: str,
    reason: str | None = None,
) -> None:
    store = AsyncAgentRunStore(session)
    run_id = int(triage["run_id"])
    await store.set_status(run_id, RunStatus.STARTING)
    await store.set_status(run_id, RunStatus.RUNNING)
    await store.append_final_answer_once(run_id, final_answer, root_run_id=run_id)
    await store.set_status(run_id, status, reason=reason)


async def test_webhook_requires_authenticated_source_identity(session):
    response = await _post_webhook(
        session,
        json={"origin": "jira.ticket_created", "payload": {"issue": {"key": "PROJ-1"}}},
    )

    assert response.status_code == 401
    assert await session.scalar(select(func.count()).select_from(InboundEventRow)) == 0


async def test_post_webhook_stores_event_even_without_policy(session):
    await _seed_connection(session)

    response = await _post_webhook(
        session,
        headers={"Authorization": f"Bearer {RAW_TOKEN}"},
        json={
            "origin": "jira.ticket_created",
            "payload": {"issue": {"key": "PROJ-1"}},
            "idempotency_key": "jira:PROJ-1:created",
        },
    )

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "review_required"
    assert body["matched_policy_id"] is None
    triage = await _assert_queued_triage(
        session,
        body["ilo_outcome"],
        reason="no_matching_source_policy",
    )

    event = (await session.scalars(select(InboundEventRow))).one()
    assert event.origin == "jira.ticket_created"
    assert event.raw_payload == {"issue": {"key": "PROJ-1"}}
    assert event.connection_id == CONNECTION_ID
    assert event.token_id == TOKEN_ID
    assert event.authority_user_id == USER_ID
    assert event.source_actor["connection_id"] == CONNECTION_ID
    assert triage["event_id"] == str(event.id)


async def test_authenticated_source_traffic_marks_pending_connection_configured(session):
    await _seed_connection(session, status="pending")

    response = await _post_webhook(
        session,
        headers={"Authorization": f"Bearer {RAW_TOKEN}"},
        json={
            "origin": "jira.ticket_created",
            "payload": {"issue": {"key": "PROJ-1"}},
            "idempotency_key": "jira:PROJ-1:created",
        },
    )

    connection = await session.get(ExternalAgentConnectionRow, CONNECTION_ID)
    token = await session.get(ExternalAgentConnectionTokenRow, TOKEN_ID)
    assert response.status_code == 202
    assert connection.status == "configured"
    assert connection.last_seen_at is not None
    assert connection.last_error is None
    assert token.last_used_at is not None


async def test_webhook_and_mcp_signals_share_inbound_event_path(session):
    await _seed_connection(session)

    webhook_response = await _post_webhook(
        session,
        headers={"Authorization": f"Bearer {RAW_TOKEN}"},
        json={
            "origin": "jira.ticket_created",
            "payload": {"issue": {"key": "PROJ-1"}},
            "idempotency_key": "jira:PROJ-1:created",
        },
    )
    mcp_response = await _post_mcp(
        session,
        headers={"Authorization": f"Bearer {RAW_TOKEN}"},
        json_body={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "illo_submit_signal",
                "arguments": {
                    "summary": "Implemented inbound coordination.",
                    "origin": "codex.progress",
                    "source_tool": "codex",
                    "repo": "illospace-project",
                    "branch": "codex/inbound-coordination-e2e",
                    "task_title": "Unify inbound PRs",
                    "files_touched": ["brain/app/api/routers/agent_mcp.py"],
                    "payload": {"tests": "e2e"},
                    "desired_outcome": "team_update",
                    "idempotency_key": "codex:e2e:1",
                    "metadata": {"hook": "post-message"},
                },
            },
        },
    )

    assert webhook_response.status_code == 202
    assert mcp_response.status_code == 200
    mcp_payload = json.loads(mcp_response.json()["result"]["content"][0]["text"])
    assert webhook_response.json()["status"] == "review_required"
    assert mcp_payload["status"] == "review_required"
    assert mcp_payload["matched_policy_id"] is None
    webhook_triage = await _assert_queued_triage(
        session,
        webhook_response.json()["ilo_outcome"],
        reason="no_matching_source_policy",
    )
    mcp_triage = await _assert_queued_triage(
        session,
        mcp_payload["ilo_outcome"],
        reason="no_matching_source_policy",
    )

    events = (await session.scalars(select(InboundEventRow).order_by(InboundEventRow.created_at))).all()
    assert [event.origin for event in events] == ["jira.ticket_created", "codex.progress"]
    assert [event.status for event in events] == ["review_required", "review_required"]
    assert [event.action_type for event in events] == ["ilo_required", "ilo_required"]
    assert all(event.connection_id == CONNECTION_ID for event in events)
    assert all(event.authority_user_id == USER_ID for event in events)
    assert events[0].ingress_context["surface"] == "webhook"
    assert events[1].ingress_context["surface"] == "mcp_personal_tool"
    assert events[1].normalized_payload["summary"] == "Implemented inbound coordination."
    assert events[1].normalized_payload["hints"]["source_tool"] == "codex"
    assert events[1].normalized_payload["hints"]["files_touched"] == [
        "brain/app/api/routers/agent_mcp.py"
    ]
    assert [webhook_triage["event_id"], mcp_triage["event_id"]] == [str(event.id) for event in events]

    receipts = (
        await session.scalars(
            select(InboundDecisionReceiptRow).order_by(InboundDecisionReceiptRow.created_at)
        )
    ).all()
    assert len(receipts) == 2
    assert [receipt.event_id for receipt in receipts] == [str(event.id) for event in events]
    assert all(receipt.outcome["reason"] == "no_matching_source_policy" for receipt in receipts)
    assert all(receipt.outcome["triage"]["status"] == "queued" for receipt in receipts)
    assert all(receipt.target["kind"] == "cortex_idea" for receipt in receipts)
    assert all(receipt.tool_use["type"] == "illo_triage" for receipt in receipts)


async def test_malformed_mcp_json_fails_without_creating_event(session):
    response = await _post_mcp_raw(
        session,
        headers={
            "Authorization": f"Bearer {RAW_TOKEN}",
            "Content-Type": "application/json",
        },
        content='{"jsonrpc":"2.0","id":2}{"extra":true}',
    )

    assert response.status_code == 400
    assert response.json() == {
        "jsonrpc": "2.0",
        "id": None,
        "error": {"code": -32600, "message": "Invalid Request"},
    }
    assert await session.scalar(select(func.count()).select_from(InboundEventRow)) == 0


async def test_mcp_codex_progress_signal_satisfies_default_checkpoint_policy(session):
    await _seed_connection(session)
    policy = await inbound.create_source_policy(
        session,
        org_id=ORG_ID,
        connection_id=CONNECTION_ID,
        name="Codex progress checkpoints",
        origin_patterns=["codex.progress"],
        schema_config={"required_paths": ["payload.checkpoint", "desired_outcome"]},
    )

    response = await _post_mcp(
        session,
        headers={"Authorization": f"Bearer {RAW_TOKEN}"},
        json_body={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "illo_submit_signal",
                "arguments": {
                    "summary": "Implemented inbound follow-up fixes.",
                    "source_tool": "codex",
                    "repo": "illospace-project",
                    "branch": "codex/inbound-followups",
                    "task_title": "E2E followups",
                    "files_touched": ["brain/app/api/routers/agent_mcp.py"],
                    "desired_outcome": "team_update",
                    "idempotency_key": "codex:e2e-followups:1",
                },
            },
        },
    )

    body = json.loads(response.json()["result"]["content"][0]["text"])
    event = (await session.scalars(select(InboundEventRow))).one()
    assert response.status_code == 200
    assert body["status"] == "review_required"
    assert body["matched_policy_id"] == str(policy.id)
    assert body["error"] is None
    assert event.status == "review_required"
    assert event.raw_payload["checkpoint"]["summary"] == "Implemented inbound follow-up fixes."
    assert event.normalized_payload["desired_outcome"] == "team_update"


async def test_mcp_codex_progress_signal_requires_non_empty_desired_outcome(session):
    await _seed_connection(session)
    await inbound.create_source_policy(
        session,
        org_id=ORG_ID,
        connection_id=CONNECTION_ID,
        name="Codex progress checkpoints",
        origin_patterns=["codex.progress"],
        schema_config={"required_paths": ["payload.checkpoint", "desired_outcome"]},
    )

    response = await _post_mcp(
        session,
        headers={"Authorization": f"Bearer {RAW_TOKEN}"},
        json_body={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "illo_submit_signal",
                "arguments": {
                    "summary": "Implemented inbound follow-up fixes.",
                    "source_tool": "codex",
                    "repo": "illospace-project",
                    "branch": "codex/inbound-followups",
                    "task_title": "E2E followups",
                    "files_touched": ["brain/app/api/routers/agent_mcp.py"],
                    "idempotency_key": "codex:e2e-followups:missing-outcome",
                },
            },
        },
    )

    body = json.loads(response.json()["result"]["content"][0]["text"])
    event = (await session.scalars(select(InboundEventRow))).one()
    assert response.status_code == 200
    assert body["status"] == "quarantined"
    assert "desired_outcome" in body["error"]
    assert event.status == "quarantined"


async def test_inbound_triage_receipt_reconciles_when_illo_run_completes(session):
    final_answer = "Illo reviewed the inbound signal and decided no workspace mutation is needed."
    triage = await _queue_unmatched_webhook_triage(
        session,
        origin="jira.ticket_created",
        issue_key="PROJ-1",
        idempotency_key="jira:PROJ-1:created",
    )
    event = (await session.scalars(select(InboundEventRow))).one()
    event.error = "schema validation required Illo triage"
    await session.flush()
    await _finish_triage_run(session, triage, status=RunStatus.COMPLETED, final_answer=final_answer)

    event = (await session.scalars(select(InboundEventRow))).one()
    receipt = (await session.scalars(select(InboundDecisionReceiptRow))).one()

    assert event.status == "processed"
    assert event.action_result["triage"]["status"] == "completed"
    assert event.action_result["triage"]["result"] == {
        "status": "completed",
        "final_answer": final_answer,
    }
    assert event.error is None
    assert event.processed_at is not None
    assert receipt.status == "processed"
    assert receipt.outcome["triage"]["status"] == "completed"
    assert receipt.outcome["triage"]["run_status"] == "completed"
    assert receipt.tool_use["status"] == "completed"
    assert receipt.tool_use["type"] == "illo_triage"
    assert receipt.tool_use["attribution"]["tags"] == ["no_workspace_change"]
    assert event.action_result["triage"]["attribution"]["summary"] == (
        "Illo resolved the signal without a workspace tool action."
    )


async def test_inbound_triage_receipt_records_tool_attribution(session):
    triage = await _queue_unmatched_webhook_triage(
        session,
        origin="jira.ticket_created",
        issue_key="PROJ-3",
        idempotency_key="jira:PROJ-3:created",
    )
    store = AsyncAgentRunStore(session)
    run_id = int(triage["run_id"])
    await store.set_status(run_id, RunStatus.STARTING)
    await store.set_status(run_id, RunStatus.RUNNING)
    read_event = await store.append_event(
        run_event(
            run_id,
            "run.tool_completed",
            {
                "tool_name": "manage_inbound",
                "args": {"action": "get_event"},
                "result": json.dumps(
                    {
                        "event_id": "999",
                        "status": "review_required",
                    }
                ),
            },
            root_run_id=run_id,
        )
    )
    tool_event = await store.append_event(
        run_event(
            run_id,
            "run.tool_completed",
            {
                "tool_name": "manage_domain",
                "args": {"action": "create_record"},
                "result": json.dumps(
                    {
                        "operation": "created",
                        "record": {"id": 220, "domain_id": 11},
                    }
                ),
            },
            root_run_id=run_id,
        )
    )
    await store.append_final_answer_once(run_id, "Created the projected ticket record.", root_run_id=run_id)
    await store.set_status(run_id, RunStatus.COMPLETED)

    event = (await session.scalars(select(InboundEventRow))).one()
    receipt = (await session.scalars(select(InboundDecisionReceiptRow))).one()
    attribution = receipt.tool_use["attribution"]
    assert attribution == event.action_result["triage"]["attribution"]
    assert attribution["summary"] == "Illo created domain_record, domain using manage_domain."
    assert attribution["tool_names"] == ["manage_inbound", "manage_domain"]
    assert "domain_management" in attribution["tags"]
    assert "created" in attribution["tags"]
    assert "inspected" in attribution["tags"]
    assert attribution["run_event_ids"] == [read_event.id, tool_event.id]
    assert {"kind": "inbound_event", "id": "999", "source": "manage_inbound"} in attribution["target_refs"]
    assert {"kind": "domain_record", "id": "220", "source": "manage_domain"} in attribution["target_refs"]
    assert {"kind": "domain", "id": "11", "source": "manage_domain"} in attribution["target_refs"]
    assert {"kind": "inbound_event", "id": "999", "source": "manage_inbound"} not in attribution["mutated_target_refs"]
    assert {"kind": "domain_record", "id": "220", "source": "manage_domain"} in attribution["mutated_target_refs"]
    assert {"kind": "domain", "id": "11", "source": "manage_domain"} in attribution["mutated_target_refs"]

    source_card = await inbound_admin.get_source_card(
        session,
        org_id=ORG_ID,
        connection_id=CONNECTION_ID,
    )
    observed = source_card["source_card"]["traffic"]["observed_outcomes"]
    assert observed["event_count_sampled"] == 1
    assert {"value": "manage_domain", "count": 1} in observed["tool_names"]
    assert {"value": "domain_management", "count": 1} in observed["common_tags"]
    assert observed["by_origin"][0]["summaries"] == [attribution["summary"]]


async def test_inbound_triage_receipt_reconciles_when_illo_run_fails(session):
    final_answer = "Illo triage failed before it could decide on the signal."
    triage = await _queue_unmatched_webhook_triage(
        session,
        origin="jira.ticket_updated",
        issue_key="PROJ-2",
        idempotency_key="jira:PROJ-2:updated",
    )
    await _finish_triage_run(
        session,
        triage,
        status=RunStatus.FAILED,
        final_answer=final_answer,
        reason="triage worker crashed",
    )

    event = (await session.scalars(select(InboundEventRow))).one()
    receipt = (await session.scalars(select(InboundDecisionReceiptRow))).one()

    assert event.status == "failed"
    assert event.error == final_answer
    assert event.action_result["triage"]["status"] == "failed"
    assert receipt.status == "failed"
    assert receipt.outcome["triage"]["result"] == {
        "status": "failed",
        "final_answer": final_answer,
    }
    assert receipt.tool_use["status"] == "failed"


async def test_mcp_signal_rejects_overlong_origin_before_inbound_processing(session):
    await _seed_connection(session)

    response = await _post_mcp(
        session,
        headers={"Authorization": f"Bearer {RAW_TOKEN}"},
        json_body={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "illo_submit_signal",
                "arguments": {
                    "summary": "This should not reach persistence.",
                    "origin": "x" * 241,
                },
            },
        },
    )

    assert response.status_code == 200
    result = response.json()["result"]
    assert result["isError"] is True
    assert "origin must be 240 characters or fewer" in result["content"][0]["text"]
    assert await session.scalar(select(func.count()).select_from(InboundEventRow)) == 0


async def test_webhook_rejects_overlong_idempotency_header(session):
    await _seed_connection(session)

    response = await _post_webhook(
        session,
        headers={
            "Authorization": f"Bearer {RAW_TOKEN}",
            "X-Illo-Idempotency-Key": "x" * 161,
        },
        json={"origin": "jira.ticket_created", "payload": {"issue": {"key": "PROJ-1"}}},
    )

    assert response.status_code == 422
    assert await session.scalar(select(func.count()).select_from(InboundEventRow)) == 0


async def test_webhook_provider_delivery_header_becomes_idempotency_key(session):
    await _seed_connection(session)

    first = await _post_webhook(
        session,
        headers={
            "Authorization": f"Bearer {RAW_TOKEN}",
            "X-GitHub-Delivery": "delivery-123",
        },
        json={
            "origin": "github.issue_created",
            "payload": {"issue": {"key": "GH-1"}},
        },
    )
    replay = await _post_webhook(
        session,
        headers={
            "Authorization": f"Bearer {RAW_TOKEN}",
            "X-GitHub-Delivery": "delivery-123",
        },
        json={
            "origin": "github.issue_created",
            "payload": {"issue": {"key": "GH-1"}},
        },
    )

    first_body = first.json()
    replay_body = replay.json()
    event = (await session.scalars(select(InboundEventRow))).one()
    assert first.status_code == 202
    assert replay.status_code == 202
    assert first_body["idempotent_replay"] is False
    assert replay_body["idempotent_replay"] is True
    assert replay_body["event_id"] == first_body["event_id"]
    assert event.idempotency_key == "x-github-delivery:delivery-123"
    assert event.ingress_context["provider_delivery"] == {
        "header": "x-github-delivery",
        "value": "delivery-123",
    }
    assert await session.scalar(select(func.count()).select_from(InboundEventRow)) == 1


async def test_explicit_webhook_idempotency_header_wins_over_provider_delivery(session):
    await _seed_connection(session)

    response = await _post_webhook(
        session,
        headers={
            "Authorization": f"Bearer {RAW_TOKEN}",
            "X-Illo-Idempotency-Key": "explicit-key",
            "X-GitHub-Delivery": "delivery-ignored",
        },
        json={
            "origin": "github.issue_created",
            "payload": {"issue": {"key": "GH-2"}},
        },
    )

    event = (await session.scalars(select(InboundEventRow))).one()
    assert response.status_code == 202
    assert event.idempotency_key == "explicit-key"
    assert event.ingress_context["provider_delivery"] == {
        "header": "x-github-delivery",
        "value": "delivery-ignored",
    }


async def test_webhook_rejects_empty_kind_before_inbound_processing(session):
    await _seed_connection(session)

    response = await _post_webhook(
        session,
        headers={"Authorization": f"Bearer {RAW_TOKEN}"},
        json={
            "origin": "jira.ticket_created",
            "kind": " ",
            "payload": {"issue": {"key": "PROJ-1"}},
        },
    )

    assert response.status_code == 422
    assert await session.scalar(select(func.count()).select_from(InboundEventRow)) == 0


async def test_idempotent_insert_integrity_error_returns_existing_replay(session, monkeypatch):
    principal = await _seed_connection(session)
    first = await inbound.submit_inbound_envelope(
        session,
        connection=principal,
        envelope={
            "origin": "jira.ticket_created",
            "payload": {"issue": {"key": "PROJ-1"}},
            "idempotency_key": "jira:PROJ-1:create",
        },
    )

    original_find = inbound._find_idempotent_event
    calls = 0

    async def miss_existing_event_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return None
        return await original_find(*args, **kwargs)

    monkeypatch.setattr(inbound, "_find_idempotent_event", miss_existing_event_once)

    replay = await inbound.submit_inbound_envelope(
        session,
        connection=principal,
        envelope={
            "origin": "jira.ticket_created",
            "payload": {"issue": {"key": "PROJ-1"}},
            "idempotency_key": "jira:PROJ-1:create",
        },
    )

    assert replay["idempotent_replay"] is True
    assert replay["event_id"] == first["event_id"]
    assert await session.scalar(select(func.count()).select_from(InboundEventRow)) == 1


async def test_source_policy_matching_uses_connection_scope_and_priority(session):
    principal = await _seed_connection(session)
    other_connection_id = "55555555-5555-4555-8555-555555555555"
    await _seed_connection(
        session,
        connection_id=other_connection_id,
        token_id="66666666-6666-4666-8666-666666666666",
        raw_token="illo_conn_other",
    )
    generic = await inbound.create_source_policy(
        session,
        org_id=ORG_ID,
        connection_id=CONNECTION_ID,
        name="Generic fallback",
        origin_patterns=["*"],
        priority=50,
    )
    specific = await inbound.create_source_policy(
        session,
        org_id=ORG_ID,
        connection_id=CONNECTION_ID,
        name="Jira ticket events",
        origin_patterns=["jira.ticket_*"],
        priority=10,
    )
    await inbound.create_source_policy(
        session,
        org_id=ORG_ID,
        connection_id=other_connection_id,
        name="Other connection should not match",
        origin_patterns=["jira.ticket_*"],
        priority=1,
    )

    result = await inbound.submit_inbound_envelope(
        session,
        connection=principal,
        envelope={
            "origin": "jira.ticket_updated",
            "payload": {"issue": {"key": "PROJ-1"}},
            "idempotency_key": "jira:PROJ-1:updated",
        },
        ingress_context={"surface": "test"},
    )

    assert result["status"] == "review_required"
    assert result["matched_policy_id"] == str(specific.id)
    assert result["matched_policy_id"] != str(generic.id)
    await _assert_queued_triage(
        session,
        result["ilo_outcome"],
        reason="matched_policy_without_projection",
    )


async def test_payload_cannot_spoof_another_connections_source_policy(session):
    principal = await _seed_connection(session)
    other_connection_id = "55555555-5555-4555-8555-555555555555"
    await _seed_connection(
        session,
        connection_id=other_connection_id,
        token_id="66666666-6666-4666-8666-666666666666",
        raw_token="illo_conn_other",
    )
    other_policy = await inbound.create_source_policy(
        session,
        org_id=ORG_ID,
        connection_id=other_connection_id,
        name="Other connection Jira policy",
        origin_patterns=["jira.ticket_*"],
        priority=1,
    )

    result = await inbound.submit_inbound_envelope(
        session,
        connection=principal,
        envelope={
            "origin": "jira.ticket_created",
            "payload": {
                "source": {"connection_id": other_connection_id},
                "issue": {"key": "PROJ-SPOOF"},
            },
            "idempotency_key": "jira:PROJ-SPOOF:create",
        },
        ingress_context={"surface": "test"},
    )

    assert result["status"] == "review_required"
    assert result["matched_policy_id"] is None
    assert result["matched_policy_id"] != str(other_policy.id)
    event = (await session.scalars(select(InboundEventRow))).one()
    assert event.connection_id == CONNECTION_ID
    assert event.raw_payload["source"]["connection_id"] == other_connection_id
    await _assert_queued_triage(
        session,
        result["ilo_outcome"],
        reason="no_matching_source_policy",
    )


async def test_domain_projection_creates_updates_and_dedupes_domain_records(session):
    principal = await _seed_connection(session)
    domain_service = AsyncDomainService(session)
    domain = await domain_service.create_domain(
        ORG_ID,
        name="Jira Tickets",
        objects=[
            {
                "key": "ticket",
                "title_field": "summary",
                "fields": [
                    {"key": "external_id", "field_type": "text", "required": True},
                    {"key": "key", "field_type": "text", "required": True},
                    {"key": "summary", "field_type": "text", "required": True},
                    {"key": "status", "field_type": "enum", "options": ["open", "done"]},
                ],
            }
        ],
        actor_id=USER_ID,
    )
    policy = await inbound.create_source_policy(
        session,
        org_id=ORG_ID,
        connection_id=CONNECTION_ID,
        name="Jira tickets to Domain",
        origin_patterns=["jira.ticket_*"],
        priority=10,
        allowed_actions=["domain_projection.upsert"],
        schema_config={"required_paths": ["payload.issue.key", "payload.issue.fields.summary"]},
    )
    projection = await inbound.create_domain_projection(
        session,
        org_id=ORG_ID,
        connection_id=CONNECTION_ID,
        policy_id=str(policy.id),
        domain_id=domain.id,
        object_key="ticket",
        external_id_path="payload.issue.key",
        external_id_field="external_id",
        field_mapping={
            "key": "payload.issue.key",
            "summary": "payload.issue.fields.summary",
            "status": "payload.issue.fields.status",
        },
        title_path="payload.issue.fields.summary",
    )

    created = await inbound.submit_inbound_envelope(
        session,
        connection=principal,
        envelope={
            "origin": "jira.ticket_created",
            "payload": {
                "issue": {
                    "key": "PROJ-7",
                    "fields": {"summary": "Payment failure", "status": "open"},
                }
            },
            "idempotency_key": "jira:PROJ-7:create",
        },
    )
    assert created["status"] == "processed"
    assert created["matched_policy_id"] == str(policy.id)
    assert created["domain_projection_id"] == str(projection.id)
    record_id = created["ilo_outcome"]["record_id"]

    duplicate = await inbound.submit_inbound_envelope(
        session,
        connection=principal,
        envelope={
            "origin": "jira.ticket_created",
            "payload": {
                "issue": {
                    "key": "PROJ-7",
                    "fields": {"summary": "Changed but duplicate", "status": "done"},
                }
            },
            "idempotency_key": "jira:PROJ-7:create",
        },
    )
    assert duplicate["idempotent_replay"] is True
    assert duplicate["event_id"] == created["event_id"]

    updated = await inbound.submit_inbound_envelope(
        session,
        connection=principal,
        envelope={
            "origin": "jira.ticket_updated",
            "payload": {
                "issue": {
                    "key": "PROJ-7",
                    "fields": {"summary": "Payment restored", "status": "done"},
                }
            },
            "idempotency_key": "jira:PROJ-7:update",
        },
    )
    assert updated["status"] == "processed"
    assert updated["ilo_outcome"]["operation"] == "updated"
    assert updated["ilo_outcome"]["record_id"] == record_id

    records = await domain_service.list_records(ORG_ID, domain.id, object_key="ticket")
    assert len(records) == 1
    record = records[0]
    assert record.id == record_id
    assert record.version == 2
    assert record.data == {
        "external_id": "PROJ-7",
        "key": "PROJ-7",
        "summary": "Payment restored",
        "status": "done",
    }
    assert await session.scalar(select(func.count()).select_from(InboundEventRow)) == 2
    assert await session.scalar(select(func.count()).select_from(InboundDecisionReceiptRow)) == 2
    projection_key = (await session.scalars(select(InboundDomainProjectionKeyRow))).one()
    assert projection_key.projection_id == str(projection.id)
    assert projection_key.external_id == "PROJ-7"
    assert projection_key.record_id == record_id
    domain_events = await domain_service.list_events(ORG_ID, domain.id, record_id=record_id)
    assert [event.event_type for event in domain_events] == ["record.updated", "record.created"]
    assert all(str(event.reason).startswith("inbound_event:") for event in domain_events)


async def test_domain_projection_requires_explicit_policy_action_permission(session):
    principal = await _seed_connection(session)
    domain_service = AsyncDomainService(session)
    domain = await domain_service.create_domain(
        ORG_ID,
        name="Guarded Jira Tickets",
        objects=[
            {
                "key": "ticket",
                "title_field": "summary",
                "fields": [
                    {"key": "external_id", "field_type": "text", "required": True},
                    {"key": "summary", "field_type": "text", "required": True},
                ],
            }
        ],
        actor_id=USER_ID,
    )
    policy = await inbound.create_source_policy(
        session,
        org_id=ORG_ID,
        connection_id=CONNECTION_ID,
        name="Jira tickets missing action permission",
        origin_patterns=["jira.ticket_*"],
        priority=10,
    )
    await inbound.create_domain_projection(
        session,
        org_id=ORG_ID,
        connection_id=CONNECTION_ID,
        policy_id=str(policy.id),
        domain_id=domain.id,
        object_key="ticket",
        external_id_path="payload.issue.key",
        external_id_field="external_id",
        field_mapping={"summary": "payload.issue.fields.summary"},
        title_path="payload.issue.fields.summary",
    )

    result = await inbound.submit_inbound_envelope(
        session,
        connection=principal,
        envelope={
            "origin": "jira.ticket_created",
            "payload": {"issue": {"key": "PROJ-8", "fields": {"summary": "Needs review"}}},
            "idempotency_key": "jira:PROJ-8:create",
        },
    )

    assert result["status"] == "review_required"
    await _assert_queued_triage(
        session,
        result["ilo_outcome"],
        reason="domain_projection_not_allowed",
    )
    assert await session.scalar(select(func.count()).select_from(DomainRecord)) == 0


async def test_domain_projection_finds_existing_record_beyond_default_list_cap(session):
    principal = await _seed_connection(session)
    domain_service = AsyncDomainService(session)
    domain = await domain_service.create_domain(
        ORG_ID,
        name="Large Jira Tickets",
        objects=[
            {
                "key": "ticket",
                "title_field": "summary",
                "fields": [
                    {"key": "external_id", "field_type": "text", "required": True},
                    {"key": "key", "field_type": "text", "required": True},
                    {"key": "summary", "field_type": "text", "required": True},
                    {"key": "status", "field_type": "enum", "options": ["open", "done"]},
                ],
            }
        ],
        actor_id=USER_ID,
    )
    obj = await domain_service.get_object_type(domain.id, "ticket")
    base_time = datetime(2026, 1, 1, tzinfo=timezone.utc)
    records = []
    for index in range(501):
        external_id = "PROJ-OLD" if index == 0 else f"PROJ-{index}"
        records.append(
            DomainRecord(
                org_id=ORG_ID,
                domain_id=domain.id,
                object_type_id=obj.id,
                title=f"Seeded ticket {index}",
                data={
                    "external_id": external_id,
                    "key": external_id,
                    "summary": f"Seeded ticket {index}",
                    "status": "open",
                },
                search_text=f"seeded ticket {index} {external_id.lower()}",
                created_by_user_id=USER_ID,
                updated_by_user_id=USER_ID,
                created_at=base_time + timedelta(seconds=index),
                updated_at=base_time + timedelta(seconds=index),
            )
        )
    session.add_all(records)
    await session.flush()

    policy = await inbound.create_source_policy(
        session,
        org_id=ORG_ID,
        connection_id=CONNECTION_ID,
        name="Large Jira tickets to Domain",
        origin_patterns=["jira.ticket_*"],
        priority=10,
        allowed_actions=["domain_projection.upsert"],
    )
    await inbound.create_domain_projection(
        session,
        org_id=ORG_ID,
        connection_id=CONNECTION_ID,
        policy_id=str(policy.id),
        domain_id=domain.id,
        object_key="ticket",
        external_id_path="payload.issue.key",
        external_id_field="external_id",
        field_mapping={
            "key": "payload.issue.key",
            "summary": "payload.issue.fields.summary",
            "status": "payload.issue.fields.status",
        },
        title_path="payload.issue.fields.summary",
    )

    result = await inbound.submit_inbound_envelope(
        session,
        connection=principal,
        envelope={
            "origin": "jira.ticket_updated",
            "payload": {
                "issue": {
                    "key": "PROJ-OLD",
                    "fields": {"summary": "Found beyond cap", "status": "done"},
                }
            },
            "idempotency_key": "jira:PROJ-OLD:update",
        },
    )

    assert result["status"] == "processed"
    assert result["ilo_outcome"]["operation"] == "updated"
    assert await session.scalar(select(func.count()).select_from(DomainRecord)) == 501
    projection_key = (await session.scalars(select(InboundDomainProjectionKeyRow))).one()
    assert projection_key.external_id == "PROJ-OLD"
    assert projection_key.record_id == records[0].id
    oldest_record = await session.get(DomainRecord, records[0].id)
    assert oldest_record is not None
    assert oldest_record.data["summary"] == "Found beyond cap"
