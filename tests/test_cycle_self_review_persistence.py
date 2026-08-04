from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from brain.platform.db.models.cycle import Cycle, CycleRun
from brain.systems.cycles import memory as cycle_memory
from brain.systems.cycles.contract_gate import (
    MISSION_RESULT_CONTRACT_VERDICT_KEY,
    evaluate_cycle_result_contract,
)


SELF_REVIEW = "I should verify the evidence gap earlier in the next run."


class _CaptureSession:
    def __init__(self) -> None:
        self.added = []

    def add(self, value) -> None:
        self.added.append(value)


def _cycle_and_run(*, self_review_summary: str | None) -> tuple[Cycle, CycleRun]:
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
    run.context_snapshot = {
        MISSION_RESULT_CONTRACT_VERDICT_KEY: {
            "settlement_status": "mission_success",
            "self_review_summary": self_review_summary,
        }
    }
    return cycle, run


def test_contract_gate_extracts_the_agent_self_review_from_the_validated_marker():
    review = evaluate_cycle_result_contract(
        candidate_answer=(
            "The cycle reviewed the workspace evidence and completed its mission.\n"
            "Evidence health: ok.\n"
            "Next action: inspect the next scheduled run.\n"
            f"Self-review summary: {SELF_REVIEW}"
        ),
        result_contract={
            "kind": "autonomous_cycle_run_result",
            "required_outputs": ["short_self_review_summary"],
        },
        mission="Review the workspace.",
    )

    assert review["approved"] is True
    assert review["self_review_summary"] == SELF_REVIEW


@pytest.mark.asyncio
async def test_cycle_evaluation_persists_self_review_and_keeps_cost_summary_in_usage(
    monkeypatch,
):
    async def summarize_usage(_session, run_id):
        assert run_id == 44
        return {"tokens_total": 12_345, "estimated_cost": 0.06789}

    monkeypatch.setattr(
        cycle_memory,
        "async_summarize_run_tree_usage_in_savepoint",
        summarize_usage,
    )
    session = _CaptureSession()
    cycle, run = _cycle_and_run(self_review_summary=SELF_REVIEW)

    await cycle_memory.record_cycle_run_evaluation(
        session,
        run,
        cycle,
        status="completed",
    )

    assert run.self_review_summary == SELF_REVIEW
    assert "tokens" not in run.self_review_summary
    assert "$" not in run.self_review_summary
    assert run.context_snapshot["usage"]["tokens_total"] == 12_345
    assert run.context_snapshot["usage"]["estimated_cost"] == 0.06789
    assert run.context_snapshot["usage"]["summary"].endswith(
        "Burn: 12,345 tokens; estimated cost $0.067890."
    )
    assert session.added[-1].summary == run.context_snapshot["usage"]["summary"]


@pytest.mark.asyncio
async def test_cycle_evaluation_persists_none_when_self_review_is_missing(monkeypatch):
    async def summarize_usage(_session, _run_id):
        return {"tokens_total": 900, "estimated_cost": 0.001}

    monkeypatch.setattr(
        cycle_memory,
        "async_summarize_run_tree_usage_in_savepoint",
        summarize_usage,
    )
    session = _CaptureSession()
    cycle, run = _cycle_and_run(self_review_summary=None)
    run.context_snapshot[MISSION_RESULT_CONTRACT_VERDICT_KEY].update(
        {
            "settlement_status": "mission_contract_failed",
            "final_missing_outputs": ["short_self_review_summary"],
        }
    )

    await cycle_memory.record_cycle_run_evaluation(
        session,
        run,
        cycle,
        status="degraded",
        error="mission_contract_failed: missing short_self_review_summary",
    )

    assert run.self_review_summary is None
    assert "900 tokens" in run.context_snapshot["usage"]["summary"]
    assert "$0.001000" in run.context_snapshot["usage"]["summary"]


@pytest.mark.asyncio
async def test_workspace_cycle_history_emits_the_agent_self_review_sentence():
    from brain.systems.runs.tool_catalog.handlers import workspace_data

    now = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)
    run = SimpleNamespace(
        id=12,
        cycle_id=7,
        revision_id=None,
        created_at=now,
        scheduled_for=now,
        started_at=now,
        completed_at=now,
        status="completed",
        error=None,
        skip_reason=None,
        idea_id=None,
        run_id=44,
        prompt_snapshot="Review the workspace.",
        guidance_snapshot=[],
        output_targets_snapshot=[],
        context_snapshot={},
        self_review_summary=SELF_REVIEW,
    )
    cycle = SimpleNamespace(
        id=7,
        name="Workspace review",
        user_id="user-1",
        org_id="org-1",
        deleted_at=None,
    )
    user = SimpleNamespace(name="Illo")

    class _RowsResult:
        def all(self):
            return [(run, cycle, user, None)]

    class _Session:
        def execute(self, _stmt):
            return _RowsResult()

    payload = {"sources": {}}
    await workspace_data._query_cycle_runs(
        _Session(),
        payload,
        start=None,
        end=None,
        org_id="org-1",
        user_id=None,
        person_ids=[],
        idea_id=None,
        cycle_id=7,
        search=None,
        include_archived=False,
        limit=10,
    )

    assert payload["sources"]["cycle_runs"][0]["self_review_summary"] == SELF_REVIEW
