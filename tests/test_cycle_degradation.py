from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from brain.platform.db.models.cycle import Cycle, CycleRun
from brain.platform.db.models.idea import Idea
from brain.systems.cycles import memory as cycle_memory
from brain.systems.cycles.contract_gate import evaluate_cycle_result_contract
from brain.systems.cycles.contracts import cycle_result_contract
from brain.systems.cycles.degradation import (
    advance_degradation_state,
    degradation_causes,
    degradation_tracking_for_run,
    empty_degradation_state,
)
from brain.systems.cycles.prompts import cycle_run_message


CAUSE_FAILURE = {
    "kind": "worker_tool_failure",
    "tool": "spawn_worker",
    "stage": "project_context_materialization",
    "repo": "Illospace/illospace",
    "error": "GITHUB_TOKEN could not be materialized for the child reader",
}


def _record_degraded_run(state: dict, scheduled_for: datetime) -> dict:
    causes = degradation_causes(
        status="degraded",
        error=None,
        evidence_health={"status": "degraded", "failures": [CAUSE_FAILURE]},
    )
    tracking = degradation_tracking_for_run(state, scheduled_for=scheduled_for)
    return advance_degradation_state(
        tracking,
        causes=causes,
        scheduled_for=scheduled_for,
        mandatory_digest_satisfied=False,
    )


def test_cross_run_degradation_escalates_only_after_three_consecutive_runs():
    first_at = datetime(2026, 7, 14, 14, 19, tzinfo=timezone.utc)
    state = empty_degradation_state()

    state = _record_degraded_run(state, first_at)
    assert state["active_causes"][0]["consecutive_degraded_runs"] == 1
    assert state["pending_escalations"] == []

    state = _record_degraded_run(state, first_at + timedelta(minutes=30))
    assert state["active_causes"][0]["consecutive_degraded_runs"] == 2
    assert state["pending_escalations"] == []

    state = _record_degraded_run(state, first_at + timedelta(minutes=60))
    assert state["active_causes"][0]["consecutive_degraded_runs"] == 3
    escalation = state["pending_escalations"][0]
    assert escalation["summary"].startswith(
        "spawn_worker / project_context_materialization / Illospace/illospace"
    )
    assert escalation["next_required_digest_at"] == "2026-07-14T17:00:00+00:00"

    before_digest = degradation_tracking_for_run(
        state,
        scheduled_for=datetime(2026, 7, 14, 16, 59, tzinfo=timezone.utc),
    )
    assert before_digest["mandatory_in_current_digest"] is False

    required_digest = degradation_tracking_for_run(
        state,
        scheduled_for=datetime(2026, 7, 14, 17, 0, tzinfo=timezone.utc),
    )
    assert required_digest["mandatory_in_current_digest"] is True
    assert required_digest["mandatory_causes"][0]["consecutive_degraded_runs"] == 3


def test_non_degraded_run_resets_counter_but_does_not_consume_pending_digest():
    first_at = datetime(2026, 7, 14, 14, 19, tzinfo=timezone.utc)
    state = empty_degradation_state()
    for offset in range(3):
        state = _record_degraded_run(state, first_at + timedelta(minutes=30 * offset))

    off_cadence = degradation_tracking_for_run(
        state,
        scheduled_for=datetime(2026, 7, 14, 16, 30, tzinfo=timezone.utc),
    )
    state = advance_degradation_state(
        off_cadence,
        causes=[],
        scheduled_for=datetime(2026, 7, 14, 16, 30, tzinfo=timezone.utc),
        mandatory_digest_satisfied=True,
    )

    assert state["active_causes"] == []
    assert len(state["pending_escalations"]) == 1


def test_self_reported_degraded_evidence_supplies_a_machine_tracked_cause():
    review = evaluate_cycle_result_contract(
        candidate_answer=(
            "The workspace review found a source gap. Evidence health: degraded — "
            "GitHub PR health returned HTTP 403. Next action: repair the project binding. "
            "Self-review summary: the evidence gap was reported."
        ),
        result_contract={"kind": "autonomous_cycle_run_result", "required_outputs": []},
        mission="Review workspace health.",
    )

    causes = degradation_causes(
        status="completed",
        error=None,
        evidence_health={"status": "pending"},
        reported_evidence_health=review["reported_evidence_health"],
    )

    assert review["reported_evidence_health"] == {
        "status": "degraded",
        "reported_value": "degraded",
        "cause": "GitHub PR health returned HTTP 403",
    }
    assert causes[0]["summary"] == "GitHub PR health returned HTTP 403"


def test_required_digest_contract_and_prompt_name_escalated_cause():
    first_at = datetime(2026, 7, 14, 14, 19, tzinfo=timezone.utc)
    state = empty_degradation_state()
    for offset in range(3):
        state = _record_degraded_run(state, first_at + timedelta(minutes=30 * offset))
    scheduled_for = datetime(2026, 7, 14, 17, 0, tzinfo=timezone.utc)
    tracking = degradation_tracking_for_run(state, scheduled_for=scheduled_for)
    contract = cycle_result_contract(tracking, run_kind="scheduled_digest")
    cause = contract["mandatory_degradation_escalations"][0]

    missing = evaluate_cycle_result_contract(
        candidate_answer=(
            "The required digest reviewed workspace evidence. Evidence health: ok. "
            "Next action: monitor recovery. Self-review summary: digest complete."
        ),
        result_contract=contract,
        mission="Publish the required digest.",
    )
    assert missing["approved"] is False
    assert missing["missing_outputs"] == [
        f"mandatory_degradation_escalation:{cause['key']}"
    ]
    assert set(missing["enforced_required_outputs"]) <= set(
        contract["required_outputs"]
    )

    named = evaluate_cycle_result_contract(
        candidate_answer=(
            "The required digest reviewed workspace evidence and names the persistent cause: "
            f"{cause['summary']}. Evidence health: degraded. Next action: repair credentials. "
            "Self-review summary: the escalated cause was surfaced."
        ),
        result_contract=contract,
        mission="Publish the required digest.",
    )
    assert named["approved"] is True

    cycle = Cycle()
    cycle.id = 7
    cycle.name = "Coordinator digest"
    cycle.prompt = "Publish the required digest."
    run = CycleRun()
    run.id = 12
    run.scheduled_for = scheduled_for
    run.guidance_snapshot = []
    run.output_targets_snapshot = []
    run.context_snapshot = {
        "scheduled_review_window": {},
        "result_contract": contract,
        "evidence_health": {"status": "pending"},
        "degradation_tracking": tracking,
    }
    idea = Idea()
    idea.id = "idea-1"
    idea.title = "Coordinator digest"

    prompt = cycle_run_message(idea, cycle, run)

    assert "MANDATORY DEGRADATION ESCALATION" in prompt
    assert cause["summary"] in prompt
    assert "MUST name" in prompt


def test_satisfied_required_digest_consumes_pending_escalation():
    first_at = datetime(2026, 7, 14, 14, 19, tzinfo=timezone.utc)
    state = empty_degradation_state()
    for offset in range(3):
        state = _record_degraded_run(state, first_at + timedelta(minutes=30 * offset))
    scheduled_for = datetime(2026, 7, 14, 17, 0, tzinfo=timezone.utc)
    tracking = degradation_tracking_for_run(state, scheduled_for=scheduled_for)

    state = advance_degradation_state(
        tracking,
        causes=[],
        scheduled_for=scheduled_for,
        mandatory_digest_satisfied=True,
    )

    assert state["pending_escalations"] == []


@pytest.mark.asyncio
async def test_cycle_evaluations_persist_counter_and_seed_next_digest_machine_record():
    class CaptureSession:
        def __init__(self):
            self.added = []

        def add(self, value):
            self.added.append(value)

    session = CaptureSession()
    cycle = Cycle()
    cycle.id = 7
    cycle.user_id = "user-1"
    cycle.org_id = "org-1"
    cycle.target_idea_id = None
    cycle.degradation_state = {}
    first_at = datetime(2026, 7, 14, 14, 19, tzinfo=timezone.utc)

    for offset in range(3):
        scheduled_for = first_at + timedelta(minutes=30 * offset)
        run = CycleRun()
        run.id = 100 + offset
        run.run_id = 200 + offset
        run.idea_id = None
        run.scheduled_for = scheduled_for
        run.context_snapshot = {
            "evidence_health": {"status": "degraded", "failures": [CAUSE_FAILURE]},
            "degradation_tracking": degradation_tracking_for_run(
                cycle.degradation_state,
                scheduled_for=scheduled_for,
            ),
        }

        await cycle_memory.record_cycle_run_evaluation(
            session,
            run,
            cycle,
            status="completed",
        )

    final_evaluation = session.added[-1]
    result_state = final_evaluation.details["degradation_tracking"]["result_state"]
    assert result_state["active_causes"][0]["consecutive_degraded_runs"] == 3
    assert len(result_state["pending_escalations"]) == 1

    digest_run = CycleRun()
    digest_run.id = 104
    digest_run.scheduled_for = datetime(2026, 7, 14, 17, 0, tzinfo=timezone.utc)
    snapshot = cycle_memory._build_cycle_run_memory_snapshot(
        cycle,
        run=digest_run,
        revision=None,
        guidance_rows=[],
        target_rows=[],
    )
    context = snapshot["context_snapshot"]

    assert context["degradation_tracking"]["mandatory_in_current_digest"] is True
    assert context["result_contract"]["mandatory_degradation_escalations"][0][
        "consecutive_degraded_runs"
    ] == 3
