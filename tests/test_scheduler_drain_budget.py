"""Regression tests for bounded scheduler drains and detached agent runs."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.schema import CreateTable

from brain.contracts.scheduler_handoff import (
    emit_detached_agent_run_handoff,
)
from brain.app.scheduler.executor import (
    async_drain_scheduler,
    async_run_scheduler_run,
)
from brain.app.scheduler.planner import async_materialize_due_runs
from brain.jobs.pipelines import aws_health_scan
from brain.platform.db.models.agent_run import AgentRunRow
from brain.platform.db.models.scheduler import (
    SchedulerJob,
    SchedulerRun,
    SchedulerRunStep,
)
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
            admission_budget_seconds=0.25,
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
        admission_budget_seconds=1,
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
        admission_budget_seconds=1,
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


async def test_undeclared_command_output_cannot_activate_detached_lifecycle(session):
    job = _due_job("ordinary_json_job", priority=100)
    session.add(job)
    await session.flush()

    async def runner(_command, **_kwargs):
        return SimpleNamespace(
            returncode=0,
            stdout=emit_detached_agent_run_handoff(321),
            stderr="",
        )

    result = await async_drain_scheduler(
        session,
        runner=runner,
        admission_budget_seconds=1,
        now=datetime(2026, 7, 28, 19, 30, tzinfo=timezone.utc),
    )

    scheduler_run = await session.get(SchedulerRun, result["results"][0]["run_id"])
    assert scheduler_run is not None
    step = await session.scalar(
        select(SchedulerRunStep).where(SchedulerRunStep.run_id == scheduler_run.id)
    )
    assert step is not None
    assert scheduler_run.status == "settled_success"
    assert scheduler_run.agent_run_id is None
    assert step.status == "settled_success"
    assert step.agent_run_id is None


async def test_declared_detached_command_rejects_malformed_handoff(session):
    now = datetime(2026, 7, 28, 19, 30, tzinfo=timezone.utc)
    job = make_scheduler_job(
        job_key="uwear_aws_health_scan",
        family="uwear_aws_health_scan",
        program_key="uwear_aws_health_scan",
        handler_ref="brain.app.scheduler.programs:uwear_aws_health_scan",
        retry_policy={"max_attempts": 1, "backoff_seconds": 0},
        next_run_at=now,
    )
    session.add(job)
    await session.flush()

    async def runner(_command, **_kwargs):
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {"scheduler_agent_run_id": 321, "status": "dispatched"}
            ),
            stderr="",
        )

    result = await async_drain_scheduler(
        session,
        runner=runner,
        admission_budget_seconds=1,
        now=now,
    )

    assert result["results"][0]["status"] == "settled_failure"
    scheduler_run = await session.get(
        SchedulerRun,
        result["results"][0]["run_id"],
    )
    assert scheduler_run is not None
    assert scheduler_run.agent_run_id is None
    assert "invalid detached AgentRun handoff envelope" in scheduler_run.error_text


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
            stdout=emit_detached_agent_run_handoff(321),
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
    step = await session.scalar(
        select(SchedulerRunStep).where(SchedulerRunStep.run_id == dispatched.id)
    )
    assert step is not None
    assert step.status == "executing"
    assert step.finished_at is None
    assert dispatched.result_summary["agent_run"] == {
        "run_id": 321,
        "status": "dispatched",
    }


@pytest.mark.parametrize(
    ("agent_status", "scheduler_status", "finished_at_field"),
    [
        ("completed", "settled_success", "completed_at"),
        ("failed", "settled_failure", "failed_at"),
        ("canceled", "settled_failure", "canceled_at"),
    ],
)
async def test_later_drain_reconciles_detached_agent_run_and_step(
    session,
    agent_status,
    scheduler_status,
    finished_at_field,
):
    await session.execute(CreateTable(AgentRunRow.__table__, if_not_exists=True))

    now = datetime(2026, 7, 28, 19, 30, tzinfo=timezone.utc)
    job = make_scheduler_job(
        job_key="uwear_aws_health_scan",
        family="uwear_aws_health_scan",
        program_key="uwear_aws_health_scan",
        handler_ref="brain.app.scheduler.programs:uwear_aws_health_scan",
        retry_policy={"max_attempts": 1, "backoff_seconds": 0},
        next_run_at=now,
    )
    session.add(job)
    await session.flush()

    async def runner(_command, **_kwargs):
        return SimpleNamespace(
            returncode=0,
            stdout=emit_detached_agent_run_handoff(321),
            stderr="",
        )

    first_drain = await async_drain_scheduler(
        session,
        job_key=job.job_key,
        runner=runner,
        admission_budget_seconds=1,
        now=now,
    )
    assert first_drain["executed"] == 1

    scheduler_run = await session.scalar(
        select(SchedulerRun).where(SchedulerRun.job_id == job.id)
    )
    assert scheduler_run is not None
    scheduler_step = await session.scalar(
        select(SchedulerRunStep).where(SchedulerRunStep.run_id == scheduler_run.id)
    )
    assert scheduler_step is not None
    assert scheduler_run.status == "executing"
    assert scheduler_step.status == "executing"

    terminal_at = datetime(2026, 7, 28, 19, 35, tzinfo=timezone.utc)
    session.add(
        AgentRunRow(
            id=321,
            thread_id=f"headless:health-scan:{agent_status}",
            profile="fast",
            recipe="fast",
            status=agent_status,
            input_message="Run the AWS health scan.",
            target_ref={},
            workspace_ref={},
            model_policy={},
            metadata_={},
            **{finished_at_field: terminal_at},
        )
    )
    await session.flush()

    second_drain = await async_drain_scheduler(
        session,
        job_key=job.job_key,
        runner=runner,
        admission_budget_seconds=1,
        now=terminal_at,
    )
    assert second_drain["executed"] == 0

    await session.refresh(scheduler_run)
    await session.refresh(scheduler_step)
    assert scheduler_run.status == scheduler_status
    assert scheduler_step.status == scheduler_status
    assert scheduler_run.finished_at is not None
    assert scheduler_step.finished_at == scheduler_run.finished_at
    assert scheduler_run.finished_at.replace(tzinfo=timezone.utc) == terminal_at
    assert scheduler_run.result_summary["agent_run"]["status"] == agent_status
    assert scheduler_step.result_summary["agent_run"]["status"] == agent_status


async def test_aws_health_scan_returns_immediately_after_dispatch(monkeypatch, capsys):
    spawn = AsyncMock(return_value=321)
    monkeypatch.setattr(aws_health_scan, "spawn_health_scan_run", spawn)

    exit_code = await asyncio.wait_for(aws_health_scan.async_main(), timeout=0.1)

    assert exit_code == 0
    spawn.assert_awaited_once_with()
    assert json.loads(capsys.readouterr().out) == {
        "type": "scheduler.detached_agent_run",
        "version": 1,
        "scheduler_agent_run_id": 321,
        "status": "dispatched",
    }
