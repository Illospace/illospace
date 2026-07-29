"""Typed detached AgentRun handoff contract tests."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from sqlalchemy import event
from sqlalchemy.schema import CreateTable

from brain.app.scheduler.detached_agent_runs import async_reconcile_detached_runs
from brain.app.scheduler.runtime import async_finish_run
from brain.contracts.scheduler_handoff import (
    DetachedAgentRunHandoff,
    DetachedAgentRunHandoffError,
    emit_detached_agent_run_handoff,
    parse_detached_agent_run_handoff,
)
from brain.platform.db.models.agent_run import AgentRunRow
from brain.platform.db.models.scheduler import SchedulerRun, SchedulerRunStep
from tests.scheduler_test_support import (
    make_scheduler_job,
    make_scheduler_test_session,
)


@pytest.fixture
async def scheduler_session(async_sqlite_session_factory):
    session = await make_scheduler_test_session(async_sqlite_session_factory)
    await session.execute(CreateTable(AgentRunRow.__table__, if_not_exists=True))
    return session


async def _add_detached_candidate(
    session,
    *,
    candidate_id: int,
    agent_status: str | None,
    terminal_at: datetime | None = None,
    attempt: int = 1,
):
    job = make_scheduler_job(
        job_key=f"detached_job_{candidate_id}",
        family=f"detached_job_{candidate_id}",
        program_key=f"detached_job_{candidate_id}",
        retry_policy={"max_attempts": 2, "backoff_seconds": 60},
        next_run_at=None,
    )
    session.add(job)
    await session.flush()

    agent_run_id = 1000 + candidate_id
    run = SchedulerRun(
        job_id=job.id,
        scheduled_for=datetime(2026, 7, 28, 19, 30, tzinfo=timezone.utc),
        window_start=datetime(2026, 7, 28, 19, 30, tzinfo=timezone.utc),
        window_end=datetime(2026, 7, 28, 19, 31, tzinfo=timezone.utc),
        status="executing",
        attempt=attempt,
        idempotency_key=f"detached-candidate-{candidate_id}",
        result_summary={"candidate": candidate_id},
        agent_run_id=agent_run_id,
        trace_id=f"run:{agent_run_id}",
        started_at=datetime(2026, 7, 28, 19, 30, tzinfo=timezone.utc),
    )
    session.add(run)
    await session.flush()

    step = SchedulerRunStep(
        run_id=run.id,
        step_key="detached",
        sequence_no=1,
        status="executing",
        attempt=attempt,
        agent_run_id=agent_run_id,
        trace_id=f"run:{agent_run_id}",
        started_at=run.started_at,
        result_summary={"candidate": candidate_id},
    )
    session.add(step)

    agent_run = None
    if agent_status is not None:
        terminal_field = {
            "completed": "completed_at",
            "failed": "failed_at",
            "canceled": "canceled_at",
        }.get(agent_status)
        agent_run = AgentRunRow(
            id=agent_run_id,
            thread_id=f"headless:detached:{candidate_id}",
            profile="fast",
            recipe="fast",
            status=agent_status,
            input_message=f"Run detached candidate {candidate_id}.",
            target_ref={},
            workspace_ref={},
            model_policy={},
            metadata_={},
            **({terminal_field: terminal_at} if terminal_field else {}),
        )
        session.add(agent_run)
    await session.flush()
    return job, run, step, agent_run


def test_detached_agent_run_handoff_round_trips_the_complete_envelope():
    encoded = emit_detached_agent_run_handoff(321)

    assert json.loads(encoded) == {
        "type": "scheduler.detached_agent_run",
        "version": 1,
        "status": "dispatched",
        "scheduler_agent_run_id": 321,
    }
    assert parse_detached_agent_run_handoff(encoded) == DetachedAgentRunHandoff(
        agent_run_id=321
    )


@pytest.mark.parametrize(
    "payload",
    [
        {
            "type": "scheduler.detached_agent_run",
            "version": 1,
            "scheduler_agent_run_id": 321,
        },
        {
            "type": "scheduler.detached_agent_run",
            "version": 1,
            "status": "completed",
            "scheduler_agent_run_id": 321,
        },
        {
            "type": "scheduler.detached_agent_run",
            "version": 2,
            "status": "dispatched",
            "scheduler_agent_run_id": 321,
        },
        {
            "type": "scheduler.detached_agent_run",
            "version": 1,
            "status": "dispatched",
            "scheduler_agent_run_id": "321",
        },
        {
            "type": "scheduler.detached_agent_run",
            "version": 1,
            "status": "dispatched",
            "scheduler_agent_run_id": 321,
            "extra": True,
        },
    ],
)
def test_detached_agent_run_handoff_rejects_any_malformed_envelope(payload):
    with pytest.raises(DetachedAgentRunHandoffError):
        parse_detached_agent_run_handoff(json.dumps(payload))


@pytest.mark.asyncio
async def test_finish_run_rejects_a_job_for_a_different_run(scheduler_session):
    job, run, _, _ = await _add_detached_candidate(
        scheduler_session,
        candidate_id=10,
        agent_status="completed",
    )
    wrong_job = make_scheduler_job(
        job_key="wrong_detached_job",
        family="wrong_detached_job",
        program_key="wrong_detached_job",
    )
    scheduler_session.add(wrong_job)
    await scheduler_session.flush()

    with pytest.raises(
        ValueError,
        match=(
            rf"Scheduler job {wrong_job.id} does not match run {run.id} "
            rf"job_id {job.id}"
        ),
    ):
        await async_finish_run(
            scheduler_session,
            run,
            job=wrong_job,
            status="settled_success",
        )

    assert run.status == "executing"
    assert run.finished_at is None
    assert job.last_finished_at is None
    assert wrong_job.last_finished_at is None


@pytest.mark.asyncio
async def test_reconcile_detached_runs_rejects_a_missing_job(scheduler_session):
    job, run, _, _ = await _add_detached_candidate(
        scheduler_session,
        candidate_id=11,
        agent_status="completed",
    )
    job_id = job.id
    run_id = run.id
    await scheduler_session.delete(job)
    await scheduler_session.commit()
    scheduler_session.expunge_all()

    with pytest.raises(
        ValueError,
        match=rf"Scheduler job {job_id} not found for detached run {run_id}",
    ):
        await async_reconcile_detached_runs(
            scheduler_session,
            retryable_failure_summary=lambda *_args, **_kwargs: (
                "settled_failure",
                {},
            ),
            apply_failure_guard=lambda *_args, **_kwargs: None,
        )


@pytest.mark.asyncio
async def test_reconcile_detached_runs_preserves_mixed_candidate_outcomes(
    scheduler_session,
):
    terminal_at = datetime(2026, 7, 28, 19, 35, tzinfo=timezone.utc)
    reconciled_at = datetime(2026, 7, 28, 19, 40, tzinfo=timezone.utc)
    completed = await _add_detached_candidate(
        scheduler_session,
        candidate_id=1,
        agent_status="completed",
        terminal_at=terminal_at,
    )
    retryable = await _add_detached_candidate(
        scheduler_session,
        candidate_id=2,
        agent_status="failed",
        terminal_at=terminal_at,
    )
    failed = await _add_detached_candidate(
        scheduler_session,
        candidate_id=3,
        agent_status="canceled",
        terminal_at=terminal_at,
        attempt=2,
    )
    active = await _add_detached_candidate(
        scheduler_session,
        candidate_id=4,
        agent_status="running",
    )
    missing = await _add_detached_candidate(
        scheduler_session,
        candidate_id=5,
        agent_status=None,
    )
    guarded_run_ids: list[int] = []

    def retryable_failure_summary(job, run, *, base_summary, now):
        del job
        summary = {
            **base_summary,
            "retry_exhausted": run.attempt >= 2,
            "next_retry_at": now.isoformat() if run.attempt < 2 else None,
        }
        if run.attempt < 2:
            return "retryable", summary
        return "settled_failure", summary

    async def apply_failure_guard(_session, _job, run, **_kwargs):
        guarded_run_ids.append(run.id)

    reconciled = await async_reconcile_detached_runs(
        scheduler_session,
        retryable_failure_summary=retryable_failure_summary,
        apply_failure_guard=apply_failure_guard,
        now=reconciled_at,
    )

    completed_job, completed_run, completed_step, _ = completed
    retryable_job, retryable_run, retryable_step, _ = retryable
    failed_job, failed_run, failed_step, _ = failed
    _, active_run, active_step, _ = active
    _, missing_run, missing_step, _ = missing
    assert reconciled == [
        {
            "run_id": completed_run.id,
            "agent_run_id": completed_run.agent_run_id,
            "agent_run_status": "completed",
            "status": "settled_success",
        },
        {
            "run_id": retryable_run.id,
            "agent_run_id": retryable_run.agent_run_id,
            "agent_run_status": "failed",
            "status": "retryable",
        },
        {
            "run_id": failed_run.id,
            "agent_run_id": failed_run.agent_run_id,
            "agent_run_status": "canceled",
            "status": "settled_failure",
        },
    ]
    assert guarded_run_ids == [retryable_run.id, failed_run.id]

    assert completed_run.status == completed_step.status == "settled_success"
    assert completed_run.error_text is None
    assert completed_step.error_text is None
    assert completed_run.finished_at == completed_step.finished_at == terminal_at
    assert completed_job.last_finished_at == terminal_at

    assert retryable_run.status == retryable_step.status == "retryable"
    assert retryable_run.error_text == (
        f"Agent run {retryable_run.agent_run_id} ended with status failed"
    )
    assert retryable_step.error_text == retryable_run.error_text
    assert retryable_run.finished_at == retryable_step.finished_at == terminal_at
    assert retryable_job.last_finished_at == terminal_at
    assert retryable_run.result_summary["retry_exhausted"] is False
    assert retryable_run.result_summary["next_retry_at"] == reconciled_at.isoformat()

    assert failed_run.status == failed_step.status == "settled_failure"
    assert failed_run.error_text == (
        f"Agent run {failed_run.agent_run_id} ended with status canceled"
    )
    assert failed_step.error_text == failed_run.error_text
    assert failed_run.finished_at == failed_step.finished_at == terminal_at
    assert failed_job.last_finished_at == terminal_at
    assert failed_run.result_summary["retry_exhausted"] is True
    assert failed_run.result_summary["next_retry_at"] is None

    for candidate_id, run, step, agent_status in (
        (1, completed_run, completed_step, "completed"),
        (2, retryable_run, retryable_step, "failed"),
        (3, failed_run, failed_step, "canceled"),
    ):
        expected_agent_summary = {
            "run_id": run.agent_run_id,
            "status": agent_status,
            "reconciled_at": reconciled_at.isoformat(),
        }
        assert run.result_summary["candidate"] == candidate_id
        assert run.result_summary["agent_run"] == expected_agent_summary
        assert step.result_summary == {
            "candidate": candidate_id,
            "agent_run": expected_agent_summary,
        }

    for run, step in (
        (active_run, active_step),
        (missing_run, missing_step),
    ):
        assert run.status == step.status == "executing"
        assert run.finished_at is None
        assert step.finished_at is None


@pytest.mark.asyncio
async def test_reconcile_detached_runs_select_count_is_constant_with_batch_size(
    scheduler_session,
):
    terminal_at = datetime(2026, 7, 28, 19, 35, tzinfo=timezone.utc)

    def retryable_failure_summary(_job, _run, *, base_summary, now):
        del now
        return "settled_failure", base_summary

    async def apply_failure_guard(*_args, **_kwargs):
        return None

    async def reconcile_and_count_selects(candidate_ids: range) -> int:
        for candidate_id in candidate_ids:
            await _add_detached_candidate(
                scheduler_session,
                candidate_id=candidate_id,
                agent_status="failed",
                terminal_at=terminal_at,
                attempt=2,
            )
        await scheduler_session.commit()
        scheduler_session.expunge_all()

        select_count = 0

        def count_selects(
            _connection,
            _cursor,
            statement,
            _parameters,
            _context,
            _executemany,
        ):
            nonlocal select_count
            if statement.lstrip().upper().startswith("SELECT"):
                select_count += 1

        engine = scheduler_session.bind
        event.listen(engine.sync_engine, "before_cursor_execute", count_selects)
        try:
            reconciled = await async_reconcile_detached_runs(
                scheduler_session,
                retryable_failure_summary=retryable_failure_summary,
                apply_failure_guard=apply_failure_guard,
                now=terminal_at,
            )
        finally:
            event.remove(
                engine.sync_engine,
                "before_cursor_execute",
                count_selects,
            )

        assert len(reconciled) == len(candidate_ids)
        return select_count

    single_candidate_selects = await reconcile_and_count_selects(range(100, 101))
    larger_batch_selects = await reconcile_and_count_selects(range(200, 208))

    assert single_candidate_selects <= 4
    assert larger_batch_selects <= 4
    assert larger_batch_selects <= single_candidate_selects
