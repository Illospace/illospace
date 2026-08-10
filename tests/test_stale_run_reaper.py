from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
from unittest.mock import patch

import pytest
from sqlalchemy import select

from brain.app.scheduler.stale_run_reaper import (
    AGENT_RUN_DEADLINE_OVERDUE_ALERT_KEY_PREFIX,
    OverdueAgentRun,
    StaleRunReaper,
    agent_run_maintenance_snapshot,
    async_overdue_agent_runs,
)
from brain.app.scheduler.overdue_alert_state import try_claim_scheduler_alert
from brain.platform.db.models.agent_run import (
    AgentRunArtifactRow,
    AgentRunEventRow,
    AgentRunRow,
)
from brain.platform.db.models.scheduler import SchedulerAlertLatch
from brain.systems.runs.deadlines import DeadlineSweepResult, sweep_agent_run_deadlines
from brain.systems.runs.domain import AgentRunRequest
from brain.systems.runs.status import RunStatus
from brain.systems.runs.store import AsyncAgentRunStore


NOW = datetime(2026, 8, 7, 12, 36, tzinfo=timezone.utc)


class _FakeUnitOfWork:
    session = object()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc_info):
        return False


class _SessionUnitOfWork:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, _exc, _tb):
        if exc_type is None:
            await self.session.flush()
        return False


@pytest.fixture(autouse=True)
def fake_reaper_unit_of_work(monkeypatch):
    monkeypatch.setattr(
        "brain.platform.db.repositories.unit_of_work.UnitOfWork",
        _FakeUnitOfWork,
    )


@pytest.fixture
async def agent_run_session(
    async_sqlite_session_factory,
    sqlite_postgres_ddl_patch,
):
    del sqlite_postgres_ddl_patch
    return await async_sqlite_session_factory(
        [
            AgentRunRow.__table__,
            AgentRunEventRow.__table__,
            AgentRunArtifactRow.__table__,
            SchedulerAlertLatch.__table__,
        ]
    )


async def _no_candidates(_session, **_kwargs):
    return ()


async def _no_deadlines(_session, **_kwargs):
    return DeadlineSweepResult()


async def _no_reap(**_kwargs):
    return 0


async def _claim_alert(**_kwargs):
    return True


async def _release_alert(**_kwargs):
    return None


def _reaper(**overrides) -> StaleRunReaper:
    dependencies = {
        "candidate_provider": _no_candidates,
        "deadline_sweep": _no_deadlines,
        "reap_runs": _no_reap,
        "claim_alert": _claim_alert,
        "release_alert": _release_alert,
    }
    dependencies.update(overrides)
    return StaleRunReaper(**dependencies)


async def _create_15926_shaped_child(
    session,
    *,
    closeout_expires_at: datetime | None = None,
    deadline_at: datetime | None = None,
    last_activity_at: datetime | None = None,
    last_event_at: datetime | None = None,
):
    deadline_at = deadline_at or NOW - timedelta(hours=2)
    last_activity_at = last_activity_at or NOW - timedelta(minutes=10)
    last_event_at = last_event_at or last_activity_at
    store = AsyncAgentRunStore(session)
    root = await store.create_run(
        AgentRunRequest(
            org_id="org-1",
            thread_id="root-15925",
            message="completed root",
        )
    )
    await store.set_status(root.id, RunStatus.STARTING)
    await store.set_status(root.id, RunStatus.RUNNING)
    await store.set_status(root.id, RunStatus.COMPLETED)
    child = await store.create_run(
        AgentRunRequest(
            org_id="org-1",
            thread_id="child-15926",
            message="stale overdue child",
            parent_run_id=root.id,
            root_run_id=root.id,
            deadline_at=deadline_at,
        ),
        parent_step_key_hash="15926-shape",
    )
    await store.set_status(child.id, RunStatus.STARTING)
    await store.set_status(child.id, RunStatus.RUNNING)
    row = await session.get(AgentRunRow, child.id)
    assert row is not None
    row.created_at = last_activity_at
    row.started_at = last_activity_at
    row.updated_at = last_activity_at
    row.deadline_at = deadline_at
    row.closeout_expires_at = closeout_expires_at
    row.execution_token = "frozen-worker"
    row.metadata_ = {
        "runner_heartbeat": {
            "at": last_activity_at.isoformat(),
            "reason": "runner_running",
        }
    }
    events = (
        await session.scalars(
            select(AgentRunEventRow).where(AgentRunEventRow.run_id == child.id)
        )
    ).all()
    for event in events:
        event.created_at = last_event_at
    await session.commit()
    return root, child, row


def test_reaper_checks_well_inside_stale_run_window():
    assert StaleRunReaper.check_interval_seconds <= 5 * 60
    assert StaleRunReaper.reap_limit == 25
    assert StaleRunReaper.deadline_sweep_limit == 25


async def test_reaper_keeps_its_cadence_after_a_failed_tick(monkeypatch):
    sleeps = []
    checks = 0
    reaper = _reaper()

    async def sleep(seconds):
        sleeps.append(seconds)

    async def run_once():
        nonlocal checks
        checks += 1
        if checks == 1:
            raise RuntimeError("first tick failed")
        raise asyncio.CancelledError

    monkeypatch.setattr(asyncio, "sleep", sleep)
    monkeypatch.setattr(reaper, "_run_once", run_once)

    with pytest.raises(asyncio.CancelledError):
        await reaper.run()

    assert len(sleeps) == 1
    assert sleeps[0] == pytest.approx(60.0, abs=0.1)
    assert checks == 2


async def test_quiet_interval_emits_zero_work_liveness(capsys):
    result = await _reaper()._run_once(now=NOW)

    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    snapshot = agent_run_maintenance_snapshot(now=NOW + timedelta(seconds=10))
    assert result.reaped == 0
    assert payload == {
        "event": "agent_run_stale_reap",
        "host": "api",
        "ok": True,
        "checked_at": NOW.isoformat(),
        "reaped": 0,
        "expired": 0,
        "closeout_requested": 0,
        "overdue_run_ids": [],
        "alert_sent": False,
        "errors": [],
    }
    assert snapshot["state"] == "good"
    assert snapshot["interval_age_seconds"] == 10
    assert snapshot["reaped"] == 0
    assert snapshot["expired"] == 0
    assert snapshot["closeout_requested"] == 0
    assert snapshot["overdue_run_ids"] == []


async def test_blocked_owner_times_out_without_stopping_later_maintenance(capsys):
    calls = []

    async def blocked_deadline_sweep(_session, **_kwargs):
        calls.append("deadlines")
        await asyncio.Event().wait()

    async def reap_runs(**_kwargs):
        calls.append("reap")
        return 1

    reaper = _reaper(
        deadline_sweep=blocked_deadline_sweep,
        reap_runs=reap_runs,
    )
    reaper.operation_timeout_seconds = 0.01

    result = await reaper._run_once(now=NOW)

    payloads = [
        json.loads(line) for line in capsys.readouterr().out.strip().splitlines()
    ]
    assert calls == ["deadlines", "reap"]
    assert result.reaped == 1
    assert result.errors == ("deadline_sweep=TimeoutError",)
    assert payloads[0]["event"] == "agent_run_deadline_sweep_failed"
    assert payloads[0]["error"] == "TimeoutError"
    assert payloads[-1]["event"] == "agent_run_stale_reap"
    assert payloads[-1]["reaped"] == 1
    assert payloads[-1]["ok"] is False


async def test_one_maintenance_failure_does_not_block_the_other_owners():
    calls = []

    async def candidates(_session, **_kwargs):
        calls.append("candidates")
        raise RuntimeError("candidate query failed")

    async def deadline_sweep(_session, **_kwargs):
        calls.append("deadlines")
        raise RuntimeError("deadline sweep failed")

    async def reap_runs(**_kwargs):
        calls.append("reap")
        return 2

    result = await _reaper(
        candidate_provider=candidates,
        deadline_sweep=deadline_sweep,
        reap_runs=reap_runs,
    )._run_once(now=NOW)

    assert calls == ["candidates", "deadlines", "reap"]
    assert result.reaped == 2
    assert result.closeout_requested == 0
    assert result.expired == 0


async def test_api_reaper_requeues_stale_run_without_scheduler_daemon(
    agent_run_session,
    monkeypatch,
):
    from brain.systems.runs.cortex import runner

    store = AsyncAgentRunStore(agent_run_session)
    run = await store.create_run(
        AgentRunRequest(
            org_id="org-1",
            thread_id="idea-1",
            message="API-hosted recovery",
        )
    )
    await store.set_status(run.id, RunStatus.STARTING)
    await store.set_status(run.id, RunStatus.RUNNING)
    old = NOW - timedelta(minutes=10)
    row = await agent_run_session.get(AgentRunRow, run.id)
    assert row is not None
    row.created_at = old
    row.started_at = old
    row.updated_at = old
    row.deadline_at = NOW + timedelta(hours=1)
    row.execution_token = "frozen-worker"
    row.metadata_ = {
        "runner_heartbeat": {"at": old.isoformat(), "reason": "runner_running"}
    }
    events = (
        await agent_run_session.scalars(
            select(AgentRunEventRow).where(AgentRunEventRow.run_id == run.id)
        )
    ).all()
    for event in events:
        event.created_at = old
    await agent_run_session.commit()

    monkeypatch.setattr(
        runner,
        "UnitOfWork",
        lambda: _SessionUnitOfWork(agent_run_session),
    )
    with (
        patch("brain.systems.runs.cortex.runner.publish_safe"),
        patch(
            "brain.systems.runs.cortex.runner._settle_idea_for_terminal_root_run_async",
            return_value=None,
        ),
        patch(
            "brain.systems.runs.cortex.runner.notify_run_interruption",
            return_value=None,
        ),
    ):
        result = await _reaper(reap_runs=runner.reap_stale_active_runs)._run_once(
            now=NOW
        )

    await agent_run_session.refresh(row)
    assert result.reaped == 1
    assert row.status == RunStatus.QUEUED.value
    assert row.execution_token is None


async def test_15926_shape_is_selected_by_alert_deadline_and_stale_reap_paths(
    agent_run_session,
    monkeypatch,
):
    from brain.systems.runs.cortex import runner

    root, child, row = await _create_15926_shaped_child(
        agent_run_session,
        closeout_expires_at=NOW - timedelta(hours=36),
        deadline_at=NOW - timedelta(hours=36, minutes=2),
        last_activity_at=NOW - timedelta(hours=36),
        last_event_at=NOW - timedelta(hours=36, minutes=2),
    )

    candidates = await async_overdue_agent_runs(
        agent_run_session,
        now=NOW,
        overdue_after=timedelta(hours=1),
        limit=25,
    )
    assert [candidate.run_id for candidate in candidates] == [child.id]

    with patch(
        "brain.systems.runs.chantier_continuation.queue_chantier_continuation_for_terminal_run",
        return_value=None,
    ):
        deadline_result = await sweep_agent_run_deadlines(
            agent_run_session,
            now=NOW,
            limit=25,
        )
    assert deadline_result.expired_run_ids == (child.id,)

    await agent_run_session.refresh(row)
    old = NOW - timedelta(minutes=10)
    row.status = RunStatus.RUNNING.value
    row.expired_at = None
    row.completed_at = None
    row.updated_at = old
    row.execution_token = "frozen-worker"
    events = (
        await agent_run_session.scalars(
            select(AgentRunEventRow).where(AgentRunEventRow.run_id == child.id)
        )
    ).all()
    for event in events:
        event.created_at = old
    await agent_run_session.commit()

    monkeypatch.setattr(
        runner,
        "UnitOfWork",
        lambda: _SessionUnitOfWork(agent_run_session),
    )
    with (
        patch("brain.systems.runs.cortex.runner.publish_safe"),
        patch(
            "brain.systems.runs.cortex.runner._settle_idea_for_terminal_root_run_async",
            return_value=None,
        ),
        patch(
            "brain.systems.runs.cortex.runner.notify_run_interruption",
            return_value=None,
        ),
    ):
        reaped = await runner.reap_stale_active_runs(now=NOW, limit=25)

    await agent_run_session.refresh(row)
    root_row = await agent_run_session.get(AgentRunRow, root.id)
    assert reaped == 1
    assert row.status == RunStatus.QUEUED.value
    assert root_row is not None
    assert root_row.status == RunStatus.COMPLETED.value


async def test_overdue_stale_run_expires_within_two_api_intervals(
    agent_run_session,
    monkeypatch,
):
    from brain.systems.runs.cortex import runner

    _root, child, row = await _create_15926_shaped_child(agent_run_session)

    def unit_of_work():
        return _SessionUnitOfWork(agent_run_session)

    monkeypatch.setattr(
        "brain.platform.db.repositories.unit_of_work.UnitOfWork",
        unit_of_work,
    )
    monkeypatch.setattr(runner, "UnitOfWork", unit_of_work)

    async def do_not_claim_alert(**_kwargs):
        return False

    async def no_settlement(*_args, **_kwargs):
        return None

    reaper = StaleRunReaper(
        claim_alert=do_not_claim_alert,
        settle_run=no_settlement,
        finalize_run=no_settlement,
    )
    with (
        patch(
            "brain.systems.runs.chantier_continuation.queue_chantier_continuation_for_terminal_run",
            return_value=None,
        ),
        patch("brain.systems.runs.cortex.runner.publish_safe"),
        patch(
            "brain.systems.runs.cortex.runner.notify_run_interruption",
            return_value=None,
        ),
    ):
        first = await reaper._run_once(now=NOW)
        second = await reaper._run_once(now=NOW + timedelta(minutes=2))

    await agent_run_session.refresh(row)
    assert first.closeout_requested == 1
    assert second.expired == 1
    assert row.status == RunStatus.EXPIRED.value
    assert row.expired_at is not None
    assert row.expired_at.replace(tzinfo=timezone.utc) == NOW + timedelta(minutes=2)


async def test_scheduler_daemon_ticks_do_not_mutate_stale_overdue_agent_runs(
    agent_run_session,
    monkeypatch,
):
    import brain.app.scheduler.daemon as scheduler_daemon

    store = AsyncAgentRunStore(agent_run_session)
    run = await store.create_run(
        AgentRunRequest(
            org_id="org-1",
            thread_id="daemon-boundary",
            message="must be maintained by the API process",
            deadline_at=NOW - timedelta(hours=2),
        )
    )
    await store.set_status(run.id, RunStatus.STARTING)
    await store.set_status(run.id, RunStatus.RUNNING)
    row = await agent_run_session.get(AgentRunRow, run.id)
    assert row is not None
    row.started_at = NOW - timedelta(hours=9)
    row.updated_at = NOW - timedelta(hours=2)
    row.execution_token = "frozen-agent-worker"
    await agent_run_session.commit()

    async def record_tick(_session, **_kwargs):
        return None

    async def reclaim(_session, **_kwargs):
        return []

    async def drain(_session, **_kwargs):
        return {"ok": True, "executed": 0, "results": []}

    monkeypatch.setattr(
        scheduler_daemon,
        "record_scheduler_liveness_checkpoint",
        record_tick,
    )
    monkeypatch.setattr(scheduler_daemon, "async_reclaim_expired_leases", reclaim)
    monkeypatch.setattr(scheduler_daemon, "async_drain_scheduler", drain)

    before = (
        row.status,
        row.execution_token,
        row.deadline_at,
        row.closeout_expires_at,
        row.expired_at,
        dict(row.metadata_),
    )
    for minute in range(2):
        await scheduler_daemon.async_scheduler_daemon_tick(
            agent_run_session,
            now=NOW + timedelta(minutes=minute),
        )
    await agent_run_session.refresh(row)

    assert (
        row.status,
        row.execution_token,
        row.deadline_at,
        row.closeout_expires_at,
        row.expired_at,
        dict(row.metadata_),
    ) == before


async def test_overdue_candidate_names_run_age_and_origin(agent_run_session):
    store = AsyncAgentRunStore(agent_run_session)
    run = await store.create_run(
        AgentRunRequest(
            org_id="org-1",
            thread_id="alerts",
            message="stuck investigation",
            metadata={"launch_envelope": {"origin": "slack.channel_message"}},
            deadline_at=NOW - timedelta(hours=2),
        )
    )
    await store.set_status(run.id, RunStatus.STARTING)
    await store.set_status(run.id, RunStatus.RUNNING)
    row = await agent_run_session.get(AgentRunRow, run.id)
    assert row is not None
    row.started_at = NOW - timedelta(hours=9)
    await agent_run_session.commit()

    candidates = await async_overdue_agent_runs(
        agent_run_session,
        now=NOW,
        overdue_after=timedelta(hours=1),
        limit=25,
    )

    assert candidates == (
        OverdueAgentRun(
            run_id=run.id,
            started_at=NOW - timedelta(hours=9),
            deadline_at=NOW - timedelta(hours=2),
            origin="slack.channel_message",
        ),
    )


async def test_run_more_than_one_hour_past_deadline_posts_named_alert(monkeypatch):
    monkeypatch.delenv("ILLO_SCHEDULER_FAILURE_ALERT_CHANNEL", raising=False)
    deliveries = []
    candidate = OverdueAgentRun(
        run_id=15266,
        started_at=NOW - timedelta(hours=9),
        deadline_at=NOW - timedelta(hours=2),
        origin="slack.channel_message",
    )

    async def candidates(_session, **_kwargs):
        return (candidate,)

    async def deliver_alert(**kwargs):
        deliveries.append(kwargs)

    result = await _reaper(
        candidate_provider=candidates,
        deliver_alert=deliver_alert,
    )._run_once(now=NOW)

    assert result.alert_sent is True
    assert result.overdue_run_ids == (15266,)
    assert len(deliveries) == 1
    alert = deliveries[0]
    assert alert["policy"].channel == "#alerts"
    assert alert["policy"].requested_by == "stale_run_reaper"
    assert alert["subject"].identity == "15266"
    assert "Run 15266" in alert["presentation"].summary
    assert "age 9h 0m" in alert["presentation"].summary
    assert "origin: slack.channel_message" in alert["presentation"].summary


async def test_two_api_replicas_share_one_durable_alert_claim():
    deliveries = []
    claimed = False
    candidate = OverdueAgentRun(
        run_id=15266,
        started_at=NOW - timedelta(hours=9),
        deadline_at=NOW - timedelta(hours=2),
        origin="slack.channel_message",
    )

    async def candidates(_session, **_kwargs):
        return (candidate,)

    async def claim_alert(*, run_id, alerted_at):
        nonlocal claimed
        assert run_id == 15266
        assert alerted_at == NOW
        if claimed:
            return False
        claimed = True
        return True

    async def deliver_alert(**kwargs):
        deliveries.append(kwargs)

    replicas = tuple(
        _reaper(
            candidate_provider=candidates,
            claim_alert=claim_alert,
            deliver_alert=deliver_alert,
        )
        for _ in range(2)
    )

    results = await asyncio.gather(
        *(replica._run_once(now=NOW) for replica in replicas)
    )

    assert [result.alert_sent for result in results].count(True) == 1
    assert len(deliveries) == 1
    assert AGENT_RUN_DEADLINE_OVERDUE_ALERT_KEY_PREFIX != "scheduler_overdue_freeze"


async def test_overdue_run_alert_latch_delivers_exactly_once(
    agent_run_session,
    monkeypatch,
):
    _root, child, _row = await _create_15926_shaped_child(agent_run_session)
    monkeypatch.setattr(
        "brain.platform.db.repositories.unit_of_work.UnitOfWork",
        lambda: _SessionUnitOfWork(agent_run_session),
    )
    deliveries = []

    async def claim_alert(*, run_id, alerted_at):
        return await try_claim_scheduler_alert(
            agent_run_session,
            alert_key=f"{AGENT_RUN_DEADLINE_OVERDUE_ALERT_KEY_PREFIX}:{run_id}",
            alerted_at=alerted_at,
        )

    async def deliver_alert(**kwargs):
        deliveries.append(kwargs)

    reaper = _reaper(
        candidate_provider=async_overdue_agent_runs,
        claim_alert=claim_alert,
        deliver_alert=deliver_alert,
    )
    first = await reaper._run_once(now=NOW)
    second = await reaper._run_once(now=NOW + timedelta(minutes=1))

    latches = (
        await agent_run_session.scalars(select(SchedulerAlertLatch))
    ).all()
    assert first.alert_sent is True
    assert second.alert_sent is False
    assert len(deliveries) == 1
    assert len(latches) == 1
    assert latches[0].alert_key == (
        f"{AGENT_RUN_DEADLINE_OVERDUE_ALERT_KEY_PREFIX}:{child.id}"
    )


async def test_api_reaper_enforces_deadlines_and_settles_expired_roots():
    calls = []

    async def candidates(_session, *, now, overdue_after, limit):
        calls.append(("candidates", now, overdue_after, limit))
        return ()

    async def deadline_sweep(_session, *, now, limit):
        calls.append(("sweep", now, limit))
        return DeadlineSweepResult(
            closeout_requested=1,
            expired=1,
            expired_run_ids=(15266,),
        )

    async def settle_run(_session, run_id):
        calls.append(("settle", run_id))

    async def finalize_run(run_id, *, status, error):
        calls.append(("finalize", run_id, status, error))

    async def reap_runs(*, now, limit):
        calls.append(("reap", now, limit))
        return 0

    result = await _reaper(
        candidate_provider=candidates,
        deadline_sweep=deadline_sweep,
        settle_run=settle_run,
        finalize_run=finalize_run,
        reap_runs=reap_runs,
    )._run_once(now=NOW)

    assert result.closeout_requested == 1
    assert result.expired == 1
    assert calls == [
        ("candidates", NOW, timedelta(hours=1), 25),
        ("sweep", NOW, 25),
        ("settle", 15266),
        ("finalize", 15266, "expired", "Agent run deadline elapsed"),
        ("reap", NOW, 25),
    ]
