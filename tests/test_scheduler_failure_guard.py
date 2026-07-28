"""Scheduler failure-guard evaluation, persistence, health, and delivery tests."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

import brain.app.scheduler.executor as scheduler_executor
import brain.app.scheduler.scheduler_failure_guard as scheduler_failure_guard
from brain.app.scheduler.daemon import (
    async_scheduler_health_snapshot,
)
from brain.systems.failure_guard.core import (
    FailureGuardEdge,
    FailureGuardEvaluation,
    FailureGuardTriggerKind,
    FailureGuardTriggerResult,
    serialize_failure_guard,
)
from brain.app.scheduler.scheduler_failure_guard import (
    CONSECUTIVE_TRIGGER_KIND, ROLLING_WINDOW_TRIGGER_KIND,
    SchedulerFailureGuardResetEvent, scheduler_failure_signature,
)
from brain.app.scheduler.planner import async_materialize_due_runs
from brain.app.scheduler.programs import nightly_heuristic_review_command
from brain.app.scheduler.runtime import RUN_STATUS_SETTLED_SUCCESS
from brain.app.scheduler.executor import async_run_scheduler_run
from brain.platform.db.models.scheduler import (
    SchedulerFailureGuardLatch,
    SchedulerJob,
    SchedulerRun,
)
from tests.scheduler_test_support import (
    make_scheduler_job as _make_scheduler_job,
    make_scheduler_test_session,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def session(async_sqlite_session_factory):
    return await make_scheduler_test_session(async_sqlite_session_factory)


def _guard_trigger(guard: dict[str, Any], kind: str) -> dict[str, Any]:
    triggers = guard["triggers"]
    assert isinstance(triggers, list)
    return next(trigger for trigger in triggers if trigger["kind"] == kind)


async def _guard_latches(
    session,
    job: SchedulerJob,
) -> dict[str, SchedulerFailureGuardLatch]:
    result = await session.scalars(
        select(SchedulerFailureGuardLatch).where(
            SchedulerFailureGuardLatch.job_id == job.id
        )
    )
    return {latch.trigger_kind: latch for latch in result.all()}


async def test_failure_signature_normalizes_volatile_runtime_identity():
    first = """scheduler_step:meta_evolution
Traceback (most recent call last):
  File \"<string>\", line 1, in <module>
RuntimeError: worker=<Worker object at 0x7f12ab90> coroutine=<coroutine object run at 0x7f12bc10> task=Task-12 request_id=0f6f39da-37cd-4d8a-9fb7-818beed63aa1 pid=417 at 2026-07-23T03:00:01Z
"""
    second = """scheduler_step:meta_evolution
Traceback (most recent call last):
  File \"<string>\", line 1, in <module>
RuntimeError: worker=<Worker object at 0x8e23cd01> coroutine=<coroutine object run at 0x8e23de20> task=Task-98 request_id=9a0247bb-6b0a-4c34-bd9d-d684781aba2c pid=991 at 2026-07-24T03:00:09Z
"""

    assert scheduler_failure_signature(first) == scheduler_failure_signature(second)


async def test_scheduler_failure_alert_bytes_are_unchanged_through_shared_delivery(
    monkeypatch,
):
    calls: dict[str, object] = {}

    class FakeSlackClient:
        async def conversations_list(self, **kwargs):
            calls["list_kwargs"] = kwargs
            return {"channels": [{"id": "C_ALERTS", "name": "alerts"}]}

        async def post_message(self, *, channel, text):
            calls["post"] = {"channel": channel, "text": text}
            return {"ok": True, "message": {"text": text}}

    async def fake_client_from_runtime(*, requested_by, reason):
        calls["runtime"] = {"requested_by": requested_by, "reason": reason}
        return FakeSlackClient()

    monkeypatch.setenv("ILLO_SCHEDULER_FAILURE_ALERT_CHANNEL", "#alerts")
    monkeypatch.setenv("ILLO_PUBLIC_URL", "https://illo.example.com")
    monkeypatch.setattr(
        scheduler_executor,
        "slack_web_client_from_runtime",
        fake_client_from_runtime,
    )

    evaluation = FailureGuardEvaluation(
        failure_signature="signature",
        last_error="RuntimeError: failed",
        edges=(
            FailureGuardEdge(
                kind=CONSECUTIVE_TRIGGER_KIND,
                public_details={
                    "count": 3,
                    "threshold": 3,
                    "window_hours": None,
                },
                alerted_at=datetime(2026, 4, 21, tzinfo=timezone.utc),
                crossed=True,
                alert_title="Scheduler job repeated failure",
                alert_summary="Consecutive failures: 3",
            ),
        ),
    )
    await scheduler_executor.async_deliver_scheduler_failure_alert(
        job_key="nightly_sleep",
        run_id=42,
        evaluation=evaluation,
        error_text="RuntimeError: failed\nvolatile traceback details",
    )

    assert calls["runtime"] == {
        "requested_by": "scheduler_failure_alert",
        "reason": "Deliver a repeated scheduler job failure alert to the team.",
    }
    assert calls["post"] == {
        "channel": "C_ALERTS",
        "text": (
            "Scheduler job repeated failure\n"
            "Job key: nightly_sleep\n"
            "Consecutive failures: 3\n"
            "Error: RuntimeError: failed\n"
            "Job: <https://illo.example.com/api/system/scheduler"
            "?job_key=nightly_sleep&run_id=42|open scheduler state>"
        ),
    }


async def test_repeated_heuristic_review_failure_emits_one_durable_alert(
    session,
    caplog,
    monkeypatch,
):
    monkeypatch.setenv("SCHEDULER_FAILURE_ALERT_THRESHOLD", "3")
    caplog.set_level(logging.ERROR, logger="brain.app.scheduler.executor")
    slack_sender = AsyncMock()
    monkeypatch.setattr(
        scheduler_executor,
        "async_deliver_scheduler_failure_alert",
        slack_sender,
    )
    job = _make_scheduler_job(
        retry_policy={"max_attempts": 5, "backoff_seconds": 0},
        default_payload={
            "name": "Nightly Sleep",
            "scheduler_split_steps": True,
            "night_budget_allowed_steps": ["heuristic_review"],
        },
    )
    session.add(job)
    await session.flush()
    run = (
        await async_materialize_due_runs(
            session,
            now=datetime(2026, 4, 21, 3, 1, tzinfo=timezone.utc),
        )
    )[0]

    failure_text = "RuntimeError: nightly heuristic review failed"

    def failing_runner(command, *, cwd=None, env=None, timeout_seconds=None):
        if "nightly scheduler wrapper initialized" in " ".join(command):
            return SimpleNamespace(
                returncode=0,
                stdout="wrapper ready",
                stderr="",
            )
        assert command == nightly_heuristic_review_command()
        return SimpleNamespace(returncode=1, stdout="", stderr=failure_text)

    alert_timestamps = []
    for minute in range(2, 6):
        failed = await async_run_scheduler_run(
            session,
            run.id,
            owner_id="tester",
            runner=failing_runner,
            now=datetime(2026, 4, 21, 3, minute, tzinfo=timezone.utc),
        )
        await session.refresh(job)
        latches = await _guard_latches(session, job)
        consecutive_latch = latches.get("consecutive")
        alert_timestamps.append(
            consecutive_latch.alerted_at if consecutive_latch else None
        )
        assert failed.status == "retryable"

    alerts = [
        record
        for record in caplog.records
        if record.getMessage().startswith(
            "Scheduler job repeated failure alert"
        )
    ]
    snapshot = await async_scheduler_health_snapshot(
        session,
        now=datetime(2026, 4, 21, 3, 6, tzinfo=timezone.utc),
    )

    assert len(alerts) == 1
    assert caplog.text.count(failure_text) == 1
    assert job.consecutive_failure_count == 4
    assert failed.result_summary["failed_step"] == "heuristic_review"
    assert alert_timestamps[:2] == [None, None]
    assert alert_timestamps[2] is not None
    assert alert_timestamps[3] == alert_timestamps[2]
    slack_sender.assert_awaited_once()
    delivery = slack_sender.await_args.kwargs
    assert delivery["job_key"] == "nightly_sleep"
    assert delivery["run_id"] == run.id
    assert delivery["error_text"] == failure_text
    delivered_guard = serialize_failure_guard(delivery["evaluation"])
    delivered_consecutive = _guard_trigger(delivered_guard, "consecutive")
    assert delivered_consecutive["count"] == 3
    assert delivered_consecutive["crossed"] is True
    assert snapshot["alerts"] == [
        {
            "type": "scheduler_job_failure_guard",
            "job_key": "nightly_sleep",
            "failure_signature": job.failure_signature,
            "last_error": failure_text,
            "triggers": [
                {
                    "kind": "consecutive",
                    "count": 4,
                    "threshold": 3,
                    "window_hours": None,
                    "alerted_at": alert_timestamps[2].isoformat(),
                    "crossed": False,
                }
            ],
        }
    ]

    succeeded = await async_run_scheduler_run(
        session,
        run.id,
        owner_id="tester",
        runner=lambda command, *, cwd=None: SimpleNamespace(
            returncode=0,
            stdout=(
                "Nightly heuristic review ran: pruned=0, skills_updated=0"
            ),
            stderr="",
        ),
        now=datetime(2026, 4, 21, 3, 6, tzinfo=timezone.utc),
    )
    await session.refresh(job)

    success_snapshot = await async_scheduler_health_snapshot(
        session,
        now=datetime(2026, 4, 21, 3, 6, tzinfo=timezone.utc),
    )
    heuristic_step = next(
        step
        for step in success_snapshot["runs"][0]["result_summary"]["steps"]
        if step["step_key"] == "heuristic_review"
    )

    assert succeeded.status == RUN_STATUS_SETTLED_SUCCESS
    assert heuristic_step["results"][0]["stdout_tail"] == (
        "Nightly heuristic review ran: pruned=0, skills_updated=0"
    )
    assert job.consecutive_failure_count == 0
    assert job.failure_signature is None
    assert await _guard_latches(session, job) == {}


async def test_consecutive_latch_rearms_when_failure_signature_changes(
    session,
    monkeypatch,
):
    monkeypatch.setenv("SCHEDULER_FAILURE_ALERT_THRESHOLD", "1")
    monkeypatch.setenv("SCHEDULER_FAILURE_RATE_THRESHOLD", "99")
    slack_sender = AsyncMock()
    monkeypatch.setattr(
        scheduler_executor,
        "async_deliver_scheduler_failure_alert",
        slack_sender,
    )
    job = _make_scheduler_job()
    session.add(job)
    await session.flush()
    base = datetime(2026, 4, 21, tzinfo=timezone.utc)

    async def fail(hour: int, error_text: str) -> SchedulerRun:
        started_at = base + timedelta(hours=hour)
        run = SchedulerRun(
            job_id=job.id,
            scheduled_for=started_at,
            window_start=started_at,
            window_end=started_at + timedelta(hours=1),
            status="settled_failure",
            idempotency_key=f"signature-change:{hour}",
            started_at=started_at,
            finished_at=started_at,
        )
        session.add(run)
        await session.flush()
        await scheduler_executor._async_apply_failure_guard(
            session,
            job,
            run,
            failure_key="health_scan",
            error_text=error_text,
            now=started_at,
        )
        return run

    first = await fail(0, "RuntimeError: first failure")
    first_alerted_at = (
        await _guard_latches(session, job)
    )["consecutive"].alerted_at
    second = await fail(1, "TimeoutError: different failure")
    second_alerted_at = (
        await _guard_latches(session, job)
    )["consecutive"].alerted_at

    assert _guard_trigger(
        first.result_summary["failure_guard"],
        "consecutive",
    )["crossed"] is True
    assert _guard_trigger(
        second.result_summary["failure_guard"],
        "consecutive",
    )["crossed"] is True
    assert second_alerted_at > first_alerted_at
    assert slack_sender.await_count == 2


async def test_scheduler_failure_rate_alert_uses_intermittent_message(monkeypatch):
    calls: dict[str, object] = {}

    class FakeSlackClient:
        async def conversations_list(self, **kwargs):
            return {"channels": [{"id": "C_ALERTS", "name": "alerts"}]}

        async def post_message(self, *, channel, text):
            calls["post"] = {"channel": channel, "text": text}
            return {"ok": True, "message": {"text": text}}

    async def fake_client_from_runtime(*, requested_by, reason):
        return FakeSlackClient()

    monkeypatch.setenv("ILLO_SCHEDULER_FAILURE_ALERT_CHANNEL", "#alerts")
    monkeypatch.setenv("ILLO_PUBLIC_URL", "https://illo.example.com")
    monkeypatch.setattr(
        scheduler_executor,
        "slack_web_client_from_runtime",
        fake_client_from_runtime,
    )

    evaluation = FailureGuardEvaluation(
        failure_signature="signature",
        last_error="TimeoutError: health scan timed out",
        edges=(
            FailureGuardEdge(
                kind=ROLLING_WINDOW_TRIGGER_KIND,
                public_details={
                    "count": 3,
                    "threshold": 3,
                    "window_hours": 24,
                },
                alerted_at=datetime(2026, 4, 21, tzinfo=timezone.utc),
                crossed=True,
                alert_title="Scheduler job intermittent failure",
                alert_summary="3 failures in the last 24h (intermittent)",
            ),
        ),
    )
    await scheduler_executor.async_deliver_scheduler_failure_alert(
        job_key="uwear_aws_health_scan",
        run_id=84,
        evaluation=evaluation,
        error_text="TimeoutError: health scan timed out\ntraceback details",
    )

    assert calls["post"] == {
        "channel": "C_ALERTS",
        "text": (
            "Scheduler job intermittent failure\n"
            "Job key: uwear_aws_health_scan\n"
            "3 failures in the last 24h (intermittent)\n"
            "Error: TimeoutError: health scan timed out\n"
            "Job: <https://illo.example.com/api/system/scheduler"
            "?job_key=uwear_aws_health_scan&run_id=84|open scheduler state>"
        ),
    }


async def test_scheduler_failure_alert_combines_simultaneous_crossings(monkeypatch):
    calls: dict[str, object] = {}

    class FakeSlackClient:
        async def post_message(self, *, channel, text):
            calls["post"] = {"channel": channel, "text": text}
            return {"ok": True, "message": {"text": text}}

    async def fake_client_from_runtime(*, requested_by, reason):
        return FakeSlackClient()

    monkeypatch.setenv("ILLO_SCHEDULER_FAILURE_ALERT_CHANNEL", "C_ALERTS")
    monkeypatch.setenv("ILLO_PUBLIC_URL", "https://illo.example.com")
    monkeypatch.setattr(
        scheduler_executor,
        "slack_web_client_from_runtime",
        fake_client_from_runtime,
    )
    alerted_at = datetime(2026, 4, 21, tzinfo=timezone.utc)
    evaluation = FailureGuardEvaluation(
        failure_signature="signature",
        last_error="RuntimeError: health scan failed",
        edges=(
            FailureGuardEdge(
                kind=CONSECUTIVE_TRIGGER_KIND,
                public_details={
                    "count": 3,
                    "threshold": 3,
                    "window_hours": None,
                },
                alerted_at=alerted_at,
                crossed=True,
                alert_title="Scheduler job repeated failure",
                alert_summary="Consecutive failures: 3",
            ),
            FailureGuardEdge(
                kind=ROLLING_WINDOW_TRIGGER_KIND,
                public_details={
                    "count": 3,
                    "threshold": 3,
                    "window_hours": 24,
                },
                alerted_at=alerted_at,
                crossed=True,
                alert_title="Scheduler job intermittent failure",
                alert_summary="3 failures in the last 24h (intermittent)",
            ),
        ),
    )

    await scheduler_executor.async_deliver_scheduler_failure_alert(
        job_key="uwear_aws_health_scan",
        run_id=84,
        evaluation=evaluation,
        error_text="RuntimeError: health scan failed",
    )

    assert calls["post"] == {
        "channel": "C_ALERTS",
        "text": (
            "Scheduler job failure guard alert\n"
            "Job key: uwear_aws_health_scan\n"
            "Triggers crossed:\n"
            "- consecutive: Consecutive failures: 3\n"
            "- rolling_window: 3 failures in the last 24h (intermittent)\n"
            "Error: RuntimeError: health scan failed\n"
            "Job: <https://illo.example.com/api/system/scheduler"
            "?job_key=uwear_aws_health_scan&run_id=84|open scheduler state>"
        ),
    }


async def test_intermittent_failures_emit_one_rate_alert_until_window_recovers(
    session,
    monkeypatch,
):
    monkeypatch.setenv("SCHEDULER_FAILURE_ALERT_THRESHOLD", "99")
    monkeypatch.setenv("SCHEDULER_FAILURE_RATE_THRESHOLD", "3")
    monkeypatch.setenv("SCHEDULER_FAILURE_RATE_WINDOW_HOURS", "24")
    slack_sender = AsyncMock()
    monkeypatch.setattr(
        scheduler_executor,
        "async_deliver_scheduler_failure_alert",
        slack_sender,
    )
    job = _make_scheduler_job()
    session.add(job)
    await session.flush()
    base = datetime(2026, 4, 21, tzinfo=timezone.utc)

    async def add_run(status: str, hour: int) -> SchedulerRun:
        started_at = base + timedelta(hours=hour)
        run = SchedulerRun(
            job_id=job.id,
            scheduled_for=started_at,
            window_start=started_at,
            window_end=started_at + timedelta(hours=1),
            status=status,
            idempotency_key=f"failure-rate:{hour}",
            started_at=started_at,
            finished_at=started_at,
        )
        session.add(run)
        await session.flush()
        if status == "settled_failure":
            await scheduler_executor._async_apply_failure_guard(
                session,
                job,
                run,
                failure_key="health_scan",
                error_text=f"RuntimeError: failed at hour {hour}",
                now=started_at,
            )
        else:
            await scheduler_executor.async_reset_scheduler_job_failure_guard(
                session,
                job,
                now=started_at,
            )
        return run

    await add_run("settled_failure", 0)
    await add_run("settled_success", 1)
    await add_run("settled_failure", 2)
    await add_run("settled_success", 3)
    threshold_run = await add_run("settled_failure", 4)

    threshold_rate = _guard_trigger(
        threshold_run.result_summary["failure_guard"],
        "rolling_window",
    )
    assert threshold_rate["count"] == 3
    assert threshold_rate["crossed"] is True
    first_rate_alerted_at = (
        await _guard_latches(session, job)
    )["rolling_window"].alerted_at
    assert first_rate_alerted_at is not None
    slack_sender.assert_awaited_once()
    first_delivery = slack_sender.await_args.kwargs
    assert first_delivery["job_key"] == "nightly_sleep"
    assert first_delivery["run_id"] == threshold_run.id
    assert first_delivery["error_text"] == "RuntimeError: failed at hour 4"
    delivered_rate = _guard_trigger(
        serialize_failure_guard(first_delivery["evaluation"]),
        "rolling_window",
    )
    assert delivered_rate["count"] == 3
    assert delivered_rate["window_hours"] == 24
    assert delivered_rate["crossed"] is True

    await add_run("settled_success", 5)
    extra_failure = await add_run("settled_failure", 6)

    extra_rate = _guard_trigger(
        extra_failure.result_summary["failure_guard"],
        "rolling_window",
    )
    assert extra_rate["count"] == 4
    assert extra_rate["crossed"] is False
    assert (
        await _guard_latches(session, job)
    )["rolling_window"].alerted_at == first_rate_alerted_at
    assert slack_sender.await_count == 1

    await add_run("settled_success", 31)
    assert "rolling_window" not in await _guard_latches(session, job)

    await add_run("settled_failure", 32)
    await add_run("settled_success", 33)
    await add_run("settled_failure", 34)
    await add_run("settled_success", 35)
    rearmed_run = await add_run("settled_failure", 36)

    rearmed_rate = _guard_trigger(
        rearmed_run.result_summary["failure_guard"],
        "rolling_window",
    )
    assert rearmed_rate["count"] == 3
    assert rearmed_rate["crossed"] is True
    assert slack_sender.await_count == 2
    last_delivery = slack_sender.await_args_list[-1].kwargs
    assert last_delivery["job_key"] == "nightly_sleep"
    assert last_delivery["run_id"] == rearmed_run.id
    assert last_delivery["error_text"] == "RuntimeError: failed at hour 36"
    assert _guard_trigger(
        serialize_failure_guard(last_delivery["evaluation"]),
        "rolling_window",
    )["crossed"] is True


async def test_consecutive_and_rate_edges_coexist_without_duplicate_delivery(
    session,
    caplog,
    monkeypatch,
):
    monkeypatch.setenv("SCHEDULER_FAILURE_ALERT_THRESHOLD", "3")
    monkeypatch.setenv("SCHEDULER_FAILURE_RATE_THRESHOLD", "3")
    monkeypatch.setenv("SCHEDULER_FAILURE_RATE_WINDOW_HOURS", "24")
    caplog.set_level(logging.ERROR, logger="brain.app.scheduler.executor")
    slack_sender = AsyncMock()
    monkeypatch.setattr(
        scheduler_executor,
        "async_deliver_scheduler_failure_alert",
        slack_sender,
    )
    job = _make_scheduler_job()
    session.add(job)
    await session.flush()
    base = datetime(2026, 4, 21, tzinfo=timezone.utc)
    threshold_run = None

    for hour in range(3):
        started_at = base + timedelta(hours=hour)
        threshold_run = SchedulerRun(
            job_id=job.id,
            scheduled_for=started_at,
            window_start=started_at,
            window_end=started_at + timedelta(hours=1),
            status="settled_failure",
            idempotency_key=f"coexisting-failure-guards:{hour}",
            started_at=started_at,
            finished_at=started_at,
        )
        session.add(threshold_run)
        await session.flush()
        await scheduler_executor._async_apply_failure_guard(
            session,
            job,
            threshold_run,
            failure_key="health_scan",
            error_text="RuntimeError: health scan failed",
            now=started_at,
        )

    assert threshold_run is not None
    guard = threshold_run.result_summary["failure_guard"]
    assert _guard_trigger(guard, "consecutive")["crossed"] is True
    assert _guard_trigger(guard, "rolling_window")["crossed"] is True
    assert set(await _guard_latches(session, job)) == {
        "consecutive",
        "rolling_window",
    }
    assert "Scheduler job combined failure guard alert" in caplog.text

    slack_sender.assert_awaited_once()
    delivery = slack_sender.await_args.kwargs
    assert delivery["job_key"] == "nightly_sleep"
    assert delivery["run_id"] == threshold_run.id
    assert delivery["error_text"] == "RuntimeError: health scan failed"
    delivered_guard = serialize_failure_guard(delivery["evaluation"])
    assert [
        trigger["kind"]
        for trigger in delivered_guard["triggers"]
        if trigger["crossed"]
    ] == ["consecutive", "rolling_window"]

    snapshot = await async_scheduler_health_snapshot(
        session,
        now=base + timedelta(hours=2),
    )
    catalog_guard = snapshot["jobs"][0]["failure_guard"]
    assert [trigger["kind"] for trigger in catalog_guard["triggers"]] == [
        "consecutive",
        "rolling_window",
    ]
    assert all(
        trigger["crossed"] is False
        for trigger in catalog_guard["triggers"]
    )
    assert [
        trigger["kind"] for trigger in snapshot["alerts"][0]["triggers"]
    ] == ["consecutive", "rolling_window"]


_RUNTIME_DURATION_TRIGGER_KIND = FailureGuardTriggerKind("runtime_duration")


@dataclass(frozen=True)
class _RuntimeDurationTrigger:
    """A differently shaped proof trigger with no count/window contract."""

    minimum_runtime_minutes: int
    kind: FailureGuardTriggerKind = field(
        default=_RUNTIME_DURATION_TRIGGER_KIND,
        init=False,
    )

    async def evaluate(
        self,
        session,
        job: SchedulerJob,
        now: datetime,
        *,
        observation=None,
    ) -> FailureGuardTriggerResult:
        del session, observation
        assert job.last_started_at is not None
        started_at = job.last_started_at
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=timezone.utc)
        elapsed_minutes = int((now - started_at).total_seconds() // 60)
        return FailureGuardTriggerResult(
            kind=self.kind,
            active=elapsed_minutes >= self.minimum_runtime_minutes,
            public_details={
                "elapsed_minutes": elapsed_minutes,
                "minimum_runtime_minutes": self.minimum_runtime_minutes,
                "measurement": "job_runtime",
            },
            alert_title="Scheduler job exceeded expected runtime",
            alert_summary=(
                f"Runtime reached {elapsed_minutes} minutes "
                f"(limit {self.minimum_runtime_minutes})"
            ),
        )

    async def should_reset(
        self,
        session,
        job: SchedulerJob,
        now: datetime,
        *,
        event: SchedulerFailureGuardResetEvent,
    ) -> bool:
        del session, job, now
        return event == "success"


async def test_third_trigger_flows_through_production_apply_health_delivery_and_reset(
    session,
    monkeypatch,
):
    monkeypatch.setenv("SCHEDULER_FAILURE_ALERT_THRESHOLD", "99")
    monkeypatch.setenv("SCHEDULER_FAILURE_RATE_THRESHOLD", "99")
    monkeypatch.setenv("ILLO_SCHEDULER_FAILURE_ALERT_CHANNEL", "C_ALERTS")
    monkeypatch.setenv("ILLO_PUBLIC_URL", "https://illo.example.com")
    base = datetime(2026, 4, 21, 4, 0, tzinfo=timezone.utc)
    job = _make_scheduler_job(
        last_started_at=base - timedelta(minutes=47),
        next_run_at=base + timedelta(hours=1),
    )
    session.add(job)
    await session.flush()
    run = SchedulerRun(
        job_id=job.id,
        scheduled_for=base,
        window_start=base,
        window_end=base + timedelta(hours=1),
        status="settled_failure",
        idempotency_key="runtime-duration-proof",
        started_at=base,
        finished_at=base,
    )
    session.add(run)
    await session.flush()

    monkeypatch.setattr(
        scheduler_failure_guard,
        "_FAILURE_GUARD_TRIGGER_PROVIDERS",
        (
            *scheduler_failure_guard._FAILURE_GUARD_TRIGGER_PROVIDERS,
            lambda: _RuntimeDurationTrigger(minimum_runtime_minutes=30),
        ),
    )
    assert [
        str(trigger.kind)
        for trigger in scheduler_failure_guard.scheduler_failure_guard_registry().triggers
    ] == ["consecutive", "rolling_window", "runtime_duration"]

    calls: dict[str, str] = {}

    class FakeSlackClient:
        async def post_message(self, *, channel, text):
            calls["channel"] = channel
            calls["text"] = text
            return {"ok": True, "message": {"text": text}}

    async def fake_client_from_runtime(*, requested_by, reason):
        return FakeSlackClient()

    monkeypatch.setattr(
        scheduler_executor,
        "slack_web_client_from_runtime",
        fake_client_from_runtime,
    )
    await scheduler_executor._async_apply_failure_guard(
        session,
        job,
        run,
        failure_key="runtime_duration",
        error_text="TimeoutError: worker still running",
        now=base,
    )
    serialized = run.result_summary["failure_guard"]
    duration = _guard_trigger(serialized, "runtime_duration")

    assert duration == {
        "kind": "runtime_duration",
        "elapsed_minutes": 47,
        "minimum_runtime_minutes": 30,
        "measurement": "job_runtime",
        "alerted_at": base.isoformat(),
        "crossed": True,
    }
    assert calls["channel"] == "C_ALERTS"
    assert "Scheduler job exceeded expected runtime" in calls["text"]
    assert "Runtime reached 47 minutes (limit 30)" in calls["text"]

    health = await async_scheduler_health_snapshot(
        session,
        now=base,
    )
    catalog_duration = _guard_trigger(
        health["jobs"][0]["failure_guard"],
        "runtime_duration",
    )
    assert catalog_duration == {
        **duration,
        "alerted_at": base.replace(tzinfo=None).isoformat(),
        "crossed": False,
    }
    assert health["alerts"][0]["triggers"] == [catalog_duration]

    await scheduler_executor.async_reset_scheduler_job_failure_guard(
        session,
        job,
        now=base + timedelta(minutes=1),
    )
    assert "runtime_duration" not in await _guard_latches(session, job)
    reset_health = await async_scheduler_health_snapshot(
        session,
        now=base + timedelta(minutes=1),
    )
    assert reset_health["alerts"] == []
    assert _guard_trigger(
        reset_health["jobs"][0]["failure_guard"],
        "runtime_duration",
    ) == {
        **duration,
        "elapsed_minutes": 48,
        "alerted_at": None,
        "crossed": False,
    }
