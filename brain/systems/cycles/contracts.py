"""Cycle run contracts, evidence windows, and launch receipts."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from brain.systems.cycles.common import (
    OFF_SLOT_MATERIAL_ALERT_RUN_KIND,
    SCHEDULED_CYCLE_ORIGIN,
    SCHEDULED_DIGEST_RUN_KIND,
    json_dict,
)

from brain.systems.cycles.degradation import mandatory_escalations

SCHEDULED_REVIEW_WINDOW_HOURS = 24

# One source of truth for the base result-contract keys and the visible sections
# named in the launch prompt. The gate validates these same labels/aliases.
RESULT_CONTRACT_OUTPUT_SECTIONS = {
    "answer_the_cycle_mission": "the mission result body (no extra label required)",
    "summarize_workspace_evidence_or_explicit_gaps": "`Evidence reviewed:`",
    "report_evidence_health": "`Evidence health:`",
    "record_next_action_or_blocker": "`Next action:` or `Blocker:`",
    "short_self_review_summary": "`Self-review summary:`",
}

# Keep each coordinator run kind's complete contract visible here. Do not derive
# one from the other: their answer formats intentionally have different footers.
CYCLE_RESULT_CONTRACT_REQUIRED_OUTPUTS_BY_RUN_KIND = {
    SCHEDULED_DIGEST_RUN_KIND: (
        "answer_the_cycle_mission",
        "summarize_workspace_evidence_or_explicit_gaps",
        "report_evidence_health",
        "record_next_action_or_blocker",
        "short_self_review_summary",
    ),
    OFF_SLOT_MATERIAL_ALERT_RUN_KIND: (
        "answer_the_cycle_mission",
        "summarize_workspace_evidence_or_explicit_gaps",
        "report_evidence_health",
    ),
}
VALID_CYCLE_RUN_KINDS = frozenset(CYCLE_RESULT_CONTRACT_REQUIRED_OUTPUTS_BY_RUN_KIND)


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


def normalize_cycle_run_kind(run_kind: str | None) -> str:
    """Return a validated coordinator run kind."""
    clean_run_kind = str(run_kind or "").strip().lower()
    if clean_run_kind not in VALID_CYCLE_RUN_KINDS:
        raise ValueError(
            "cycle run_kind must be one of: "
            f"{', '.join(sorted(VALID_CYCLE_RUN_KINDS))}"
        )
    return clean_run_kind


def cycle_result_contract(
    degradation_tracking: dict[str, Any] | None = None,
    *,
    run_kind: str,
) -> dict[str, Any]:
    """The minimum output contract for autonomous cycle runs."""
    clean_run_kind = normalize_cycle_run_kind(run_kind)
    required_outputs = list(
        CYCLE_RESULT_CONTRACT_REQUIRED_OUTPUTS_BY_RUN_KIND[clean_run_kind]
    )
    contract = {
        "kind": "autonomous_cycle_run_result",
        "run_kind": clean_run_kind,
        "required_outputs": required_outputs,
        "degraded_when": [
            "workspace_evidence_sources_fail_or_return_unexpectedly_sparse_results",
            "the_run_cannot_access_required_context_or_output_targets",
            "the_final_response_does_not_state_evidence_health",
        ],
        "pagination_health": (
            "truncated_with_next_page_means_more_available_not_degraded; "
            "follow_next_page_to_completion_and_report_ok_when_no_reader_warnings_or_failures_remain"
        ),
    }
    escalations = mandatory_escalations(degradation_tracking)
    if escalations:
        contract["required_outputs"].extend(
            f"mandatory_degradation_escalation:{escalation['key']}"
            for escalation in escalations
            if str(escalation.get("key") or "").strip()
        )
        contract["mandatory_degradation_escalations"] = escalations
        contract["degradation_escalation_instruction"] = (
            "This is the required digest at or after each escalation's "
            "next_required_digest_at. The visible digest MUST name every cause exactly; "
            "off-cadence silence is not allowed to consume the escalation."
        )
    return contract


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
            "Follow next_page tokens until pagination is complete. Routine truncation with a cursor "
            "is more_available, not degraded; report evidence_health=ok after all pages complete. "
            "If evidence readers are empty, warning, failing, or cannot be paged to completion in a "
            "way that conflicts with the mission, report evidence_health=degraded and name the gap "
            "before drawing strong conclusions."
        ),
    }


def cycle_launch_receipt(
    *,
    cycle_id: int | None,
    cycle_run_id: int | None,
    scheduled_for: datetime | None,
    timezone_name: str | None = None,
    launch_context: dict | None = None,
    result_contract: dict[str, Any] | None = None,
) -> dict[str, Any]:
    launch = json_dict(launch_context)
    origin = str(launch.get("origin") or SCHEDULED_CYCLE_ORIGIN)
    local_scheduled_for = None
    timezone_name = timezone_name or "UTC"
    try:
        local_timezone = ZoneInfo(timezone_name)
    except (TypeError, ValueError, ZoneInfoNotFoundError):
        timezone_name = "UTC"
        local_timezone = ZoneInfo("UTC")
    if scheduled_for is not None:
        local_scheduled_for = _aware_utc(scheduled_for).astimezone(
            local_timezone
        ).isoformat()
    return {
        "kind": "cycle_launch_receipt",
        "cycle_id": cycle_id,
        "cycle_run_id": cycle_run_id,
        "origin": origin,
        "launch_context": launch,
        "scheduled_for": _iso(_aware_utc(scheduled_for)),
        "timezone": timezone_name,
        "local_scheduled_for": local_scheduled_for,
        "scheduled_review_window": cycle_scheduled_review_window(scheduled_for),
        "result_contract": result_contract
        or cycle_result_contract(
            run_kind=str(launch.get("run_kind") or SCHEDULED_DIGEST_RUN_KIND)
        ),
        "evidence_health": pending_evidence_health_receipt(scheduled_for),
    }
