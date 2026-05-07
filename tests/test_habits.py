from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch


def _signature_task():
    return "Refine run summaries for the habit compiler"


def _base_run():
    return SimpleNamespace(
        id=77,
        contract_type="freeform",
        target_status="validated",
        worktree_branch="pr-07-habit-compiler",
        model_used="openai/gpt-5-mini",
        thinking_used="adaptive",
        brain_context_loaded=True,
        attention_required=True,
        preloaded_memory_count=4,
        skill_used="coordinate",
        contract_status="satisfied",
        scout_class="validated",
        verification_attempts=1,
        verification_warnings=["minor warning"],
    )


def test_build_habit_signature_is_stable_and_narrow():
    from brain.systems.feedback.predict import build_habit_signature

    run = _base_run()
    first = build_habit_signature(
        _signature_task(),
        skill_name="coordinate",
        run=run,
        success=True,
        duration_sec=12,
        tokens_used=900,
        cost=0.25,
    )
    second = build_habit_signature(
        _signature_task(),
        skill_name="coordinate",
        run=run,
        success=True,
        duration_sec=12,
        tokens_used=900,
        cost=0.25,
    )

    assert first["signature_hash"] == second["signature_hash"]
    assert first["task_family"] != "general"
    assert "run" in first["task_family"]
    assert first["contract_type"] == "freeform"
    assert "skill:coordinate" in first["context_shape"]


def test_compile_habit_proposals_from_repeated_source_runs():
    from brain.systems.learning.habits import compile_habit_proposals, source_run_from_signature
    from brain.systems.feedback.predict import build_habit_signature

    run = _base_run()
    signature = build_habit_signature(
        _signature_task(),
        skill_name="coordinate",
        run=run,
        success=True,
        duration_sec=12,
        tokens_used=900,
    )

    runs = [
        source_run_from_signature(
            {**signature, "source_run_id": run.id + offset},
            task=_signature_task(),
            success=True,
            duration_sec=12 + offset,
            tokens_used=900 + (offset * 10),
        )
        for offset in range(3)
    ]

    proposals = compile_habit_proposals(runs, min_source_runs=3)

    assert len(proposals) == 1
    proposal = proposals[0]
    assert proposal.task_family == signature["task_family"]
    assert proposal.source_run_ids == [77, 78, 79]
    assert proposal.matcher["task_family"] == signature["task_family"]
    assert proposal.matcher["source"]["source_run_count"] == 3
    assert proposal.preconditions["fallback"]["allow_full_pipeline"] is True
    assert proposal.step_graph[0]["kind"] == "guard"
    assert proposal.activation_signals["promotion_ready"] is True
    assert proposal.verifier_profile["match_confidence_floor"] == 0.85


def test_aggregate_habit_source_runs_preserves_narrow_activation_signals():
    from brain.systems.learning.habits import aggregate_habit_source_runs, source_run_from_signature
    from brain.systems.feedback.predict import build_habit_signature

    run = _base_run()
    signature = build_habit_signature(
        _signature_task(),
        skill_name="coordinate",
        run=run,
        success=True,
        duration_sec=12,
        tokens_used=900,
    )
    runs = [
        source_run_from_signature(
            {**signature, "source_run_id": run.id + offset},
            task=_signature_task(),
            success=True,
            duration_sec=12 + offset,
            tokens_used=900 + (offset * 10),
        )
        for offset in range(3)
    ]

    aggregates = aggregate_habit_source_runs(runs, min_source_runs=3)

    assert len(aggregates) == 1
    aggregate = aggregates[0]
    assert aggregate.matcher["runtime"]["runtime_fingerprint"] == signature["runtime_fingerprint"]
    assert aggregate.preconditions["verification"]["match_confidence_floor"] == 0.85
    assert aggregate.activation_signals["promotion_ready"] is True
    assert "variance_spike" in aggregate.demotion_signals


def test_habit_matcher_rejects_workspace_drift():
    from brain.systems.learning.habits import evaluate_habit_match

    signature = {
        "task_family": "run-summary-habit",
        "source_skill": "coordinate",
        "contract_type": "freeform",
        "target_status": "validated",
        "workspace_fingerprint": "workspace-a",
        "runtime_fingerprint": "openai/gpt-5-mini|adaptive",
        "context_shape": ["brain_context", "skill:coordinate", "contract:freeform"],
    }
    matcher = {
        "task_family": "run-summary-habit",
        "source": {
            "skill_name": "coordinate",
            "contract_type": "freeform",
            "target_status": "validated",
        },
        "runtime": {
            "workspace_fingerprint": "workspace-b",
            "runtime_fingerprint": "openai/gpt-5-mini|adaptive",
        },
        "context": {
            "required": ["brain_context", "skill:coordinate", "contract:freeform"],
        },
    }

    result = evaluate_habit_match(signature, matcher, {"fallback_to_full_pipeline": True})

    assert result.matched is False
    assert "workspace" in (result.fallback_reason or "").lower()
    assert "workspace_drift" in result.signal_summary["demotion_signals"]


def test_record_habit_shadow_executions_marks_full_pipeline_fallback():
    from brain.systems.learning.habits import record_habit_shadow_executions

    session = MagicMock()
    habit_version = SimpleNamespace(
        id=5,
        habit_id=11,
        matcher={
            "task_family": "run-summary-habit",
            "source_skill": "coordinate",
            "source": {
                "skill_name": "coordinate",
                "contract_type": "freeform",
                "target_status": "validated",
            },
            "runtime": {
                "workspace_fingerprint": "workspace-a",
                "runtime_fingerprint": "openai/gpt-5-mini|adaptive",
            },
            "context": {
                "required": ["brain_context", "skill:coordinate", "contract:freeform"],
            },
        },
        preconditions={"fallback_to_full_pipeline": True, "verification": {"match_confidence_floor": 0.85}},
        step_graph=[
            {"step_id": "guard_compile", "kind": "guard", "depends_on": [], "checks": ["task_family"]},
            {"step_id": "execute_primary", "kind": "execution", "depends_on": ["guard_compile"], "skill_name": "coordinate"},
        ],
    )
    session.execute.return_value.scalars.return_value.all.return_value = [habit_version]
    added = []
    session.add.side_effect = added.append

    run = _base_run()
    signature = {
        "task_family": "run-summary-habit",
        "source_skill": "coordinate",
        "contract_type": "freeform",
        "target_status": "validated",
        "workspace_fingerprint": "workspace-a",
        "runtime_fingerprint": "openai/gpt-5-mini|adaptive",
        "context_shape": ["brain_context", "skill:coordinate", "contract:freeform"],
    }

    records = record_habit_shadow_executions(
        session,
        run=run,
        signature=signature,
        duration_sec=14,
        tokens_used=910,
        cost=0.3,
    )

    assert len(records) == 1
    assert records[0].status == "shadow_match"
    assert "full pipeline" in (records[0].fallback_reason or "").lower()
    assert records[0].signal_summary["promotion_ready"] is True
    assert records[0].verifier_result["fallback_mode"] == "full_pipeline"
    assert added and type(added[0]).__name__ == "HabitExecution"


def test_summarize_habit_shadow_feedback_collects_activation_and_demotion_signals():
    from brain.systems.learning.habits import HabitExecutionRecord, summarize_habit_shadow_feedback

    records = [
        HabitExecutionRecord(
            run_id=1,
            habit_id=11,
            habit_version_id=5,
            match_confidence=0.91,
            guard_result={"matched": True},
            status="shadow_match",
            fallback_reason="full pipeline retained during shadow evaluation",
            signal_summary={"promotion_ready": True, "demotion_signals": []},
        ),
        HabitExecutionRecord(
            run_id=1,
            habit_id=11,
            habit_version_id=6,
            match_confidence=0.2,
            guard_result={"matched": False},
            status="shadow_rejected",
            fallback_reason="workspace drift",
            signal_summary={"promotion_ready": False, "demotion_signals": ["workspace_drift"]},
        ),
    ]

    summary = summarize_habit_shadow_feedback(records)

    assert summary["observations"] == 2
    assert summary["promotion_ready"] == 1
    assert summary["promotion_ready_ids"] == [5]
    assert summary["demotion_signals"] == ["workspace_drift"]
    assert summary["fallback_path"] == "full_pipeline"
