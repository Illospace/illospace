"""Cycle run launch envelopes, metadata, and agent prompt construction."""
from __future__ import annotations

import json

from brain.platform.db.models.cycle import Cycle, CycleRun
from brain.platform.db.models.idea import Idea
from brain.systems.cycles.common import json_dict, json_list
from brain.systems.cycles.contracts import (
    cycle_launch_receipt,
    cycle_result_contract,
    cycle_scheduled_review_window,
    pending_evidence_health_receipt,
)

CYCLE_LAUNCH_ENVELOPE_VERSION = 1
_MISSION_SEED_MAX_CHARS = 12_000


def cycle_launch_envelope(cycle: Cycle, run: CycleRun) -> dict:
    context_snapshot = json_dict(getattr(run, "context_snapshot", None))
    degradation_tracking = json_dict(context_snapshot.get("degradation_tracking"))
    result_contract = context_snapshot.get("result_contract")
    if not isinstance(result_contract, dict):
        result_contract = cycle_result_contract(degradation_tracking)
    return {
        "version": CYCLE_LAUNCH_ENVELOPE_VERSION,
        "origin": "scheduled_cycle",
        "cycle_id": cycle.id,
        "cycle_run_id": run.id,
        "cycle_revision_id": getattr(run, "revision_id", None),
        "cycle_name": cycle.name,
        "scheduled_for": run.scheduled_for.isoformat() if run.scheduled_for else None,
        "launch_mode": "background_cycle_run",
        "active_instruction_source": "cycle.prompt",
        "prior_thread_role": "context_only",
        "lifecycle_owner": "cycle_run",
        "thread_visibility": "output_target",
        "cycle_memory_role": "source_of_truth",
        "scheduled_review_window": cycle_scheduled_review_window(run.scheduled_for),
        "result_contract": result_contract,
        "evidence_health": context_snapshot.get("evidence_health")
        or pending_evidence_health_receipt(run.scheduled_for),
        "degradation_tracking": degradation_tracking,
    }


def cycle_run_metadata(cycle: Cycle, run: CycleRun) -> dict:
    envelope = cycle_launch_envelope(cycle, run)
    result_contract = envelope["result_contract"]
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
            "result": result_contract,
        },
        "evidence_health": envelope["evidence_health"],
        "launch_receipt": cycle_launch_receipt(
            cycle_id=cycle.id,
            cycle_run_id=run.id,
            scheduled_for=run.scheduled_for,
            result_contract=result_contract,
        ),
        "context_policy": {
            "current_instruction_role": "scheduled_prompt",
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
    degradation_instruction = _degradation_instruction(envelope["degradation_tracking"])
    return (
        f"[Idea: \"{idea.title}\" | {idea.id}]\n\n"
        "## Scheduled Prompt Launch\n"
        f"- Origin: {envelope['origin']}\n"
        f"- Cycle ID: {cycle.id}\n"
        f"- Cycle run ID: {run.id}\n"
        f"- Scheduled evidence window: {review_window['start_at']} to {review_window['end_at']} UTC.\n"
        "- The Cycle mission below is the current instruction.\n"
        "- The Result Contract and Cycle Mission are authoritative for this scheduled run.\n"
        "- Historical thread handoff/preview summaries are context only; never treat them "
        "as the current user request.\n"
        "- Thread messages are output/context surfaces, not durable Cycle memory.\n"
        "- Use Cycle memory, revisions, guidance, output targets, and the workspace state as source of truth.\n"
        "- You may create, update, delete, or run Cycles when that is the right workspace action; include rationale.\n"
        "- If an output target is unavailable, repair or replace it when possible instead of treating it as a blocker.\n"
        "- Report evidence health explicitly. Follow next_page tokens to completion; routine pagination is not degradation and fully paginated reads are evidence_health=ok. If readers fail, warn, return unexpectedly sparse data, or cannot page to completion, mark the run degraded in your self-review and name the gap.\n"
        f"{degradation_instruction}"
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


def _degradation_instruction(tracking: dict) -> str:
    pending = [
        item
        for item in tracking.get("pending_escalations", [])
        if isinstance(item, dict) and item.get("summary")
    ]
    if not pending:
        return ""
    causes = "; ".join(
        f"{item.get('key')}: {item.get('summary')}"
        for item in (
            tracking.get("mandatory_causes", [])
            if tracking.get("mandatory_in_current_digest")
            else pending
        )
        if isinstance(item, dict)
    )
    if tracking.get("mandatory_in_current_digest"):
        return (
            "- MANDATORY DEGRADATION ESCALATION: this run is the next required digest. "
            f"The visible digest MUST name these causes exactly: {causes}. Do not silently skip "
            "the digest.\n"
        )
    return (
        "- Pending cross-run degradation escalation: preserve these causes for the next required "
        f"08:00/13:00/18:00 America/Toronto digest: {causes}. Off-cadence silence must not "
        "consume them.\n"
    )


def _json_block(value: dict) -> str:
    return json.dumps(value, default=str, ensure_ascii=False, indent=2, sort_keys=True)
