"""Read async inbound submission results."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from brain.platform.db.models.agent_run import AgentRunRow
from brain.systems.inbound import admin as inbound_admin
from brain.systems.inbound.reconciliation import reconcile_inbound_triage_run
from brain.systems.runs.failure_diagnostic import read_run_failure_diagnostic
from brain.systems.runs.failures import public_agent_start_retry_failure


class InboundSubmissionResultState(str, Enum):
    """Whether an inbound event result is visible to the caller."""

    FOUND = "found"
    NOT_FOUND = "not_found"
    NOT_VISIBLE_TO_CONNECTION = "not_visible_to_connection"


@dataclass(frozen=True)
class InboundSubmissionResult:
    state: InboundSubmissionResultState
    payload: dict[str, Any] | None = None
    mutated_inbound: bool = False

    def __post_init__(self) -> None:
        has_payload = self.payload is not None
        if has_payload != (self.state is InboundSubmissionResultState.FOUND):
            raise ValueError("payload must be present if and only if state is FOUND")


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


def project_inbound_submission_result(
    payload: dict[str, Any], *, compact: bool = False
) -> dict[str, Any]:
    """Project the full internal result for external MCP callers without mutating it.

    final_answer, evidence_contract and attribution have one top-level home.
    Evidence remains readable at evidence_status and evidence_contract.status;
    evidence_contract.mutated_target_refs remains available as well. Top-level
    mutated_target_refs, attribution.tags, completed_at and reconciled_at expose
    the current handling result without traversing receipt history.
    latest_receipt holds the newest receipt; receipts holds only older receipts.
    Existing status fields and nested completion timestamps remain readable.
    """
    event = payload.get("event") or {}
    action_result = event.get("action_result") or {}
    latest_receipt = payload.get("latest_receipt") or {}
    outcome = latest_receipt.get("outcome") or {}
    handling = (
        _result_handling(action_result)
        or action_result.get("triage")
        or outcome.get("handling")
        or outcome.get("triage")
        or {}
    )
    result = handling.get("result") or {}
    tool_use = latest_receipt.get("tool_use") or {}
    attribution = handling.get("attribution") or tool_use.get("attribution") or {}
    evidence_contract = payload.get("evidence_contract")
    final_answer = handling.get("final_answer", result.get("final_answer"))
    summary = {
        "event_id": payload.get("event_id"),
        "run_id": payload.get("run_id"),
        "run_status": payload.get("run_status"),
        "handling_status": payload.get("handling_status"),
        "evidence_status": payload.get("evidence_status"),
        "completed_at": handling.get("completed_at"),
        "reconciled_at": handling.get("reconciled_at"),
        "mutated_target_refs": (evidence_contract or {}).get(
            "mutated_target_refs", attribution.get("mutated_target_refs", [])
        ),
        "attribution": attribution,
    }
    if compact:
        summary["attribution"] = {"tags": attribution.get("tags", [])}
        if str(payload.get("run_status") or "").upper() == "COMPLETED":
            summary["final_answer"] = final_answer
        return summary

    def without_duplicates(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: without_duplicates(item)
                for key, item in value.items()
                if key not in {"final_answer", "evidence_contract", "attribution"}
            }
        if isinstance(value, list):
            return [without_duplicates(item) for item in value]
        return value

    def receipt_view(receipt: dict[str, Any]) -> dict[str, Any]:
        return {
            key: without_duplicates(value) if key in {"outcome", "tool_use"} else value
            for key, value in receipt.items()
        }

    return {
        **payload,
        **summary,
        "final_answer": final_answer,
        "evidence_contract": evidence_contract,
        "event": {**event, "action_result": without_duplicates(action_result)},
        "latest_receipt": receipt_view(latest_receipt) if latest_receipt else None,
        "receipts": [receipt_view(receipt) for receipt in (payload.get("receipts") or [])[1:]],
    }


async def read_inbound_submission_result(
    session: AsyncSession,
    *,
    org_id: str,
    connection_id: str,
    event_id: str,
    include_payload: bool = True,
    limit: int = 25,
    expected_connection_id: str | None = None,
) -> InboundSubmissionResult:
    """Read an owned event; also check the connection encoded in a thread id, if supplied."""
    try:
        event = await inbound_admin.require_event_for_org(
            session,
            org_id=org_id,
            event_id=event_id,
        )
    except inbound_admin.InboundAdminError:
        return InboundSubmissionResult(
            state=InboundSubmissionResultState.NOT_FOUND,
        )
    if str(event.connection_id) != str(connection_id) or (
        expected_connection_id is not None and expected_connection_id != str(connection_id)
    ):
        return InboundSubmissionResult(
            state=InboundSubmissionResultState.NOT_VISIBLE_TO_CONNECTION,
        )

    action_result = dict(event.action_result or {})
    handling = _result_handling(action_result)
    reconciled = False
    current_run = None
    current_run_status = None
    selected_run_id = _run_id(handling.get("run_id"))
    if selected_run_id is not None:
        current_run = await session.get(AgentRunRow, selected_run_id)
        current_run_status = (
            getattr(current_run, "status", None) if current_run is not None else None
        )
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
        current_run_status = (
            getattr(current_run, "status", None) if current_run is not None else None
        )

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
    if current_run is not None:
        diagnostic = await read_run_failure_diagnostic(session, run=current_run)
        if diagnostic is not None and failure is None and diagnostic.retry_scheduled:
            failure = public_agent_start_retry_failure()
        if diagnostic is not None and failure is not None:
            failure["diagnostic"] = diagnostic.as_payload()
    return InboundSubmissionResult(
        state=InboundSubmissionResultState.FOUND,
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


__all__ = [
    "InboundSubmissionResult",
    "InboundSubmissionResultState",
    "project_inbound_submission_result",
    "read_inbound_submission_result",
]
