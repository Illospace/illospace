from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from sqlalchemy import select

from brain.app.scheduler.stale_run_reaper import (
    AGENT_RUN_DEADLINE_OVERDUE_ALERT_KEY_PREFIX,
    OverdueAgentRun,
    StaleRunReaper,
    async_overdue_agent_runs,
)
from brain.platform.db.models.agent_run import (
    AgentRunArtifactRow,
    AgentRunEventRow,
    AgentRunRow,
)
from brain.systems.runs.deadlines import DeadlineSweepResult
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

    assert sleeps == [60.0, 60.0]
    assert checks == 2


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
