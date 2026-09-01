from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

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
from brain.systems.runs.failure_diagnostic import RunFailureStage
from brain.systems.runs.failures import RunFailureCategory, UPSTREAM_FAILED_RUN_MESSAGE
from brain.systems.runs.status import RunStatus
from brain.systems.runs.store import AsyncAgentRunStore
from brain.systems.user_domains.service import AsyncDomainService


pytestmark = pytest.mark.asyncio

ORG_ID = "11111111-1111-4111-8111-111111111111"
USER_ID = "22222222-2222-4222-8222-222222222222"
CONNECTION_ID = "33333333-3333-4333-8333-333333333333"
TOKEN_ID = "44444444-4444-4444-8444-444444444444"
RAW_TOKEN = "illo_conn_test_webhook_token"
ROLLBAR_ITEM_2328_URL = "https://app.rollbar.com/a/uwear/fix/item/Uwear-API/2328"
ROLLBAR_ITEM_2328_TENTH_ERROR = (
    f"<{ROLLBAR_ITEM_2328_URL}|#2328 10th error: "
    "ValidationError: 1 validation error for GenerationPlan\n"
    "Value error, Generation clothing snapshot identity is inconsistent>"
)
ROLLBAR_ITEM_2328_RATE_ALERT = (
    f"<{ROLLBAR_ITEM_2328_URL}|#2328 10 occurrences in 5 minutes: "
    "ValidationError: 1 validation error for GenerationPlan\n"
    "Value error, Generation clothing snapshot identity is inconsistent>"
)


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
    # Flush so the org/user rows exist before the connection's FKs reference
    # them — Postgres enforces the ordering; SQLite's no-FK default hides it.
    await session.flush()
    connection = ExternalAgentConnectionRow(
        id=connection_id,
        org_id=ORG_ID,
        owner_user_id=USER_ID,
        display_name="Jira webhook",
        agent_kind="jira",
        transport="webhook",
        status=status,
        remote_agent_card={},
        capabilities={
            "illo_submit": True,
            "illo_read": True,
            "illo_act": True,
            "illo_get_result": True,
        },
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


def _slack_channel_alert_envelope(text: str, *, idempotency_key: str) -> dict:
    return {
        "origin": "slack.channel_message",
        "payload": {"text": text},
        "summary": text,
        "idempotency_key": idempotency_key,
    }


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


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            ROLLBAR_ITEM_2328_URL,
            "rollbar:Uwear-API:2328",
        ),
        (
            ROLLBAR_ITEM_2328_TENTH_ERROR,
            "rollbar:Uwear-API:2328",
        ),
        (
            "https://app.posthog.com/project/1/alerts/2328",
            None,
        ),
    ],
)
async def test_alert_signature_extraction_is_provider_specific(text, expected):
    assert inbound.extract_alert_signature(text) == expected


async def test_same_rollbar_signature_deduplicates_channel_triage_run(session, caplog):
    principal = await _seed_connection(session)
    caplog.set_level(logging.INFO, logger="brain.systems.inbound.service")

    first = await inbound.submit_inbound_envelope(
        session,
        connection=principal,
        envelope=_slack_channel_alert_envelope(
            ROLLBAR_ITEM_2328_TENTH_ERROR,
            idempotency_key="slack:rollbar:2328:tenth",
        ),
    )
    second = await inbound.submit_inbound_envelope(
        session,
        connection=principal,
        envelope=_slack_channel_alert_envelope(
            ROLLBAR_ITEM_2328_RATE_ALERT,
            idempotency_key="slack:rollbar:2328:rate",
        ),
    )

    assert first["status"] == "review_required"
    assert second["status"] == "processed"
    assert second["ilo_outcome"] == {
        "reason": "alert_signature_deduplicated",
        "deduplicated": True,
        "alert_signature": "rollbar:Uwear-API:2328",
        "original_event_id": first["event_id"],
    }
    assert await session.scalar(select(func.count()).select_from(AgentRunRow)) == 1
    assert await session.scalar(select(func.count()).select_from(InboundEventRow)) == 2
    first_event = await session.get(InboundEventRow, first["event_id"])
    second_event = await session.get(InboundEventRow, second["event_id"])
    assert first_event is not None
    assert second_event is not None
    assert first_event.envelope["alert_signature"] == "rollbar:Uwear-API:2328"
    assert second_event.envelope["alert_signature"] == "rollbar:Uwear-API:2328"
    assert second_event.action_type == "alert_signature.deduplicated"
    assert second_event.action_result["original_event_id"] == first["event_id"]
    assert (
        await session.scalar(select(func.count()).select_from(InboundDecisionReceiptRow))
        == 2
    )
    dedup_logs = [
        record
        for record in caplog.records
        if record.getMessage() == "inbound_alert_signature_deduplicated"
    ]
    assert len(dedup_logs) == 1
    assert dedup_logs[0].alert_signature == "rollbar:Uwear-API:2328"
    assert dedup_logs[0].original_event_id == first["event_id"]


async def test_different_rollbar_items_queue_distinct_triage_runs(session):
    principal = await _seed_connection(session)
    other_item = ROLLBAR_ITEM_2328_RATE_ALERT.replace(
        "/2328|#2328",
        "/2329|#2329",
    )

    for index, text in enumerate((ROLLBAR_ITEM_2328_TENTH_ERROR, other_item)):
        await inbound.submit_inbound_envelope(
            session,
            connection=principal,
            envelope=_slack_channel_alert_envelope(
                text,
                idempotency_key=f"slack:rollbar:different:{index}",
            ),
        )

    assert await session.scalar(select(func.count()).select_from(AgentRunRow)) == 2


async def test_rollbar_signature_outside_window_queues_new_triage_run(session):
    principal = await _seed_connection(session)
    first = await inbound.submit_inbound_envelope(
        session,
        connection=principal,
        envelope=_slack_channel_alert_envelope(
            ROLLBAR_ITEM_2328_TENTH_ERROR,
            idempotency_key="slack:rollbar:outside:first",
        ),
    )
    first_event = await session.get(InboundEventRow, first["event_id"])
    assert first_event is not None
    first_event.created_at = (
        inbound.utcnow()
        - inbound.ALERT_SIGNATURE_DEDUP_WINDOW
        - timedelta(seconds=1)
    )
    await session.flush()

    second = await inbound.submit_inbound_envelope(
        session,
        connection=principal,
        envelope=_slack_channel_alert_envelope(
            ROLLBAR_ITEM_2328_RATE_ALERT,
            idempotency_key="slack:rollbar:outside:second",
        ),
    )

    assert second["status"] == "review_required"
    assert await session.scalar(select(func.count()).select_from(AgentRunRow)) == 2


async def test_unsigned_channel_alerts_preserve_existing_triage_behavior(session):
    principal = await _seed_connection(session)

    for index, text in enumerate(
        (
            "Production error threshold reached.",
            "Production error rate threshold reached.",
        )
    ):
        await inbound.submit_inbound_envelope(
            session,
            connection=principal,
            envelope=_slack_channel_alert_envelope(
                text,
                idempotency_key=f"slack:plain-alert:{index}",
            ),
        )

    events = (await session.scalars(select(InboundEventRow))).all()
    assert len(events) == 2
    assert all("alert_signature" not in event.envelope for event in events)
    assert await session.scalar(select(func.count()).select_from(AgentRunRow)) == 2


async def _assert_queued_submission(session, outcome: dict) -> dict:
    assert outcome["operation"] == "queued"
    assert outcome["message"] == "Submission accepted and queued for Illo handling."
    handling = outcome["handling"]
    assert handling["status"] == "queued"
    assert handling["event_id"]
    assert handling["run_id"]

    run = await session.get(AgentRunRow, handling["run_id"])
    assert run is not None
    assert run.thread_id == f"inbound:{CONNECTION_ID}:{handling['event_id']}"
    assert run.status == "queued"
    assert run.metadata_["producer"] == "inbound"
    assert run.target_ref["kind"] == "inbound_submission"
    assert run.target_ref["event_id"] == handling["event_id"]
    assert run.metadata_["submission"]["message"]
    return handling


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
    failure_category: RunFailureCategory = RunFailureCategory.INTERNAL,
) -> None:
    store = AsyncAgentRunStore(session)
    run_id = int(triage["run_id"])
    await store.set_status(run_id, RunStatus.STARTING)
    await store.set_status(run_id, RunStatus.RUNNING)
    await store.append_final_answer_once(run_id, final_answer, root_run_id=run_id)
    if status == RunStatus.FAILED:
        await store.fail_run(
            run_id,
            category=failure_category,
            stage=RunFailureStage.AGENT_EXECUTION,
            reason=reason,
        )
    else:
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


async def test_webhook_and_mcp_submit_share_inbound_event_path(session, monkeypatch):
    monkeypatch.setenv("ILLO_PUBLIC_URL", "https://illo.example.com/app")
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
                "name": "illo_submit",
                "arguments": {
                    "message": "Ask Illo to review inbound coordination and decide what the team should do.",
                    "origin": "codex.submit",
                    "source_tool": "codex",
                    "repo": "illospace-project",
                    "branch": "codex/inbound-coordination-e2e",
                    "task_title": "Unify inbound PRs",
                    "files_touched": ["brain/app/api/routers/agent_mcp.py"],
                    "parts": [{"type": "text", "text": "Implemented inbound coordination."}],
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
    assert mcp_payload["submission_id"] == mcp_payload["event_id"]
    assert mcp_payload["result_id"] == mcp_payload["event_id"]
    assert mcp_payload["operation"] == "queued"
    webhook_triage = await _assert_queued_triage(
        session,
        webhook_response.json()["ilo_outcome"],
        reason="no_matching_source_policy",
    )
    mcp_handling = await _assert_queued_submission(session, mcp_payload["ilo_outcome"])

    events = (await session.scalars(select(InboundEventRow).order_by(InboundEventRow.created_at))).all()
    assert [event.origin for event in events] == ["jira.ticket_created", "codex.submit"]
    assert [event.status for event in events] == ["review_required", "review_required"]
    assert [event.action_type for event in events] == ["ilo_required", "illo.submit_queued"]
    assert all(event.connection_id == CONNECTION_ID for event in events)
    assert all(event.authority_user_id == USER_ID for event in events)
    assert events[0].ingress_context["surface"] == "webhook"
    assert events[1].ingress_context["surface"] == "mcp_personal_tool"
    assert events[1].normalized_payload["message"] == (
        "Ask Illo to review inbound coordination and decide what the team should do."
    )
    assert events[1].normalized_payload["source"]["source_tool"] == "codex"
    assert events[1].normalized_payload["source"]["files_touched"] == [
        "brain/app/api/routers/agent_mcp.py"
    ]
    assert webhook_triage["event_id"] == str(events[0].id)
    assert mcp_payload["event_id"] == str(events[1].id)

    receipts = (
        await session.scalars(
            select(InboundDecisionReceiptRow).order_by(InboundDecisionReceiptRow.created_at)
        )
    ).all()
    assert len(receipts) == 2
    assert [receipt.event_id for receipt in receipts] == [str(event.id) for event in events]
    assert receipts[0].outcome["reason"] == "no_matching_source_policy"
    assert receipts[0].outcome["triage"]["status"] == "queued"
    assert receipts[1].outcome["operation"] == "queued"
    assert receipts[1].outcome["handling"]["run_id"] == mcp_handling["run_id"]
    assert receipts[0].target["kind"] == "cortex_idea"
    assert receipts[1].target["kind"] == "inbound_submission"
    assert receipts[0].tool_use["type"] == "illo_triage"
    assert receipts[1].tool_use["type"] == "illo_submit"


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


async def test_mcp_submit_ignores_signal_policy_and_queues_illo(session, monkeypatch):
    monkeypatch.setenv("ILLO_PUBLIC_URL", "https://illo.example.com")
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
                "name": "illo_submit",
                "arguments": {
                    "message": "Ask Illo to review inbound follow-up fixes.",
                    "origin": "codex.submit",
                    "source_tool": "codex",
                    "repo": "illospace-project",
                    "branch": "codex/inbound-followups",
                    "task_title": "E2E followups",
                    "files_touched": ["brain/app/api/routers/agent_mcp.py"],
                    "parts": [{"type": "text", "text": "Implemented inbound follow-up fixes."}],
                    "idempotency_key": "codex:e2e-followups:1",
                },
            },
        },
    )

    body = json.loads(response.json()["result"]["content"][0]["text"])
    event = (await session.scalars(select(InboundEventRow))).one()
    assert response.status_code == 200
    assert body["status"] == "review_required"
    assert body["operation"] == "queued"
    assert body["matched_policy_id"] is None
    assert body["error"] is None
    assert await _assert_queued_submission(session, body["ilo_outcome"])
    assert event.status == "review_required"
    assert event.kind == "submission"
    assert event.action_type == "illo.submit_queued"
    assert event.raw_payload["message"] == "Ask Illo to review inbound follow-up fixes."
    assert event.normalized_payload["source"]["source_tool"] == "codex"
    assert await session.scalar(select(func.count()).select_from(Idea)) == 0


async def test_mcp_submit_requires_non_empty_message(session):
    await _seed_connection(session)

    response = await _post_mcp(
        session,
        headers={"Authorization": f"Bearer {RAW_TOKEN}"},
        json_body={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "illo_submit",
                "arguments": {
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

    body = response.json()["result"]
    assert response.status_code == 200
    assert body["isError"] is True
    assert "message" in body["content"][0]["text"]
    assert await session.scalar(select(func.count()).select_from(InboundEventRow)) == 0


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
    raw_diagnostic = "Illo triage failed: provider token=super-secret"
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
        final_answer=raw_diagnostic,
        reason="triage worker crashed",
        failure_category=RunFailureCategory.UPSTREAM,
    )

    event = (await session.scalars(select(InboundEventRow))).one()
    receipt = (await session.scalars(select(InboundDecisionReceiptRow))).one()
    failure = {
        "status": "failed",
        "category": "upstream",
        "message": UPSTREAM_FAILED_RUN_MESSAGE,
    }

    assert event.status == "failed"
    assert event.error == UPSTREAM_FAILED_RUN_MESSAGE
    assert event.action_result["triage"]["status"] == "failed"
    assert event.action_result["triage"]["failure"] == failure
    assert receipt.status == "failed"
    assert receipt.outcome["triage"]["result"] == {
        "status": "failed",
        "failure": failure,
    }
    assert receipt.tool_use["status"] == "failed"
    assert raw_diagnostic not in json.dumps(event.action_result)
    assert raw_diagnostic not in json.dumps(receipt.outcome)

    response = await _post_mcp(
        session,
        headers={"Authorization": f"Bearer {RAW_TOKEN}"},
        json_body={
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "illo_get_result",
                "arguments": {"event_id": str(event.id)},
            },
        },
    )
    result_payload = json.loads(response.json()["result"]["content"][0]["text"])

    assert result_payload["failure"] == failure
    assert result_payload["event"]["failure"] == failure
    assert result_payload["latest_receipt"]["failure"] == failure
    assert raw_diagnostic not in json.dumps(result_payload)


async def test_mcp_submit_rejects_overlong_origin_before_inbound_processing(session):
    await _seed_connection(session)

    response = await _post_mcp(
        session,
        headers={"Authorization": f"Bearer {RAW_TOKEN}"},
        json_body={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "illo_submit",
                "arguments": {
                    "message": "This should not reach persistence.",
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


async def test_identical_body_replay_preserves_satisfied_evidence(session):
    principal = await _seed_connection(session)
    envelope = {
        "origin": "codex.memory",
        "payload": {"message": "Preserve the completed work."},
        "idempotency_key": "codex:memory:identical",
    }
    first = await inbound.submit_inbound_envelope(
        session,
        connection=principal,
        envelope=envelope,
    )
    event = await session.get(InboundEventRow, first["event_id"])
    assert event is not None
    stored_outcome = {
        "event_id": first["event_id"],
        "run_id": "prior-run",
        "final_answer": "The work was preserved.",
        "evidence_status": "satisfied",
        "mutated_target_refs": [{"kind": "thread", "id": "thread-1"}],
    }
    event.action_result = stored_outcome
    await session.flush()

    replay = await inbound.submit_inbound_envelope(
        session,
        connection=principal,
        envelope=envelope,
    )

    assert replay["idempotent_replay"] is True
    assert replay["ilo_outcome"] == stored_outcome
    assert replay["ilo_outcome"]["evidence_status"] == "satisfied"
    assert replay["replay_body_matches"] is True


async def test_colliding_key_with_different_body_marks_replay_mismatch(session):
    principal = await _seed_connection(session)
    first = await inbound.submit_inbound_envelope(
        session,
        connection=principal,
        envelope={
            "origin": "codex.memory",
            "payload": {"message": "Preserve run one."},
            "idempotency_key": "codex:memory:collision",
        },
    )
    event = await session.get(InboundEventRow, first["event_id"])
    assert event is not None
    stored_outcome = {
        "event_id": first["event_id"],
        "run_id": "prior-run",
        "final_answer": "Run one was preserved.",
        "evidence_status": "satisfied",
        "mutated_target_refs": [{"kind": "thread", "id": "thread-1"}],
    }
    event.action_result = stored_outcome
    await session.flush()

    replay = await inbound.submit_inbound_envelope(
        session,
        connection=principal,
        envelope={
            "origin": "codex.memory",
            "payload": {"message": "Preserve run two."},
            "idempotency_key": "codex:memory:collision",
        },
    )

    assert replay["idempotent_replay"] is True
    assert replay["event_id"] == first["event_id"]
    assert replay["replay_body_matches"] is False
    assert replay["submitted_envelope_digest"] != replay["stored_envelope_digest"]
    assert replay["ilo_outcome"] == {
        "evidence_status": "replay_mismatch",
        "mutated_target_refs": [],
        "reason": "idempotency_key_reused_with_different_body",
    }
    assert replay["ilo_outcome"]["evidence_status"] != "satisfied"
    assert replay["stored_ilo_outcome"] == stored_outcome
    assert event.normalized_payload["payload"]["message"] == "Preserve run one."
    assert await session.scalar(select(func.count()).select_from(InboundEventRow)) == 1


async def test_integrity_error_replay_with_different_body_marks_same_mismatch(
    session,
    monkeypatch,
):
    principal = await _seed_connection(session)
    first = await inbound.submit_inbound_envelope(
        session,
        connection=principal,
        envelope={
            "origin": "codex.memory",
            "payload": {"message": "Preserve race winner."},
            "idempotency_key": "codex:memory:race-collision",
        },
    )
    event = await session.get(InboundEventRow, first["event_id"])
    assert event is not None
    stored_outcome = {
        "evidence_status": "satisfied",
        "mutated_target_refs": [{"kind": "thread", "id": "thread-1"}],
    }
    event.action_result = stored_outcome
    await session.flush()

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
            "origin": "codex.memory",
            "payload": {"message": "Preserve race loser."},
            "idempotency_key": "codex:memory:race-collision",
        },
    )

    assert calls == 2
    assert replay["idempotent_replay"] is True
    assert replay["replay_body_matches"] is False
    assert replay["ilo_outcome"]["evidence_status"] == "replay_mismatch"
    assert replay["ilo_outcome"]["mutated_target_refs"] == []
    assert replay["stored_ilo_outcome"] == stored_outcome
    assert replay["submitted_envelope_digest"] != replay["stored_envelope_digest"]
    assert await session.scalar(select(func.count()).select_from(InboundEventRow)) == 1


async def test_submission_envelope_queues_headless_illo_and_replays_idempotently(session):
    principal = await _seed_connection(session)
    envelope = {
        "kind": "submission",
        "origin": "codex.submit",
        "message": "Review the agent trace and decide what the team should do.",
        "parts": [
            {"type": "conversation", "title": "Codex thread", "text": "User asked; Codex replied."},
            {"type": "diff", "title": "Implementation diff", "text": "+ new behavior"},
        ],
        "source": {"source_tool": "codex", "repo": "illospace-project"},
        "correlation": {"thread_id": "77777777-7777-4777-8777-777777777777"},
        "response": {"mode": "webhook"},
        "idempotency_key": "codex:submission:1",
    }

    first = await inbound.submit_inbound_envelope(
        session,
        connection=principal,
        envelope=envelope,
        ingress_context={"surface": "test"},
    )
    replay = await inbound.submit_inbound_envelope(
        session,
        connection=principal,
        envelope=envelope,
        ingress_context={"surface": "test"},
    )

    assert first["status"] == "review_required"
    assert first["matched_policy_id"] is None
    handling = await _assert_queued_submission(session, first["ilo_outcome"])
    assert replay["idempotent_replay"] is True
    assert replay["event_id"] == first["event_id"]
    assert await session.scalar(select(func.count()).select_from(InboundEventRow)) == 1
    assert await session.scalar(select(func.count()).select_from(AgentRunRow)) == 1

    event = await session.get(InboundEventRow, first["event_id"])
    assert event is not None
    assert event.kind == "submission"
    assert event.status == "review_required"
    assert event.action_type == "illo.submit_queued"
    assert event.normalized_payload["message"] == "Review the agent trace and decide what the team should do."
    assert event.normalized_payload["source"]["source_tool"] == "codex"
    assert event.normalized_payload["correlation"]["thread_id"] == "77777777-7777-4777-8777-777777777777"
    assert event.normalized_payload["response"]["mode"] == "webhook"

    receipt = (await session.scalars(select(InboundDecisionReceiptRow))).one()
    assert receipt.event_id == str(event.id)
    assert receipt.target["kind"] == "inbound_submission"
    assert receipt.target["run_id"] == handling["run_id"]
    assert receipt.tool_use["type"] == "illo_submit"


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


@pytest.mark.requires_db
async def test_merged_github_projection_has_no_deploy_side_effect_and_replay_is_inert(
    session, monkeypatch
):
    repo = "uwear-ai/uwear-backend"
    principal = await _seed_connection(session)
    domain_service = AsyncDomainService(session)
    domain = await domain_service.create_domain(
        ORG_ID,
        name="GitHub Tickets",
        objects=[
            {
                "key": "github_ticket",
                "title_field": "external_id",
                "fields": [
                    {"key": "external_id", "field_type": "text", "required": True},
                    {"key": "repo", "field_type": "text"},
                ],
            }
        ],
    )
    policy = await inbound.create_source_policy(
        session,
        org_id=ORG_ID,
        connection_id=CONNECTION_ID,
        name="GitHub events to Domain",
        origin_patterns=["github:*"],
        envelope_kinds=["github_event"],
        allowed_actions=["domain_projection.upsert"],
    )
    await inbound.create_domain_projection(
        session,
        org_id=ORG_ID,
        connection_id=CONNECTION_ID,
        policy_id=str(policy.id),
        domain_id=domain.id,
        object_key="github_ticket",
        external_id_path="hints.node_id",
        external_id_field="external_id",
        field_mapping={"repo": "hints.repo"},
    )
    envelope = {
        "origin": f"github:{repo}",
        "kind": "github_event",
        "payload": {"pull_request": {"number": 859}},
        "hints": {
            "provider": "github",
            "event": "pull_request",
            "action": "closed",
            "repo": repo,
            "number": 859,
            "node_id": "PR_859",
            "merged": True,
            "base_ref": "main",
            "head_ref": "staging",
            "merge_commit_sha": "promotion-sha",
            "merged_at": "2026-07-10T12:00:00Z",
        },
        "idempotency_key": "github:delivery-859",
    }

    first = await inbound.submit_inbound_envelope(
        session,
        connection=principal,
        envelope=envelope,
    )
    replay = await inbound.submit_inbound_envelope(
        session,
        connection=principal,
        envelope=envelope,
    )

    assert first["status"] == "processed"
    assert replay["idempotent_replay"] is True
    records = await domain_service.list_records(
        ORG_ID,
        domain.id,
        object_key="github_ticket",
    )
    assert len(records) == 1
    assert records[0].data == {"external_id": "PR_859", "repo": repo}


@pytest.mark.requires_db
async def test_postgres_merged_main_envelope_has_no_deploy_side_effect(
    db_session, monkeypatch
):
    repo = "uwear-ai/uwear-backend"
    principal = await _seed_connection(db_session)
    domain_service = AsyncDomainService(db_session)
    domain = await domain_service.create_domain(
        ORG_ID,
        name="Deploy Read Integration",
        slug="deploy-read-integration",
        objects=[
            {
                "key": "github_ticket",
                "title_field": "title",
                "fields": [
                    {"key": "external_id", "field_type": "text", "required": True},
                    {"key": "title", "field_type": "text", "required": True},
                    {"key": "repo", "field_type": "text"},
                    {"key": "fix_pr", "field_type": "text"},
                    {"key": "fix_merge_sha", "field_type": "text"},
                    {
                        "key": "status",
                        "field_type": "enum",
                        "options": ["Todo", "In Progress", "Done"],
                    },
                    {"key": "progress_note", "field_type": "long_text"},
                ],
            }
        ],
    )
    record = await domain_service.create_record(
        ORG_ID,
        domain.id,
        "github_ticket",
        title="Production alert",
        data={
            "external_id": "PR_859",
            "title": "Production alert",
            "repo": repo,
            "status": "Todo",
            "progress_note": "fix merged to staging",
            "fix_pr": f"{repo}#905",
            "fix_merge_sha": "f" * 40,
        },
    )
    policy = await inbound.create_source_policy(
        db_session,
        org_id=ORG_ID,
        connection_id=CONNECTION_ID,
        name="GitHub deploy projection",
        origin_patterns=["github:*"],
        envelope_kinds=["github_event"],
        allowed_actions=["domain_projection.upsert"],
    )
    await inbound.create_domain_projection(
        db_session,
        org_id=ORG_ID,
        connection_id=CONNECTION_ID,
        policy_id=str(policy.id),
        domain_id=domain.id,
        object_key="github_ticket",
        external_id_path="hints.node_id",
        external_id_field="external_id",
        field_mapping={"repo": "hints.repo"},
    )
    envelope = {
        "origin": f"github:{repo}",
        "kind": "github_event",
        "payload": {"pull_request": {"number": 859}},
        "hints": {
            "event": "pull_request",
            "action": "closed",
            "repo": repo,
            "number": 859,
            "node_id": "PR_859",
            "merged": True,
            "base_ref": "main",
            "head_ref": "staging",
            "merge_commit_sha": "promotion-sha",
            "merged_at": "2026-07-10T12:00:00Z",
        },
        "idempotency_key": "github:deploy-integration-859",
    }

    first = await inbound.submit_inbound_envelope(
        db_session,
        connection=principal,
        envelope=envelope,
    )
    version_after_first = record.version
    replay = await inbound.submit_inbound_envelope(
        db_session,
        connection=principal,
        envelope=envelope,
    )

    await db_session.refresh(record)
    assert first["status"] == "processed"
    assert replay["idempotent_replay"] is True
    assert record.version == version_after_first
    assert record.data["fix_pr"] == f"{repo}#905"
    assert record.data["fix_merge_sha"] == "f" * 40
    assert "verified" not in record.data
    domain_events = (
        await db_session.scalars(
            select(DomainEvent).where(DomainEvent.record_id == record.id)
        )
    ).all()
    assert all(
        not str(event.reason or "").startswith("deploy")
        for event in domain_events
    )


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
