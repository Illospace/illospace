from __future__ import annotations

import pytest

from brain.jobs.evals import list_default_scenarios, run_backend_eval_suite
from brain.jobs.evals.scenarios import EvalScenario


def test_default_eval_suite_covers_golden_and_chaos_cases():
    scenarios = list_default_scenarios()
    scenario_ids = {scenario.scenario_id for scenario in scenarios}

    assert {
        "direct_reply",
        "clarification",
        "memory_recall",
        "recurring_report",
        "code_doc_update",
        "correction_truth_update",
        "slack_casual_dm_voice",
        "slack_social_ack_reaction",
        "slack_team_coordination_voice",
        "slack_incident_voice",
        "slack_personal_failure_voice",
        "slack_question_needs_text",
        "provider_timeout",
        "embedding_unavailable",
        "scheduler_restart",
        "worker_crash_lease_expiry",
    }.issubset(scenario_ids)
    assert {scenario.kind for scenario in scenarios} == {"golden", "chaos"}


def test_mocked_eval_suite_runs_without_live_provider():
    result = run_backend_eval_suite()
    payload = result.to_dict()

    assert payload["live_provider"] is False
    assert payload["passed"] is True
    assert payload["summary"]["total"] == 16
    assert payload["summary"]["failed"] == 0
    assert all(case["observed"]["evidence"] for case in payload["results"])


def test_conversation_golden_cases_cover_tone_and_chat_native_boundaries():
    scenarios = {scenario.scenario_id: scenario for scenario in list_default_scenarios()}

    assert scenarios["slack_casual_dm_voice"].expected["forced_joke"] is False
    assert scenarios["slack_incident_voice"].expected["humour"] == "none"
    assert scenarios["slack_personal_failure_voice"].expected["tone"] == "kind_direct"
    assert scenarios["slack_social_ack_reaction"].expected == {
        "response_tool": "react_to_slack_message",
        "text_required": False,
        "max_reactions": 1,
    }
    assert scenarios["slack_question_needs_text"].expected["reaction_is_sufficient"] is False


def test_eval_suite_returns_machine_readable_failures():
    scenario = EvalScenario(
        scenario_id="bad_case",
        kind="golden",
        user_prompt="This should fail.",
        expected={"requires_run": True},
    )

    result = run_backend_eval_suite(
        scenarios=[scenario],
        backend=lambda _scenario: {"requires_run": False},
    ).to_dict()

    assert result["passed"] is False
    assert result["summary"] == {"total": 1, "passed": 0, "failed": 1}
    assert "requires_run" in result["results"][0]["errors"][0]


def test_live_provider_eval_requires_explicit_backend():
    with pytest.raises(ValueError, match="explicit backend"):
        run_backend_eval_suite(live_provider=True)


@pytest.mark.live_provider
def test_live_provider_marker_is_opt_in_only():
    # This marker exists so future live evals can be excluded from normal CI.
    assert True
