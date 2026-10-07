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
from brain.systems.runs.status import TERMINAL_RUN_STATUSES, coerce_run_status


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


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _select_result_handling(action_result: Any) -> tuple[dict[str, Any], str | None]:
    """Select the event-owned current block and its path, including legacy runs."""
    action_result = _as_dict(action_result)
    handling = _result_handling(action_result)
    if handling:
        return handling, "handling" if isinstance(action_result.get("handling"), dict) else None
    triage = action_result.get("triage")
    return (triage, "triage") if isinstance(triage, dict) else ({}, None)


def _without_equal_value(value: Any, path: tuple[str, ...], published: Any) -> Any:
    """Copy only a known path, removing its value if an equal copy is published."""
    if published is None or published == {} or not isinstance(value, dict) or path[0] not in value:
        return value
    key, *rest = path
    if rest:
        return {**value, key: _without_equal_value(value[key], tuple(rest), published)}
    if value[key] == published:
        return {name: item for name, item in value.items() if name != key}
    return value


# None marks the selected handling or triage block in these known paths.
_RESULT_DUPLICATE_PATHS = (
    (("event", "action_result", None, "final_answer"), "final_answer"),
    (("event", "action_result", None, "result", "final_answer"), "final_answer"),
    (("event", "action_result", None, "attribution"), "attribution"),
    (("event", "action_result", None, "evidence_contract"), "evidence_contract"),
    (("latest_receipt", "outcome", None, "final_answer"), "final_answer"),
    (("latest_receipt", "outcome", None, "result", "final_answer"), "final_answer"),
    (("latest_receipt", "outcome", None, "attribution"), "attribution"),
    (("latest_receipt", "outcome", None, "evidence_contract"), "evidence_contract"),
    (("latest_receipt", "tool_use", "attribution"), "attribution"),
    (("latest_receipt", "tool_use", "evidence_contract"), "evidence_contract"),
)


def _without_result_duplicates(
    value: dict[str, Any], source: str | None, published: dict[str, Any]
) -> dict[str, Any]:
    for path, key in _RESULT_DUPLICATE_PATHS:
        if None in path and source is None:
            continue
        if key != "final_answer" and not isinstance(published[key], dict):
            continue
        selected_path = tuple(source if part is None else part for part in path)
        value = _without_equal_value(value, selected_path, published[key])
    return value


def _run_id(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def project_inbound_submission_result(
    payload: dict[str, Any], *, compact: bool = False
) -> dict[str, Any]:
    """Publish current result fields once at known paths; retain distinct history.

    Unknown shapes and unequal nested values are preserved. Compact polling includes
    terminal status, any terminal answer, and the existing public failure summary.
    """
    event = _as_dict(payload.get("event"))
    handling, source = _select_result_handling(event.get("action_result"))
    result = _as_dict(handling.get("result"))
    attribution = handling.get("attribution", payload.get("attribution", {}))
    evidence_contract = payload.get("evidence_contract")
    final_answer = handling.get("final_answer", result.get("final_answer", payload.get("final_answer")))
    summary = {
        "event_id": payload.get("event_id"),
        "run_id": payload.get("run_id"),
        "run_status": payload.get("run_status"),
        "handling_status": payload.get("handling_status"),
        "evidence_status": payload.get("evidence_status"),
        "completed_at": handling.get("completed_at", payload.get("completed_at")),
        "reconciled_at": handling.get("reconciled_at", payload.get("reconciled_at")),
        "mutated_target_refs": _as_dict(evidence_contract).get(
            "mutated_target_refs", _as_dict(attribution).get("mutated_target_refs", [])
        ),
        "attribution": attribution,
    }
    if compact:
        if isinstance(attribution, dict):
            summary["attribution"] = {"tags": attribution.get("tags", [])}
        summary["terminal"] = coerce_run_status(summary["run_status"]) in TERMINAL_RUN_STATUSES
        if summary["terminal"]:
            if final_answer is not None:
                summary["final_answer"] = final_answer
            failure = payload.get("failure")
            if isinstance(failure, dict):
                summary["failure"] = {
                    key: failure[key] for key in ("status", "category", "message") if key in failure
                }
            elif failure is not None:
                summary["failure"] = failure
        return summary

    projected = {**payload, **summary, "final_answer": final_answer, "evidence_contract": evidence_contract}
    projected = _without_result_duplicates(projected, source, projected)
    latest_receipt = payload.get("latest_receipt")
    receipts = payload.get("receipts")
    if (
        isinstance(receipts, list) and receipts
        and _as_dict(latest_receipt).get("id") is not None
        and _as_dict(receipts[0]).get("id") == latest_receipt["id"]
    ):
        # Omit a fully duplicated receipt; retain any distinct same-id data.
        projected["receipts"] = (
            receipts[1:] if receipts[0] == latest_receipt
            else [
                _without_result_duplicates(
                    {"latest_receipt": receipts[0]}, source, projected
                )["latest_receipt"],
                *receipts[1:],
            ]
        )
    return projected


async def read_inbound_submission_result(
    session: AsyncSession,
    *,
    org_id: str,
    connection_id: str,
    event_id: str,
    include_payload: bool = True,
    limit: int = 25,
) -> InboundSubmissionResult:
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
    if str(event.connection_id) != str(connection_id):
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
