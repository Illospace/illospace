from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import UUID

from brain.platform.db.models.cycle import (
    Cycle,
    CycleGuidance,
    CycleOutputTarget,
    CycleRevision,
    CycleRun,
)
from brain.systems.cycles import memory as cycle_memory


def test_build_cycle_run_memory_snapshot_returns_jsonb_safe_values():
    created_at = datetime(2026, 5, 29, 14, 25, tzinfo=timezone.utc)
    workspace_id = UUID("00000000-0000-0000-0000-000000000123")
    owner_user_id = UUID("00000000-0000-0000-0000-000000000456")
    target_idea_id = UUID("00000000-0000-0000-0000-000000000789")
    source_id = UUID("00000000-0000-0000-0000-000000000abc")

    cycle = Cycle()
    cycle.id = 42
    cycle.user_id = owner_user_id
    cycle.org_id = workspace_id
    cycle.creator_type = "user"
    cycle.creator_id = owner_user_id
    cycle.target_idea_id = target_idea_id

    revision = CycleRevision()
    revision.id = 11
    revision.cycle_id = cycle.id
    revision.revision_number = 3
    revision.source_type = "user"
    revision.source_id = source_id
    revision.rationale = "Updated guidance"
    revision.name = "Morning brief"
    revision.prompt = "Summarize the current workspace"
    revision.schedule_expr = "0 9 * * *"
    revision.timezone = "America/Toronto"
    revision.enabled = True
    revision.model_override = None
    revision.thinking_override = "medium"
    revision.target_idea_id = target_idea_id
    revision.context_policy = {"workspace_id": workspace_id, "captured_at": created_at}
    revision.created_at = created_at

    guidance = CycleGuidance()
    guidance.id = 12
    guidance.cycle_id = cycle.id
    guidance.revision_id = revision.id
    guidance.source_type = "user"
    guidance.source_id = source_id
    guidance.guidance = "Mention stuck runs"
    guidance.rationale = None
    guidance.is_active = True
    guidance.created_at = created_at

    output_target = CycleOutputTarget()
    output_target.id = 13
    output_target.cycle_id = cycle.id
    output_target.revision_id = revision.id
    output_target.target_type = "cycle_ledger"
    output_target.target_id = str(cycle.id)
    output_target.label = "Cycle ledger"
    output_target.config = {"nested": {"captured_at": created_at, "owner": owner_user_id}}
    output_target.source_type = "system"
    output_target.source_id = None
    output_target.rationale = "Durable memory target"
    output_target.is_active = True
    output_target.created_at = created_at
    output_target.updated_at = created_at

    snapshot = cycle_memory._build_cycle_run_memory_snapshot(
        cycle,
        revision=revision,
        guidance_rows=[guidance],
        target_rows=[output_target],
    )

    json.dumps(snapshot)
    assert snapshot["revision_id"] == revision.id
    assert (
        snapshot["context_snapshot"]["revision"]["created_at"]
        == created_at.isoformat()
    )
    assert snapshot["context_snapshot"]["revision"]["context_policy"][
        "workspace_id"
    ] == str(workspace_id)
    assert snapshot["guidance_snapshot"][0]["created_at"] == created_at.isoformat()
    assert snapshot["output_targets_snapshot"][0]["created_at"] == created_at.isoformat()
    assert (
        snapshot["output_targets_snapshot"][0]["config"]["nested"]["captured_at"]
        == created_at.isoformat()
    )
    assert snapshot["output_targets_snapshot"][0]["config"]["nested"]["owner"] == str(
        owner_user_id
    )


def test_append_cycle_run_output_target_snapshot_returns_jsonb_safe_values():
    created_at = datetime(2026, 5, 29, 14, 25, tzinfo=timezone.utc)
    owner_user_id = UUID("00000000-0000-0000-0000-000000000456")
    run = CycleRun()
    run.output_targets_snapshot = []

    cycle_memory.append_cycle_run_output_target_snapshot(
        run,
        target_type="thread",
        target_id="idea-123",
        label="Cycle thread",
        config={"captured_at": created_at, "owner": owner_user_id},
    )

    json.dumps(run.output_targets_snapshot)
    assert (
        run.output_targets_snapshot[0]["config"]["captured_at"]
        == created_at.isoformat()
    )
    assert run.output_targets_snapshot[0]["config"]["owner"] == str(owner_user_id)
