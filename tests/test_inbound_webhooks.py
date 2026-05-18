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
from brain.platform.db.models.inbound import (
    InboundDecisionReceiptRow,
    InboundDomainProjectionKeyRow,
    InboundDomainProjectionRow,
    InboundEventRow,
    InboundSourcePolicyRow,
)
from brain.systems.external_agents import service as external_agents
from brain.systems.inbound import service as inbound
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

    original = SQLiteDDLCompiler.get_column_default_string

    def patched(self, column, **kw):
        result = original(self, column, **kw)
        if result:
            result = re.sub(r"::jsonb", "", result)
            result = result.replace("NOW()", "CURRENT_TIMESTAMP")
            result = result.replace("TRUE", "1").replace("FALSE", "0")
        return result

    SQLiteDDLCompiler.get_column_default_string = patched


@pytest.fixture
async def session(async_sqlite_session_factory):
    _patch_sqlite_for_pg_types()
    return await async_sqlite_session_factory(
        [
            ExternalAgentConnectionRow.__table__,
            ExternalAgentConnectionTokenRow.__table__,
            Domain.__table__,
            DomainObjectType.__table__,
            DomainFieldDefinition.__table__,
            DomainRelationType.__table__,
            DomainRecord.__table__,
            DomainRelation.__table__,
            DomainEvent.__table__,
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
) -> external_agents.AgentBridgePrincipal:
    connection = ExternalAgentConnectionRow(
        id=connection_id,
        org_id=ORG_ID,
        owner_user_id=USER_ID,
        display_name="Jira webhook",
        agent_kind="jira",
        transport="webhook",
        status="online",
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
    assert body["ilo_outcome"] == {"reason": "no_matching_source_policy"}

    event = (await session.scalars(select(InboundEventRow))).one()
    assert event.origin == "jira.ticket_created"
    assert event.raw_payload == {"issue": {"key": "PROJ-1"}}
    assert event.connection_id == CONNECTION_ID
    assert event.token_id == TOKEN_ID
    assert event.authority_user_id == USER_ID
    assert event.source_actor["connection_id"] == CONNECTION_ID


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
    assert mcp_payload["ilo_outcome"] == {"reason": "no_matching_source_policy"}

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

    receipts = (
        await session.scalars(
            select(InboundDecisionReceiptRow).order_by(InboundDecisionReceiptRow.created_at)
        )
    ).all()
    assert len(receipts) == 2
    assert [receipt.event_id for receipt in receipts] == [str(event.id) for event in events]
    assert all(receipt.outcome == {"reason": "no_matching_source_policy"} for receipt in receipts)


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
    assert result["ilo_outcome"] == {"reason": "domain_projection_not_allowed"}
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
