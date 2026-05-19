from __future__ import annotations

import json
import re
from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.dialects.sqlite.base import SQLiteDDLCompiler, SQLiteTypeCompiler

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
from brain.platform.db.models.org import Org, User
from brain.systems.external_agents import service as external_agents
from brain.systems.inbound import service as inbound_service
from brain.systems.runs.execution_context import AgentExecutionContext, bind_agent_context
from brain.systems.runs.tool_catalog.handlers.inbound import _handle_manage_inbound
from brain.systems.user_domains.service import AsyncDomainService


pytestmark = pytest.mark.asyncio

ORG_ID = "11111111-1111-4111-8111-111111111111"
USER_ID = "22222222-2222-4222-8222-222222222222"


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
            InboundSourcePolicyRow.__table__,
            InboundDomainProjectionRow.__table__,
            InboundDomainProjectionKeyRow.__table__,
            InboundEventRow.__table__,
            InboundDecisionReceiptRow.__table__,
        ]
    )


@pytest.fixture
async def seeded_session(session):
    session.add_all(
        [
            Org(id=ORG_ID, name="Uwear", slug="uwear"),
            User(id=USER_ID, org_id=ORG_ID, name="Reda", email="reda@example.com", approved=True),
        ]
    )
    await session.flush()
    return session


@pytest.fixture
def patch_unit_of_work(monkeypatch, seeded_session):
    class _SessionUnitOfWork:
        async def __aenter__(self):
            self.session = seeded_session
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            if exc_type is None:
                await seeded_session.flush()
            return False

    monkeypatch.setattr(
        "brain.platform.db.repositories.unit_of_work.UnitOfWork",
        _SessionUnitOfWork,
    )


def _decode(result: str) -> dict:
    return json.loads(result)


async def _create_issue_domain(session) -> Domain:
    return await AsyncDomainService(session).create_domain(
        ORG_ID,
        name="Incoming Jira Issues",
        objects=[
            {
                "key": "issue",
                "name": "Issue",
                "title_field": "summary",
                "fields": [
                    {"key": "external_id", "field_type": "text", "required": True},
                    {"key": "summary", "field_type": "text", "required": True},
                    {"key": "status", "field_type": "text"},
                ],
            }
        ],
        actor_id=USER_ID,
    )


async def test_illo_can_configure_connection_policy_projection_and_token(
    seeded_session,
    patch_unit_of_work,
):
    domain = await _create_issue_domain(seeded_session)

    with bind_agent_context(AgentExecutionContext(user_id=USER_ID, org_id=ORG_ID)):
        connection_body = _decode(
            await _handle_manage_inbound(
                action="create_connection",
                display_name="Jira webhook",
                agent_kind="jira",
                transport="webhook",
            )
        )
        connection = connection_body["connection"]
        connection_id = connection["id"]

        token_body = _decode(
            await _handle_manage_inbound(
                action="mint_token",
                connection_id=connection_id,
                token_name="Jira signal token",
            )
        )

        policy_body = _decode(
            await _handle_manage_inbound(
                action="create_policy",
                connection_id=connection_id,
                name="Jira issue events",
                origin_patterns=["jira.issue_*"],
                schema_config={"required_paths": ["payload.issue.key", "payload.issue.summary"]},
            )
        )
        policy = policy_body["policy"]

        projection_body = _decode(
            await _handle_manage_inbound(
                action="create_projection",
                connection_id=connection_id,
                policy_id=policy["id"],
                domain_id=domain.id,
                object_key="issue",
                external_id_path="payload.issue.key",
                external_id_field="external_id",
                field_mapping={
                    "summary": "payload.issue.summary",
                    "status": "payload.issue.status",
                },
                title_path="payload.issue.summary",
            )
        )

        dry_run = _decode(
            await _handle_manage_inbound(
                action="dry_run_match",
                connection_id=connection_id,
                origin="jira.issue_created",
                payload={"issue": {"key": "ILO-7", "summary": "Webhook config"}},
            )
        )["dry_run"]

        listed_tokens = _decode(await _handle_manage_inbound(action="list_tokens", connection_id=connection_id))
        fetched_token = _decode(
            await _handle_manage_inbound(
                action="get_token",
                token_id=token_body["token"]["id"],
            )
        )

    token = token_body["token"]
    assert token["token"].startswith("illo_conn_")
    assert token["scopes"] == [external_agents.SCOPE_SIGNAL_SUBMIT]
    assert token["token_note"]
    assert listed_tokens["tokens"][0]["id"] == token["id"]
    assert "token" not in listed_tokens["tokens"][0]
    assert fetched_token["token"]["id"] == token["id"]
    assert "token" not in fetched_token["token"]

    updated_policy = await seeded_session.get(InboundSourcePolicyRow, policy["id"])
    assert inbound_service.ACTION_DOMAIN_PROJECTION_UPSERT in updated_policy.allowed_actions
    assert projection_body["projection"]["policy_id"] == policy["id"]
    assert dry_run["matched_policy_id"] == policy["id"]
    assert dry_run["domain_projection_id"] == projection_body["projection"]["id"]
    assert dry_run["would_project_domain_record"] is True


async def test_configured_projection_processes_signal_and_illo_can_inspect_logs(
    seeded_session,
    patch_unit_of_work,
):
    domain = await _create_issue_domain(seeded_session)

    with bind_agent_context(AgentExecutionContext(user_id=USER_ID, org_id=ORG_ID)):
        connection = _decode(
            await _handle_manage_inbound(
                action="create_connection",
                display_name="Jira webhook",
                agent_kind="jira",
                transport="webhook",
            )
        )["connection"]
        policy = _decode(
            await _handle_manage_inbound(
                action="create_policy",
                connection_id=connection["id"],
                name="Jira issue events",
                origin_patterns=["jira.issue_*"],
            )
        )["policy"]
        await _handle_manage_inbound(
            action="create_projection",
            connection_id=connection["id"],
            policy_id=policy["id"],
            domain_id=domain.id,
            object_key="issue",
            external_id_path="payload.issue.key",
            external_id_field="external_id",
            field_mapping={"summary": "payload.issue.summary"},
            title_path="payload.issue.summary",
        )

    result = await inbound_service.submit_inbound_envelope(
        seeded_session,
        connection={
            "connection_id": connection["id"],
            "org_id": ORG_ID,
            "owner_user_id": USER_ID,
            "display_name": "Jira webhook",
            "agent_kind": "jira",
            "scopes": [external_agents.SCOPE_SIGNAL_SUBMIT],
        },
        envelope={
            "kind": "signal",
            "origin": "jira.issue_created",
            "payload": {"issue": {"key": "ILO-7", "summary": "Webhook config"}},
            "idempotency_key": "jira:ILO-7:created",
        },
        ingress_context={"surface": "webhook"},
    )

    with bind_agent_context(AgentExecutionContext(user_id=USER_ID, org_id=ORG_ID)):
        events = _decode(await _handle_manage_inbound(action="list_events", include_payload=False))
        event_detail = _decode(
            await _handle_manage_inbound(
                action="get_event",
                event_id=result["event_id"],
                include_receipts=True,
            )
        )
        receipts = _decode(await _handle_manage_inbound(action="list_receipts", event_id=result["event_id"]))

    record = (await seeded_session.scalars(select(DomainRecord))).one()
    assert result["status"] == inbound_service.STATUS_PROCESSED
    assert record.data["external_id"] == "ILO-7"
    assert record.data["summary"] == "Webhook config"
    assert events["events"][0]["id"] == result["event_id"]
    assert "raw_payload" not in events["events"][0]
    assert event_detail["event"]["raw_payload"] == {"issue": {"key": "ILO-7", "summary": "Webhook config"}}
    assert event_detail["receipts"][0]["status"] == inbound_service.STATUS_PROCESSED
    assert receipts["receipts"][0]["event_id"] == result["event_id"]


async def test_dry_run_uses_same_projection_order_as_runtime(
    seeded_session,
    patch_unit_of_work,
):
    domain = await _create_issue_domain(seeded_session)

    with bind_agent_context(AgentExecutionContext(user_id=USER_ID, org_id=ORG_ID)):
        connection = _decode(
            await _handle_manage_inbound(
                action="create_connection",
                display_name="Jira webhook",
                agent_kind="jira",
                transport="webhook",
            )
        )["connection"]
        policy = _decode(
            await _handle_manage_inbound(
                action="create_policy",
                connection_id=connection["id"],
                name="Jira issue events",
                origin_patterns=["jira.issue_*"],
                allowed_actions=[inbound_service.ACTION_DOMAIN_PROJECTION_UPSERT],
            )
        )["policy"]
        first_projection = _decode(
            await _handle_manage_inbound(
                action="create_projection",
                connection_id=connection["id"],
                policy_id=policy["id"],
                domain_id=domain.id,
                object_key="issue",
                external_id_path="payload.issue.key",
                external_id_field="external_id",
                field_mapping={"summary": "payload.issue.summary"},
            )
        )["projection"]
        second_projection = _decode(
            await _handle_manage_inbound(
                action="create_projection",
                connection_id=connection["id"],
                policy_id=policy["id"],
                domain_id=domain.id,
                object_key="issue",
                external_id_path="payload.issue.newer_key",
                external_id_field="external_id",
                field_mapping={"summary": "payload.issue.summary"},
            )
        )["projection"]
        first_row = await seeded_session.get(InboundDomainProjectionRow, first_projection["id"])
        second_row = await seeded_session.get(InboundDomainProjectionRow, second_projection["id"])
        first_row.created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
        second_row.created_at = datetime(2026, 1, 2, tzinfo=timezone.utc)
        await seeded_session.flush()
        dry_run = _decode(
            await _handle_manage_inbound(
                action="dry_run_match",
                connection_id=connection["id"],
                origin="jira.issue_created",
                payload={"issue": {"key": "ILO-7", "newer_key": "ILO-NEW", "summary": "Order"}},
            )
        )["dry_run"]

    assert dry_run["domain_projection_id"] == first_projection["id"]
    assert dry_run["would_project_domain_record"] is True


async def test_dry_run_honors_projection_permission_and_required_external_id(
    seeded_session,
    patch_unit_of_work,
):
    domain = await _create_issue_domain(seeded_session)

    with bind_agent_context(AgentExecutionContext(user_id=USER_ID, org_id=ORG_ID)):
        connection = _decode(
            await _handle_manage_inbound(
                action="create_connection",
                display_name="Jira webhook",
                agent_kind="jira",
                transport="webhook",
            )
        )["connection"]
        policy = _decode(
            await _handle_manage_inbound(
                action="create_policy",
                connection_id=connection["id"],
                name="Jira issue events",
                origin_patterns=["jira.issue_*"],
            )
        )["policy"]
        projection = _decode(
            await _handle_manage_inbound(
                action="create_projection",
                connection_id=connection["id"],
                policy_id=policy["id"],
                domain_id=domain.id,
                object_key="issue",
                external_id_path="payload.issue.key",
                external_id_field="external_id",
                field_mapping={"summary": "payload.issue.summary"},
                auto_allow_policy_action=False,
            )
        )["projection"]
        blocked = _decode(
            await _handle_manage_inbound(
                action="dry_run_match",
                connection_id=connection["id"],
                origin="jira.issue_created",
                payload={"issue": {"key": "ILO-7", "summary": "Blocked"}},
            )
        )["dry_run"]
        await _handle_manage_inbound(
            action="update_policy",
            policy_id=policy["id"],
            allowed_actions=[inbound_service.ACTION_DOMAIN_PROJECTION_UPSERT],
        )
        missing_external_id = _decode(
            await _handle_manage_inbound(
                action="dry_run_match",
                connection_id=connection["id"],
                origin="jira.issue_created",
                payload={"issue": {"summary": "Missing key"}},
            )
        )["dry_run"]
        await _handle_manage_inbound(
            action="update_projection",
            projection_id=projection["id"],
            validation_failure_status=inbound_service.STATUS_QUARANTINED,
        )
        quarantined_missing_external_id = _decode(
            await _handle_manage_inbound(
                action="dry_run_match",
                connection_id=connection["id"],
                origin="jira.issue_created",
                payload={"issue": {"summary": "Missing key"}},
            )
        )["dry_run"]
        await _handle_manage_inbound(
            action="update_projection",
            projection_id=projection["id"],
            validation_failure_status=inbound_service.STATUS_FAILED,
        )
        failed_missing_external_id = _decode(
            await _handle_manage_inbound(
                action="dry_run_match",
                connection_id=connection["id"],
                origin="jira.issue_created",
                payload={"issue": {"summary": "Missing key"}},
            )
        )["dry_run"]

    assert blocked["would_project_domain_record"] is False
    assert blocked["would_require_ilo"] is True
    assert blocked["projection_error"] == "domain_projection_not_allowed"
    assert missing_external_id["would_project_domain_record"] is False
    assert missing_external_id["would_require_ilo"] is True
    assert missing_external_id["projection_error"] == "Missing projection external id at 'payload.issue.key'"
    assert quarantined_missing_external_id["would_project_domain_record"] is False
    assert quarantined_missing_external_id["would_require_ilo"] is False
    assert quarantined_missing_external_id["projection_error"] == (
        "Missing projection external id at 'payload.issue.key'"
    )
    assert failed_missing_external_id["would_project_domain_record"] is False
    assert failed_missing_external_id["would_require_ilo"] is False
    assert failed_missing_external_id["projection_error"] == "Missing projection external id at 'payload.issue.key'"


async def test_dry_run_schema_errors_match_runtime_quarantine(
    seeded_session,
    patch_unit_of_work,
):
    domain = await _create_issue_domain(seeded_session)

    with bind_agent_context(AgentExecutionContext(user_id=USER_ID, org_id=ORG_ID)):
        connection = _decode(
            await _handle_manage_inbound(
                action="create_connection",
                display_name="Jira webhook",
                agent_kind="jira",
                transport="webhook",
            )
        )["connection"]
        policy = _decode(
            await _handle_manage_inbound(
                action="create_policy",
                connection_id=connection["id"],
                name="Jira issue events",
                origin_patterns=["jira.issue_*"],
                schema_config={"required_paths": ["payload.issue.key"]},
                allowed_actions=[inbound_service.ACTION_DOMAIN_PROJECTION_UPSERT],
            )
        )["policy"]
        await _handle_manage_inbound(
            action="create_projection",
            connection_id=connection["id"],
            policy_id=policy["id"],
            domain_id=domain.id,
            object_key="issue",
            external_id_path="payload.issue.key",
            external_id_field="external_id",
            field_mapping={"summary": "payload.issue.summary"},
        )
        dry_run = _decode(
            await _handle_manage_inbound(
                action="dry_run_match",
                connection_id=connection["id"],
                origin="jira.issue_created",
                payload={"issue": {"summary": "Missing key"}},
            )
        )["dry_run"]

    assert dry_run["matched_policy_id"] == policy["id"]
    assert dry_run["would_project_domain_record"] is False
    assert dry_run["would_require_ilo"] is False
    assert dry_run["schema_error"] == "Missing required inbound field(s): payload.issue.key"


async def test_replay_events_previews_current_config_without_mutating_domains(
    seeded_session,
    patch_unit_of_work,
):
    domain = await _create_issue_domain(seeded_session)

    with bind_agent_context(AgentExecutionContext(user_id=USER_ID, org_id=ORG_ID)):
        connection = _decode(
            await _handle_manage_inbound(
                action="create_connection",
                display_name="Jira webhook",
                agent_kind="jira",
                transport="webhook",
            )
        )["connection"]
        policy = _decode(
            await _handle_manage_inbound(
                action="create_policy",
                connection_id=connection["id"],
                name="Jira issue events",
                origin_patterns=["jira.issue_*"],
                allowed_actions=[inbound_service.ACTION_DOMAIN_PROJECTION_UPSERT],
            )
        )["policy"]
        await _handle_manage_inbound(
            action="create_projection",
            connection_id=connection["id"],
            policy_id=policy["id"],
            domain_id=domain.id,
            object_key="issue",
            external_id_path="payload.issue.key",
            external_id_field="external_id",
            field_mapping={"summary": "payload.issue.summary"},
            title_path="payload.issue.summary",
        )

    first = await inbound_service.submit_inbound_envelope(
        seeded_session,
        connection={
            "connection_id": connection["id"],
            "org_id": ORG_ID,
            "owner_user_id": USER_ID,
            "display_name": "Jira webhook",
            "agent_kind": "jira",
            "scopes": [external_agents.SCOPE_SIGNAL_SUBMIT],
        },
        envelope={
            "kind": "signal",
            "origin": "jira.issue_created",
            "payload": {"issue": {"key": "ILO-7", "summary": "First"}},
            "idempotency_key": "jira:ILO-7:created",
        },
        ingress_context={"surface": "webhook"},
    )
    second = await inbound_service.submit_inbound_envelope(
        seeded_session,
        connection={
            "connection_id": connection["id"],
            "org_id": ORG_ID,
            "owner_user_id": USER_ID,
            "display_name": "Jira webhook",
            "agent_kind": "jira",
            "scopes": [external_agents.SCOPE_SIGNAL_SUBMIT],
        },
        envelope={
            "kind": "signal",
            "origin": "jira.issue_updated",
            "payload": {"issue": {"key": "ILO-8", "summary": "Second"}},
            "idempotency_key": "jira:ILO-8:updated",
        },
        ingress_context={"surface": "webhook"},
    )
    assert first["status"] == inbound_service.STATUS_PROCESSED
    assert second["status"] == inbound_service.STATUS_PROCESSED
    records_before = list((await seeded_session.scalars(select(DomainRecord))).all())

    with bind_agent_context(AgentExecutionContext(user_id=USER_ID, org_id=ORG_ID)):
        await _handle_manage_inbound(
            action="update_policy",
            policy_id=policy["id"],
            enabled=False,
        )
        replay = _decode(
            await _handle_manage_inbound(
                action="replay_events",
                connection_id=connection["id"],
                limit=10,
            )
        )["replay"]

    records_after = list((await seeded_session.scalars(select(DomainRecord))).all())
    reloaded_first = await seeded_session.get(InboundEventRow, first["event_id"])
    assert len(records_before) == 2
    assert [record.id for record in records_after] == [record.id for record in records_before]
    assert reloaded_first.status == inbound_service.STATUS_PROCESSED
    assert replay["mode"] == "dry_run_replay"
    assert replay["mutates_workspace"] is False
    assert replay["event_count"] == 2
    assert replay["summary"]["would_statuses"] == {inbound_service.STATUS_REVIEW_REQUIRED: 2}
    assert replay["summary"]["would_require_ilo"] == 2
    assert replay["summary"]["would_project_domain_record"] == 0
    assert replay["summary"]["changed"] == {
        "policy_match": 2,
        "domain_projection_match": 2,
        "status": 2,
    }
    assert {result["original"]["status"] for result in replay["results"]} == {
        inbound_service.STATUS_PROCESSED,
    }
    assert {result["replay"]["reason"] for result in replay["results"]} == {
        "no_matching_source_policy",
    }
    assert all("raw_payload" not in result["event"] for result in replay["results"])


async def test_replay_event_can_include_payload_for_single_event_inspection(
    seeded_session,
    patch_unit_of_work,
):
    domain = await _create_issue_domain(seeded_session)

    with bind_agent_context(AgentExecutionContext(user_id=USER_ID, org_id=ORG_ID)):
        connection = _decode(
            await _handle_manage_inbound(
                action="create_connection",
                display_name="Jira webhook",
                agent_kind="jira",
                transport="webhook",
            )
        )["connection"]
        policy = _decode(
            await _handle_manage_inbound(
                action="create_policy",
                connection_id=connection["id"],
                name="Jira issue events",
                origin_patterns=["jira.issue_*"],
            )
        )["policy"]
        projection = _decode(
            await _handle_manage_inbound(
                action="create_projection",
                connection_id=connection["id"],
                policy_id=policy["id"],
                domain_id=domain.id,
                object_key="issue",
                external_id_path="payload.issue.key",
                external_id_field="external_id",
                field_mapping={"summary": "payload.issue.summary"},
                title_path="payload.issue.summary",
            )
        )["projection"]

    result = await inbound_service.submit_inbound_envelope(
        seeded_session,
        connection={
            "connection_id": connection["id"],
            "org_id": ORG_ID,
            "owner_user_id": USER_ID,
            "display_name": "Jira webhook",
            "agent_kind": "jira",
            "scopes": [external_agents.SCOPE_SIGNAL_SUBMIT],
        },
        envelope={
            "kind": "signal",
            "origin": "jira.issue_created",
            "payload": {"issue": {"key": "ILO-9", "summary": "Payload replay"}},
            "idempotency_key": "jira:ILO-9:created",
        },
        ingress_context={"surface": "webhook"},
    )

    with bind_agent_context(AgentExecutionContext(user_id=USER_ID, org_id=ORG_ID)):
        replay = _decode(
            await _handle_manage_inbound(
                action="replay_events",
                event_id=result["event_id"],
                include_payload=True,
            )
        )["replay"]

    replayed = replay["results"][0]
    assert replay["event_count"] == 1
    assert replayed["event"]["raw_payload"] == {"issue": {"key": "ILO-9", "summary": "Payload replay"}}
    assert replayed["original"]["matched_policy_id"] == policy["id"]
    assert replayed["replay"]["matched_policy_id"] == policy["id"]
    assert replayed["replay"]["domain_projection_id"] == projection["id"]
    assert replayed["replay"]["would_status"] == inbound_service.STATUS_PROCESSED
    assert replayed["changed"] == {
        "policy_match": False,
        "domain_projection_match": False,
        "status": False,
    }


async def test_source_card_summarizes_connection_and_persists_manual_context(
    seeded_session,
    patch_unit_of_work,
):
    domain = await _create_issue_domain(seeded_session)

    with bind_agent_context(AgentExecutionContext(user_id=USER_ID, org_id=ORG_ID)):
        connection = _decode(
            await _handle_manage_inbound(
                action="create_connection",
                display_name="Jira webhook",
                agent_kind="jira",
                transport="webhook",
            )
        )["connection"]
        policy = _decode(
            await _handle_manage_inbound(
                action="create_policy",
                connection_id=connection["id"],
                name="Jira issue events",
                origin_patterns=["jira.issue_*"],
                schema_config={"required_paths": ["payload.issue.key"]},
                allowed_actions=[inbound_service.ACTION_DOMAIN_PROJECTION_UPSERT],
                instructions="Store Jira issues in the incoming issues Domain.",
            )
        )["policy"]
        projection = _decode(
            await _handle_manage_inbound(
                action="create_projection",
                connection_id=connection["id"],
                policy_id=policy["id"],
                domain_id=domain.id,
                object_key="issue",
                external_id_path="payload.issue.key",
                external_id_field="external_id",
                field_mapping={"summary": "payload.issue.summary"},
                title_path="payload.issue.summary",
            )
        )["projection"]

    await inbound_service.submit_inbound_envelope(
        seeded_session,
        connection={
            "connection_id": connection["id"],
            "org_id": ORG_ID,
            "owner_user_id": USER_ID,
            "display_name": "Jira webhook",
            "agent_kind": "jira",
            "scopes": [external_agents.SCOPE_SIGNAL_SUBMIT],
        },
        envelope={
            "kind": "signal",
            "origin": "jira.issue_created",
            "payload": {"issue": {"key": "ILO-10", "summary": "Source card"}},
            "idempotency_key": "jira:ILO-10:created",
        },
        ingress_context={"surface": "webhook"},
    )
    await inbound_service.submit_inbound_envelope(
        seeded_session,
        connection={
            "connection_id": connection["id"],
            "org_id": ORG_ID,
            "owner_user_id": USER_ID,
            "display_name": "Jira webhook",
            "agent_kind": "jira",
            "scopes": [external_agents.SCOPE_SIGNAL_SUBMIT],
        },
        envelope={
            "kind": "signal",
            "origin": "jira.issue_updated",
            "payload": {"issue": {"summary": "Missing key"}},
            "idempotency_key": "jira:missing-key",
        },
        ingress_context={"surface": "webhook"},
    )

    with bind_agent_context(AgentExecutionContext(user_id=USER_ID, org_id=ORG_ID)):
        before = _decode(
            await _handle_manage_inbound(
                action="get_source_card",
                connection_id=connection["id"],
                limit=10,
            )
        )
        refreshed = _decode(
            await _handle_manage_inbound(
                action="refresh_source_card",
                connection_id=connection["id"],
                source_purpose="Mirror Jira issues into IloSpace for team awareness.",
                source_notes="Created from the inbound coordination smoke slice.",
                source_tags=["jira", "tickets"],
                limit=10,
            )
        )["source_card"]
        after = _decode(
            await _handle_manage_inbound(
                action="get_source_card",
                connection_id=connection["id"],
                limit=10,
            )
        )

    persisted_connection = await seeded_session.get(ExternalAgentConnectionRow, connection["id"])
    source_card = persisted_connection.metadata_["source_card"]
    assert before["persisted_source_card"] is None
    assert refreshed["connection"]["display_name"] == "Jira webhook"
    assert refreshed["purpose"] == "Mirror Jira issues into IloSpace for team awareness."
    assert refreshed["notes"] == "Created from the inbound coordination smoke slice."
    assert refreshed["tags"] == ["jira", "tickets"]
    assert refreshed["configured_rules"]["policy_count"] == 1
    assert refreshed["configured_rules"]["projection_count"] == 1
    assert refreshed["configured_rules"]["policies"][0]["id"] == policy["id"]
    assert refreshed["configured_rules"]["policies"][0]["has_instructions"] is True
    assert refreshed["configured_rules"]["policies"][0]["schema_required_paths"] == ["payload.issue.key"]
    assert refreshed["configured_rules"]["projections"][0]["id"] == projection["id"]
    assert refreshed["traffic"]["event_count_sampled"] == 2
    assert {"value": "jira.issue_created", "count": 1} in refreshed["traffic"]["common_origins"]
    assert {"value": inbound_service.STATUS_PROCESSED, "count": 1} in refreshed["traffic"]["statuses"]
    assert {"value": inbound_service.STATUS_QUARANTINED, "count": 1} in refreshed["traffic"]["statuses"]
    assert {"value": "payload.issue.key", "count": 1} in refreshed["traffic"]["payload_shapes"]
    assert {"value": "payload.issue.summary", "count": 2} in refreshed["traffic"]["payload_shapes"]
    assert refreshed["traffic"]["recent_failures"][0]["status"] == inbound_service.STATUS_QUARANTINED
    assert source_card["generated_at"] == refreshed["generated_at"]
    assert after["persisted_source_card"]["generated_at"] == refreshed["generated_at"]
    assert after["source_card"]["purpose"] == refreshed["purpose"]


async def test_manage_inbound_requires_org_scoped_run(patch_unit_of_work):
    with bind_agent_context(AgentExecutionContext(user_id=USER_ID)):
        result = _decode(await _handle_manage_inbound(action="list_connections"))

    assert result == {"error": "manage_inbound requires an org-scoped run"}
