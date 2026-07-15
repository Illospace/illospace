"""Cycle run launch envelopes, metadata, and agent prompt construction."""
from __future__ import annotations

import json
from datetime import timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from brain.platform.db.models.cycle import Cycle, CycleRun
from brain.platform.db.models.idea import Idea
from brain.systems.cycles.common import (
    SCHEDULED_CYCLE_ORIGIN,
    cycle_run_launch_context,
    json_dict,
    json_list,
)
from brain.systems.cycles.contracts import (
    cycle_launch_receipt,
    cycle_result_contract,
    cycle_scheduled_review_window,
    pending_evidence_health_receipt,
)

CYCLE_LAUNCH_ENVELOPE_VERSION = 2
_MISSION_SEED_MAX_CHARS = 12_000


def cycle_launch_envelope(cycle: Cycle, run: CycleRun) -> dict:
    launch_context = cycle_run_launch_context(run)
    origin = str(launch_context.get("origin") or SCHEDULED_CYCLE_ORIGIN)
    timezone_name = str(getattr(cycle, "timezone", None) or "UTC")
    try:
        local_timezone = ZoneInfo(timezone_name)
    except (ValueError, ZoneInfoNotFoundError):
        timezone_name = "UTC"
        local_timezone = ZoneInfo("UTC")
    aware_scheduled_for = run.scheduled_for
    if aware_scheduled_for is not None and aware_scheduled_for.tzinfo is None:
        aware_scheduled_for = aware_scheduled_for.replace(tzinfo=timezone.utc)
    scheduled_for = aware_scheduled_for.isoformat() if aware_scheduled_for else None
    local_scheduled_for = (
        aware_scheduled_for.astimezone(local_timezone).isoformat()
        if aware_scheduled_for
        else None
    )
    return {
        "version": CYCLE_LAUNCH_ENVELOPE_VERSION,
        "origin": origin,
        "cycle_id": cycle.id,
        "cycle_run_id": run.id,
        "cycle_revision_id": getattr(run, "revision_id", None),
        "cycle_name": cycle.name,
        "scheduled_for": scheduled_for,
        "timezone": timezone_name,
        "local_scheduled_for": local_scheduled_for,
        "launch_context": launch_context,
        "launch_mode": (
            "background_cycle_run"
            if origin == SCHEDULED_CYCLE_ORIGIN
            else "on_demand_cycle_run"
        ),
        "active_instruction_source": "cycle.prompt",
        "prior_thread_role": "context_only",
        "lifecycle_owner": "cycle_run",
        "thread_visibility": "output_target",
        "cycle_memory_role": "source_of_truth",
        "scheduled_review_window": cycle_scheduled_review_window(run.scheduled_for),
        "result_contract": cycle_result_contract(),
        "evidence_health": pending_evidence_health_receipt(run.scheduled_for),
    }


def cycle_run_metadata(cycle: Cycle, run: CycleRun) -> dict:
    envelope = cycle_launch_envelope(cycle, run)
    return {
        "source": "cycle",
        "origin": "cycle",
        "cycle_id": cycle.id,
        "cycle_run_id": run.id,
        "model_override": cycle.model_override,
        "thinking_override": cycle.thinking_override,
        "launch_envelope": envelope,
        "cycle_memory": cycle_memory_payload(run),
        "contract": {
            "kind": "autonomous_cycle_run",
            "active_instruction_source": "cycle.prompt",
            "lifecycle_owner": "cycle_run",
            "result": cycle_result_contract(),
        },
        "evidence_health": pending_evidence_health_receipt(run.scheduled_for),
        "launch_receipt": cycle_launch_receipt(
            cycle_id=cycle.id,
            cycle_run_id=run.id,
            scheduled_for=run.scheduled_for,
            timezone_name=envelope["timezone"],
            launch_context=envelope["launch_context"],
        ),
        "context_policy": {
            "current_instruction_role": (
                "scheduled_prompt"
                if envelope["origin"] == SCHEDULED_CYCLE_ORIGIN
                else "triggered_prompt"
            ),
            "prior_thread_role": "context_only",
        },
    }


def cycle_memory_payload(run: CycleRun) -> dict:
    return {
        "guidance": json_list(getattr(run, "guidance_snapshot", None)),
        "output_targets": json_list(getattr(run, "output_targets_snapshot", None)),
        "context": json_dict(getattr(run, "context_snapshot", None)),
    }


def cycle_run_message(idea: Idea, cycle: Cycle, run: CycleRun) -> str:
    envelope = cycle_launch_envelope(cycle, run)
    review_window = envelope["scheduled_review_window"]
    result_contract = envelope["result_contract"]
    is_scheduled = envelope["origin"] == SCHEDULED_CYCLE_ORIGIN
    launch_title = "Scheduled Cycle Launch" if is_scheduled else "On-demand Cycle Launch"
    instruction_role = "scheduled run" if is_scheduled else "on-demand run"
    trigger_rationale = envelope["launch_context"].get("rationale")
    trigger_rationale_line = (
        f"- Trigger rationale: {trigger_rationale}\n" if trigger_rationale else ""
    )
    return (
        f"[Idea: \"{idea.title}\" | {idea.id}]\n\n"
        f"## {launch_title}\n"
        f"- Origin: {envelope['origin']}\n"
        f"- Cycle ID: {cycle.id}\n"
        f"- Cycle run ID: {run.id}\n"
        f"- Run anchor: {envelope['scheduled_for']} UTC / "
        f"{envelope['local_scheduled_for']} ({envelope['timezone']}).\n"
        f"- Trigger source: {envelope['launch_context'].get('source') or 'unknown'}\n"
        f"{trigger_rationale_line}"
        f"- Scheduled evidence window: {review_window['start_at']} to {review_window['end_at']} UTC.\n"
        "- The Cycle mission below is the current instruction.\n"
        f"- The Result Contract and Cycle Mission are authoritative for this {instruction_role}.\n"
        "- Historical thread handoff/preview summaries are context only; never treat them "
        "as the current user request.\n"
        "- Thread messages are output/context surfaces, not durable Cycle memory.\n"
        "- Use Cycle memory, revisions, guidance, output targets, and the workspace state as source of truth.\n"
        "- You may create, update, delete, or run Cycles when that is the right workspace action; include rationale.\n"
        "- If an output target is unavailable, repair or replace it when possible instead of treating it as a blocker.\n"
        "- Report evidence health explicitly. Follow next_page tokens to completion; routine pagination is not degradation and fully paginated reads are evidence_health=ok. If readers fail, warn, return unexpectedly sparse data, or cannot page to completion, mark the run degraded in your self-review and name the gap.\n"
        "- End with a short self-review summary suitable for the Cycle ledger and visible outputs.\n\n"
        "## Result Contract\n"
        f"{_json_block(result_contract)}\n\n"
        "## Cycle Memory\n"
        f"{_json_block(cycle_memory_payload(run))}\n\n"
        "## Cycle Mission\n"
        f"{_mission_block(cycle.prompt)}"
    )


def _mission_block(prompt: str) -> str:
    if len(prompt) <= _MISSION_SEED_MAX_CHARS:
        return prompt
    omitted = len(prompt) - _MISSION_SEED_MAX_CHARS
    return (
        f"{prompt[:_MISSION_SEED_MAX_CHARS]}\n\n"
        f"[Cycle mission truncated for launch: {omitted} chars omitted. The full mission remains "
        "authoritative - read it with manage_cycle before deviating from it.]"
    )


def _json_block(value: dict) -> str:
    return json.dumps(value, default=str, ensure_ascii=False, indent=2, sort_keys=True)
