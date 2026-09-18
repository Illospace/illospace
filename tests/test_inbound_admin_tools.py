from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from brain.platform.db.models.domain import DomainRecord
from brain.platform.db.models.external_agent import ExternalAgentConnectionRow
from brain.platform.db.models.inbound import InboundDecisionReceiptRow, InboundEventRow
from brain.systems.inbound import admin as inbound_admin
from brain.systems.inbound import service as inbound_service
from brain.systems.runs.failures import DEFAULT_FAILED_RUN_MESSAGE
from brain.systems.runs.execution_context import AgentExecutionContext, bind_agent_context
from brain.systems.runs.tool_catalog.handlers.inbound import _handle_manage_inbound
from tests.inbound_admin_support import (
    ORG_ID,
    USER_ID,
    _create_issue_domain,
    _decode,
    _submit_issue_signal,
    patch_unit_of_work as patch_unit_of_work,
    seeded_session as seeded_session,
    session as session,
)


pytestmark = pytest.mark.asyncio


async def test_illo_can_list_attention_events_for_recovery(
    seeded_session,
    patch_unit_of_work,
):
    with bind_agent_context(AgentExecutionContext(user_id=USER_ID, org_id=ORG_ID)):
        connection = _decode(
            await _handle_manage_inbound(
                action="create_connection",
                display_name="Webhook recovery source",
                agent_kind="custom",
                transport="webhook",
            )
        )["connection"]

    def event(status: str, *, origin: str, error: str | None = None) -> InboundEventRow:
        return InboundEventRow(
            org_id=ORG_ID,
            connection_id=connection["id"],
            kind="signal",
            origin=origin,
            raw_payload={"case": origin},
            normalized_payload={},
            envelope={},
            ingress_context={"surface": "webhook"},
            source_actor={"connection_id": connection["id"]},
            status=status,
            action_type="ilo_required" if status == inbound_service.STATUS_REVIEW_REQUIRED else None,
            error=error,
        )

    seeded_session.add_all(
        [
            event(inbound_service.STATUS_PROCESSED, origin="custom.ok"),
            event(inbound_service.STATUS_REVIEW_REQUIRED, origin="custom.needs_illo"),
            event(inbound_service.STATUS_QUARANTINED, origin="custom.bad_schema", error="missing key"),
            event(inbound_service.STATUS_FAILED, origin="custom.failed_run", error="Illo run failed"),
            event(inbound_service.STATUS_PROCESSED, origin="custom.processed_with_error", error="late warning"),
        ]
    )
    await seeded_session.flush()

    with bind_agent_context(AgentExecutionContext(user_id=USER_ID, org_id=ORG_ID)):
        attention = _decode(
            await _handle_manage_inbound(
                action="list_attention_events",
                connection_id=connection["id"],
                include_payload=False,
                limit=10,
            )
        )

    statuses = {row["status"] for row in attention["events"]}
    origins = {row["origin"] for row in attention["events"]}
    assert len(attention["events"]) == 4
    assert inbound_service.STATUS_REVIEW_REQUIRED in statuses
    assert inbound_service.STATUS_QUARANTINED in statuses
    assert inbound_service.STATUS_FAILED in statuses
    assert "custom.processed_with_error" in origins
    assert "custom.ok" not in origins
    assert "raw_payload" not in attention["events"][0]
    assert attention["summary"]["event_count"] == 4
    assert {"value": inbound_service.STATUS_REVIEW_REQUIRED, "count": 1} in attention["summary"]["statuses"]
    assert {"value": inbound_service.STATUS_QUARANTINED, "count": 1} in attention["summary"]["statuses"]
    assert {"value": inbound_service.STATUS_FAILED, "count": 1} in attention["summary"]["statuses"]



async def test_failed_inbound_serializers_redact_legacy_run_diagnostics():
    raw_diagnostic = "provider rejected token=super-secret"
    legacy_outcome = {
        "triage": {
            "status": "failed",
            "run_status": "failed",
            "final_answer": raw_diagnostic,
            "result": {"status": "failed", "final_answer": raw_diagnostic},
        }
    }
    event = InboundEventRow(
        org_id=ORG_ID,
        connection_id="33333333-3333-4333-8333-333333333333",
        kind="signal",
        origin="custom.failed_run",
        status=inbound_service.STATUS_FAILED,
        action_result=legacy_outcome,
        error=raw_diagnostic,
    )
    receipt = InboundDecisionReceiptRow(
        event_id="44444444-4444-4444-8444-444444444444",
        org_id=ORG_ID,
        connection_id="33333333-3333-4333-8333-333333333333",
        status=inbound_service.STATUS_FAILED,
        outcome=legacy_outcome,
        tool_use={
            "status": "failed",
            "final_answer": raw_diagnostic,
            "error": raw_diagnostic,
        },
        reasoning_summary=raw_diagnostic,
    )

    event_payload = inbound_admin.serialize_event(event)
    receipt_payload = inbound_admin.serialize_receipt(receipt)
    failure = {
        "status": "failed",
        "category": "internal",
        "message": DEFAULT_FAILED_RUN_MESSAGE,
    }

    assert event_payload["failure"] == failure
    assert event_payload["error"] == DEFAULT_FAILED_RUN_MESSAGE
    assert event_payload["action_result"]["triage"]["failure"] == failure
    assert receipt_payload["failure"] == failure
    assert receipt_payload["outcome"]["triage"]["result"]["failure"] == failure
    assert receipt_payload["tool_use"] == {"status": "failed", "failure": failure}
    assert receipt_payload["reasoning_summary"] is None
    assert raw_diagnostic not in json.dumps(event_payload)
    assert raw_diagnostic not in json.dumps(receipt_payload)



async def test_generic_failed_inbound_serializers_preserve_non_run_contract():
    diagnostic = "projection policy rejected this payload"
    event = InboundEventRow(
        org_id=ORG_ID,
        connection_id="33333333-3333-4333-8333-333333333333",
        kind="signal",
        origin="custom.projection_failure",
        status=inbound_service.STATUS_FAILED,
        action_result={"status": "failed", "reason": "policy_rejected"},
        error=diagnostic,
    )
    receipt = InboundDecisionReceiptRow(
        event_id="44444444-4444-4444-8444-444444444444",
        org_id=ORG_ID,
        connection_id="33333333-3333-4333-8333-333333333333",
        status=inbound_service.STATUS_FAILED,
        outcome={"status": "failed", "reason": "policy_rejected"},
        reasoning_summary=diagnostic,
    )

    event_payload = inbound_admin.serialize_event(event)
    receipt_payload = inbound_admin.serialize_receipt(receipt)

    assert "failure" not in event_payload
    assert event_payload["error"] == diagnostic
    assert event_payload["action_result"] == {"status": "failed", "reason": "policy_rejected"}
    assert "failure" not in receipt_payload
    assert receipt_payload["outcome"] == {"status": "failed", "reason": "policy_rejected"}
    assert receipt_payload["reasoning_summary"] == diagnostic



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

    first = await _submit_issue_signal(
        seeded_session,
        connection,
        origin="jira.issue_created",
        issue={"key": "ILO-7", "summary": "First"},
        idempotency_key="jira:ILO-7:created",
    )
    second = await _submit_issue_signal(
        seeded_session,
        connection,
        origin="jira.issue_updated",
        issue={"key": "ILO-8", "summary": "Second"},
        idempotency_key="jira:ILO-8:updated",
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

    result = await _submit_issue_signal(
        seeded_session,
        connection,
        origin="jira.issue_created",
        issue={"key": "ILO-9", "summary": "Payload replay"},
        idempotency_key="jira:ILO-9:created",
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

    await _submit_issue_signal(
        seeded_session,
        connection,
        origin="jira.issue_created",
        issue={"key": "ILO-10", "summary": "Source card"},
        idempotency_key="jira:ILO-10:created",
    )
    await _submit_issue_signal(
        seeded_session,
        connection,
        origin="jira.issue_updated",
        issue={"summary": "Missing key"},
        idempotency_key="jira:missing-key",
    )
    seeded_session.add(
        InboundEventRow(
            org_id=ORG_ID,
            connection_id=connection["id"],
            kind="signal",
            origin="jira.issue_needs_triage",
            raw_payload={},
            normalized_payload={},
            envelope={},
            ingress_context={"surface": "webhook"},
            source_actor={"connection_id": connection["id"]},
            status=inbound_service.STATUS_REVIEW_REQUIRED,
            action_type="ilo_required",
        )
    )
    await seeded_session.flush()

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
    assert refreshed["traffic"]["event_count_sampled"] == 3
    assert {"value": "jira.issue_created", "count": 1} in refreshed["traffic"]["common_origins"]
    assert {"value": inbound_service.STATUS_PROCESSED, "count": 1} in refreshed["traffic"]["statuses"]
    assert {"value": inbound_service.STATUS_QUARANTINED, "count": 1} in refreshed["traffic"]["statuses"]
    assert {"value": inbound_service.STATUS_REVIEW_REQUIRED, "count": 1} in refreshed["traffic"]["statuses"]
    assert {"value": "payload.issue.key", "count": 1} in refreshed["traffic"]["payload_shapes"]
    assert {"value": "payload.issue.summary", "count": 2} in refreshed["traffic"]["payload_shapes"]
    assert {
        inbound_service.STATUS_QUARANTINED,
        inbound_service.STATUS_REVIEW_REQUIRED,
    }.issubset({row["status"] for row in refreshed["traffic"]["recent_attention"]})
    assert refreshed["traffic"]["recent_failures"][0]["status"] == inbound_service.STATUS_QUARANTINED
    assert all(
        row["status"] != inbound_service.STATUS_REVIEW_REQUIRED
        for row in refreshed["traffic"]["recent_failures"]
    )
    assert source_card["generated_at"] == refreshed["generated_at"]
    assert after["persisted_source_card"]["generated_at"] == refreshed["generated_at"]
    assert after["source_card"]["purpose"] == refreshed["purpose"]



async def test_manage_inbound_requires_workspace_context(patch_unit_of_work):
    with bind_agent_context(AgentExecutionContext(user_id=USER_ID)):
        result = _decode(await _handle_manage_inbound(action="list_connections"))

    assert result == {"error": "manage_inbound could not access this workspace context"}
