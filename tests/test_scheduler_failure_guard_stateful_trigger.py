"""Production integration coverage for scheduler stateful triggers."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import pytest

import brain.app.scheduler.executor as scheduler_executor
import brain.app.scheduler.scheduler_failure_guard as scheduler_failure_guard
from brain.app.scheduler.daemon import async_scheduler_health_snapshot
from brain.app.scheduler.scheduler_failure_guard import (
    SchedulerFailureGuardResetEvent,
)
from brain.platform.db.models.scheduler import SchedulerRun
from brain.systems.failure_guard.core import (
    FailureGuardStatefulTriggerRegistration,
    FailureGuardTriggerKind,
    FailureGuardTriggerResult,
)
from tests.scheduler_test_support import (
    guard_latches,
    guard_trigger,
    guard_trigger_states,
    make_scheduler_job,
    make_scheduler_test_session,
)


pytestmark = pytest.mark.asyncio

_DISTINCT_FAILURE_CLASSES_TRIGGER_KIND = FailureGuardTriggerKind(
    "distinct_failure_classes"
)


@pytest.fixture
async def session(async_sqlite_session_factory):
    return await make_scheduler_test_session(async_sqlite_session_factory)


@dataclass(frozen=True)
class _DistinctFailureClassesTrigger:
    """Stateful proof trigger that remembers distinct failure classifications."""

    threshold: int
    kind: FailureGuardTriggerKind = field(
        default=_DISTINCT_FAILURE_CLASSES_TRIGGER_KIND,
        init=False,
    )

    async def transition_state(self, context, state, *, event):
        if event == "success":
            return None
        assert context.record is not None
        failure_class = context.record.signature_input.splitlines()[0]
        failure_classes = list(state.get("failure_classes", []))
        if failure_class not in failure_classes:
            failure_classes.append(failure_class)
        return {"failure_classes": failure_classes}

    async def evaluate_with_state(
        self,
        context,
        *,
        state,
    ) -> FailureGuardTriggerResult:
        del context
        failure_classes = list(state.get("failure_classes", []))
        count = len(failure_classes)
        return FailureGuardTriggerResult(
            kind=self.kind,
            active=count >= self.threshold,
            public_details={
                "distinct_count": count,
                "failure_classes": failure_classes,
                "threshold": self.threshold,
            },
            alert_title="Scheduler job crossed distinct failure classes",
            alert_summary=(
                f"Distinct failure classes: {count} "
                f"(threshold {self.threshold})"
            ),
        )

    async def should_reset(
        self,
        context,
        *,
        event: SchedulerFailureGuardResetEvent,
    ) -> bool:
        del context
        return event == "success"


async def test_stateful_third_trigger_flows_through_production_registry(
    session,
    monkeypatch,
):
    monkeypatch.setenv("SCHEDULER_FAILURE_ALERT_THRESHOLD", "99")
    monkeypatch.setenv("SCHEDULER_FAILURE_RATE_THRESHOLD", "99")
    monkeypatch.setenv("SCHEDULER_STANDING_FAILURE_ALERT_THRESHOLD", "99")
    monkeypatch.setenv("ILLO_SCHEDULER_FAILURE_ALERT_CHANNEL", "C_ALERTS")
    monkeypatch.setenv("ILLO_PUBLIC_URL", "https://illo.example.com")
    base = datetime(2026, 4, 21, 4, 0, tzinfo=timezone.utc)
    job = make_scheduler_job(next_run_at=base + timedelta(hours=1))
    session.add(job)
    await session.flush()

    monkeypatch.setattr(
        scheduler_failure_guard,
        "_FAILURE_GUARD_TRIGGER_PROVIDERS",
        (
            *scheduler_failure_guard._FAILURE_GUARD_TRIGGER_PROVIDERS,
            lambda: FailureGuardStatefulTriggerRegistration(
                trigger=_DistinctFailureClassesTrigger(threshold=2),
            ),
        ),
    )
    assert [
        str(trigger.kind)
        for trigger in (
            scheduler_failure_guard.scheduler_failure_guard_registry().triggers
        )
    ] == [
        "consecutive",
        "standing_failure",
        "rolling_window",
        "configuration",
        "distinct_failure_classes",
    ]

    deliveries: list[dict[str, str]] = []

    class FakeSlackClient:
        async def post_message(self, *, channel, text):
            deliveries.append({"channel": channel, "text": text})
            return {"ok": True, "message": {"text": text}}

    async def fake_client_from_runtime(*, requested_by, reason):
        return FakeSlackClient()

    monkeypatch.setattr(
        scheduler_executor,
        "slack_web_client_from_runtime",
        fake_client_from_runtime,
    )

    async def apply_failure(
        *,
        offset: int,
        failure_key: str,
        error_text: str,
    ) -> SchedulerRun:
        observed_at = base + timedelta(minutes=offset)
        run = SchedulerRun(
            job_id=job.id,
            scheduled_for=observed_at,
            window_start=observed_at,
            window_end=observed_at + timedelta(hours=1),
            status="settled_failure",
            idempotency_key=f"stateful-trigger:{offset}",
            started_at=observed_at,
            finished_at=observed_at,
        )
        session.add(run)
        await session.flush()
        await scheduler_executor._async_apply_failure_guard(
            session,
            job,
            run,
            failure_key=failure_key,
            error_text=error_text,
            now=observed_at,
        )
        return run

    first_run = await apply_failure(
        offset=0,
        failure_key="extract",
        error_text="ValueError: malformed response",
    )
    first_trigger = guard_trigger(
        first_run.result_summary["failure_guard"],
        "distinct_failure_classes",
    )
    assert first_trigger["distinct_count"] == 1
    assert first_trigger["crossed"] is False
    assert deliveries == []
    assert (
        await guard_trigger_states(session, job)
    )["distinct_failure_classes"].trigger_state == {
        "failure_classes": ["extract"]
    }

    second_run = await apply_failure(
        offset=1,
        failure_key="publish",
        error_text="TimeoutError: downstream unavailable",
    )
    second_trigger = guard_trigger(
        second_run.result_summary["failure_guard"],
        "distinct_failure_classes",
    )
    assert second_trigger == {
        "kind": "distinct_failure_classes",
        "distinct_count": 2,
        "failure_classes": ["extract", "publish"],
        "threshold": 2,
        "alerted_at": (base + timedelta(minutes=1)).isoformat(),
        "crossed": True,
    }
    assert deliveries[0]["channel"] == "C_ALERTS"
    assert "Scheduler job crossed distinct failure classes" in deliveries[0]["text"]

    health = await async_scheduler_health_snapshot(
        session,
        now=base + timedelta(minutes=1),
    )
    catalog_trigger = guard_trigger(
        health["jobs"][0]["failure_guard"],
        "distinct_failure_classes",
    )
    assert catalog_trigger == {
        **second_trigger,
        "alerted_at": (
            base + timedelta(minutes=1)
        ).replace(tzinfo=None).isoformat(),
        "crossed": False,
    }
    assert health["alerts"][0]["triggers"] == [catalog_trigger]

    await scheduler_executor.async_reset_scheduler_job_failure_guard(
        session,
        job,
        now=base + timedelta(minutes=2),
    )
    assert "distinct_failure_classes" not in await guard_latches(session, job)
    assert (
        "distinct_failure_classes"
        not in await guard_trigger_states(session, job)
    )
    reset_health = await async_scheduler_health_snapshot(
        session,
        now=base + timedelta(minutes=2),
    )
    assert reset_health["alerts"] == []
    assert guard_trigger(
        reset_health["jobs"][0]["failure_guard"],
        "distinct_failure_classes",
    ) == {
        **second_trigger,
        "distinct_count": 0,
        "failure_classes": [],
        "alerted_at": None,
        "crossed": False,
    }
