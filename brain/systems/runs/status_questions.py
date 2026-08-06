"""Typed same-thread run context for honest status-question answers."""

from __future__ import annotations

import logging
import re
from typing import Any, Mapping

from sqlalchemy import select

from brain.platform.db.models.agent_run import AgentRunArtifactRow, AgentRunRow
from brain.systems.runs.status import (
    OPEN_RUN_STATUSES,
    RunStatus,
    coerce_run_status,
)

logger = logging.getLogger(__name__)

_STATUS_QUESTION_RE = re.compile(
    r"(?:"
    r"\b(?:was|is|are)\s+(?:it|that|this|the\s+(?:work|task|ticket|issue))\s+"
    r"(?:done|complete|completed|finished|ready)\b|"
    r"\bdid\s+(?:you|illo|it)\s+(?:do|finish|complete|create|file|open|send|assign)\b|"
    r"\b(?:what(?:'s| is)\s+the\s+status|status\s+update|any\s+(?:update|progress)|"
    r"how(?:'s| is)\s+(?:it|that|the\s+(?:work|task))\s+going)\b"
    r")",
    re.IGNORECASE,
)
_TICKET_RE = re.compile(r"\b(?:ticket|issue)\b", re.IGNORECASE)
_GITHUB_RE = re.compile(r"\bgithub\b", re.IGNORECASE)
_CUSTOMER_BUG_RE = re.compile(
    r"\b(?:bug|customer|client|support|email|reported|complaint)\b",
    re.IGNORECASE,
)
_ASSIGNMENT_RE = re.compile(
    r"\b(?:assign(?:ed|ment)?|owner|assignee)\b",
    re.IGNORECASE,
)

# A top-level same-thread run needs coordination while it is open. This is
# intentionally conservative: the run model has no typed "investigating this
# question" state, so queued, starting, running, paused, and verifying runs all
# count. Child runs never count because they are work owned by their root run,
# not independent siblings of the newly admitted run.
SAME_THREAD_COORDINATION_POLICY = OPEN_RUN_STATUSES


def is_status_question(message: str | None) -> bool:
    """Return whether the current message is asking about prior work's status."""

    return bool(_STATUS_QUESTION_RE.search(str(message or "")))


def enumerate_status_deliverables(request: str | None) -> list[dict[str, str]]:
    """Enumerate the status-relevant deliverables declared by an originating ask."""

    text = str(request or "")
    deliverables: list[dict[str, str]] = []
    if _TICKET_RE.search(text):
        if _GITHUB_RE.search(text) or _CUSTOMER_BUG_RE.search(text):
            deliverables.append({"kind": "github_issue", "label": "GitHub ticket"})
        else:
            deliverables.append({"kind": "ticket", "label": "ticket"})
    if _ASSIGNMENT_RE.search(text):
        deliverables.append({"kind": "assignment", "label": "ticket assignment"})
    if not deliverables:
        deliverables.append({"kind": "request", "label": "originating request"})
    return deliverables


async def build_same_thread_run_context(
    session: Any,
    *,
    thread_id: str,
    org_id: str | None = None,
    origin_search_limit: int = 32,
    include_status_details: bool = False,
) -> dict[str, Any] | None:
    """Snapshot prior top-level runs on a thread for admission-time policies."""

    clean_thread_id = str(thread_id or "").strip()
    if not clean_thread_id:
        return {
            "thread_id": "",
            "lookup_status": "failed",
            "lookup_error": "missing thread id",
            "status_question": bool(include_status_details),
            "originating_run": None,
            "live_sibling_runs": [],
            "deliverables": [],
        }
    if not hasattr(session, "scalars"):
        return {
            "thread_id": clean_thread_id,
            "lookup_status": "failed",
            "lookup_error": "run lookup unavailable",
            "status_question": bool(include_status_details),
            "originating_run": None,
            "live_sibling_runs": [],
            "deliverables": [],
        }

    try:
        filters = (
            AgentRunRow.thread_id == clean_thread_id,
            AgentRunRow.parent_run_id.is_(None),
        )
        clean_org_id = str(org_id or "").strip()
        if clean_org_id:
            filters = (*filters, AgentRunRow.org_id == clean_org_id)

        live_result = await session.scalars(
            select(AgentRunRow)
            .where(
                *filters,
                AgentRunRow.status.in_(
                    tuple(
                        status.value
                        for status in RunStatus
                        if status in SAME_THREAD_COORDINATION_POLICY
                    )
                ),
            )
            .order_by(AgentRunRow.created_at.desc(), AgentRunRow.id.desc())
        )
        live_rows = list(live_result.all())

        origin_rows: list[Any] = []
        if include_status_details:
            origin_result = await session.scalars(
                select(AgentRunRow)
                .where(*filters)
                .order_by(AgentRunRow.created_at.desc(), AgentRunRow.id.desc())
                .limit(max(1, int(origin_search_limit)))
            )
            origin_rows = list(origin_result.all())
    except Exception as exc:
        logger.warning(
            "same_thread_run_lookup_failed thread_id=%s error=%s",
            clean_thread_id,
            exc,
        )
        return {
            "thread_id": clean_thread_id,
            "lookup_status": "failed",
            "lookup_error": "same-thread run lookup failed",
            "status_question": bool(include_status_details),
            "originating_run": None,
            "live_sibling_runs": [],
            "deliverables": [],
        }

    live_runs = [
        {
            "run_id": int(row.id),
            "status": coerce_run_status(
                getattr(row, "status", None),
                default=RunStatus.FAILED,
            ),
        }
        for row in live_rows
    ]
    origin_with_status = next(
        (
            (
                row,
                coerce_run_status(
                    getattr(row, "status", None),
                    default=RunStatus.FAILED,
                ),
            )
            for row in origin_rows
            if not is_status_question(getattr(row, "input_message", None))
        ),
        None,
    )
    origin_payload: dict[str, Any] | None = None
    if include_status_details and origin_with_status is not None:
        origin, origin_status = origin_with_status
        final_output = await _latest_final_output(session, int(origin.id))
        origin_payload = {
            "run_id": int(origin.id),
            "status": origin_status,
            "request": str(origin.input_message or ""),
            "final_output": final_output or None,
        }

    return {
        "thread_id": clean_thread_id,
        "lookup_status": "verified",
        "status_question": bool(include_status_details),
        "originating_run": origin_payload,
        "live_sibling_runs": live_runs,
        "deliverables": (
            enumerate_status_deliverables(
                origin_payload.get("request") if origin_payload else None
            )
            if include_status_details
            else []
        ),
    }


async def build_status_question_context(
    session: Any,
    *,
    thread_id: str,
    message: str,
    org_id: str | None = None,
    origin_search_limit: int = 32,
) -> dict[str, Any] | None:
    """Snapshot the prior same-thread run that a status question refers to."""

    if not is_status_question(message):
        return None
    return await build_same_thread_run_context(
        session,
        thread_id=thread_id,
        org_id=org_id,
        origin_search_limit=origin_search_limit,
        include_status_details=True,
    )


async def _latest_final_output(session: Any, run_id: int) -> str:
    try:
        result = await session.scalars(
            select(AgentRunArtifactRow)
            .where(
                AgentRunArtifactRow.run_id == int(run_id),
                AgentRunArtifactRow.artifact_type == "final_answer",
            )
            .order_by(
                AgentRunArtifactRow.created_at.desc(),
                AgentRunArtifactRow.id.desc(),
            )
            .limit(1)
        )
        artifact = result.first()
    except Exception:
        logger.debug(
            "status_question_final_output_lookup_failed run_id=%s",
            run_id,
            exc_info=True,
        )
        return ""
    return str(getattr(artifact, "text", None) or "") if artifact is not None else ""


def format_status_question_context(value: Mapping[str, Any] | None) -> str:
    """Render typed status evidence as an authoritative prompt section."""

    if not isinstance(value, Mapping) or not value:
        return ""
    lines = ["Authoritative status-check evidence:"]
    lookup_status = str(value.get("lookup_status") or "").strip().lower()
    origin = value.get("originating_run")
    origin = origin if isinstance(origin, Mapping) else None
    live_runs = [
        item for item in list(value.get("live_sibling_runs") or [])
        if isinstance(item, Mapping)
    ]

    if lookup_status != "verified":
        lines.append(
            "The same-thread run lookup could not verify an originating outcome. "
            "Do not report the prior request as done."
        )
    elif origin is None:
        lines.append(
            "No originating run outcome was found on this thread. "
            "Do not infer completion from an incidental record."
        )
    else:
        run_id = origin.get("run_id")
        status = str(origin.get("status") or "unknown")
        request = " ".join(str(origin.get("request") or "").split())
        lines.append(f"Originating run {run_id} status: {status}.")
        if request:
            lines.append(f"Originating ask: {request}")

    for run in live_runs:
        lines.append(
            f"Sibling run {run.get('run_id')} is {run.get('status')}; "
            "you must report the request as in progress, never yes/done."
        )

    deliverables = [
        str(item.get("label") or item.get("kind") or "").strip()
        for item in list(value.get("deliverables") or [])
        if isinstance(item, Mapping)
    ]
    deliverables = [item for item in deliverables if item]
    if deliverables:
        lines.append("Deliverables to verify individually: " + ", ".join(deliverables) + ".")

    final_output = str((origin or {}).get("final_output") or "").strip()
    if final_output:
        lines.append("Originating run final outcome: " + final_output[:1600])
    elif origin is not None:
        lines.append("The originating run has no verified final outcome with refs yet.")

    lines.append(
        "A downstream Domain/tracker record proves only that a partial step ran. "
        "State each unresolved deliverable explicitly."
    )
    return "\n".join(lines)


def format_active_sibling_context(value: Mapping[str, Any] | None) -> str:
    """Render active siblings as authoritative admission-time context."""

    if not isinstance(value, Mapping) or not value:
        return ""
    active_runs = [
        item
        for item in list(value.get("live_sibling_runs") or [])
        if isinstance(item, Mapping)
    ]
    if not active_runs:
        return ""

    lines = ["Authoritative active-sibling evidence captured at admission:"]
    for run in active_runs:
        lines.append(
            f"Sibling run {run.get('run_id')} status: "
            f"{run.get('status') or 'unknown'}."
        )
    lines.append(
        "Do not answer as if this is the only active work on the Slack thread. "
        "Your final reply must wait for the active work, reference an active sibling "
        "run, or explicitly hand the question off."
    )
    lines.append(
        "Declare that choice in the reply tool's coordination field as "
        "{'action': 'wait'}, {'action': 'reference', 'run_id': N}, or "
        "{'action': 'handoff'}."
    )
    lines.append(
        "This admission snapshot is authoritative even if the sibling work seems "
        "unrelated or may have finished since admission."
    )
    return "\n".join(lines)


__all__ = [
    "SAME_THREAD_COORDINATION_POLICY",
    "build_same_thread_run_context",
    "build_status_question_context",
    "enumerate_status_deliverables",
    "format_active_sibling_context",
    "format_status_question_context",
    "is_status_question",
]
