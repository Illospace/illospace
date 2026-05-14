from __future__ import annotations

import asyncio
import logging
import threading
import time


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


async def _noop_reap(**_kwargs):
    return 0
