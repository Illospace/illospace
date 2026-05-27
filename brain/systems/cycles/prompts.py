"""Cycle run launch envelopes, metadata, and agent prompt construction."""
from __future__ import annotations

import json

from brain.platform.db.models.cycle import Cycle, CycleRun
from brain.platform.db.models.idea import Idea
from brain.systems.cycles.common import json_dict, json_list

CYCLE_LAUNCH_ENVELOPE_VERSION = 1


def cycle_launch_envelope(cycle: Cycle, run: CycleRun) -> dict:
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
        },
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
    return (
        f"[Idea: \"{idea.title}\" | {idea.id}]\n\n"
        "## Scheduled Prompt Launch\n"
        f"- Origin: {envelope['origin']}\n"
        f"- Cycle ID: {cycle.id}\n"
        f"- Cycle run ID: {run.id}\n"
        "- The Cycle mission below is the current instruction.\n"
        "- Thread messages are output/context surfaces, not durable Cycle memory.\n"
        "- Use Cycle memory, revisions, guidance, output targets, and the workspace state as source of truth.\n"
        "- You may create, update, delete, or run Cycles when that is the right workspace action; include rationale.\n"
        "- If an output target is unavailable, repair or replace it when possible instead of treating it as a blocker.\n"
        "- End with a short self-review summary suitable for the Cycle ledger and visible outputs.\n\n"
        "## Cycle Memory\n"
        f"{_json_block(cycle_memory_payload(run))}\n\n"
        "## Cycle Mission\n"
        f"{cycle.prompt[:2000]}"
    )


def _json_block(value: dict) -> str:
    return json.dumps(value, default=str, ensure_ascii=False, indent=2, sort_keys=True)
