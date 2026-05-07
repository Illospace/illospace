from __future__ import annotations

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


def test_start_runner_starts_configured_worker_pool(monkeypatch):
    from brain.systems.runs.cortex import runner

    release = threading.Event()
    entered = 0
    entered_lock = threading.Lock()

    def fake_loop(_slot_stop_event=None):
        nonlocal entered
        with entered_lock:
            entered += 1
        release.wait(timeout=1)

    runner.stop_runner()
    monkeypatch.setattr(runner, "_runner_concurrency", lambda: 3)
    monkeypatch.setattr(runner, "_loop", fake_loop)
    try:
        runner.start_runner()
        for _ in range(100):
            with entered_lock:
                if entered == 3:
                    break
            time.sleep(0.01)
        active_names = [thread.name for thread in runner._active_runner_threads()]
        assert len(active_names) == 3
        assert all(name.startswith("agent-runner-") for name in active_names)
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

    def fake_loop(_slot_stop_event=None):
        entered.set()
        release.wait(timeout=1)
        finished.set()

    runner.stop_runner()
    monkeypatch.setattr(runner, "_runner_concurrency", lambda: 1)
    monkeypatch.setattr(runner, "_loop", fake_loop)
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
