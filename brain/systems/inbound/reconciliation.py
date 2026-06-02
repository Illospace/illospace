"""Reconcile inbound decision receipts after Illo triage runs finish."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from brain.platform.db.models.agent_run import AgentRunArtifactRow, AgentRunRow
from brain.platform.db.models.inbound import InboundDecisionReceiptRow, InboundEventRow
from brain.systems.inbound.attribution import summarize_inbound_run_attribution
from brain.systems.runs.domain import AgentRun, ArtifactType
from brain.systems.runs.status import RunStatus, TERMINAL_RUN_STATUSES, coerce_run_status


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
        if tool_use.get("type") not in {"illo_triage", "illo_submit"}:
            continue
        if str(tool_use.get("run_id") or target.get("run_id") or "") == str(run_id):
            return row
    return None


async def reconcile_inbound_triage_run(
    session: AsyncSession,
    run: AgentRunRow | AgentRun | int,
) -> InboundDecisionReceiptRow | None:
    """Close the decision receipt loop for an inbound triage run.

    Triage admission starts with an inbound event in ``review_required``. Once the
    admitted Illo run reaches a terminal state, the event and receipt should show
    the final run outcome instead of only the queued handoff.
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
    receipt = await _inbound_receipt_for_run(session, event_id=event_id, run_id=run_id)
    if receipt is None:
        return None

    now = datetime.now(timezone.utc)
    terminal_at = _run_datetime(row, status)
    final_answer = await _latest_final_answer(session, run_id)
    attribution = await summarize_inbound_run_attribution(session, run_id=run_id, status=status)
    terminal_status = _receipt_terminal_status(status)
    triage_terminal = _triage_terminal_payload(
        run_id=run_id,
        status=status,
        reconciled_at=now,
        terminal_at=terminal_at,
        final_answer=final_answer,
    )

    outcome = _json_dict(receipt.outcome)
    tool_use = _json_dict(receipt.tool_use)
    outcome_key = "handling" if tool_use.get("type") == "illo_submit" else "triage"
    handling = _json_dict(outcome.get(outcome_key))
    outcome[outcome_key] = {
        **handling,
        **triage_terminal,
        "result": _triage_result(status, final_answer),
        "attribution": attribution,
    }

    receipt.status = terminal_status
    receipt.outcome = outcome
    receipt.tool_use = {
        **tool_use,
        **_tool_use_terminal_payload(
            status=status,
            reconciled_at=now,
            terminal_at=terminal_at,
        ),
        "attribution": attribution,
    }

    event.status = terminal_status
    event.action_result = {
        **_json_dict(event.action_result),
        outcome_key: outcome[outcome_key],
    }
    event.processed_at = event.processed_at or now
    if status == RunStatus.COMPLETED:
        event.error = None
    else:
        event.error = final_answer or f"Illo triage run ended with status {status.value}"

    await session.flush()
    return receipt


__all__ = ["reconcile_inbound_triage_run"]
