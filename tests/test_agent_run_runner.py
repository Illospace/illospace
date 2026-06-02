from __future__ import annotations

import asyncio
import logging
import threading
import time
from datetime import datetime, timedelta, timezone


def test_runner_concurrency_is_configurable(monkeypatch):
    from brain.systems.runs.cortex import runner

    monkeypatch.setenv("ILLO_AGENT_RUNNER_CONCURRENCY", "0")
    assert runner._runner_concurrency() == 1

    monkeypatch.setenv("ILLO_AGENT_RUNNER_CONCURRENCY", "999")
    assert runner._runner_concurrency() == 32

    monkeypatch.setenv("ILLO_AGENT_RUNNER_CONCURRENCY", "nope")
    assert runner._runner_concurrency() == 4


def test_run_cancel_token_sync_check_does_not_open_db_loop():
    from brain.systems.runs.cancel import RunCancelToken

    def fail_uow():
        raise AssertionError("sync cancellation checks must not open DB sessions")

    token = RunCancelToken(42, uow_factory=fail_uow)

    assert token.is_set() is False
    token._canceled = True
    assert token.is_set() is True


def test_start_runner_starts_configured_worker_pool(monkeypatch):
    from brain.systems.runs.cortex import runner

    release = threading.Event()
    entered = 0
    entered_lock = threading.Lock()

    async def fake_loop(_slot_stop_event=None):
        nonlocal entered
        with entered_lock:
            entered += 1
        await asyncio.to_thread(release.wait, 1)

    runner.stop_runner()
    monkeypatch.setattr(runner, "_runner_concurrency", lambda: 3)
    monkeypatch.setattr(runner, "_loop", fake_loop)
    monkeypatch.setattr(runner, "_reap_stale_runs_if_due_async", _noop_reap)
    monkeypatch.setattr(runner, "_nudge_stale_queued_runs_if_due_async", _noop_nudge)
    try:
        runner.start_runner()
        for _ in range(100):
            with entered_lock:
                if entered == 3:
                    break
            time.sleep(0.01)
        assert runner._active_runner_count() == 3
        with entered_lock:
            assert entered == 3
    finally:
        release.set()
        runner.stop_runner()


def test_stop_runner_can_drain_active_slots(monkeypatch):
    from brain.systems.runs.cortex import runner

    release = threading.Event()
    entered = threading.Event()
    finished = threading.Event()

    async def fake_loop(_slot_stop_event=None):
        entered.set()
        await asyncio.to_thread(release.wait, 1)
        finished.set()

    runner.stop_runner()
    monkeypatch.setattr(runner, "_runner_concurrency", lambda: 1)
    monkeypatch.setattr(runner, "_loop", fake_loop)
    monkeypatch.setattr(runner, "_reap_stale_runs_if_due_async", _noop_reap)
    monkeypatch.setattr(runner, "_nudge_stale_queued_runs_if_due_async", _noop_nudge)
    try:
        runner.start_runner()
        assert entered.wait(timeout=1)
        timer = threading.Timer(0.05, release.set)
        timer.start()
        started = time.monotonic()
        runner.stop_runner(drain_timeout_seconds=1)
        elapsed = time.monotonic() - started

        assert finished.is_set()
        assert elapsed >= 0.04
    finally:
        release.set()
        runner.stop_runner()


def test_runner_pool_uses_one_event_loop(monkeypatch):
    from brain.systems.runs.cortex import runner

    loops: list[int] = []
    release = threading.Event()

    async def fake_loop(_slot_stop_event=None):
        loops.append(id(asyncio.get_running_loop()))
        await asyncio.to_thread(release.wait, 1)

    runner.stop_runner()
    monkeypatch.setattr(runner, "_runner_concurrency", lambda: 3)
    monkeypatch.setattr(runner, "_loop", fake_loop)
    monkeypatch.setattr(runner, "_reap_stale_runs_if_due_async", _noop_reap)
    monkeypatch.setattr(runner, "_nudge_stale_queued_runs_if_due_async", _noop_nudge)
    try:
        runner.start_runner()
        for _ in range(100):
            if len(loops) == 3:
                break
            time.sleep(0.01)
    finally:
        release.set()
        runner.stop_runner()

    assert len(loops) == 3
    assert len(set(loops)) == 1


def test_runner_health_snapshot_requires_supervisor_and_slots(monkeypatch):
    from brain.systems.runs.cortex import runner

    class AliveThread:
        def is_alive(self):
            return True

    class DeadThread:
        def is_alive(self):
            return False

    runner.stop_runner()
    monkeypatch.setattr(runner, "_runner_concurrency", lambda: 2)
    monkeypatch.setattr(runner, "_active_runner_count", lambda: 1)
    monkeypatch.setattr(runner, "_runner_supervisor_thread", AliveThread())
    assert runner.runner_health_snapshot()["runner_running"] is True

    monkeypatch.setattr(runner, "_runner_supervisor_thread", DeadThread())
    assert runner.runner_health_snapshot()["runner_running"] is False

    monkeypatch.setattr(runner, "_runner_supervisor_thread", AliveThread())
    monkeypatch.setattr(runner, "_active_runner_count", lambda: 0)
    assert runner.runner_health_snapshot()["runner_running"] is False


async def test_queued_backlog_health_snapshot_marks_stale_queue(monkeypatch):
    from brain.systems.runs.cortex import queue_health

    oldest = datetime.now(timezone.utc) - timedelta(seconds=60)

    async def fake_snapshot():
        return 2, oldest, 0

    monkeypatch.setattr(queue_health, "queued_backlog_snapshot_async", fake_snapshot)
    monkeypatch.setattr(queue_health, "queued_watchdog_after_seconds", lambda: 10)
    monkeypatch.setattr(queue_health, "runner_concurrency", lambda: 4)

    health = await queue_health.queued_backlog_health_snapshot_async()

    assert health["queued"] == 2
    assert health["active_runs"] == 0
    assert health["stale_queued_backlog"] is True


async def test_queued_backlog_health_snapshot_respects_full_capacity(monkeypatch):
    from brain.systems.runs.cortex import queue_health

    oldest = datetime.now(timezone.utc) - timedelta(seconds=60)

    async def fake_snapshot():
        return 2, oldest, 4

    monkeypatch.setattr(queue_health, "queued_backlog_snapshot_async", fake_snapshot)
    monkeypatch.setattr(queue_health, "queued_watchdog_after_seconds", lambda: 10)
    monkeypatch.setattr(queue_health, "runner_concurrency", lambda: 4)

    health = await queue_health.queued_backlog_health_snapshot_async()

    assert health["queued"] == 2
    assert health["active_runs"] == 4
    assert health["stale_queued_backlog"] is False


async def test_supervisor_starts_runner_slots_before_stale_reconcile_finishes(monkeypatch):
    from brain.systems.runs.cortex import runner

    stale_started = asyncio.Event()
    release_stale = asyncio.Event()
    slot_entered = asyncio.Event()
    release_slot = asyncio.Event()

    async def blocked_reap(**_kwargs):
        stale_started.set()
        await release_stale.wait()
        return 0

    async def fake_loop(_slot_stop_event=None):
        slot_entered.set()
        await release_slot.wait()

    runner.stop_runner()
    runner._stop_event.clear()
    monkeypatch.setattr(runner, "_runner_concurrency", lambda: 1)
    monkeypatch.setattr(runner, "_reap_stale_runs_if_due_async", blocked_reap)
    monkeypatch.setattr(runner, "_nudge_stale_queued_runs_if_due_async", _noop_nudge)
    monkeypatch.setattr(runner, "_loop", fake_loop)
    supervisor = asyncio.create_task(runner._supervisor_async_loop())
    try:
        await asyncio.wait_for(stale_started.wait(), timeout=1)
        await asyncio.wait_for(slot_entered.wait(), timeout=1)
    finally:
        runner._stop_event.set()
        release_stale.set()
        release_slot.set()
        await asyncio.wait_for(supervisor, timeout=1)
        runner.stop_runner()


async def test_runner_slot_logs_and_survives_queue_errors(monkeypatch, caplog):
    from brain.systems.runs.cortex import runner

    calls = 0
    slot_stop_event = asyncio.Event()

    async def fake_run_queued_once():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("claim failed")
        slot_stop_event.set()
        return 0

    runner.stop_runner()
    runner._stop_event.clear()
    monkeypatch.setattr(runner, "_run_queued_once_async", fake_run_queued_once)
    monkeypatch.setattr(runner, "_poll_interval_sec", 0)
    caplog.set_level(logging.ERROR, logger=runner.__name__)
    try:
        await asyncio.wait_for(runner._loop(slot_stop_event), timeout=1)
    finally:
        slot_stop_event.set()
        runner.stop_runner()

    assert calls == 2
    assert "agent_run_runner_slot_failed" in caplog.text


async def test_queued_watchdog_nudges_stale_queue_when_capacity_available(monkeypatch, caplog):
    from brain.systems.runs.cortex import runner

    calls = 0
    oldest = datetime.now(timezone.utc) - timedelta(seconds=60)
    release = asyncio.Event()

    async def fake_snapshot():
        return 1, oldest, 0

    async def fake_run_queued_once(*, limit=1):
        nonlocal calls
        calls += 1
        assert limit == 1
        await release.wait()
        return 1

    runner._queued_watchdog_tasks.clear()
    runner._last_queued_watchdog_monotonic = 0
    monkeypatch.setattr(runner, "_queued_backlog_snapshot_async", fake_snapshot)
    monkeypatch.setattr(runner, "_queued_watchdog_after_seconds", lambda: 10)
    monkeypatch.setattr(runner, "_runner_concurrency", lambda: 4)
    monkeypatch.setattr(runner, "_run_queued_once_async", fake_run_queued_once)
    caplog.set_level(logging.WARNING, logger=runner.__name__)

    nudged = await runner._nudge_stale_queued_runs_if_due_async(force=True)
    assert nudged is True
    assert len(runner._queued_watchdog_tasks) == 1
    task = next(iter(runner._queued_watchdog_tasks))
    release.set()
    await asyncio.wait_for(task, timeout=1)
    await asyncio.sleep(0)

    assert calls == 1
    assert "agent_run_queued_watchdog_nudge" in caplog.text


async def test_queued_watchdog_respects_configured_capacity(monkeypatch):
    from brain.systems.runs.cortex import runner

    oldest = datetime.now(timezone.utc) - timedelta(seconds=60)

    async def fake_snapshot():
        return 1, oldest, 4

    async def fail_run_queued_once(*, limit=1):
        raise AssertionError("watchdog should not run when capacity is full")

    runner._queued_watchdog_tasks.clear()
    runner._last_queued_watchdog_monotonic = 0
    monkeypatch.setattr(runner, "_queued_backlog_snapshot_async", fake_snapshot)
    monkeypatch.setattr(runner, "_queued_watchdog_after_seconds", lambda: 10)
    monkeypatch.setattr(runner, "_runner_concurrency", lambda: 4)
    monkeypatch.setattr(runner, "_run_queued_once_async", fail_run_queued_once)

    assert await runner._nudge_stale_queued_runs_if_due_async(force=True) is False
    assert not runner._queued_watchdog_tasks


async def _noop_reap(**_kwargs):
    return 0


async def _noop_nudge(**_kwargs):
    return False
