"""Cycle run contracts, evidence windows, and launch receipts."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

SCHEDULED_REVIEW_WINDOW_HOURS = 24


def _aware_utc(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc) if value is not None else None


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def cycle_scheduled_review_window(scheduled_for: datetime | None) -> dict[str, Any]:
    """Return the stable evidence window a scheduled cycle should inspect."""
    end = _aware_utc(scheduled_for)
    start = end - timedelta(hours=SCHEDULED_REVIEW_WINDOW_HOURS) if end else None
    return {
        "anchor": "cycle_run.scheduled_for",
        "duration_hours": SCHEDULED_REVIEW_WINDOW_HOURS,
        "start_at": _iso(start),
        "end_at": _iso(end),
        "recommendation": (
            "For daily review cycles, inspect [start_at, end_at) instead of a moving "
            "last_24h window based on execution time."
        ),
    }


def cycle_result_contract() -> dict[str, Any]:
    """The minimum output contract for autonomous cycle runs."""
    return {
        "kind": "autonomous_cycle_run_result",
        "required_outputs": [
            "answer_the_cycle_mission",
            "summarize_workspace_evidence_or_explicit_gaps",
            "report_evidence_health",
            "record_next_action_or_blocker",
            "short_self_review_summary",
        ],
        "degraded_when": [
            "workspace_evidence_sources_fail_or_return_unexpectedly_sparse_results",
            "the_run_cannot_access_required_context_or_output_targets",
            "the_final_response_does_not_state_evidence_health",
        ],
    }


def pending_evidence_health_receipt(scheduled_for: datetime | None) -> dict[str, Any]:
    """Pre-run evidence-health receipt recorded before the agent has inspected tools."""
    return {
        "status": "pending",
        "checked_at": None,
        "scheduled_review_window": cycle_scheduled_review_window(scheduled_for),
        "expected_checks": [
            "workspace_activity_read",
            "cycle_run_history_read",
            "project_context_read_when_relevant",
            "output_target_available",
        ],
        "repair_instruction": (
            "If evidence readers are empty, warning, or failing in a way that conflicts with "
            "the mission, report evidence_health=degraded and name the repair before drawing "
            "strong conclusions."
        ),
    }


def cycle_launch_receipt(*, cycle_id: int | None, cycle_run_id: int | None, scheduled_for: datetime | None) -> dict[str, Any]:
    return {
        "kind": "cycle_launch_receipt",
        "cycle_id": cycle_id,
        "cycle_run_id": cycle_run_id,
        "scheduled_for": _iso(_aware_utc(scheduled_for)),
        "scheduled_review_window": cycle_scheduled_review_window(scheduled_for),
        "result_contract": cycle_result_contract(),
        "evidence_health": pending_evidence_health_receipt(scheduled_for),
    }
