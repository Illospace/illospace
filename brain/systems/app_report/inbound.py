"""Inbound-envelope processing for customer reports submitted from the Uwear app."""

from __future__ import annotations

from typing import Any, Mapping

from sqlalchemy.ext.asyncio import AsyncSession

from brain.kernel.common.coercion import optional_text
from brain.platform.db.models.inbound import InboundEventRow
from brain.systems.app_report.triggers import (
    APP_REPORT_ENVELOPE_KIND,
    AppReportValidationError,
    build_app_report_work_intake_payload,
)
from brain.systems.inbound.handlers import (
    InboundEventCompleter,
    InboundHandlerContext,
)
from brain.systems.inbound.surface_admission import (
    SurfaceAdmissionSpec,
    SurfaceIdentity,
    SurfaceTarget,
    admit_surface_envelope,
)

ACTION_APP_REPORT_RUN_ADMITTED = "app_report.run_admitted"


async def process_app_report_envelope(
    session: AsyncSession,
    *,
    context: InboundHandlerContext,
    event: InboundEventRow,
    normalized: Mapping[str, Any],
    complete: InboundEventCompleter,
) -> dict[str, Any]:
    """Admit one normalized in-app report as a customer-request work signal."""

    return await admit_surface_envelope(
        session,
        context=context,
        event=event,
        normalized=normalized,
        complete=complete,
        spec=APP_REPORT_ADMISSION,
    )


async def _resolve_app_report_identity(
    _session: AsyncSession,
    context: InboundHandlerContext,
    _normalized: Mapping[str, Any],
) -> SurfaceIdentity:
    return SurfaceIdentity(authority_user_id=optional_text(context.owner_user_id))


async def _build_app_report_payload(
    _session: AsyncSession,
    context: InboundHandlerContext,
    event: InboundEventRow,
    normalized: Mapping[str, Any],
    identity: SurfaceIdentity,
) -> dict[str, Any]:
    return build_app_report_work_intake_payload(
        org_id=context.org_id,
        authority_user_id=str(identity.authority_user_id),
        payload=dict(normalized.get("payload") or {}),
        inbound_event_id=str(event.id),
        connection_id=context.connection_id,
        idempotency_key=optional_text(normalized.get("idempotency_key")),
        origin=optional_text(normalized.get("origin")),
    )


def _app_report_target(
    trigger_payload: Mapping[str, Any],
    _normalized: Mapping[str, Any],
) -> SurfaceTarget:
    return SurfaceTarget(value=dict(trigger_payload.get("target") or {}))


def _app_report_ack(
    event_id: str,
    _normalized: Mapping[str, Any],
    trigger_payload: Mapping[str, Any],
    _target: Mapping[str, Any],
) -> Mapping[str, Any]:
    report = dict((trigger_payload.get("payload") or {}).get("app_report") or {})
    return {
        "ack": _reporter_ack(
            event_id=event_id,
            report_type=str(report.get("type") or "report"),
        )
    }


def _reporter_ack(*, event_id: str, report_type: str) -> dict[str, str]:
    return {
        "status": "accepted",
        "message": f"Thanks — your {report_type.lower()} was received.",
        "event_id": event_id,
    }


APP_REPORT_ADMISSION = SurfaceAdmissionSpec(
    kind=APP_REPORT_ENVELOPE_KIND,
    action_type=ACTION_APP_REPORT_RUN_ADMITTED,
    success_operation="app_report_run_admitted",
    failure_operation="app_report_run_admission_failed",
    tool_type="app_report_intake",
    resolve_identity=_resolve_app_report_identity,
    build_payload=_build_app_report_payload,
    build_target=_app_report_target,
    build_ack=_app_report_ack,
    success_tool_status="accepted",
    success_reasoning=(
        "The in-app customer report was acknowledged and admitted through the shared "
        "work-intake boundary with its deterministic generation and batch references."
    ),
    admission_failure_reasoning=(
        "The app report could not be admitted as customer-request work."
    ),
    missing_authority_error="App-report connection has no authority user",
    missing_authority_reasoning=(
        "App reports need a connection owner to admit customer-request work."
    ),
    payload_error_types=(AppReportValidationError,),
    invalid_payload_reason="invalid_app_report_payload",
)


__all__ = ["process_app_report_envelope"]
