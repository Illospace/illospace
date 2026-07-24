"""Inbound-envelope processing for customer reports submitted from the Uwear app."""

from __future__ import annotations

from typing import Any, Mapping

from sqlalchemy.ext.asyncio import AsyncSession

from brain.platform.db.models.inbound import InboundEventRow
from brain.systems.app_report.triggers import (
    APP_REPORT_ENVELOPE_KIND,
    AppReportValidationError,
    build_app_report_work_intake_payload,
)
from brain.systems.inbound.service import _clean_optional, _complete_event
from brain.systems.inbound.status import STATUS_FAILED, STATUS_PROCESSED
from brain.systems.runs.work_intake import WorkIntakeEvent, admit_work

ACTION_APP_REPORT_RUN_ADMITTED = "app_report.run_admitted"


async def process_app_report_envelope(
    session: AsyncSession,
    *,
    context: Any,
    event: InboundEventRow,
    normalized: Mapping[str, Any],
) -> dict[str, Any]:
    """Admit one normalized in-app report as a customer-request work signal."""

    authority_user_id = _clean_optional(context.owner_user_id)
    if not authority_user_id:
        return await _complete_event(
            session,
            event,
            policy=None,
            status=STATUS_FAILED,
            action_type=ACTION_APP_REPORT_RUN_ADMITTED,
            action_result={
                "operation": "app_report_run_admission_failed",
                "reason": "missing_authority_user",
                "event_id": str(event.id),
            },
            confidence=0.0,
            error="App-report connection has no authority user",
            target={"kind": APP_REPORT_ENVELOPE_KIND},
            tool_use={"type": "app_report_intake", "status": "failed"},
            reasoning_summary="App reports need a connection owner to admit customer-request work.",
        )

    try:
        trigger_payload = build_app_report_work_intake_payload(
            org_id=context.org_id,
            authority_user_id=authority_user_id,
            payload=dict(normalized.get("payload") or {}),
            inbound_event_id=str(event.id),
            connection_id=context.connection_id,
            idempotency_key=_clean_optional(normalized.get("idempotency_key")),
            origin=_clean_optional(normalized.get("origin")),
        )
    except AppReportValidationError as exc:
        return await _complete_event(
            session,
            event,
            policy=None,
            status=STATUS_FAILED,
            action_type=ACTION_APP_REPORT_RUN_ADMITTED,
            action_result={
                "operation": "app_report_run_admission_failed",
                "reason": "invalid_app_report_payload",
                "event_id": str(event.id),
            },
            confidence=0.0,
            error=str(exc),
            target={"kind": APP_REPORT_ENVELOPE_KIND},
            tool_use={"type": "app_report_intake", "status": "failed"},
            reasoning_summary=str(exc),
        )

    admission = await admit_work(
        session,
        WorkIntakeEvent.from_trigger_payload(trigger_payload),
    )
    report = dict((trigger_payload.get("payload") or {}).get("app_report") or {})
    target = dict(trigger_payload.get("target") or {})
    if admission.ok:
        if admission.run_id is not None:
            target["run_id"] = admission.run_id
        ack = _reporter_ack(
            event_id=str(event.id),
            report_type=str(report.get("type") or "report"),
        )
        return await _complete_event(
            session,
            event,
            policy=None,
            status=STATUS_PROCESSED,
            action_type=ACTION_APP_REPORT_RUN_ADMITTED,
            action_result={
                "operation": "app_report_run_admitted",
                "run_id": admission.run_id,
                "event_id": str(event.id),
                "origin": normalized.get("origin"),
                "ack": ack,
            },
            confidence=1.0,
            target=target,
            tool_use={
                "type": "app_report_intake",
                "status": "accepted",
                "run_id": admission.run_id,
            },
            reasoning_summary=(
                "The in-app customer report was acknowledged and admitted through the shared "
                "work-intake boundary with its deterministic generation and batch references."
            ),
            reusable_pattern_candidate={
                "kind": APP_REPORT_ENVELOPE_KIND,
                "origin": normalized.get("origin"),
                "source_kind": context.source_kind,
            },
        )

    return await _complete_event(
        session,
        event,
        policy=None,
        status=STATUS_FAILED,
        action_type=ACTION_APP_REPORT_RUN_ADMITTED,
        action_result={
            "operation": "app_report_run_admission_failed",
            "reason": admission.skipped_reason or "run_admission_failed",
            "event_id": str(event.id),
            "origin": normalized.get("origin"),
        },
        confidence=0.0,
        error=admission.skipped_reason or "run_admission_failed",
        target=target,
        tool_use={"type": "app_report_intake", "status": "failed"},
        reasoning_summary=admission.skipped_reason
        or "The app report could not be admitted as customer-request work.",
    )


def _reporter_ack(*, event_id: str, report_type: str) -> dict[str, str]:
    return {
        "status": "accepted",
        "message": f"Thanks — your {report_type.lower()} was received.",
        "event_id": event_id,
    }


__all__ = ["process_app_report_envelope"]
