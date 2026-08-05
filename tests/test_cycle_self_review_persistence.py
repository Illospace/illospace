from __future__ import annotations

from datetime import datetime, timezone

import pytest

from brain.platform.db.models.cycle import Cycle, CycleRun
from brain.systems.cycles import memory as cycle_memory
from brain.systems.cycles.contract_gate import (
    CLOSING_BLOCK_VERDICT_KEY,
    extract_self_review_summary,
)
from brain.systems.cycles.contracts import PROMOTION_READINESS_CYCLE_NAME


SELF_REVIEW = "I should verify the evidence gap earlier in the next run."


class _CaptureSession:
    def __init__(self) -> None:
        self.added = []

    def add(self, value) -> None:
        self.added.append(value)


def _cycle_and_run() -> tuple[Cycle, CycleRun]:
    cycle = Cycle()
    cycle.id = 7
    cycle.user_id = "user-1"
    cycle.org_id = "org-1"
    cycle.target_idea_id = None
    cycle.degradation_state = {}

    run = CycleRun()
    run.id = 12
    run.run_id = 44
    run.idea_id = None
    run.scheduled_for = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)
    run.context_snapshot = {}
    return cycle, run


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        (f"Self-review summary: {SELF_REVIEW}", SELF_REVIEW),
        (f"Self-review: {SELF_REVIEW}", SELF_REVIEW),
        (f"My self review is that {SELF_REVIEW}", None),
        ("Self-review summary:\nNext action: inspect the next run.", None),
    ],
    ids=["summary-marker", "short-marker", "loose-phrase", "empty-marker"],
)
def test_extract_self_review_summary_accepts_only_populated_markers(answer, expected):
    assert extract_self_review_summary(answer) == expected


@pytest.mark.asyncio
async def test_cycle_evaluation_keeps_usage_numeric_and_summary_in_evaluation(monkeypatch):
    async def summarize_usage(_session, run_id):
        assert run_id == 44
        return {"tokens_total": 12_345, "estimated_cost": 0.06789}

    monkeypatch.setattr(
        cycle_memory,
        "async_summarize_run_tree_usage_in_savepoint",
        summarize_usage,
    )
    session = _CaptureSession()
    cycle, run = _cycle_and_run()

    await cycle_memory.record_cycle_run_evaluation(
        session,
        run,
        cycle,
        status="completed",
    )

    assert run.context_snapshot["usage"]["tokens_total"] == 12_345
    assert run.context_snapshot["usage"]["estimated_cost"] == 0.06789
    assert "summary" not in run.context_snapshot["usage"]
    assert session.added[-1].summary.endswith(
        "Burn: 12,345 tokens; estimated cost $0.067890."
    )
    assert session.added[-1].details["usage"] == run.context_snapshot["usage"]


@pytest.mark.asyncio
async def test_cycle_evaluation_omits_usage_when_no_usage_was_recorded(monkeypatch):
    async def summarize_usage(_session, _run_id):
        return None

    monkeypatch.setattr(
        cycle_memory,
        "async_summarize_run_tree_usage_in_savepoint",
        summarize_usage,
    )
    session = _CaptureSession()
    cycle, run = _cycle_and_run()

    await cycle_memory.record_cycle_run_evaluation(
        session,
        run,
        cycle,
        status="completed",
    )

    assert run.self_review_summary is None
    assert "usage" not in run.context_snapshot
    assert session.added[-1].details["usage"] is None


@pytest.mark.asyncio
async def test_failed_promotion_run_records_that_the_closing_gate_was_not_reached():
    session = _CaptureSession()
    cycle, run = _cycle_and_run()
    cycle.name = PROMOTION_READINESS_CYCLE_NAME
    run.run_id = None

    await cycle_memory.record_cycle_run_evaluation(
        session,
        run,
        cycle,
        status="failed",
        error="agent budget exhausted",
    )

    closing = run.context_snapshot["mission_result_contract_verdict"][
        CLOSING_BLOCK_VERDICT_KEY
    ]
    assert closing == {
        "risk": "UNKNOWN",
        "evaluated": "No — closing gate was not reached before failed",
        "posted": (
            "Unknown — no posting verdict was recorded (agent budget exhausted)"
        ),
        "outcome": "gate_not_reached",
    }
    assert run.self_review_summary == (
        "Risk: UNKNOWN\n"
        "Evaluated: No — closing gate was not reached before failed\n"
        "Posted: Unknown — no posting verdict was recorded (agent budget exhausted)"
    )
    assert session.added[-1].details["mission_result_contract_verdict"][
        CLOSING_BLOCK_VERDICT_KEY
    ]["outcome"] == "gate_not_reached"
