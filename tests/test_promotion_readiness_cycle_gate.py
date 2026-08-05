from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from brain.platform.db.models.cycle import Cycle, CycleRun
from brain.systems.cycles.contracts import PROMOTION_READINESS_CYCLE_NAME
from brain.systems.cycles.promotion_readiness import (
    async_apply_promotion_readiness_gate,
)


NOW = datetime(2026, 8, 5, 15, 0, tzinfo=timezone.utc)


def _cycle_and_run() -> tuple[Cycle, CycleRun]:
    cycle = Cycle()
    cycle.id = 9
    cycle.name = PROMOTION_READINESS_CYCLE_NAME
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


@pytest.mark.asyncio
async def test_unchanged_pair_skips_per_pr_sweep_but_keeps_posting_path():
    cycle, run = _cycle_and_run()
    compare = AsyncMock(return_value={"status": "ahead", "ahead_by": 47})

    async def read_head(_repo, branch, *, token):
        assert token == "app-token"
        return {"staging": "staging-same", "main": "main-same"}[branch]

    decision = await async_apply_promotion_readiness_gate(
        object(),
        cycle=cycle,
        run=run,
        now=NOW,
        baseline_reader=AsyncMock(return_value=("staging-same", "main-same")),
        token_resolver=AsyncMock(return_value="app-token"),
        branch_head_reader=read_head,
        branch_comparison_reader=compare,
    )

    assert decision is not None
    assert decision.outcome == "unchanged"
    assert decision.short_circuit is True
    assert decision.skip_agent is False
    assert decision.requires_per_pr_review is False
    assert decision.reaches_posting_path is True
    assert decision.skip_reason is None
    compare.assert_awaited_once()
    assert run.started_at is None
    assert run.context_snapshot["promotion_readiness_gate"]["short_circuit"] is True
    assert run.context_snapshot["promotion_readiness_gate"][
        "requires_per_pr_review"
    ] is False
    assert run.context_snapshot["promotion_readiness_gate"][
        "reaches_posting_path"
    ] is True
    assert run.self_review_summary is None
    assert "mission_result_contract_verdict" not in run.context_snapshot


@pytest.mark.asyncio
async def test_changed_pair_with_staging_ahead_reaches_agent_review_path():
    cycle, run = _cycle_and_run()
    compare = AsyncMock(return_value={"status": "ahead", "ahead_by": 47})

    async def read_head(_repo, branch, *, token):
        assert token == "app-token"
        return {"staging": "staging-new", "main": "main-new"}[branch]

    decision = await async_apply_promotion_readiness_gate(
        object(),
        cycle=cycle,
        run=run,
        now=NOW,
        baseline_reader=AsyncMock(return_value=("staging-old", "main-old")),
        token_resolver=AsyncMock(return_value="app-token"),
        branch_head_reader=read_head,
        branch_comparison_reader=compare,
    )

    assert decision is not None
    assert decision.outcome == "evaluate"
    assert decision.short_circuit is False
    assert decision.requires_per_pr_review is True
    assert decision.reaches_posting_path is True
    assert decision.ahead_by == 47
    assert run.context_snapshot["promotion_readiness_gate"]["outcome"] == "evaluate"
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

    decision = await async_apply_promotion_readiness_gate(
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
    )

    assert decision is not None
    assert decision.outcome == "idle"
    assert decision.short_circuit is True
    assert decision.skip_agent is True
    assert decision.requires_per_pr_review is False
    assert decision.reaches_posting_path is False
    assert "Risk: IDLE" in run.self_review_summary
    assert "Posted: No — staging is not ahead" in run.self_review_summary


@pytest.mark.asyncio
async def test_failed_cheap_read_degrades_open_to_agent_review():
    cycle, run = _cycle_and_run()

    decision = await async_apply_promotion_readiness_gate(
        object(),
        cycle=cycle,
        run=run,
        now=NOW,
        baseline_reader=AsyncMock(side_effect=RuntimeError("status record unavailable")),
        token_resolver=AsyncMock(return_value="app-token"),
        branch_head_reader=AsyncMock(),
        branch_comparison_reader=AsyncMock(),
    )

    assert decision is not None
    assert decision.outcome == "unavailable"
    assert decision.short_circuit is False
    assert decision.error == "RuntimeError: status record unavailable"
    assert run.self_review_summary is None
