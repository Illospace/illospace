from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace


NOW = datetime(2026, 4, 25, 12, 0, tzinfo=timezone.utc)


def _case(*, old_is_critical: bool = False) -> dict:
    old = {
        "id": "old",
        "item_id": "memory:old",
        "content": "Use pip for project dependencies.",
        "subject_type": "repo",
        "subject_ref": "illo-brain",
        "source_digest": "sha256:old",
        "freshness_status": "stale",
        "freshness_confidence": 0.95,
        "confidence": 0.2,
        "attention_score": 0.1,
        "estimated_tokens": 120,
        "critical": old_is_critical,
    }
    new = {
        "id": "new",
        "item_id": "memory:new",
        "content": "Use uv for project dependencies.",
        "subject_type": "repo",
        "subject_ref": "illo-brain",
        "source_digest": "sha256:new",
        "freshness_status": "fresh",
        "freshness_confidence": 0.9,
        "confidence": 0.9,
        "attention_score": 0.9,
        "estimated_tokens": 80,
        "critical": True,
    }
    return {
        "case_id": "case-1",
        "trace_id": "run:42",
        "task_class": "implement",
        "selected_memories": [old, new],
        "context_labels": [
            {"target_type": "memory", "item_id": "memory:new", "label": "useful"},
        ],
        "conflict_scout": [
            {
                "conflict_ids": ["old", "new"],
                "severity": "high",
                "recommended_action": "include_one",
                "preferred_memory_id": "new",
                "confidence": 0.9,
            }
        ],
        "quality": {
            "verifier_status": "passed",
            "outcome_label": {
                "verifier_signal": "passed",
                "user_feedback_signal": "positive",
            },
        },
    }


def test_context_policy_eval_promotes_candidate_when_thresholds_pass():
    from brain.systems.learning.context_evals import evaluate_context_policy_candidates

    payload = evaluate_context_policy_candidates(
        [_case()],
        thresholds={
            "min_eval_cases": 1,
            "min_token_savings_rate": 0.05,
            "max_stale_conflicted_memory_inclusion_rate": 0.5,
            "max_fallback_rate": 0.0,
        },
        evaluated_at=NOW,
    )

    candidate = payload["candidates"][0]

    assert payload["active_policy"]["runtime_flags_mutated"] is False
    assert candidate["eligible"] is True
    assert candidate["decision"] == "eligible_for_review"
    assert candidate["metrics"]["saved_tokens"] == 120
    assert candidate["metrics"]["token_savings_rate"] == 0.6
    assert candidate["metrics"]["missed_critical_memory_rate"] == 0.0
    assert candidate["metrics"]["stale_conflicted_memory_inclusion_rate"] == 0.5
    assert candidate["active_policy_changed"] is False
    assert "Use pip" not in str(candidate)


def test_context_policy_eval_keeps_candidate_shadow_when_critical_memory_is_missed():
    from brain.systems.learning.context_evals import build_context_policy_candidate_decision

    decision = build_context_policy_candidate_decision(
        [_case(old_is_critical=True)],
        thresholds={
            "min_eval_cases": 1,
            "min_token_savings_rate": 0.05,
            "max_missed_critical_memory_rate": 0.0,
        },
        evaluated_at=NOW,
    )

    assert decision["eligible"] is False
    assert decision["status"] == "shadow"
    assert "threshold_failed:max_missed_critical_memory_rate" in decision["decision_reasons"]
    assert decision["case_results"][0]["missed_critical_memory_ids"] == ["memory:old"]


def test_eval_case_normalization_accepts_row_like_payload_and_policy_values():
    from brain.systems.learning.context_evals import (
        candidate_to_policy_update_values,
        normalize_context_policy_eval_case,
    )

    row = SimpleNamespace(
        eval_digest="eval-1",
        trace_id="run:42",
        payload=_case(),
        quality={"outcome_label": {"verifier_signal": "passed"}},
    )

    case = normalize_context_policy_eval_case(row)
    values = candidate_to_policy_update_values(
        {
            "candidate_digest": "candidate-digest",
            "candidate": {"candidate_id": "context-policy-v2"},
            "eligible": False,
            "metrics": {"token_savings_rate": 0.1},
        },
        user_id="user-1",
        org_id="org-1",
        visibility="org",
    )

    assert case.case_id == "case-1"
    assert case.replayable is True
    assert case.critical_memory_ids == frozenset({"memory:new"})
    assert case.source_ref["eval_digest"] == "eval-1"
    assert values["candidate_digest"] == "candidate-digest"
    assert values["candidate_type"] == "context_policy"
    assert values["status"] == "shadow"
    assert values["org_id"] == "org-1"
    assert values["visibility"] == "org"


def test_nightly_context_eval_pipeline_is_shadow_only_for_provided_sources():
    from brain.jobs.pipelines.nightly_context_eval import run_nightly_context_policy_eval

    result = run_nightly_context_policy_eval(
        target_date=NOW.date(),
        sources=[_case()],
        load_recent=False,
        now=NOW,
    )

    assert result["pipeline"] == "nightly_context_eval"
    assert result["target_date"] == "2026-04-25"
    assert result["active_policy_changed"] is False
    assert result["runtime_flags_mutated"] is False
    assert result["evaluation"]["replayable_case_count"] == 1
