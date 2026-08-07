"""Lifecycle coverage for scheduler configuration failures."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

import brain.app.scheduler.executor as scheduler_executor
from brain.app.scheduler.executor import async_execute_scheduler_run
from brain.app.scheduler.planner import async_materialize_due_runs
from brain.app.scheduler.scheduler_failure_guard import (
    async_reset_scheduler_job_failure_guard,
)
from brain.contracts.scheduler_outcomes import (
    SchedulerConfigurationSkip,
    SchedulerSkipKind,
    configuration_skip_summary,
    find_configuration_skips,
    scheduler_skip_kind,
)
from brain.platform.db.models.scheduler import SchedulerRun
from tests.scheduler_test_support import (
    guard_latches,
    make_scheduler_job,
    make_scheduler_test_session,
)


NOW = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def session(async_sqlite_session_factory):
    return await make_scheduler_test_session(async_sqlite_session_factory)


def _failed_run(job_id: int, *, key: str, now: datetime) -> SchedulerRun:
    return SchedulerRun(
        job_id=job_id,
        scheduled_for=now,
        window_start=now,
        window_end=now + timedelta(hours=1),
        status="settled_failure",
        idempotency_key=key,
        started_at=now,
        finished_at=now,
    )


async def test_classifier_returns_every_typed_configuration_gap():
    payload = {
        "ok": False,
        "results": [
            {
                "repo": "Uwear-AI/uwear",
                "outcome": "skipped",
                "skip_kind": "configuration",
                "reason": "binding missing",
            },
            {
                "repo": "Uwear-AI/uwear-app",
                "outcome": "skipped",
                "skip_kind": "configuration",
                "reason": "token missing",
            },
        ],
    }

    skips = find_configuration_skips(payload)

    assert isinstance(skips, tuple)
    assert all(isinstance(skip, SchedulerConfigurationSkip) for skip in skips)
    assert [(skip.repository, skip.reason) for skip in skips] == [
        ("Uwear-AI/uwear", "binding missing"),
        ("Uwear-AI/uwear-app", "token missing"),
    ]
    assert configuration_skip_summary(skips) == (
        "Configuration gaps:\n"
        "- Uwear-AI/uwear: binding missing\n"
        "- Uwear-AI/uwear-app: token missing"
    )
    assert scheduler_skip_kind(
        {
            "ok": True,
            "outcome": "skipped",
            "skip_kind": "transient",
        }
    ) is SchedulerSkipKind.TRANSIENT


async def test_configuration_latch_is_per_job_and_success_rearms_only_that_job(
    session,
    monkeypatch,
):
    monkeypatch.setenv("SCHEDULER_FAILURE_ALERT_THRESHOLD", "99")
    monkeypatch.setenv("SCHEDULER_STANDING_FAILURE_ALERT_THRESHOLD", "99")
    monkeypatch.setenv("SCHEDULER_FAILURE_RATE_THRESHOLD", "99")
    deliver = AsyncMock()
    monkeypatch.setattr(
        scheduler_executor,
        "async_deliver_scheduler_failure_alert",
        deliver,
    )
    first_job = make_scheduler_job(
        job_key="first-configuration-job",
        family="first-configuration-job",
        program_key="first-configuration-job",
    )
    second_job = make_scheduler_job(
        job_key="second-configuration-job",
        family="second-configuration-job",
        program_key="second-configuration-job",
    )
    session.add_all([first_job, second_job])
    await session.flush()

    first_run = _failed_run(first_job.id, key="first:1", now=NOW)
    second_run = _failed_run(second_job.id, key="second:1", now=NOW)
    session.add_all([first_run, second_run])
    await session.flush()
    for job, run in ((first_job, first_run), (second_job, second_run)):
        await scheduler_executor._async_apply_failure_guard(
            session,
            job,
            run,
            failure_key="handler_execute",
            error_text=f"{job.job_key} binding missing",
            now=NOW,
            failure_kind=SchedulerSkipKind.CONFIGURATION,
        )

    assert deliver.await_count == 2
    assert set(await guard_latches(session, first_job)) == {"configuration"}
    assert set(await guard_latches(session, second_job)) == {"configuration"}

    repeated_run = _failed_run(
        first_job.id,
        key="first:2",
        now=NOW + timedelta(minutes=1),
    )
    session.add(repeated_run)
    await session.flush()
    await scheduler_executor._async_apply_failure_guard(
        session,
        first_job,
        repeated_run,
        failure_key="handler_execute",
        error_text="first job still blocked",
        now=NOW + timedelta(minutes=1),
        failure_kind=SchedulerSkipKind.CONFIGURATION,
    )
    assert deliver.await_count == 2

    await async_reset_scheduler_job_failure_guard(
        session,
        first_job,
        now=NOW + timedelta(minutes=2),
    )
    assert "configuration" not in await guard_latches(session, first_job)
    assert "configuration" in await guard_latches(session, second_job)

    later_run = _failed_run(
        first_job.id,
        key="first:3",
        now=NOW + timedelta(minutes=3),
    )
    session.add(later_run)
    await session.flush()
    await scheduler_executor._async_apply_failure_guard(
        session,
        first_job,
        later_run,
        failure_key="handler_execute",
        error_text="first job has a later configuration incident",
        now=NOW + timedelta(minutes=3),
        failure_kind=SchedulerSkipKind.CONFIGURATION,
    )

    assert deliver.await_count == 3
    assert "configuration" in await guard_latches(session, first_job)
    assert "configuration" in await guard_latches(session, second_job)


async def test_callable_handler_uses_the_same_configuration_classifier(
    session,
    monkeypatch,
):
    monkeypatch.setenv("SCHEDULER_FAILURE_ALERT_THRESHOLD", "99")
    monkeypatch.setenv("SCHEDULER_STANDING_FAILURE_ALERT_THRESHOLD", "99")
    monkeypatch.setenv("SCHEDULER_FAILURE_RATE_THRESHOLD", "99")
    job = make_scheduler_job(
        job_key="callable-configuration-job",
        family="callable-configuration-job",
        program_key="callable-configuration-job",
        handler_kind="python_callable",
        handler_ref="tests.fake:configuration_handler",
    )
    session.add(job)
    await session.flush()
    run = (
        await async_materialize_due_runs(
            session,
            now=NOW,
            allowed_owner_modes=("scheduler",),
        )
    )[0]
    deliver = AsyncMock()
    monkeypatch.setattr(
        scheduler_executor,
        "async_deliver_scheduler_failure_alert",
        deliver,
    )
    monkeypatch.setattr(
        scheduler_executor,
        "_resolve_handler",
        lambda _handler_ref: lambda _payload, **_kwargs: {
            "ok": False,
            "outcome": "skipped",
            "skip_kind": "configuration",
            "reason": "callable binding missing",
        },
    )

    executed = await async_execute_scheduler_run(
        session,
        run.id,
        owner_id="tester",
        now=NOW + timedelta(minutes=1),
    )

    assert executed is not None
    assert executed.status == "settled_failure"
    assert executed.error_text == "callable binding missing"
    assert "configuration" in await guard_latches(session, job)
    deliver.assert_awaited_once()
