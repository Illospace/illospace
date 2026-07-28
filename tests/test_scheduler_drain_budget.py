"""Regression tests for bounded scheduler drains and detached agent runs."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select

from brain.app.scheduler.executor import (
    async_drain_scheduler,
    async_reconcile_scheduler_agent_runs,
    async_run_scheduler_run,
)
from brain.app.scheduler.planner import async_materialize_due_runs
from brain.jobs.pipelines import aws_health_scan
from brain.platform.db.models.agent_run import AgentRunRow
from brain.platform.db.models.scheduler import SchedulerJob, SchedulerRun
from tests.scheduler_test_support import (
    make_scheduler_job,
    make_scheduler_test_session,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def session(async_sqlite_session_factory):
    return await make_scheduler_test_session(async_sqlite_session_factory)


def _due_job(job_key: str, *, priority: int) -> SchedulerJob:
    return make_scheduler_job(
        job_key=job_key,
        family=job_key,
        program_key=job_key,
        handler_kind="command",
        handler_ref=job_key,
        priority=priority,
        retry_policy={"max_attempts": 1, "backoff_seconds": 0},
        default_payload={"name": job_key.replace("_", " ").title()},
        next_run_at=datetime(2026, 7, 28, 19, 30, tzinfo=timezone.utc),
    )


async def test_drain_budget_bounds_admission_not_in_flight_execution(session):
    blocker = _due_job("blocking_job", priority=200)
    blocker.timeout_seconds = 60
    sibling = _due_job("sibling_job", priority=100)
    session.add_all([blocker, sibling])
    await session.flush()

    runner_timeouts = []

    async def runner(command, **_kwargs):
        runner_timeouts.append(_kwargs["timeout_seconds"])
        if command == ["blocking_job"]:
            await asyncio.sleep(0.3)
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    now = datetime(2026, 7, 28, 19, 30, tzinfo=timezone.utc)
    first = await asyncio.wait_for(
        async_drain_scheduler(
            session,
            max_runs=2,
            runner=runner,
            execution_budget_seconds=0.25,
            now=now,
        ),
        timeout=1.5,
    )

    assert first["executed"] == 1
    assert first["budget_exhausted"] is True
    assert first["results"] == [
        {
            "run_id": first["results"][0]["run_id"],
            "job_id": blocker.id,
            "status": "settled_success",
            "error_text": None,
        }
    ]
    assert runner_timeouts == [blocker.timeout_seconds]

    sibling_run = await session.scalar(
        select(SchedulerRun).where(SchedulerRun.job_id == sibling.id)
    )
    assert sibling_run is not None
    assert sibling_run.status == "recorded"
    assert sibling_run.lease_id is None

    second = await async_drain_scheduler(
        session,
        max_runs=2,
        runner=runner,
        execution_budget_seconds=1,
        now=now,
    )

    assert second["executed"] == 1
    assert second["results"][0]["job_id"] == sibling.id
    assert second["results"][0]["status"] == "settled_success"
    assert "budget_exhausted" not in second

    await session.refresh(sibling_run)
    assert sibling_run.status == "settled_success"


async def test_healthy_drain_keeps_existing_result_shape(session):
    job = _due_job("healthy_job", priority=100)
    session.add(job)
    await session.flush()

    async def runner(_command, **_kwargs):
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    result = await async_drain_scheduler(
        session,
        runner=runner,
        execution_budget_seconds=1,
        now=datetime(2026, 7, 28, 19, 30, tzinfo=timezone.utc),
    )

    assert result == {
        "ok": True,
        "executed": 1,
        "results": [
            {
                "run_id": result["results"][0]["run_id"],
                "job_id": job.id,
                "status": "settled_success",
                "error_text": None,
            }
        ],
    }


async def test_structured_dispatch_leaves_scheduler_run_for_reconciliation(session):
    job = make_scheduler_job(
        job_key="uwear_aws_health_scan",
        family="uwear_aws_health_scan",
        program_key="uwear_aws_health_scan",
        handler_ref="brain.app.scheduler.programs:uwear_aws_health_scan",
        retry_policy={"max_attempts": 1, "backoff_seconds": 0},
        next_run_at=datetime(2026, 7, 28, 19, 30, tzinfo=timezone.utc),
    )
    session.add(job)
    await session.flush()
    run = (
        await async_materialize_due_runs(
            session,
            now=datetime(2026, 7, 28, 19, 30, tzinfo=timezone.utc),
        )
    )[0]

    async def runner(_command, **_kwargs):
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {"scheduler_agent_run_id": 321, "status": "dispatched"}
            ),
            stderr="",
        )

    dispatched = await async_run_scheduler_run(
        session,
        run.id,
        runner=runner,
        now=datetime(2026, 7, 28, 19, 30, tzinfo=timezone.utc),
    )

    assert dispatched.status == "executing"
    assert dispatched.agent_run_id == 321
    assert dispatched.lease_id is None
    assert dispatched.finished_at is None
    assert dispatched.result_summary["agent_run"] == {
        "run_id": 321,
        "status": "dispatched",
    }


async def test_terminal_agent_run_is_recorded_on_scheduler_run(monkeypatch):
    now = datetime(2026, 7, 28, 19, 44, tzinfo=timezone.utc)
    scheduler_run = SimpleNamespace(
        id=17,
        job_id=9,
        status="executing",
        agent_run_id=321,
        trace_id="run:321",
        result_summary={
            "steps": [],
            "agent_run": {"run_id": 321, "status": "dispatched"},
        },
        error_text=None,
        finished_at=None,
        lease_id=None,
    )
    job = SimpleNamespace(
        id=9,
        job_key="uwear_aws_health_scan",
        enabled=True,
        pause_reason=None,
        last_finished_at=None,
    )
    agent_run = SimpleNamespace(
        id=321,
        status="completed",
        completed_at=now,
        failed_at=None,
        canceled_at=None,
    )
    scalar_result = MagicMock()
    scalar_result.all.return_value = [scheduler_run]
    mock_session = MagicMock()
    mock_session.scalars = AsyncMock(return_value=scalar_result)
    mock_session.scalar = AsyncMock(return_value=None)
    mock_session.flush = AsyncMock()

    async def get(model, row_id):
        if model is AgentRunRow:
            assert row_id == 321
            return agent_run
        if model is SchedulerJob:
            assert row_id == 9
            return job
        raise AssertionError(f"Unexpected model lookup: {model}")

    mock_session.get = AsyncMock(side_effect=get)
    reset_guard = AsyncMock()
    monkeypatch.setattr(
        "brain.app.scheduler.executor.async_reset_scheduler_job_failure_guard",
        reset_guard,
    )

    reconciled = await async_reconcile_scheduler_agent_runs(
        mock_session,
        now=now,
    )

    assert reconciled == [
        {
            "run_id": 17,
            "agent_run_id": 321,
            "agent_run_status": "completed",
            "status": "settled_success",
        }
    ]
    assert scheduler_run.status == "settled_success"
    assert scheduler_run.finished_at == now
    assert scheduler_run.result_summary["agent_run"] == {
        "run_id": 321,
        "status": "completed",
        "reconciled_at": now.isoformat(),
    }
    reset_guard.assert_awaited_once_with(mock_session, job, now=now)


async def test_aws_health_scan_returns_immediately_after_dispatch(monkeypatch, capsys):
    spawn = AsyncMock(return_value=321)
    monkeypatch.setattr(aws_health_scan, "spawn_health_scan_run", spawn)

    exit_code = await asyncio.wait_for(aws_health_scan.async_main(), timeout=0.1)

    assert exit_code == 0
    spawn.assert_awaited_once_with()
    assert json.loads(capsys.readouterr().out) == {
        "scheduler_agent_run_id": 321,
        "status": "dispatched",
    }
