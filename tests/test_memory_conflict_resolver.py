"""Tests for nightly memory conflict resolution planning."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from brain.systems.learning.budget import BudgetLane, LearningBudgetPolicy
from brain.systems.memory.conflict_scout import ContextConflictNotice
from brain.systems.memory.conflict_resolver import resolve_memory_conflicts
from brain.jobs.pipelines import nightly_memory_quality


def _night_policy(tokens: int) -> LearningBudgetPolicy:
    return LearningBudgetPolicy(
        lane_token_limits={
            BudgetLane.HOT_PATH: 1_500,
            BudgetLane.AFTER_RUN: 20_000,
            BudgetLane.NIGHT: tokens,
            BudgetLane.TENANT_DAILY: 100_000,
        }
    )


def test_user_correction_supersedes_older_with_rollback_metadata():
    plan = resolve_memory_conflicts(
        user_corrections=[
            {
                "id": "correction-1",
                "old_memory_id": "old",
                "new_memory_id": "new",
                "reason": "Alex corrected the dependency manager.",
            }
        ],
        memories=[
            {
                "id": "old",
                "content": "Use pip for this repo.",
                "truth_status": "reviewed",
                "review_status": "reviewed",
                "valid_from": "2026-03-01T00:00:00Z",
            },
            {
                "id": "new",
                "content": "Use uv for this repo.",
                "truth_status": "reviewed",
                "review_status": "reviewed",
                "valid_from": "2026-04-01T00:00:00Z",
            },
        ],
        use_night_budget=False,
    )

    assert len(plan.actions) == 1
    action = plan.actions[0]
    assert action.action == "supersede_older"
    assert action.targets["superseded_memory_id"] == "old"
    assert action.targets["superseding_memory_id"] == "new"
    assert action.source_ids == ("user_correction:correction-1",)
    assert action.rollback_metadata["preserves_memory_content"] is True
    assert action.rollback_metadata["restore_fields"]["old"]["truth_status"] == "reviewed"
    assert action.metadata["llm_call"] is False


def test_context_notice_and_freshness_archive_stale_side_once():
    notice = ContextConflictNotice(
        conflict_ids=("a", "b"),
        severity="high",
        recommended_action="include_both_with_warning",
        reasons=("same_subject_different_claim_digest",),
        confidence=0.7,
    )

    plan = resolve_memory_conflicts(
        context_notices=[notice],
        freshness_signals={
            "a": {"status": "stale", "confidence": 0.9, "reasons": ["source_digest_mismatch"]},
            "b": {"status": "fresh", "confidence": 0.8},
        },
        memories=[
            {"id": "a", "content": "The API uses Flask.", "subject_ref": "api"},
            {"id": "b", "content": "The API uses FastAPI.", "subject_ref": "api"},
        ],
        use_night_budget=False,
    )

    assert len(plan.actions) == 1
    action = plan.actions[0]
    assert action.action == "archive_stale"
    assert action.targets["stale_memory_id"] == "a"
    assert "source_freshness_mismatch" in action.reasons
    assert set(action.source_ids) == {"context_notice:a-b", "freshness:a"}


def test_non_overlapping_claim_windows_keep_both():
    now = datetime(2026, 4, 25, 12, tzinfo=timezone.utc)

    plan = resolve_memory_conflicts(
        contradiction_rows=[
            {
                "id": 77,
                "left_memory_id": 1,
                "right_memory_id": 2,
                "contradiction_type": "temporal_conflict",
                "severity": 0.7,
                "status": "open",
            }
        ],
        memories=[
            {
                "id": 1,
                "content": "The launch window is March.",
                "valid_from": now - timedelta(days=60),
                "valid_until": now - timedelta(days=30),
            },
            {
                "id": 2,
                "content": "The launch window is April.",
                "valid_from": now - timedelta(days=10),
            },
        ],
        use_night_budget=False,
    )

    assert [action.action for action in plan.actions] == ["keep_both_with_windows"]
    assert plan.actions[0].targets["windowed_memory_ids"] == ["1", "2"]
    assert "temporal_claim_windows_separate" in plan.actions[0].reasons


def test_ambiguous_high_severity_contradiction_quarantines_without_model_call():
    plan = resolve_memory_conflicts(
        contradiction_rows=[
            {
                "id": "contradiction-1",
                "left_memory_id": "left",
                "right_memory_id": "right",
                "contradiction_type": "semantic_conflict",
                "severity": 0.92,
                "status": "open",
            }
        ],
        use_night_budget=False,
    )

    assert len(plan.actions) == 1
    action = plan.actions[0]
    assert action.action == "quarantine_uncertain"
    assert action.metadata["llm_call"] is False
    assert action.metadata["deferred_escalations"][0]["kind"] == "semantic_adjudication"
    assert set(action.rollback_metadata["affected_memory_ids"]) == {"left", "right"}


def test_night_budget_defers_actions_when_budget_is_exhausted():
    plan = resolve_memory_conflicts(
        user_corrections=[
            {"id": "correction-1", "old_memory_id": "old", "new_memory_id": "new"}
        ],
        policy=_night_policy(tokens=0),
    )

    assert len(plan.actions) == 1
    action = plan.actions[0]
    assert action.action == "supersede_older"
    assert action.deferred is True
    assert action.budget_decision["allowed"] is False
    assert "budget exhausted" in action.budget_decision["reason"]
    assert plan.budget_summary["deferred_count"] == 1


def test_plan_idempotency_is_stable_across_input_order():
    kwargs = {
        "context_notices": [
            {
                "conflict_ids": ["a", "b"],
                "recommended_action": "include_one",
                "preferred_memory_id": "b",
                "reasons": ["explicit_supersession"],
                "confidence": 0.88,
            }
        ],
        "freshness_signals": [
            {"memory_id": "a", "status": "stale", "confidence": 0.9},
            {"memory_id": "b", "status": "fresh", "confidence": 0.9},
        ],
        "use_night_budget": False,
    }

    first = resolve_memory_conflicts(**kwargs)
    second = resolve_memory_conflicts(
        context_notices=list(reversed(kwargs["context_notices"])),
        freshness_signals=list(reversed(kwargs["freshness_signals"])),
        use_night_budget=False,
    )

    assert first.to_dict() == second.to_dict()


def test_pipeline_shell_returns_plan_only_payload(monkeypatch):
    monkeypatch.setattr(
        nightly_memory_quality,
        "gather_memory_quality_inputs",
        lambda **_: {
            "contradiction_rows": [
                {
                    "id": 9,
                    "left_memory_id": "old",
                    "right_memory_id": "new",
                    "contradiction_type": "semantic_supersession",
                    "severity": 0.8,
                    "status": "open",
                }
            ],
            "freshness_signals": [],
            "memories": [
                {
                    "id": "old",
                    "valid_from": "2026-03-01T00:00:00Z",
                },
                {
                    "id": "new",
                    "valid_from": "2026-04-01T00:00:00Z",
                },
            ],
        },
    )

    payload = nightly_memory_quality.run_nightly_memory_quality(
        target_date=date(2026, 4, 25),
        use_night_budget=False,
    )

    assert payload["pipeline"] == "nightly_memory_quality"
    assert payload["mode"] == "plan_only"
    assert payload["llm_calls"] == 0
    assert payload["mutates_memory_rows"] is False
    assert payload["actions"][0]["action"] == "supersede_older"
