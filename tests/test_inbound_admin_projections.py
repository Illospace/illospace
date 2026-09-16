from __future__ import annotations

import re
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from brain.platform.db.models.domain import DomainRecord
from brain.platform.db.models.inbound import InboundDomainProjectionRow, InboundSourcePolicyRow
from brain.systems.external_agents import service as external_agents
from brain.systems.inbound import admin as inbound_admin
from brain.systems.inbound import service as inbound_service
from brain.systems.inbound.github_webhook import github_event_to_envelope
from brain.systems.runs.execution_context import AgentExecutionContext, bind_agent_context
from brain.systems.runs.tool_catalog.handlers.inbound import _handle_manage_inbound
from brain.systems.user_domains.service import AsyncDomainService
from tests.inbound_admin_support import (
    ORG_ID,
    USER_ID,
    _bridge_connection,
    _create_issue_domain,
    _decode,
    _submit_issue_signal,
    patch_unit_of_work as patch_unit_of_work,
    seeded_session as seeded_session,
    session as session,
)


pytestmark = pytest.mark.asyncio


@pytest.fixture
async def github_projection(seeded_session):
    domain = await _create_issue_domain(seeded_session)
    connection = await inbound_admin.create_connection(
        seeded_session, org_id=ORG_ID, owner_user_id=USER_ID,
        display_name="GitHub webhook", agent_kind="github", transport="webhook",
    )
    policy = await inbound_admin.create_policy(
        seeded_session, org_id=ORG_ID, connection_id=str(connection.id),
        name="GitHub issues", origin_patterns=["github:uwear-ai/uwear-website"],
        envelope_kinds=["github_event"], allowed_actions=["domain_projection.upsert"],
    )
    projection = await inbound_admin.create_projection(
        seeded_session, org_id=ORG_ID, connection_id=str(connection.id),
        policy_id=str(policy.id), domain_id=domain.id, object_key="issue",
        external_id_path="github:{hints.repo}:issue:{hints.number}",
        external_id_field="external_id",
        field_mapping={"summary": "payload.issue.title", "status": "hints.issue_outcome"},
        validation_failure_status="quarantined",
    )
    envelope = github_event_to_envelope("issues", {
        "action": "closed",
        "repository": {"full_name": "uwear-ai/uwear-website"},
        "issue": {
            "number": 253, "node_id": "I_kwDO_test253", "title": "Fix issue state sync",
            "html_url": "https://github.com/uwear-ai/uwear-website/issues/253",
            "state": "closed", "state_reason": "completed",
            "closed_at": "2026-09-16T12:00:00Z", "updated_at": "2026-09-16T12:00:00Z",
            "user": {"login": "reporter"},
        },
    }, delivery_id="issue-253-closed")
    return connection, projection, envelope


@pytest.mark.parametrize("existing_record", [False, True])
async def test_github_composed_identity_matches_live_projection_and_preview(
    seeded_session, github_projection, existing_record,
):
    connection, projection, envelope = github_projection
    expected_id = "github:uwear-ai/uwear-website:issue:253"
    if existing_record:
        original = await AsyncDomainService(seeded_session).create_record(
            ORG_ID, projection.domain_id, "issue", title=expected_id,
            data={"external_id": expected_id, "summary": "Fix issue state sync", "status": "open"},
            actor_id=USER_ID,
        )
    preview = await inbound_admin._preview_envelope(
        seeded_session, org_id=ORG_ID, connection_id=str(connection.id), **envelope,
    )
    result = await inbound_service.submit_inbound_envelope(
        seeded_session,
        connection=_bridge_connection(inbound_admin.serialize_connection(connection)),
        envelope=envelope,
    )
    record = (await seeded_session.scalars(select(DomainRecord))).one()
    assert preview["projection_error"] is None
    assert preview["would_project_domain_record"] is True
    assert preview["external_id"] == result["ilo_outcome"]["external_id"] == expected_id
    assert result["status"] == inbound_service.STATUS_PROCESSED
    assert result["ilo_outcome"]["operation"] == ("updated" if existing_record else "created")
    assert record.data == {
        "external_id": expected_id, "summary": "Fix issue state sync", "status": "closed",
    }
    if existing_record:
        assert record.id == original.id


@pytest.mark.parametrize("missing_part", ["missing", "null", "empty"])
async def test_github_projection_missing_part_matches_preview_without_writing(
    seeded_session, github_projection, missing_part,
):
    connection, projection, envelope = github_projection
    if missing_part == "missing":
        del envelope["hints"]["number"]
    else:
        envelope["hints"]["number"] = None if missing_part == "null" else ""
    preview = await inbound_admin._preview_envelope(
        seeded_session, org_id=ORG_ID, connection_id=str(connection.id), **envelope,
    )
    result = await inbound_service.submit_inbound_envelope(
        seeded_session,
        connection=_bridge_connection(inbound_admin.serialize_connection(connection)),
        envelope=envelope,
    )
    expected_error = f"Missing projection external id at '{projection.external_id_path}'"
    assert preview["projection_error"] == expected_error
    assert preview["external_id"] is None
    assert preview["would_project_domain_record"] is False
    assert result["status"] == inbound_service.STATUS_QUARANTINED
    assert result["error"] == expected_error
    assert list(await seeded_session.scalars(select(DomainRecord))) == []


@pytest.mark.parametrize("template", ["github:{hints.repo", "github:{}"])
async def test_github_projection_malformed_template_matches_preview_without_writing(
    seeded_session, github_projection, template,
):
    connection, projection, envelope = github_projection
    projection.external_id_path = template
    await seeded_session.flush()
    preview = await inbound_admin._preview_envelope(
        seeded_session, org_id=ORG_ID, connection_id=str(connection.id), **envelope,
    )
    result = await inbound_service.submit_inbound_envelope(
        seeded_session,
        connection=_bridge_connection(inbound_admin.serialize_connection(connection)),
        envelope=envelope,
    )
    assert preview["projection_error"].startswith("path template")
    assert preview["external_id"] is None
    assert preview["would_project_domain_record"] is False
    assert result["status"] == inbound_service.STATUS_QUARANTINED
    assert result["error"] == preview["projection_error"]
    assert list(await seeded_session.scalars(select(DomainRecord))) == []


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



async def test_projection_expressions_round_trip_through_admin(seeded_session, patch_unit_of_work):
    domain = await _create_issue_domain(seeded_session)
    with bind_agent_context(AgentExecutionContext(user_id=USER_ID, org_id=ORG_ID)):
        connection = _decode(await _handle_manage_inbound(
            action="create_connection", display_name="Expression source", transport="webhook",
        ))["connection"]
        mapping = {
            "summary": "payload.issue.summary",
            "status": {"const": "open"},
            "synced_at": {"now": True},
        }
        created = _decode(await _handle_manage_inbound(
            action="create_projection", connection_id=connection["id"],
            domain_id=domain.id, object_key="issue", external_id_path="payload.issue.key",
            external_id_field="external_id", field_mapping=mapping,
        ))["projection"]
        assert created["field_mapping"] == mapping
        row = await seeded_session.get(InboundDomainProjectionRow, created["id"])
        await seeded_session.refresh(row)
        assert row.field_mapping == mapping

        mapping = {**mapping, "status": {"path": "payload.issue.status"}}
        updated = _decode(await _handle_manage_inbound(
            action="update_projection", projection_id=created["id"], field_mapping=mapping,
        ))["projection"]
        assert updated["field_mapping"] == mapping
        await seeded_session.refresh(row)
        assert row.field_mapping == mapping



@pytest.mark.parametrize("action", ["create", "update"])
@pytest.mark.parametrize("expression, message", [
    ({"now": False}, "field_mapping.status.now must be true"),
    ({"now": "yes"}, "field_mapping.status.now must be true"),
    ({"now": 1}, "field_mapping.status.now must be true"),
    ({"unknown": True}, "must use exactly one of const, path, or now"),
    ({}, "must use exactly one of const, path, or now"),
    ({"now": True, "const": "x"}, "must use exactly one of const, path, or now"),
    ({"now": True, "unknown": True}, "must use exactly one of const, path, or now"),
    ({"path": 42}, "field_mapping.status.path must be a string"),
    ([], "must be a string path or mapping expression"),
])
async def test_admin_rejects_invalid_projection_expressions(seeded_session, action, expression, message):
    domain = await _create_issue_domain(seeded_session)
    connection = await inbound_admin.create_connection(
        seeded_session, org_id=ORG_ID, owner_user_id=USER_ID,
        display_name="Expression source", transport="webhook",
    )
    kwargs = dict(
        org_id=ORG_ID, connection_id=str(connection.id), domain_id=domain.id,
        object_key="issue", external_id_path="payload.issue.key", external_id_field="external_id",
    )
    original_mapping = {"summary": "payload.issue.summary"}
    if action == "update":
        row = await inbound_admin.create_projection(
            seeded_session, **kwargs, field_mapping=original_mapping,
        )
    with pytest.raises(inbound_service.InboundValidationError, match=re.escape(message)):
        if action == "create":
            await inbound_admin.create_projection(
                seeded_session, **kwargs, field_mapping={"status": expression},
            )
        else:
            await inbound_admin.update_projection(
                seeded_session, org_id=ORG_ID, projection_id=str(row.id),
                field_mapping={"status": expression}, enabled=False,
            )
    if action == "update":
        await seeded_session.refresh(row)
        assert row.field_mapping == original_mapping
        assert row.enabled is True
    else:
        assert list(await seeded_session.scalars(select(InboundDomainProjectionRow))) == []



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

    result = await _submit_issue_signal(
        seeded_session,
        connection,
        origin="jira.issue_created",
        issue={"key": "ILO-7", "summary": "Webhook config"},
        idempotency_key="jira:ILO-7:created",
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
    assert blocked["domain_projection_id"] is None
    assert blocked["projection_error"] == "domain_projection_not_allowed"
    assert missing_external_id["would_project_domain_record"] is False
    assert missing_external_id["would_require_ilo"] is True
    assert missing_external_id["domain_projection_id"] == projection["id"]
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
    assert dry_run["domain_projection_id"] is None
    assert dry_run["would_project_domain_record"] is False
    assert dry_run["would_require_ilo"] is False
    assert dry_run["schema_error"] == "Missing required inbound field(s): payload.issue.key"
