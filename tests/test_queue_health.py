from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from brain.platform.db.models.agent_run import AgentRunRow


@pytest.fixture
async def queue_health_session(
    async_sqlite_session_factory,
    sqlite_postgres_ddl_patch,
):
    return await async_sqlite_session_factory([AgentRunRow.__table__])


class _SessionUnitOfWork:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc_info):
        await self.session.flush()
        return False


def _run(
    *,
    thread_id: str,
    status: str,
    created_at: datetime,
    updated_at: datetime,
    started_at: datetime | None = None,
    execution_attempt: int = 0,
) -> AgentRunRow:
    return AgentRunRow(
        thread_id=thread_id,
        profile="fast",
        recipe="fast",
        status=status,
        input_message=thread_id,
        created_at=created_at,
        updated_at=updated_at,
        started_at=started_at,
        execution_attempt=execution_attempt,
    )


async def test_requeued_run_age_starts_at_requeue(
    queue_health_session,
    monkeypatch,
):
    from brain.systems.runs.cortex import queue_health

    now = datetime.now(timezone.utc)
    created_at = now - timedelta(hours=4)
    requeued_at = now - timedelta(seconds=30)
    queue_health_session.add(
        _run(
            thread_id="requeued",
            status="queued",
            created_at=created_at,
            updated_at=requeued_at,
            started_at=created_at,
            execution_attempt=1,
        )
    )
    await queue_health_session.flush()
    monkeypatch.setattr(
        queue_health,
        "UnitOfWork",
        lambda: _SessionUnitOfWork(queue_health_session),
    )

    queued, oldest_queued_at, recent_active = (
        await queue_health.queued_backlog_snapshot_async(
            stale_after_seconds=60,
        )
    )

    assert queued == 1
    assert oldest_queued_at is not None
    assert 25 <= (now - oldest_queued_at).total_seconds() <= 35
    assert recent_active == 0


async def test_stale_processing_rows_do_not_hide_queue_starvation(
    queue_health_session,
    monkeypatch,
):
    from brain.systems.runs.cortex import queue_health

    now = datetime.now(timezone.utc)
    old = now - timedelta(minutes=10)
    rows = [
        _run(
            thread_id="queued",
            status="queued",
            created_at=old,
            updated_at=old,
        )
    ]
    rows.extend(
        _run(
            thread_id=f"zombie-{index}",
            status="running",
            created_at=old,
            updated_at=old,
            started_at=old,
            execution_attempt=1,
        )
        for index in range(4)
    )
    queue_health_session.add_all(rows)
    await queue_health_session.flush()
    monkeypatch.setattr(
        queue_health,
        "UnitOfWork",
        lambda: _SessionUnitOfWork(queue_health_session),
    )
    monkeypatch.setattr(queue_health, "runner_concurrency", lambda: 4)

    health = await queue_health.queued_backlog_health_snapshot_async(
        stale_after_seconds=60,
    )

    assert health["active_runs"] == 0
    assert health["queue_moving_at_capacity"] is False
    assert health["stale_queued_backlog"] is True


async def test_recent_processing_claims_excuse_saturated_queue(
    queue_health_session,
    monkeypatch,
):
    from brain.systems.runs.cortex import queue_health

    now = datetime.now(timezone.utc)
    old = now - timedelta(minutes=10)
    rows = [
        _run(
            thread_id="queued",
            status="queued",
            created_at=old,
            updated_at=old,
        )
    ]
    rows.extend(
        _run(
            thread_id=f"active-{index}",
            status="running",
            created_at=old,
            updated_at=now,
            started_at=old,
            execution_attempt=1,
        )
        for index in range(4)
    )
    queue_health_session.add_all(rows)
    await queue_health_session.flush()
    monkeypatch.setattr(
        queue_health,
        "UnitOfWork",
        lambda: _SessionUnitOfWork(queue_health_session),
    )
    monkeypatch.setattr(queue_health, "runner_concurrency", lambda: 4)

    health = await queue_health.queued_backlog_health_snapshot_async(
        stale_after_seconds=60,
    )

    assert health["active_runs"] == 4
    assert health["queue_moving_at_capacity"] is True
    assert health["stale_queued_backlog"] is False


def test_queue_health_cli_fails_with_legible_starvation_message(
    monkeypatch,
    capsys,
):
    from brain.systems.runs.cortex import queue_health

    async def fake_health(*, stale_after_seconds):
        assert stale_after_seconds == 600
        return {
            "queued": 3,
            "recent_active_runs": 0,
            "configured_concurrency": 4,
            "oldest_queued_age_seconds": 900,
            "watchdog_after_seconds": 600,
            "stale_queued_backlog": True,
        }

    monkeypatch.setattr(
        queue_health,
        "queued_backlog_health_snapshot_async",
        fake_health,
    )

    assert queue_health.main(["--stale-after-seconds", "600"]) == 1
    captured = capsys.readouterr()
    assert "AgentRun queue starvation" in captured.err
    assert "No worker is claiming the queued backlog" in captured.err
