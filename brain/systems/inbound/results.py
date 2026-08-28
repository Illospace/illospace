"""Read async inbound submission results."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from brain.platform.db.models.agent_run import AgentRunEventRow, AgentRunRow
from brain.systems.inbound import admin as inbound_admin
from brain.systems.inbound.reconciliation import reconcile_inbound_triage_run


@dataclass(frozen=True)
class InboundSubmissionResult:
    payload: dict[str, Any]
    mutated_inbound: bool = False


_TOOL_EXECUTION_EVENT_TYPES = frozenset(
    {"run.tool_started", "run.tool_completed", "run.tool_failed"}
)
_SAFE_DIAGNOSTIC_LABEL = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]{0,79}$")
_CAPTURED_EXCEPTION_CLASS = re.compile(
    r"^run_execution_failed: ([A-Za-z_][A-Za-z0-9_.]{0,79}):"
)


def _result_handling(action_result: dict[str, Any]) -> dict[str, Any]:
    handling = action_result.get("handling")
    if isinstance(handling, dict):
        return dict(handling)
    if action_result.get("operation") == "slack_run_admitted":
        return dict(action_result)
    return {}


def _run_id(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _safe_diagnostic_label(value: Any) -> str | None:
    candidate = str(value or "").strip()
    return candidate if _SAFE_DIAGNOSTIC_LABEL.fullmatch(candidate) else None


async def _failure_diagnostic(
    session: AsyncSession,
    *,
    run: AgentRunRow,
) -> dict[str, Any] | None:
    """Build bounded operator diagnostics for the programmatic receipt."""

    if str(run.status or "") != "failed":
        return None

    events = list(
        (
            await session.scalars(
                select(AgentRunEventRow)
                .where(
                    AgentRunEventRow.run_id == int(run.id),
                    AgentRunEventRow.event_type.in_(
                        (*_TOOL_EXECUTION_EVENT_TYPES, "run.failed")
                    ),
                )
                .order_by(AgentRunEventRow.sequence_no.desc(), AgentRunEventRow.id.desc())
            )
        ).all()
    )
    tool_execution_started = any(
        str(event.event_type or "") in _TOOL_EXECUTION_EVENT_TYPES for event in events
    )
    failure_event = next(
        (event for event in events if str(event.event_type or "") == "run.failed"),
        None,
    )
    event_payload = (
        dict(failure_event.payload or {}) if failure_event is not None else {}
    )
    metadata_failure = dict((run.metadata_ or {}).get("failure") or {})
    stage = _safe_diagnostic_label(
        metadata_failure.get("stage") or event_payload.get("failure_stage")
    ) or ("tool_execution" if tool_execution_started else "run_execution")
    exception_class = _safe_diagnostic_label(
        metadata_failure.get("exception_class") or event_payload.get("exception_class")
    )
    if exception_class is None:
        captured = _CAPTURED_EXCEPTION_CLASS.match(str(event_payload.get("error") or ""))
        exception_class = captured.group(1) if captured is not None else None

    # Reconciliation follows a replacement run before this helper is called.
    # A current run that is still failed has no scheduled successor.
    return {
        "stage": stage,
        "exception_class": exception_class,
        "tool_execution_started": tool_execution_started,
        "terminal": True,
        "retry_scheduled": False,
    }


async def read_inbound_submission_result(
    session: AsyncSession,
    *,
    org_id: str,
    connection_id: str,
    event_id: str,
    include_payload: bool = True,
    limit: int = 25,
) -> InboundSubmissionResult:
    event = await inbound_admin.require_event_for_org(session, org_id=org_id, event_id=event_id)
    if str(event.connection_id) != str(connection_id):
        raise ValueError("Inbound event not found")

    action_result = dict(event.action_result or {})
    handling = _result_handling(action_result)
    reconciled = False
    current_run = None
    current_run_status = None
    selected_run_id = _run_id(handling.get("run_id"))
    if selected_run_id is not None:
        current_run = await session.get(AgentRunRow, selected_run_id)
        current_run_status = getattr(current_run, "status", None) if current_run is not None else None
        receipt = await reconcile_inbound_triage_run(session, selected_run_id)
        reconciled = receipt is not None
        if reconciled:
            await session.refresh(event)

    # Reconciliation can replace a failed monitored-channel run. Re-read the
    # event-owned contract so illo_get_result follows the replacement rather
    # than returning the original terminal run forever.
    action_result = dict(event.action_result or {})
    handling = _result_handling(action_result)
    current_run_id = _run_id(handling.get("run_id"))
    if current_run_id is not None and current_run_id != selected_run_id:
        current_run = await session.get(AgentRunRow, current_run_id)
        current_run_status = getattr(current_run, "status", None) if current_run is not None else None

    receipts = await inbound_admin.list_receipts(
        session,
        org_id=org_id,
        event_id=str(event.id),
        limit=limit,
    )
    receipt_payloads = [inbound_admin.serialize_receipt(receipt) for receipt in receipts]
    event_payload = inbound_admin.serialize_event(event, include_payload=include_payload)

    preservation = dict(action_result.get("preservation") or {})
    evidence_contract = dict(handling.get("evidence_contract") or {})
    requires_evidence = bool(
        evidence_contract.get("required")
        or preservation.get("requires_durable_evidence")
    )
    evidence_status = str(
        evidence_contract.get("status")
        or ("pending" if requires_evidence else "not_required")
    )
    latest_receipt = receipt_payloads[0] if receipt_payloads else None
    failure = next(
        (
            dict(candidate["failure"])
            for candidate in (event_payload, latest_receipt)
            if isinstance(candidate, dict) and isinstance(candidate.get("failure"), dict)
        ),
        None,
    )
    if failure is not None and current_run is not None:
        diagnostic = await _failure_diagnostic(session, run=current_run)
        if diagnostic is not None:
            failure["diagnostic"] = diagnostic
    return InboundSubmissionResult(
        payload={
            "event_id": str(event.id),
            "submission_id": str(event.id),
            "result_id": str(event.id),
            "status": event.status,
            "handling_status": handling.get("status"),
            "run_id": handling.get("run_id"),
            "run_status": handling.get("run_status") or current_run_status,
            "retry_attempt": handling.get("retry_attempt"),
            "original_run_id": handling.get("original_run_id"),
            "replacement_run_id": handling.get("replacement_run_id"),
            "retry_lineage": handling.get("retry_lineage"),
            "requires_durable_evidence": requires_evidence,
            "evidence_status": evidence_status,
            "evidence_contract": evidence_contract or preservation or None,
            "event": event_payload,
            "latest_receipt": latest_receipt,
            "receipts": receipt_payloads,
            **({"failure": failure} if failure is not None else {}),
        },
        mutated_inbound=reconciled,
    )


__all__ = ["InboundSubmissionResult", "read_inbound_submission_result"]
