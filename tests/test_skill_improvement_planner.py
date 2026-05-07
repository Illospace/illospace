from __future__ import annotations

import json
from datetime import datetime, timezone


NOW = datetime(2026, 4, 25, 12, 0, tzinfo=timezone.utc)


def test_skill_improvement_planner_emits_advisory_actions_without_mutating_skills():
    from brain.systems.skills.improvement import SkillImprovementActionType, plan_skill_improvements

    plan = plan_skill_improvements(
        skills=[
            {
                "id": 1,
                "name": "repo-debug",
                "effective_digest": "sha256:repo-debug",
                "use_count": 40,
                "success_count": 14,
                "failure_count": 18,
                "trust_level": "private_local",
            }
        ],
        quality_scores=[
            {
                "score": 0.42,
                "confidence": 0.82,
                "rating": "weak",
                "skill": {"name": "repo-debug", "effective_digest": "sha256:repo-debug"},
                "evidence": {"count": 40, "sample_size_confidence": 1.0},
            }
        ],
        missing_context_signals=[
            {
                "id": "sig-1",
                "skill_name": "repo-debug",
                "label": "missing_context",
                "count": 4,
                "reason": ["asset_not_loaded"],
                "requested_section": "asset:repo-layout.md",
            }
        ],
    )
    payload = plan.to_payload()

    assert payload["mode"] == "plan_only"
    assert payload["advisory_only"] is True
    assert payload["evidence_summary"]["mutates_skills"] is False
    assert payload["actions"][0]["action"] == SkillImprovementActionType.DRAFT_OVERLAY_REFINEMENT.value
    assert payload["actions"][0]["payload"]["overlay"]["refinement_focus"] == [
        "failure_reduction",
        "missing_context_recovery",
        "quality_recovery",
    ]
    assert payload["actions"][0]["safety"]["auto_apply"] is False
    assert "asset:repo-layout.md" in payload["actions"][0]["payload"]["overlay"]["missing_context"]["requested_sections"]
    json.dumps(payload, sort_keys=True)


def test_agent_draft_generates_eval_bundle_action_before_graduation():
    from brain.systems.skills.improvement import SkillImprovementActionType, plan_skill_improvements

    plan = plan_skill_improvements(
        skills=[
            {
                "name": "draft-pr-helper",
                "effective_digest": "sha256:draft",
                "source_kind": "agent_draft",
                "trust_level": "agent_draft",
                "success_count": 4,
                "use_count": 5,
                "redacted_eval_case_count": 0,
            }
        ],
        quality_scores={
            "draft-pr-helper": {
                "score": 0.73,
                "confidence": 0.54,
                "skill": {"name": "draft-pr-helper", "effective_digest": "sha256:draft"},
            }
        },
    )

    actions = plan.to_payload()["actions"]

    assert len(actions) == 1
    assert actions[0]["action"] == SkillImprovementActionType.RUN_EVAL_BUNDLE.value
    assert actions[0]["payload"]["eval_requirements"]["include_raw_memory_content"] is False
    assert actions[0]["payload"]["eval_requirements"]["include_tenant_identifiers"] is False


def test_hosted_public_bundle_update_is_recommendation_not_auto_update():
    from brain.systems.skills.improvement import SkillImprovementActionType, plan_skill_improvements

    plan = plan_skill_improvements(
        bundle_update_candidates=[
            {
                "skill_name": "public-research",
                "bundle_namespace": "marketplace",
                "bundle_name": "public-research",
                "trust_level": "public",
                "hosted": True,
                "current_version": "1.0.0",
                "current_digest": "sha256:old",
                "current_quality_score": 0.61,
                "available_version": "1.1.0",
                "available_digest": "sha256:new",
                "available_quality_score": 0.82,
                "available_approved": True,
            }
        ]
    )

    action = plan.to_payload()["actions"][0]

    assert action["action"] == SkillImprovementActionType.RECOMMEND_VERSION_UPDATE.value
    assert action["payload"]["auto_apply"] is False
    assert action["payload"]["requires_trust_policy"] is True
    assert action["safety"]["hosted_public_auto_update"] is False
    assert action["safety"]["requires_admin_review"] is True


def test_repeated_pattern_creates_private_skill_draft():
    from brain.systems.skills.improvement import SkillImprovementActionType, plan_skill_improvements

    plan = plan_skill_improvements(
        repeated_patterns=[
            {
                "pattern_id": "pattern-1",
                "task_class": "repo_triage",
                "summary": "Review repo failures and propose focused patches",
                "count": 5,
            }
        ]
    )

    action = plan.to_payload()["actions"][0]

    assert action["action"] == SkillImprovementActionType.CREATE_PRIVATE_SKILL.value
    assert action["payload"]["draft"]["trust_level"] == "private_local"
    assert action["payload"]["draft"]["provisional"] is True
    assert action["safety"]["public_export_allowed"] is False


def test_nightly_skill_quality_pipeline_accepts_json_payload():
    from brain.jobs.pipelines.nightly_skill_quality import run_nightly_skill_quality_from_payload

    result = run_nightly_skill_quality_from_payload(
        {
            "skills": [
                {
                    "name": "debug",
                    "effective_digest": "sha256:debug",
                    "use_count": 20,
                    "success_count": 5,
                }
            ],
            "quality_scores": {
                "debug": {"score": 0.40, "confidence": 0.9, "skill": {"name": "debug"}},
            },
        },
        target_date=NOW.date(),
        use_night_budget=False,
        now=NOW,
    )

    assert result["pipeline"] == "nightly_skill_quality"
    assert result["target_date"] == "2026-04-25"
    assert result["llm_calls"] == 0
    assert result["mutates_skills"] is False
    assert result["action_count"] == 1
