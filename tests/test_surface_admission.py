from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from brain.platform.db.models.agent_run import AgentRunEventRow, AgentRunRow
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
from brain.systems.runs.direct_targets import (
    register_direct_target_kind,
    unregister_direct_target_kind,
)


ORG_ID = "11111111-1111-4111-8111-111111111111"
USER_ID = "22222222-2222-4222-8222-222222222222"
CONNECTION_ID = "33333333-3333-4333-8333-333333333333"
THIRD_SURFACE_KIND = "hypothetical_third_surface"


@pytest.fixture
async def session(async_sqlite_session_factory, sqlite_postgres_ddl_patch):
    return await async_sqlite_session_factory(
        [
            Org.__table__,
            User.__table__,
            ExternalAgentConnectionRow.__table__,
            AgentRunRow.__table__,
            AgentRunEventRow.__table__,
            OpenAsk.__table__,
            InboundEventRow.__table__,
            InboundDecisionReceiptRow.__table__,
        ]
    )


async def _seed_connection(session: AsyncSession) -> ExternalAgentConnectionRow:
    session.add(Org(id=ORG_ID, name="Test Org", slug="test-org"))
    session.add(User(id=USER_ID, org_id=ORG_ID, name="Reda", email="reda@example.com"))
    connection = ExternalAgentConnectionRow(
        id=CONNECTION_ID,
        org_id=ORG_ID,
        owner_user_id=USER_ID,
        display_name="Hypothetical surface",
        agent_kind=THIRD_SURFACE_KIND,
        transport="webhook",
        status="online",
        remote_agent_card={},
        capabilities={THIRD_SURFACE_KIND: True},
        auth_metadata={},
        metadata_={},
    )
    session.add(connection)
    await session.flush()
    return connection


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
            "kind": THIRD_SURFACE_KIND,
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
    register_direct_target_kind(THIRD_SURFACE_KIND)
    register_inbound_envelope_handler(THIRD_SURFACE_KIND, _process_third_surface)


def _unregister_third_surface() -> None:
    unregister_inbound_envelope_handler(THIRD_SURFACE_KIND)
    unregister_direct_target_kind(THIRD_SURFACE_KIND)


@pytest.mark.asyncio
async def test_registered_third_surface_admits_without_work_intake_kind_edit(session):
    from brain.systems.inbound.service import submit_inbound_envelope

    connection = await _seed_connection(session)
    work_intake_source = (
        Path(__file__).resolve().parents[1]
        / "brain/systems/runs/work_intake.py"
    ).read_text(encoding="utf-8")
    assert THIRD_SURFACE_KIND not in work_intake_source

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
    assert run.target_ref["kind"] == THIRD_SURFACE_KIND
    assert receipt.status == "processed"
    assert receipt.target["kind"] == THIRD_SURFACE_KIND
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
        "kind": THIRD_SURFACE_KIND,
        "event_id": str(event.id),
        "thread_id": f"hypothetical:{event.id}",
    }
    assert receipt.tool_use == {
        "type": "hypothetical_intake",
        "status": "failed",
    }


def test_registered_lane_handlers_use_the_public_typed_contract():
    root = Path(__file__).resolve().parents[1]
    for relative_path in (
        "brain/systems/slack/inbound.py",
        "brain/systems/app_report/inbound.py",
    ):
        source = (root / relative_path).read_text(encoding="utf-8")
        assert "from brain.systems.inbound.service import" not in source
        assert "context: Any" not in source
        assert "_complete_event" not in source
        assert "_clean_optional" not in source
        assert "context: InboundHandlerContext" in source
        assert "complete: InboundEventCompleter" in source


def test_registered_direct_target_requires_thread_id():
    from brain.systems.runs.direct_targets import resolve_direct_target

    register_direct_target_kind(THIRD_SURFACE_KIND)
    try:
        with pytest.raises(
            ValueError,
            match="hypothetical_third_surface target requires thread_id",
        ):
            resolve_direct_target({"kind": THIRD_SURFACE_KIND, "event_id": "event-1"})
    finally:
        unregister_direct_target_kind(THIRD_SURFACE_KIND)
