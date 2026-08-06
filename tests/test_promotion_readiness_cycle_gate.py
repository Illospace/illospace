from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from brain.platform.db.models.cycle import Cycle, CycleRun
from brain.systems.cycles.promotion_readiness import (
    PROMOTION_READINESS_POLICY,
    PromotionReadinessOutcome,
    async_apply_promotion_readiness_gate,
    async_validate_promotion_readiness_policy_configuration,
)


NOW = datetime(2026, 8, 5, 15, 0, tzinfo=timezone.utc)


def _cycle_and_run() -> tuple[Cycle, CycleRun]:
    cycle = Cycle()
    cycle.id = 9
    cycle.name = PROMOTION_READINESS_POLICY.expected_cycle_name
    cycle.org_id = "org-1"

    run = CycleRun()
    run.id = 2507
    run.cycle_id = cycle.id
    run.scheduled_for = NOW
    run.started_at = None
    run.context_snapshot = {
        "launch_context": {
            "origin": "cycle_scheduler",
            "source": "cycle_scheduler",
            "run_kind": "scheduled_digest",
        }
    }
    return cycle, run


def _configured_cycle_ids() -> AsyncMock:
    return AsyncMock(return_value=(9,))


@pytest.mark.asyncio
async def test_unchanged_pair_skips_per_pr_sweep_but_keeps_posting_path():
    cycle, run = _cycle_and_run()
    compare = AsyncMock(return_value={"status": "ahead", "ahead_by": 47})

    async def read_head(_repo, branch, *, token):
        assert token == "app-token"
        return {"staging": "staging-same", "main": "main-same"}[branch]

    outcome = await async_apply_promotion_readiness_gate(
        object(),
        cycle=cycle,
        run=run,
        now=NOW,
        baseline_reader=AsyncMock(return_value=("staging-same", "main-same")),
        token_resolver=AsyncMock(return_value="app-token"),
        branch_head_reader=read_head,
        branch_comparison_reader=compare,
        configured_cycle_ids_reader=_configured_cycle_ids(),
    )

    assert outcome is PromotionReadinessOutcome.UNCHANGED
    compare.assert_awaited_once()
    assert run.started_at is None
    gate = run.context_snapshot["promotion_readiness_gate"]
    assert gate["outcome"] == "unchanged"
    assert set(gate) == {"outcome", "evidence"}
    assert gate["evidence"]["ahead_by"] == 47
    assert run.self_review_summary is None
    assert "mission_result_contract_verdict" not in run.context_snapshot


@pytest.mark.asyncio
async def test_changed_pair_with_staging_ahead_reaches_agent_review_path():
    cycle, run = _cycle_and_run()
    compare = AsyncMock(return_value={"status": "ahead", "ahead_by": 47})

    async def read_head(_repo, branch, *, token):
        assert token == "app-token"
        return {"staging": "staging-new", "main": "main-new"}[branch]

    outcome = await async_apply_promotion_readiness_gate(
        object(),
        cycle=cycle,
        run=run,
        now=NOW,
        baseline_reader=AsyncMock(return_value=("staging-old", "main-old")),
        token_resolver=AsyncMock(return_value="app-token"),
        branch_head_reader=read_head,
        branch_comparison_reader=compare,
        configured_cycle_ids_reader=_configured_cycle_ids(),
    )

    assert outcome is PromotionReadinessOutcome.EVALUATE
    assert run.context_snapshot["promotion_readiness_gate"]["outcome"] == "evaluate"
    assert run.context_snapshot["promotion_readiness_gate"]["evidence"][
        "ahead_by"
    ] == 47
    assert run.self_review_summary is None
    compare.assert_awaited_once_with(
        "uwear-ai/uwear-backend",
        "main",
        "staging",
        token="app-token",
    )


@pytest.mark.asyncio
async def test_changed_pair_without_staging_ahead_short_circuits_as_idle():
    cycle, run = _cycle_and_run()

    async def read_head(_repo, branch, *, token):
        return {"staging": "staging-new", "main": "main-new"}[branch]

    outcome = await async_apply_promotion_readiness_gate(
        object(),
        cycle=cycle,
        run=run,
        now=NOW,
        baseline_reader=AsyncMock(return_value=("staging-old", "main-old")),
        token_resolver=AsyncMock(return_value="app-token"),
        branch_head_reader=read_head,
        branch_comparison_reader=AsyncMock(
            return_value={"status": "identical", "ahead_by": 0}
        ),
        configured_cycle_ids_reader=_configured_cycle_ids(),
    )

    assert outcome is PromotionReadinessOutcome.IDLE
    assert run.context_snapshot["promotion_readiness_gate"]["outcome"] == "idle"
    assert "Risk: IDLE" in run.self_review_summary
    assert "Posted: No — staging is not ahead" in run.self_review_summary


@pytest.mark.asyncio
async def test_failed_cheap_read_degrades_open_to_agent_review():
    cycle, run = _cycle_and_run()

    outcome = await async_apply_promotion_readiness_gate(
        object(),
        cycle=cycle,
        run=run,
        now=NOW,
        baseline_reader=AsyncMock(side_effect=RuntimeError("status record unavailable")),
        token_resolver=AsyncMock(return_value="app-token"),
        branch_head_reader=AsyncMock(),
        branch_comparison_reader=AsyncMock(),
        configured_cycle_ids_reader=_configured_cycle_ids(),
    )

    assert outcome is PromotionReadinessOutcome.UNAVAILABLE
    gate = run.context_snapshot["promotion_readiness_gate"]
    assert gate["outcome"] == "unavailable"
    assert gate["evidence"]["error"] == "RuntimeError: status record unavailable"
    assert run.self_review_summary is None


@pytest.mark.asyncio
async def test_ambiguous_configured_cycle_finishes_with_a_configuration_verdict():
    cycle, run = _cycle_and_run()

    outcome = await async_apply_promotion_readiness_gate(
        object(),
        cycle=cycle,
        run=run,
        now=NOW,
        configured_cycle_ids_reader=AsyncMock(return_value=(9, 10)),
    )

    assert outcome is PromotionReadinessOutcome.CONFIGURATION_ERROR
    assert run.context_snapshot["promotion_readiness_gate"] == {
        "outcome": "configuration_error",
        "evidence": {
            "evaluated_at": NOW.isoformat(),
            "repository": "uwear-ai/uwear-backend",
            "error": "configured cycle name is ambiguous",
            "matching_cycle_ids": [9, 10],
        },
    }
    assert "Risk: UNKNOWN" in run.self_review_summary
    assert "gate was not reached" in run.self_review_summary


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("cycle_ids", "expected_status"),
    [
        ((), "missing_or_renamed"),
        ((9, 10), "ambiguous"),
    ],
)
async def test_configuration_validation_alerts_on_unsafe_name_resolution(
    caplog,
    cycle_ids,
    expected_status,
):
    status = await async_validate_promotion_readiness_policy_configuration(
        object(),
        configured_cycle_ids_reader=AsyncMock(return_value=cycle_ids),
    )

    assert status == expected_status
    assert "cycle_execution_policy_configuration_error" in caplog.text
