"""Cycle scheduler liveness tests."""
from __future__ import annotations

import asyncio

import pytest

from brain.systems.cycles import scheduler

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _reset_scheduler_state():
    scheduler.stop_cycle_scheduler()
    yield
    scheduler.stop_cycle_scheduler()


async def test_timed_out_tick_is_cancelled_and_loop_continues(monkeypatch, caplog):
    calls = 0
    timed_out_tick_cancelled = asyncio.Event()

    async def schedule_due_cycles_once():
        nonlocal calls
        calls += 1
        if calls == 1:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                timed_out_tick_cancelled.set()
                raise
        with scheduler._scheduler_state_lock:
            scheduler._scheduler_running = False

    monkeypatch.setattr(scheduler, "async_schedule_due_cycles_once", schedule_due_cycles_once)
    monkeypatch.setattr(scheduler, "_tick_timeout_sec", lambda: 0.01)
    monkeypatch.setattr(scheduler, "_poll_interval_sec", 0)
    with scheduler._scheduler_state_lock:
        scheduler._scheduler_running = True
        scheduler._last_tick_ok = 10.0

    await asyncio.wait_for(scheduler._scheduler_loop(), timeout=0.2)

    assert calls == 2
    assert timed_out_tick_cancelled.is_set()
    assert scheduler._last_tick_ok == 10.0
    assert "Cycle scheduler tick timed out after 0.01s" in caplog.text


async def test_successful_tick_advances_heartbeat(monkeypatch):
    async def schedule_due_cycles_once():
        return []

    async def stop_after_tick(_delay):
        with scheduler._scheduler_state_lock:
            scheduler._scheduler_running = False

    monkeypatch.setattr(scheduler, "async_schedule_due_cycles_once", schedule_due_cycles_once)
    monkeypatch.setattr(scheduler.asyncio, "sleep", stop_after_tick)
    monkeypatch.setattr(scheduler.time, "monotonic", lambda: 42.0)
    with scheduler._scheduler_state_lock:
        scheduler._scheduler_running = True
        scheduler._last_tick_ok = 10.0

    await scheduler._scheduler_loop()

    assert scheduler._last_tick_ok == 42.0


async def test_tick_age_is_none_before_start_and_after_stop(monkeypatch):
    assert scheduler.seconds_since_last_cycle_tick() is None

    monkeypatch.setattr(scheduler.time, "monotonic", lambda: 20.0)
    with scheduler._scheduler_state_lock:
        scheduler._scheduler_running = True
        scheduler._last_tick_ok = 15.0

    assert scheduler.seconds_since_last_cycle_tick() == 5.0

    scheduler.stop_cycle_scheduler()

    assert scheduler.seconds_since_last_cycle_tick() is None
