from brain.systems.learning.budget import BudgetDecisionAction, BudgetLane, LearningBudgetPolicy
from brain.systems.learning.night_budget import (
    NightBudgetCandidate,
    NightWorkType,
    build_night_budget_plan,
)


def _policy(*, night_tokens: int, tenant_daily_tokens: int = 100_000) -> LearningBudgetPolicy:
    return LearningBudgetPolicy(
        lane_token_limits={
            BudgetLane.HOT_PATH: 1_500,
            BudgetLane.AFTER_RUN: 20_000,
            BudgetLane.NIGHT: night_tokens,
            BudgetLane.TENANT_DAILY: tenant_daily_tokens,
        }
    )


def _item_by_id(plan, candidate_id):
    return next(item for item in plan.items if item.candidate.candidate_id == candidate_id)


def test_high_impact_failed_run_runs_before_low_impact_repo_refresh():
    plan = build_night_budget_plan(
        [
            NightBudgetCandidate(
                "low-repo",
                NightWorkType.REPO_SUMMARY_REFRESH,
                estimated_tokens=900,
                org_id="org-1",
                signals={"repo_staleness_days": 1},
            ),
            NightBudgetCandidate(
                "failed-run",
                NightWorkType.CONTEXT_POLICY_EVAL,
                estimated_tokens=1_200,
                org_id="org-1",
                impact_score=40,
                signals={"status": "failed", "run_priority": 9, "missed_memory_signals": 2},
            ),
        ],
        policy=_policy(night_tokens=1_800),
    )

    assert [item.candidate.candidate_id for item in plan.allowed] == ["failed-run"]
    low_repo = _item_by_id(plan, "low-repo")
    assert low_repo.decision.action == BudgetDecisionAction.DEFER
    assert "budget exhausted" in low_repo.decision.reason
    assert plan.spent_by_tenant == {"org:org-1": 1_200}


def test_budget_is_partitioned_by_org_tenant_and_daily_cap():
    plan = build_night_budget_plan(
        [
            NightBudgetCandidate(
                "org-1-priority",
                NightWorkType.MEMORY_CONFLICT_RESOLUTION,
                estimated_tokens=1_500,
                org_id="org-1",
                impact_score=50,
                signals={"memory_access_count": 20, "staleness_score": 0.9},
            ),
            NightBudgetCandidate(
                "org-1-over-cap",
                NightWorkType.SKILL_EVAL,
                estimated_tokens=800,
                org_id="org-1",
                impact_score=45,
                signals={"skill_traffic_count": 80, "skill_confidence": 0.4},
            ),
            NightBudgetCandidate(
                "org-2-priority",
                NightWorkType.CONTEXT_POLICY_EVAL,
                estimated_tokens=1_500,
                org_id="org-2",
                impact_score=48,
                signals={"missed_memory_signals": 3},
            ),
        ],
        policy=_policy(night_tokens=5_000, tenant_daily_tokens=2_000),
    )

    assert {item.candidate.candidate_id for item in plan.allowed} == {
        "org-1-priority",
        "org-2-priority",
    }
    over_cap = _item_by_id(plan, "org-1-over-cap")
    assert over_cap.decision.action == BudgetDecisionAction.SKIP
    assert over_cap.decision.reason == "tenant daily learning budget exhausted"
    assert plan.spent_by_tenant == {"org:org-1": 1_500, "org:org-2": 1_500}
    assert set(plan.budget_by_tenant) == {"org:org-1", "org:org-2"}


def test_frequently_used_stale_memories_outrank_less_used_stale_memories():
    plan = build_night_budget_plan(
        [
            {
                "candidate_id": "rare-stale",
                "work_type": "memory_conflict_resolution",
                "estimated_tokens": 900,
                "org_id": "org-1",
                "signals": {"memory_access_count": 1, "staleness_score": 0.9},
            },
            {
                "candidate_id": "hot-stale",
                "work_type": "memory_conflict_resolution",
                "estimated_tokens": 900,
                "org_id": "org-1",
                "signals": {"memory_access_count": 30, "staleness_score": 0.9},
            },
        ],
        policy=_policy(night_tokens=1_000),
    )

    assert [item.candidate.candidate_id for item in plan.allowed] == ["hot-stale"]
    assert _item_by_id(plan, "rare-stale").decision.action == BudgetDecisionAction.DEFER


def test_high_traffic_uncertain_skills_outrank_low_traffic_uncertain_skills():
    plan = build_night_budget_plan(
        [
            NightBudgetCandidate(
                "low-traffic-uncertain",
                NightWorkType.SKILL_EVAL,
                estimated_tokens=900,
                org_id="org-1",
                signals={"skill_traffic_count": 3, "skill_confidence": 0.1},
            ),
            NightBudgetCandidate(
                "high-traffic-uncertain",
                NightWorkType.SKILL_EVAL,
                estimated_tokens=900,
                org_id="org-1",
                signals={"skill_traffic_count": 120, "skill_confidence": 0.35},
            ),
        ],
        policy=_policy(night_tokens=1_000),
    )

    assert [item.candidate.candidate_id for item in plan.allowed] == ["high-traffic-uncertain"]
    assert _item_by_id(plan, "low-traffic-uncertain").decision.action == BudgetDecisionAction.DEFER


def test_repeated_missed_memory_signals_drive_context_policy_priority():
    plan = build_night_budget_plan(
        [
            {
                "candidate_id": "single-miss",
                "work_type": "context_policy_eval",
                "estimated_tokens": 800,
                "org_id": "org-1",
                "signals": {"missed_memory_signals": 1},
            },
            {
                "candidate_id": "repeated-miss",
                "work_type": "context_policy_eval",
                "estimated_tokens": 800,
                "org_id": "org-1",
                "signals": {"missed_memory_signals": 5, "brain_recall_used": False},
            },
        ],
        policy=_policy(night_tokens=1_000),
    )

    assert [item.candidate.candidate_id for item in plan.allowed] == ["repeated-miss"]
    assert _item_by_id(plan, "single-miss").decision.action == BudgetDecisionAction.DEFER


def test_night_budget_plan_is_deterministic_and_payload_safe():
    candidates = [
        {
            "candidate_id": "skill-a",
            "work_type": "skill_eval",
            "estimated_tokens": 700,
            "org_id": "org-1",
            "signals": {"skill_traffic_count": 20, "skill_confidence": 0.5},
        },
        {
            "candidate_id": "skill-b",
            "work_type": "skill_eval",
            "estimated_tokens": 700,
            "org_id": "org-1",
            "signals": {"skill_traffic_count": 10, "skill_confidence": 0.7},
        },
    ]

    first = build_night_budget_plan(candidates, policy=_policy(night_tokens=1_000))
    second = build_night_budget_plan(candidates, policy=_policy(night_tokens=1_000))

    assert first.to_payload() == second.to_payload()
    assert first.to_payload()["deferred_count"] == 1
    assert "items" in first.to_payload()
