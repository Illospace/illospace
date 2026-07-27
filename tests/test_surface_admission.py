from __future__ import annotations

from typing import Any, Mapping

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from brain.platform.db.models.agent_run import AgentRunEventRow, AgentRunRow
from brain.platform.db.models.domain import Domain, DomainObjectType, DomainRecord
from brain.platform.db.models.external_agent import ExternalAgentConnectionRow
from brain.platform.db.models.inbound import InboundDecisionReceiptRow, InboundEventRow
from brain.platform.db.models.open_ask import OpenAsk
from brain.platform.db.models.org import Org, User
from brain.systems.inbound.handlers import (
    InboundEventCompleter,
    InboundHandlerContext,
    register_inbound_envelope_handler,
    unregister_inbound_envelope_handler,
)
from brain.systems.inbound.surface_admission import (
    SurfaceAdmissionSpec,
    SurfaceIdentity,
    SurfaceTarget,
    admit_surface_envelope,
)
from brain.systems.runs.direct_targets import DIRECT_TARGET_KINDS


ORG_ID = "11111111-1111-4111-8111-111111111111"
USER_ID = "22222222-2222-4222-8222-222222222222"
CONNECTION_ID = "33333333-3333-4333-8333-333333333333"
THIRD_SURFACE_KIND = "hypothetical_third_surface"
THIRD_SURFACE_TARGET_KIND = "inbound_submission"


@pytest.fixture
async def session(async_sqlite_session_factory, sqlite_postgres_ddl_patch):
    return await async_sqlite_session_factory(
        [
            Org.__table__,
            User.__table__,
            Domain.__table__,
            DomainObjectType.__table__,
            DomainRecord.__table__,
            ExternalAgentConnectionRow.__table__,
            AgentRunRow.__table__,
            AgentRunEventRow.__table__,
            OpenAsk.__table__,
            InboundEventRow.__table__,
            InboundDecisionReceiptRow.__table__,
        ]
    )


async def _seed_connection(
    session: AsyncSession,
    *,
    surface_kind: str = THIRD_SURFACE_KIND,
    owner_user_id: str | None = USER_ID,
) -> ExternalAgentConnectionRow:
    session.add(Org(id=ORG_ID, name="Test Org", slug="test-org"))
    if owner_user_id is not None:
        session.add(User(id=USER_ID, org_id=ORG_ID, name="Reda", email="reda@example.com"))
    connection = ExternalAgentConnectionRow(
        id=CONNECTION_ID,
        org_id=ORG_ID,
        owner_user_id=owner_user_id,
        display_name=f"{surface_kind} surface",
        agent_kind=surface_kind,
        transport="webhook",
        status="online",
        remote_agent_card={},
        capabilities={surface_kind: True},
        auth_metadata={},
        metadata_={},
    )
    session.add(connection)
    await session.flush()
    return connection


def _surface_envelope(surface_kind: str) -> dict[str, Any]:
    if surface_kind == "app_report":
        return {
            "kind": "app_report",
            "origin": "uwear.app_report",
            "payload": {
                "email": "customer@example.com",
                "profileId": "profile-123",
                "type": "Issue",
                "message": "The generation completed, but the result stayed blank.",
                "attachments": [],
                "generation_ids": [901, "gen-902"],
                "batch_ids": [77, "batch-78"],
            },
            "idempotency_key": "app-report:profile-123:surface-test",
        }
    if surface_kind == "slack_message":
        return {
            "kind": "slack_message",
            "origin": "slack.app_mention",
            "payload": {
                "event_kind": "mention",
                "team_id": "T789",
                "channel_id": "C456",
                "channel_type": "channel",
                "message_ts": "1716900000.000100",
                "thread_ts": "1716900000.000100",
                "slack_user_id": "U123",
                "text": "<@BILLO> turn this into work",
            },
            "idempotency_key": "slack:T789:C456:1716900000.000100",
        }
    raise AssertionError(f"Unsupported test surface: {surface_kind}")


def _assert_failed_surface_receipt(
    event: InboundEventRow,
    receipt: InboundDecisionReceiptRow,
    *,
    action_type: str,
    outcome: Mapping[str, Any],
    error: str,
    target: Mapping[str, Any],
    tool_type: str,
    reasoning_summary: str,
) -> None:
    assert event.status == "failed"
    assert event.action_type == action_type
    assert event.action_result == outcome
    assert event.confidence == 0.0
    assert event.error == error
    assert receipt.status == "failed"
    assert receipt.outcome == outcome
    assert receipt.confidence == 0.0
    assert receipt.target == target
    assert receipt.tool_use == {"type": tool_type, "status": "failed"}
    assert receipt.reasoning_summary == reasoning_summary
    assert receipt.reusable_pattern_candidate == {}


async def _resolve_identity(
    _session: AsyncSession,
    context: InboundHandlerContext,
    _normalized: Mapping[str, Any],
) -> SurfaceIdentity:
    return SurfaceIdentity(authority_user_id=context.owner_user_id)


async def _build_payload(
    _session: AsyncSession,
    context: InboundHandlerContext,
    event: InboundEventRow,
    normalized: Mapping[str, Any],
    identity: SurfaceIdentity,
) -> dict[str, Any]:
    return {
        "source": THIRD_SURFACE_KIND,
        "event_type": "hypothetical.requested",
        "org_id": context.org_id,
        "actor": {
            "id": identity.authority_user_id,
            "org_id": context.org_id,
            "principal_type": "hypothetical_user",
        },
        "target": {
            "kind": THIRD_SURFACE_TARGET_KIND,
            "event_id": str(event.id),
            "thread_id": f"hypothetical:{event.id}",
        },
        "payload": {
            "run_message": str(
                (normalized.get("payload") or {}).get("message") or ""
            ),
            "workspace_ref": {"source": THIRD_SURFACE_KIND, "mode": "headless"},
            "metadata": {
                "headless": True,
                "execution_profile": "fast",
            },
            "user_id": identity.authority_user_id,
        },
        "idempotency_key": normalized.get("idempotency_key"),
        "policy": {
            "producer": THIRD_SURFACE_KIND,
            "run_event": "requested",
        },
    }


def _build_target(
    trigger_payload: Mapping[str, Any],
    _normalized: Mapping[str, Any],
) -> SurfaceTarget:
    return SurfaceTarget(value=dict(trigger_payload.get("target") or {}))


def _build_ack(
    event_id: str,
    _normalized: Mapping[str, Any],
    _trigger_payload: Mapping[str, Any],
    _target: Mapping[str, Any],
) -> Mapping[str, Any]:
    return {"ack": {"status": "accepted", "event_id": event_id}}


THIRD_SURFACE_ADMISSION = SurfaceAdmissionSpec(
    kind=THIRD_SURFACE_KIND,
    action_type="hypothetical.run_admitted",
    success_operation="hypothetical_run_admitted",
    failure_operation="hypothetical_run_admission_failed",
    tool_type="hypothetical_intake",
    resolve_identity=_resolve_identity,
    build_payload=_build_payload,
    build_target=_build_target,
    build_ack=_build_ack,
    success_tool_status="accepted",
    success_reasoning="The hypothetical request was admitted.",
    admission_failure_reasoning="The hypothetical request could not be admitted.",
    missing_authority_error="Hypothetical surface has no authority user",
    missing_authority_reasoning="Hypothetical requests require an authority user.",
)


async def _process_third_surface(
    session: AsyncSession,
    *,
    context: InboundHandlerContext,
    event: InboundEventRow,
    normalized: Mapping[str, Any],
    complete: InboundEventCompleter,
) -> dict[str, Any]:
    return await admit_surface_envelope(
        session,
        context=context,
        event=event,
        normalized=normalized,
        complete=complete,
        spec=THIRD_SURFACE_ADMISSION,
    )


def _register_third_surface() -> None:
    register_inbound_envelope_handler(THIRD_SURFACE_KIND, _process_third_surface)


def _unregister_third_surface() -> None:
    unregister_inbound_envelope_handler(THIRD_SURFACE_KIND)


@pytest.mark.asyncio
async def test_registered_third_surface_uses_the_direct_target_manifest(session):
    from brain.systems.inbound.service import submit_inbound_envelope

    connection = await _seed_connection(session)

    _register_third_surface()
    try:
        result = await submit_inbound_envelope(
            session,
            connection=connection,
            envelope={
                "kind": THIRD_SURFACE_KIND,
                "origin": "test.hypothetical",
                "payload": {"message": "Handle this third-surface request."},
                "idempotency_key": "hypothetical:1",
            },
        )
    finally:
        _unregister_third_surface()

    event = (await session.scalars(select(InboundEventRow))).one()
    run = (await session.scalars(select(AgentRunRow))).one()
    receipt = (await session.scalars(select(InboundDecisionReceiptRow))).one()

    assert result["status"] == "processed"
    assert result["ilo_outcome"]["operation"] == "hypothetical_run_admitted"
    assert run.thread_id == f"hypothetical:{event.id}"
    assert run.target_ref["kind"] == THIRD_SURFACE_TARGET_KIND
    assert receipt.status == "processed"
    assert receipt.target["kind"] == THIRD_SURFACE_TARGET_KIND
    assert receipt.tool_use == {
        "type": "hypothetical_intake",
        "status": "accepted",
        "run_id": run.id,
    }


@pytest.mark.asyncio
async def test_surface_template_owns_failed_admission_completion(
    session,
    monkeypatch,
):
    from brain.systems.inbound.service import submit_inbound_envelope
    from brain.systems.runs.work_intake import WorkIntakeResult

    async def reject_work(_session, _event):
        return WorkIntakeResult(ok=False, skipped_reason="synthetic admission rejection")

    monkeypatch.setattr(
        "brain.systems.inbound.surface_admission.admit_work",
        reject_work,
    )
    connection = await _seed_connection(session)
    _register_third_surface()
    try:
        result = await submit_inbound_envelope(
            session,
            connection=connection,
            envelope={
                "kind": THIRD_SURFACE_KIND,
                "origin": "test.hypothetical",
                "payload": {"message": "Reject this request."},
                "idempotency_key": "hypothetical:failed",
            },
        )
    finally:
        _unregister_third_surface()

    event = (await session.scalars(select(InboundEventRow))).one()
    receipt = (await session.scalars(select(InboundDecisionReceiptRow))).one()

    assert result["status"] == "failed"
    assert result["error"] == "synthetic admission rejection"
    assert event.action_type == "hypothetical.run_admitted"
    assert event.action_result == {
        "operation": "hypothetical_run_admission_failed",
        "reason": "synthetic admission rejection",
        "event_id": str(event.id),
        "origin": "test.hypothetical",
    }
    assert receipt.status == "failed"
    assert receipt.target == {
        "kind": THIRD_SURFACE_TARGET_KIND,
        "event_id": str(event.id),
        "thread_id": f"hypothetical:{event.id}",
    }
    assert receipt.tool_use == {
        "type": "hypothetical_intake",
        "status": "failed",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("surface_kind", "action_type", "operation", "tool_type", "error", "reasoning"),
    [
        (
            "app_report",
            "app_report.run_admitted",
            "app_report_run_admission_failed",
            "app_report_intake",
            "App-report connection has no authority user",
            "App reports need a connection owner to admit customer-request work.",
        ),
        (
            "slack_message",
            "slack.run_admitted",
            "slack_run_admission_failed",
            "slack_teammate_run",
            "Slack connection has no authority user",
            "Slack events need a connection owner for the permissive self-hosted MVP.",
        ),
    ],
    ids=["app-report", "slack"],
)
async def test_actual_surface_missing_authority_receipt_shape(
    session,
    monkeypatch,
    surface_kind,
    action_type,
    operation,
    tool_type,
    error,
    reasoning,
):
    from brain.systems.inbound.service import submit_inbound_envelope

    connection = await _seed_connection(session, surface_kind=surface_kind)

    async def resolve_without_authority(_session, _connection):
        return InboundHandlerContext(
            connection_id=str(connection.id),
            org_id=str(connection.org_id),
            owner_user_id=None,
            token_id=None,
            scopes=None,
            display_name=connection.display_name,
            source_kind=connection.agent_kind,
        )

    monkeypatch.setattr(
        "brain.systems.inbound.service._resolve_connection_context",
        resolve_without_authority,
    )
    result = await submit_inbound_envelope(
        session,
        connection=connection,
        envelope=_surface_envelope(surface_kind),
    )

    event = (await session.scalars(select(InboundEventRow))).one()
    receipt = (await session.scalars(select(InboundDecisionReceiptRow))).one()
    outcome = {
        "operation": operation,
        "reason": "missing_authority_user",
        "event_id": str(event.id),
    }

    assert result["status"] == "failed"
    assert result["ilo_outcome"] == outcome
    assert result["error"] == error
    _assert_failed_surface_receipt(
        event,
        receipt,
        action_type=action_type,
        outcome=outcome,
        error=error,
        target={"kind": surface_kind},
        tool_type=tool_type,
        reasoning_summary=reasoning,
    )


@pytest.mark.asyncio
async def test_actual_app_report_validation_failure_receipt_shape(session):
    from brain.systems.inbound.service import submit_inbound_envelope

    connection = await _seed_connection(session, surface_kind="app_report")
    envelope = _surface_envelope("app_report")
    envelope["payload"].pop("email")
    result = await submit_inbound_envelope(
        session,
        connection=connection,
        envelope=envelope,
    )

    event = (await session.scalars(select(InboundEventRow))).one()
    receipt = (await session.scalars(select(InboundDecisionReceiptRow))).one()
    outcome = {
        "operation": "app_report_run_admission_failed",
        "reason": "invalid_app_report_payload",
        "event_id": str(event.id),
    }

    assert result["status"] == "failed"
    assert result["ilo_outcome"] == outcome
    assert result["error"] == "email is required"
    _assert_failed_surface_receipt(
        event,
        receipt,
        action_type="app_report.run_admitted",
        outcome=outcome,
        error="email is required",
        target={"kind": "app_report"},
        tool_type="app_report_intake",
        reasoning_summary="email is required",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("surface_kind", "action_type", "operation", "tool_type"),
    [
        (
            "app_report",
            "app_report.run_admitted",
            "app_report_run_admission_failed",
            "app_report_intake",
        ),
        (
            "slack_message",
            "slack.run_admitted",
            "slack_run_admission_failed",
            "slack_teammate_run",
        ),
    ],
    ids=["app-report", "slack"],
)
async def test_actual_surface_rejected_admission_receipt_shape(
    session,
    monkeypatch,
    surface_kind,
    action_type,
    operation,
    tool_type,
):
    from brain.systems.inbound.service import submit_inbound_envelope
    from brain.systems.runs.work_intake import WorkIntakeResult

    async def reject_work(_session, _event):
        return WorkIntakeResult(ok=False, skipped_reason="synthetic admission rejection")

    monkeypatch.setattr(
        "brain.systems.inbound.surface_admission.admit_work",
        reject_work,
    )
    connection = await _seed_connection(session, surface_kind=surface_kind)
    envelope = _surface_envelope(surface_kind)
    result = await submit_inbound_envelope(
        session,
        connection=connection,
        envelope=envelope,
    )

    event = (await session.scalars(select(InboundEventRow))).one()
    receipt = (await session.scalars(select(InboundDecisionReceiptRow))).one()
    if surface_kind == "app_report":
        target = {
            "kind": "app_report",
            "event_id": str(event.id),
            "thread_id": f"app-report:{event.id}",
            "profile_id": "profile-123",
            "generation_ids": [901, "gen-902"],
            "batch_ids": [77, "batch-78"],
        }
    else:
        target = {
            "kind": "slack_message",
            "team_id": "T789",
            "channel_id": "C456",
            "message_ts": "1716900000.000100",
            "thread_ts": "1716900000.000100",
            "slack_thread_id": "slack:T789:C456:1716900000.000100",
        }
    outcome = {
        "operation": operation,
        "reason": "synthetic admission rejection",
        "event_id": str(event.id),
        "origin": envelope["origin"],
    }
    if surface_kind == "slack_message":
        outcome["slack"] = target

    assert result["status"] == "failed"
    assert result["ilo_outcome"] == outcome
    assert result["error"] == "synthetic admission rejection"
    _assert_failed_surface_receipt(
        event,
        receipt,
        action_type=action_type,
        outcome=outcome,
        error="synthetic admission rejection",
        target=target,
        tool_type=tool_type,
        reasoning_summary="synthetic admission rejection",
    )


def test_registered_direct_target_requires_thread_id():
    from brain.systems.runs.direct_targets import resolve_direct_target

    assert isinstance(DIRECT_TARGET_KINDS, frozenset)
    with pytest.raises(
        ValueError,
        match="inbound_submission target requires thread_id",
    ):
        resolve_direct_target({"kind": THIRD_SURFACE_TARGET_KIND, "event_id": "event-1"})
