from __future__ import annotations

from types import SimpleNamespace


def _context_signal() -> dict:
    return {
        "signal_digest": "signal-context-1",
        "signal_type": "context_usefulness_shell",
        "status": "recorded",
        "created_at": "2026-04-25T10:00:00+00:00",
        "payload": {
            "context": {
                "run_id": 42,
                "trace_id": "run:42",
                "labels": [
                    {
                        "target_type": "memory",
                        "label": "useful",
                        "evidence": {"estimated_tokens": 120, "budget_tokens": 200},
                    },
                    {
                        "target_type": "memory",
                        "label": "missed",
                        "evidence": {"estimated_tokens": 80, "budget_tokens": 200},
                    },
                    {
                        "target_type": "section",
                        "label": "over_budget",
                        "evidence": {"estimated_tokens": 420, "budget_tokens": 300},
                    },
                ],
                "summary": {
                    "label_count": 3,
                    "counts": {"useful": 1, "missed": 1, "over_budget": 1},
                    "cognitive_miss_count": 1,
                    "raw_private_memory_exported": False,
                },
                "context": {"cognitive_miss_count": 1},
            }
        },
    }


def test_learning_observatory_payload_is_deterministic_and_complete():
    from brain.systems.learning.read_models import (
        LEARNING_OBSERVATORY_CONTROL_KEYS,
        build_learning_observatory_read_model,
    )

    outcome_sources = [
        {
            "trace_id": "run:42",
            "signal_type": "trajectory_eval_case_capture",
            "status": "recorded",
            "created_at": "2026-04-25T10:01:00+00:00",
            "outcome_label": {"outcome_class": "good", "label_confidence": 0.91},
        },
        SimpleNamespace(
            run_id=41,
            status="recorded",
            created_at="2026-04-25T09:59:00+00:00",
            outcome_label="weak",
            label_confidence=0.52,
        ),
    ]
    skill_quality_sources = [
        {
            "created_at": "2026-04-25T09:00:00+00:00",
            "score": 0.62,
            "confidence": 0.55,
            "rating": "usable",
            "skill": {"name": "debug", "effective_digest": "sha256:debug"},
            "evidence": {"count": 8, "latest_observed_at": "2026-04-25T09:00:00+00:00"},
        },
        {
            "created_at": "2026-04-25T10:00:00+00:00",
            "score": 0.82,
            "confidence": 0.69,
            "rating": "strong",
            "skill": {"name": "debug", "effective_digest": "sha256:debug"},
            "evidence": {"count": 12, "latest_observed_at": "2026-04-25T10:00:00+00:00"},
        },
        {
            "skill_name": "writer",
            "skill_effective_digest": "sha256:writer",
            "outcome_label": "failed",
            "label_confidence": 0.8,
            "created_at": "2026-04-25T10:05:00+00:00",
        },
    ]
    stale_conflict_sources = [
        {
            "id": "memory-a",
            "status": "resolved",
            "truth_status": "stale",
            "staleness_score": 0.9,
            "resolved_at": "2026-04-25T09:10:00+00:00",
        },
        {
            "id": "memory-b",
            "status": "needs_review",
            "truth_status": "conflict",
            "conflict_severity": 0.95,
        },
    ]
    night_budget_source = {
        "budget_tokens": 2_000,
        "spent_tokens": 1_300,
        "remaining_tokens": 700,
        "allowed_count": 2,
        "deferred_count": 1,
        "items": [],
        "spent_by_work_type": {
            "memory_conflict_resolution": 500,
            "skill_eval": 800,
        },
        "spent_by_tenant": {"org:org-1": 1_300},
    }
    policy_sources = [
        {
            "id": 1,
            "candidate_type": "context_policy_eval",
            "status": "proposed",
            "review_status": "unreviewed",
            "policy_payload": {"policy_key": "context:recall-threshold"},
            "created_at": "2026-04-25T09:30:00+00:00",
        },
        {
            "id": 2,
            "promotion_type": "route_override",
            "status": "active",
            "policy_payload": {"policy_key": "route:debug-small"},
            "version": 3,
            "activated_at": "2026-04-25T10:20:00+00:00",
            "created_at": "2026-04-25T10:15:00+00:00",
        },
    ]

    first = build_learning_observatory_read_model(
        outcome_sources=outcome_sources,
        skill_quality_sources=skill_quality_sources,
        context_sources=[_context_signal()],
        stale_conflict_sources=stale_conflict_sources,
        night_budget_source=night_budget_source,
        policy_sources=policy_sources,
        generated_at="2026-04-25T10:30:00+00:00",
        scope={"org_id": "org-1"},
    ).to_payload()
    second = build_learning_observatory_read_model(
        outcome_sources=outcome_sources,
        skill_quality_sources=skill_quality_sources,
        context_sources=[_context_signal()],
        stale_conflict_sources=stale_conflict_sources,
        night_budget_source=night_budget_source,
        policy_sources=policy_sources,
        generated_at="2026-04-25T10:30:00+00:00",
        scope={"org_id": "org-1"},
    ).to_payload()

    assert second == first
    assert first["schema_version"] == 1
    assert first["summary"]["outcome_label_count"] == 2
    assert first["observatory"]["recent_outcomes"]["by_label"][0] == {
        "key": "good",
        "count": 1,
        "share": 0.5,
    }

    trends = {
        item["skill_name"]: item
        for item in first["observatory"]["skill_quality"]["trends"]
    }
    assert trends["debug"]["direction"] == "improving"
    assert trends["debug"]["delta"] == 0.2
    assert trends["debug"]["graduation_candidate"] is True
    assert trends["writer"]["current_score"] == 0.0
    assert first["observatory"]["skill_quality"]["weak_count"] == 1

    context = first["observatory"]["context_usefulness"]
    assert context["total_label_count"] == 3
    assert context["usefulness_rate"] == 0.333
    assert context["estimated_tokens"] == 620
    assert context["budget_tokens"] == 700
    assert context["over_budget_count"] == 1

    stale = first["observatory"]["stale_conflicts"]
    assert stale["stale_count"] == 1
    assert stale["conflict_count"] == 1
    assert stale["resolved_count"] == 1
    assert stale["pending_count"] == 1

    night = first["observatory"]["night_budget"]
    assert night["utilization"] == 0.65
    assert night["by_work_type"][0]["key"] == "skill_eval"
    assert night["by_tenant"] == [{"key": "org:org-1", "count": 1300, "share": 1.0}]

    policy = first["observatory"]["policy_candidates"]
    assert policy["pending_review_count"] == 1
    assert policy["rollbackable_count"] == 1

    controls = {item["key"]: item for item in first["controls"]}
    assert tuple(controls) == LEARNING_OBSERVATORY_CONTROL_KEYS
    assert controls["pause_learning"]["read_only_metadata"] is True
    assert controls["rollback_policy_update"]["available"] is True
    assert controls["approve_skill_graduation"]["target_count"] == 1
    assert controls["export_redacted_eval_artifact"]["available"] is True


def test_empty_learning_observatory_is_stable_and_read_only():
    from brain.systems.learning.read_models import build_learning_observatory_read_model

    payload = build_learning_observatory_read_model(
        generated_at="2026-04-25T00:00:00+00:00"
    ).to_payload()

    assert payload["summary"] == {
        "outcome_label_count": 0,
        "skill_count": 0,
        "context_label_count": 0,
        "stale_count": 0,
        "conflict_count": 0,
        "night_budget_utilization": 0.0,
        "policy_candidate_count": 0,
    }
    assert [item["key"] for item in payload["controls"]] == [
        "pause_learning",
        "rollback_policy_update",
        "approve_skill_graduation",
        "export_redacted_eval_artifact",
    ]
    assert all(item["mutation_endpoint"] is None for item in payload["controls"])
