"""Reconcile inbound decision receipts after inbound-admitted runs finish."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from brain.platform.db.models.agent_run import AgentRunArtifactRow, AgentRunRow
from brain.platform.db.models.inbound import InboundDecisionReceiptRow, InboundEventRow
from brain.systems.inbound.attribution import summarize_inbound_run_attribution
from brain.systems.inbound.preservation import (
    PRESERVATION_MISSING_REASON,
    preservation_contract_from_run_metadata,
    preservation_evidence_result,
)
from brain.systems.inbound.status import STATUS_REVIEW_REQUIRED
from brain.systems.runs.domain import AgentRun, ArtifactType
from brain.systems.runs.status import RunStatus, TERMINAL_RUN_STATUSES, coerce_run_status

logger = logging.getLogger(__name__)

# Receipt lanes. Triage/submit receipts are written NON-terminal at admission
# and this module transitions them when the run finishes; the slack-teammate
# lane writes its receipt already-terminal ("run admitted" IS its decision),
# so it gets a mint check here but never a receipt/event rewrite.
_TRIAGE_RECEIPT_TYPES = frozenset({"illo_triage", "illo_submit"})
_ACTIONABLE_RECEIPT_TYPES = frozenset({"slack_teammate_run"})


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


def _log_mint_result(*, event_id: str, run_id: int, lane: str, result: Any) -> None:
    """One line per mint decision (worker logs at INFO). This is what makes a
    dormant lane visible: silence around packets is itself the bug this line
    guards against — skips say WHY, mints say what happened."""
    logger.info(
        "packet mint: event=%s run=%s lane=%s ok=%s created=%s posted=%s reason=%s",
        event_id,
        run_id,
        lane,
        getattr(result, "ok", None),
        getattr(result, "created", None),
        getattr(result, "posted", None),
        getattr(result, "reason", "") or "minted",
    )


async def _mint_for_actionable_run(
    session: AsyncSession,
    *,
    event: InboundEventRow,
    run_row: AgentRunRow | AgentRun,
    status: RunStatus,
    event_id: str,
    run_id: int,
) -> None:
    """Mint check for runs admitted by the slack-teammate lane.

    Their receipts are terminal at admission, so the triage path's
    transition guard can never fire — the mint's own stamp guard (see
    :func:`mint_packet_after_actionable_run`) provides once-per-event.
    Fully contained: this hook may never break run-status persistence."""
    try:
        receipt = await _inbound_receipt_for_run(
            session, event_id=event_id, run_id=run_id, receipt_types=_ACTIONABLE_RECEIPT_TYPES
        )
        if receipt is None:
            return
        lane = str(_json_dict(receipt.tool_use).get("type") or "actionable")
        if status != RunStatus.COMPLETED:
            logger.info(
                "packet mint skipped: event=%s run=%s lane=%s run_status=%s",
                event_id, run_id, lane, status.value,
            )
            return
        attribution = await summarize_inbound_run_attribution(session, run_id=run_id, status=status)
        from brain.systems.briefing.mint import mint_packet_after_actionable_run

        result = await mint_packet_after_actionable_run(
            session, event=event, run_row=run_row, attribution=attribution
        )
        _log_mint_result(event_id=event_id, run_id=run_id, lane=lane, result=result)
    except Exception:  # noqa: BLE001 — belt to mint's own containment
        logger.warning("packet mint hook failed for event %s", event_id, exc_info=True)


async def reconcile_inbound_triage_run(
    session: AsyncSession,
    run: AgentRunRow | AgentRun | int,
) -> InboundDecisionReceiptRow | None:
    """Close the decision receipt loop for an inbound-admitted run.

    Triage admission starts with an inbound event in ``review_required``. Once the
    admitted Illo run reaches a terminal state, the event and receipt should show
    the final run outcome instead of only the queued handoff.

    Slack-teammate admissions close their receipt at admission time instead;
    for those this function leaves receipt and event untouched and only runs
    the packet-mint check (durable work → handoff packet).
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
        # Not the triage/submit lane. The slack-teammate lane lands here —
        # its receipt closed at admission, but a COMPLETED run that created
        # durable work still owes a handoff packet (the gate that silently
        # never fired in prod: this lane is where actionable runs actually
        # complete).
        await _mint_for_actionable_run(
            session, event=event, run_row=row, status=status, event_id=event_id, run_id=run_id
        )
        return None
    # Captured BEFORE mutation: the packet hook fires only on the transition
    # INTO a terminal receipt state. Re-reconciles of an already-terminal
    # receipt (e.g. illo_get_result polls call this on every read) must
    # never re-gather or re-mint — that keeps the read path free of live
    # Slack/GitHub I/O and starves the brief self-echo loop.
    receipt_was_terminal = str(receipt.status or "") in {"processed", "failed"}

    now = datetime.now(timezone.utc)
    terminal_at = _run_datetime(row, status)
    final_answer = await _latest_final_answer(session, run_id)
    attribution = await summarize_inbound_run_attribution(session, run_id=run_id, status=status)
    evidence_contract = preservation_evidence_result(
        preservation_contract_from_run_metadata(metadata),
        run_status=status,
        attribution=attribution,
    )
    evidence_missing = evidence_contract.get("status") == "missing"
    terminal_status = STATUS_REVIEW_REQUIRED if evidence_missing else _receipt_terminal_status(status)
    outcome_status = "needs_action" if evidence_missing else status.value
    triage_terminal = _triage_terminal_payload(
        run_id=run_id,
        status=status,
        reconciled_at=now,
        terminal_at=terminal_at,
        final_answer=final_answer,
    )
    triage_terminal["status"] = outcome_status
    triage_terminal["evidence_contract"] = evidence_contract

    outcome = _json_dict(receipt.outcome)
    tool_use = _json_dict(receipt.tool_use)
    outcome_key = "handling" if tool_use.get("type") == "illo_submit" else "triage"
    handling = _json_dict(outcome.get(outcome_key))
    result_payload = _triage_result(status, final_answer)
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
        event.error = final_answer or f"Illo triage run ended with status {status.value}"

    await session.flush()

    # Handoff packet (spec: illo-handoff-packets slice 05): a COMPLETED run
    # that routes work mints the packet that makes it arrive warm. Once per
    # receipt lifecycle (see receipt_was_terminal above). Lanes differ:
    # triage completion IS the routing moment, so illo_triage mints
    # unconditionally; illo_submit runs often just answer, so they mint only
    # when attribution shows durable work (the actionable-run path, which
    # also carries a stamp guard against evidence-missing receipts that
    # never reach a terminal state and re-enter here on every poll). Double
    # containment: mint never raises by contract, and the import + call sit
    # under their own guard so even an import-chain break cannot take down
    # the receipt loop that worked before packets existed.
    receipt_type = str(tool_use.get("type") or "")
    if status == RunStatus.COMPLETED and not receipt_was_terminal:
        try:
            if receipt_type == "illo_triage":
                from brain.systems.briefing.mint import mint_packet_after_triage

                result = await mint_packet_after_triage(
                    session, event=event, run_row=row, attribution=attribution
                )
            else:  # illo_submit — the only other admitted triage-lane type
                from brain.systems.briefing.mint import mint_packet_after_actionable_run

                result = await mint_packet_after_actionable_run(
                    session, event=event, run_row=row, attribution=attribution
                )
            _log_mint_result(event_id=event_id, run_id=run_id, lane=receipt_type, result=result)
        except Exception:  # noqa: BLE001 — belt to mint's own containment
            logger.warning(
                "packet mint hook failed for event %s", event_id, exc_info=True
            )

    return receipt


__all__ = ["reconcile_inbound_triage_run"]
