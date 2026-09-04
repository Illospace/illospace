"""Reconcile inbound decision receipts after inbound-admitted runs finish."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from brain.platform.db.models.agent_run import AgentRunArtifactRow, AgentRunEventRow, AgentRunRow
from brain.platform.db.models.inbound import InboundDecisionReceiptRow, InboundEventRow
from brain.platform.integrations.provider_error_sentinel import (
    is_retryable_provider_error,
)
from brain.platform.integrations.providers import is_transient_transport_disconnect
from brain.systems.inbound.attribution import summarize_inbound_run_attribution
from brain.systems.inbound.preservation import (
    PRESERVATION_MISSING_REASON,
    preservation_contract_from_run_metadata,
    preservation_evidence_result,
)
from brain.systems.inbound.status import STATUS_REVIEW_REQUIRED
from brain.systems.runs.domain import AgentRun, ArtifactType
from brain.systems.runs.failures import failure_category_for_error, public_run_failure
from brain.systems.runs.interactive_reply import is_interactive_transport_fallback
from brain.systems.runs.status import RunStatus, TERMINAL_RUN_STATUSES, coerce_run_status

logger = logging.getLogger(__name__)

# Receipt lanes. Triage/submit receipts are written NON-terminal at admission
# and this module transitions them when the run finishes; the slack-teammate
# lane writes its receipt already-terminal ("run admitted" IS its decision).
# The monitored-channel transient failure path is the deliberate exception: it
# rewrites the receipt/event once to follow the replacement run.
_TRIAGE_RECEIPT_TYPES = frozenset({"illo_triage", "illo_submit"})
_ACTIONABLE_RECEIPT_TYPES = frozenset({"slack_teammate_run"})
_MONITORED_CHANNEL_ORIGIN = "slack.channel_message"
_MAX_MONITORED_CHANNEL_RETRY_ATTEMPTS = 1


def _json_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _iso(value: Any) -> str | None:
    return value.isoformat() if hasattr(value, "isoformat") else (str(value) if value else None)


def _run_metadata(run: AgentRunRow | AgentRun) -> dict[str, Any]:
    value = getattr(run, "metadata_", None)
    if value is None:
        value = getattr(run, "metadata", None)
    return _json_dict(value)


def _run_datetime(run: AgentRunRow | AgentRun, status: RunStatus) -> datetime | None:
    if status == RunStatus.COMPLETED:
        return getattr(run, "completed_at", None)
    if status == RunStatus.FAILED:
        return getattr(run, "failed_at", None)
    if status == RunStatus.CANCELED:
        return getattr(run, "canceled_at", None)
    return getattr(run, "updated_at", None)


def _receipt_terminal_status(status: RunStatus) -> str:
    return "processed" if status == RunStatus.COMPLETED else "failed"


def _triage_terminal_payload(
    *,
    run_id: int,
    status: RunStatus,
    reconciled_at: datetime,
    terminal_at: datetime | None,
    final_answer: str | None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "run_id": run_id,
        "run_status": status.value,
        "status": status.value,
        "reconciled_at": reconciled_at.isoformat(),
    }
    if terminal_at is not None:
        payload["completed_at"] = _iso(terminal_at)
    if final_answer:
        payload["final_answer"] = final_answer
    return payload


def _triage_result(status: RunStatus, final_answer: str | None) -> dict[str, Any]:
    result: dict[str, Any] = {"status": status.value}
    if final_answer:
        result["final_answer"] = final_answer
    return result


def _tool_use_terminal_payload(
    *,
    status: RunStatus,
    reconciled_at: datetime,
    terminal_at: datetime | None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "status": status.value,
        "run_status": status.value,
        "reconciled_at": reconciled_at.isoformat(),
    }
    if terminal_at is not None:
        payload["completed_at"] = _iso(terminal_at)
    return payload


async def _latest_final_answer(session: AsyncSession, run_id: int) -> str | None:
    row = (
        await session.scalars(
            select(AgentRunArtifactRow)
            .where(
                AgentRunArtifactRow.run_id == int(run_id),
                AgentRunArtifactRow.artifact_type == ArtifactType.FINAL_ANSWER.value,
            )
            .order_by(AgentRunArtifactRow.created_at.desc(), AgentRunArtifactRow.id.desc())
            .limit(1)
        )
    ).first()
    text = str(getattr(row, "text", "") or "").strip() if row is not None else ""
    return text or None


async def _inbound_receipt_for_run(
    session: AsyncSession,
    *,
    event_id: str,
    run_id: int,
    receipt_types: frozenset[str],
) -> InboundDecisionReceiptRow | None:
    rows = (
        await session.scalars(
            select(InboundDecisionReceiptRow)
            .where(InboundDecisionReceiptRow.event_id == str(event_id))
            .order_by(InboundDecisionReceiptRow.created_at.desc(), InboundDecisionReceiptRow.id.desc())
        )
    ).all()
    for row in rows:
        tool_use = _json_dict(row.tool_use)
        target = _json_dict(row.target)
        if tool_use.get("type") not in receipt_types:
            continue
        if str(tool_use.get("run_id") or target.get("run_id") or "") == str(run_id):
            return row
    return None


async def _run_failure_reason(session: AsyncSession, run_id: int) -> str | None:
    """Read the durable, typed run-failure record used by terminal reconciliation.

    ``set_status`` appends ``run.status_changed`` before invoking this module;
    ``run.failed`` is appended slightly later by the engine. Supporting both
    keeps explicit reconciliation of an already-failed run equivalent to the
    inline terminal-status path without classifying arbitrary event/output text.
    """

    rows = (
        await session.scalars(
            select(AgentRunEventRow)
            .where(
                AgentRunEventRow.run_id == int(run_id),
                AgentRunEventRow.event_type.in_(("run.status_changed", "run.failed")),
            )
            .order_by(AgentRunEventRow.sequence_no.desc(), AgentRunEventRow.id.desc())
        )
    ).all()
    for failure_event in rows:
        payload = _json_dict(failure_event.payload)
        if failure_event.event_type == "run.status_changed":
            if str(payload.get("to_status") or "") != RunStatus.FAILED.value:
                continue
            reason = payload.get("reason")
        else:
            reason = payload.get("error")
        text = str(reason or "").strip()
        if text:
            return text
    return None


async def _run_failure_category(session: AsyncSession, run_id: int) -> Any:
    rows = (
        await session.scalars(
            select(AgentRunEventRow)
            .where(
                AgentRunEventRow.run_id == int(run_id),
                AgentRunEventRow.event_type == "run.failed",
            )
            .order_by(AgentRunEventRow.sequence_no.desc(), AgentRunEventRow.id.desc())
        )
    ).all()
    for failure_event in rows:
        payload = _json_dict(failure_event.payload)
        category = payload.get("failure_category") or payload.get("category")
        if category:
            return category
    return failure_category_for_error(await _run_failure_reason(session, run_id))


def _retry_attempt_from_contract(
    *,
    event: InboundEventRow,
    receipt: InboundDecisionReceiptRow,
    run_metadata: dict[str, Any],
) -> int:
    values = [
        run_metadata.get("retry_attempt"),
        _json_dict(run_metadata.get("inbound_event")).get("retry_attempt"),
        _json_dict(event.action_result).get("retry_attempt"),
        _json_dict(receipt.tool_use).get("retry_attempt"),
        _json_dict(receipt.outcome).get("retry_attempt"),
    ]
    attempts = []
    for value in values:
        try:
            attempts.append(int(value or 0))
        except (TypeError, ValueError):
            continue
    return max(attempts, default=0)


def _stored_event_envelope(event: InboundEventRow) -> dict[str, Any]:
    envelope = _json_dict(event.envelope)
    if envelope:
        return envelope
    normalized = _json_dict(event.normalized_payload)
    if normalized:
        return normalized
    return {
        "kind": event.kind,
        "origin": event.origin,
        "payload": _json_dict(event.raw_payload),
        "idempotency_key": event.idempotency_key,
    }


def _attempt_idempotency_key(
    event: InboundEventRow,
    run_row: AgentRunRow | AgentRun,
    *,
    retry_attempt: int,
) -> str | None:
    envelope = _stored_event_envelope(event)
    candidates = (
        envelope.get("idempotency_key"),
        event.idempotency_key,
        getattr(run_row, "source_idempotency_key", None),
    )
    base_key = next((str(value).strip() for value in candidates if str(value or "").strip()), "")
    if not base_key.startswith("slack:"):
        return None
    return f"{base_key}:attempt:{retry_attempt}"


def _retry_lineage(original_run_id: int, replacement_run_id: int) -> list[dict[str, int]]:
    return [
        {"run_id": int(original_run_id), "retry_attempt": 0},
        {"run_id": int(replacement_run_id), "retry_attempt": 1},
    ]


def _record_readmission(
    *,
    event: InboundEventRow,
    receipt: InboundDecisionReceiptRow,
    original_run_id: int,
    replacement_run_id: int,
    idempotency_key: str,
) -> None:
    lineage = _retry_lineage(original_run_id, replacement_run_id)
    retry_contract = {
        "run_id": int(replacement_run_id),
        "original_run_id": int(original_run_id),
        "replacement_run_id": int(replacement_run_id),
        "retry_attempt": 1,
        "retry_lineage": lineage,
        "retry_idempotency_key": idempotency_key,
    }

    action_result = _json_dict(event.action_result)
    slack = _json_dict(action_result.get("slack"))
    if slack:
        action_result["slack"] = {**slack, "run_id": int(replacement_run_id)}
    event.action_result = {**action_result, **retry_contract}

    receipt.outcome = {**_json_dict(receipt.outcome), **event.action_result}
    receipt.target = {**_json_dict(receipt.target), **retry_contract}
    receipt.tool_use = {**_json_dict(receipt.tool_use), **retry_contract}


async def _readmit_failed_monitored_channel_once(
    session: AsyncSession,
    *,
    event: InboundEventRow,
    receipt: InboundDecisionReceiptRow,
    run_row: AgentRunRow | AgentRun,
    run_metadata: dict[str, Any],
    run_id: int,
) -> bool:
    """Admit one replacement run for a typed transient monitor failure."""

    if str(event.origin or "") != _MONITORED_CHANNEL_ORIGIN:
        return False
    retry_attempt = _retry_attempt_from_contract(
        event=event,
        receipt=receipt,
        run_metadata=run_metadata,
    )
    if retry_attempt >= _MAX_MONITORED_CHANNEL_RETRY_ATTEMPTS:
        return False

    failure_reason = await _run_failure_reason(session, run_id)
    if not (
        is_transient_transport_disconnect(failure_reason)
        or is_interactive_transport_fallback(failure_reason)
        or is_retryable_provider_error(failure_reason)
    ):
        return False

    next_attempt = retry_attempt + 1
    idempotency_key = _attempt_idempotency_key(
        event,
        run_row,
        retry_attempt=next_attempt,
    )
    authority_user_id = str(
        getattr(run_row, "user_id", None) or event.authority_user_id or ""
    ).strip()
    if not idempotency_key or not authority_user_id:
        logger.warning(
            "monitored channel re-admission skipped: event=%s run=%s missing=%s",
            event.id,
            run_id,
            "idempotency_key" if not idempotency_key else "authority_user_id",
        )
        return False

    from brain.systems.runs.work_intake import WorkIntakeEvent, admit_work
    from brain.systems.slack.triggers import build_slack_work_intake_payload

    envelope = _stored_event_envelope(event)
    trigger_payload = build_slack_work_intake_payload(
        org_id=str(event.org_id),
        authority_user_id=authority_user_id,
        payload=_json_dict(envelope.get("payload")),
        inbound_event_id=str(event.id),
        connection_id=str(event.connection_id),
        idempotency_key=idempotency_key,
    )
    payload = _json_dict(trigger_payload.get("payload"))
    metadata = _json_dict(payload.get("metadata"))
    inbound_event = _json_dict(metadata.get("inbound_event"))
    metadata.update(
        {
            "retry_attempt": next_attempt,
            "retry_lineage": [{"run_id": int(run_id), "retry_attempt": retry_attempt}],
            "inbound_event": {
                **inbound_event,
                "retry_attempt": next_attempt,
                "original_run_id": int(run_id),
            },
        }
    )
    trigger_payload["payload"] = {**payload, "metadata": metadata}

    admission = await admit_work(
        session,
        WorkIntakeEvent.from_trigger_payload(trigger_payload),
    )
    if not admission.ok or admission.run_id is None:
        logger.warning(
            "monitored channel re-admission failed: event=%s run=%s reason=%s",
            event.id,
            run_id,
            admission.skipped_reason or "run_admission_failed",
        )
        return False

    replacement_run_id = int(admission.run_id)
    replacement = await session.get(AgentRunRow, replacement_run_id)
    if replacement is not None:
        replacement_metadata = _json_dict(replacement.metadata_)
        replacement_inbound_event = _json_dict(replacement_metadata.get("inbound_event"))
        lineage = _retry_lineage(run_id, replacement_run_id)
        replacement.metadata_ = {
            **replacement_metadata,
            "retry_attempt": next_attempt,
            "retry_lineage": lineage,
            "inbound_event": {
                **replacement_inbound_event,
                "retry_attempt": next_attempt,
                "original_run_id": int(run_id),
                "replacement_run_id": replacement_run_id,
            },
        }
    _record_readmission(
        event=event,
        receipt=receipt,
        original_run_id=run_id,
        replacement_run_id=replacement_run_id,
        idempotency_key=idempotency_key,
    )
    await session.flush()
    logger.info(
        "monitored channel re-admitted: event=%s original_run=%s replacement_run=%s retry_attempt=%s",
        event.id,
        run_id,
        replacement_run_id,
        next_attempt,
    )
    return True


async def reconcile_inbound_triage_run(
    session: AsyncSession,
    run: AgentRunRow | AgentRun | int,
) -> InboundDecisionReceiptRow | None:
    """Close the decision receipt loop for an inbound-admitted run.

    Triage admission starts with an inbound event in ``review_required``. Once the
    admitted Illo run reaches a terminal state, the event and receipt should show
    the final run outcome instead of only the queued handoff.

    Slack-teammate admissions close their receipt at admission time instead;
    this function leaves those completed receipts and events untouched.
    """

    row = await session.get(AgentRunRow, int(run)) if isinstance(run, int) else run
    if row is None:
        return None
    status = coerce_run_status(getattr(row, "status", None))
    if status not in TERMINAL_RUN_STATUSES:
        return None

    metadata = _run_metadata(row)
    inbound_event = _json_dict(metadata.get("inbound_event"))
    event_id = str(inbound_event.get("event_id") or "").strip()
    if not event_id:
        return None

    event = await session.get(InboundEventRow, event_id)
    if event is None:
        return None

    run_id = int(getattr(row, "id"))
    receipt = await _inbound_receipt_for_run(
        session, event_id=event_id, run_id=run_id, receipt_types=_TRIAGE_RECEIPT_TYPES
    )
    if receipt is None:
        # Not the triage/submit lane. The slack-teammate lane lands here. A
        # monitored-channel transport failure gets one replacement admission;
        # every other terminal outcome leaves the admission-time receipt alone.
        actionable_receipt = await _inbound_receipt_for_run(
            session,
            event_id=event_id,
            run_id=run_id,
            receipt_types=_ACTIONABLE_RECEIPT_TYPES,
        )
        if (
            status == RunStatus.FAILED
            and actionable_receipt is not None
            and await _readmit_failed_monitored_channel_once(
                session,
                event=event,
                receipt=actionable_receipt,
                run_row=row,
                run_metadata=metadata,
                run_id=run_id,
            )
        ):
            return actionable_receipt

        return None

    now = datetime.now(timezone.utc)
    terminal_at = _run_datetime(row, status)
    final_answer = await _latest_final_answer(session, run_id)
    stored_failure_category = _json_dict(metadata.get("failure")).get("category")
    failure = public_run_failure(
        status,
        stored_failure_category
        or (await _run_failure_category(session, run_id) if status == RunStatus.FAILED else None),
    )
    attribution = await summarize_inbound_run_attribution(session, run_id=run_id, status=status)
    evidence_contract = preservation_evidence_result(
        preservation_contract_from_run_metadata(metadata),
        run_status=status,
        attribution=attribution,
    )
    evidence_missing = evidence_contract.get("status") == "missing"
    public_final_answer = final_answer if failure is None else None
    if evidence_missing:
        public_final_answer = evidence_contract.get("reason") or PRESERVATION_MISSING_REASON
    terminal_status = STATUS_REVIEW_REQUIRED if evidence_missing else _receipt_terminal_status(status)
    outcome_status = "needs_action" if evidence_missing else status.value
    triage_terminal = _triage_terminal_payload(
        run_id=run_id,
        status=status,
        reconciled_at=now,
        terminal_at=terminal_at,
        final_answer=public_final_answer,
    )
    triage_terminal["status"] = outcome_status
    triage_terminal["evidence_contract"] = evidence_contract
    if failure is not None:
        triage_terminal["failure"] = failure

    outcome = _json_dict(receipt.outcome)
    tool_use = _json_dict(receipt.tool_use)
    outcome_key = "handling" if tool_use.get("type") == "illo_submit" else "triage"
    handling = _json_dict(outcome.get(outcome_key))
    result_payload = _triage_result(status, public_final_answer)
    if failure is not None:
        result_payload["failure"] = failure
    if evidence_missing:
        result_payload = {
            **result_payload,
            "status": outcome_status,
            "reason": evidence_contract.get("reason") or PRESERVATION_MISSING_REASON,
        }
    outcome[outcome_key] = {
        **handling,
        **triage_terminal,
        "result": result_payload,
        "attribution": attribution,
    }

    receipt.status = terminal_status
    receipt.outcome = outcome
    tool_terminal = _tool_use_terminal_payload(
        status=status,
        reconciled_at=now,
        terminal_at=terminal_at,
    )
    if evidence_missing:
        tool_terminal["status"] = outcome_status
    tool_terminal["evidence_contract"] = evidence_contract
    if failure is not None:
        tool_terminal["failure"] = failure
    receipt.tool_use = {
        **tool_use,
        **tool_terminal,
        "attribution": attribution,
    }

    event.status = terminal_status
    event.action_result = {
        **_json_dict(event.action_result),
        outcome_key: outcome[outcome_key],
    }
    event.processed_at = event.processed_at or now
    if evidence_missing:
        event.error = evidence_contract.get("reason") or PRESERVATION_MISSING_REASON
    elif status == RunStatus.COMPLETED:
        event.error = None
    else:
        event.error = failure["message"] if failure is not None else None

    await session.flush()

    return receipt


__all__ = ["reconcile_inbound_triage_run"]
