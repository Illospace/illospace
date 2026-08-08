import signal
import threading
from datetime import datetime, timezone

import pytest
from unittest.mock import patch


def _queue_health(*, stale_queued_backlog: bool):
    from brain.systems.runs.cortex.queue_health import QueueHealth

    return QueueHealth(
        queued=1 if stale_queued_backlog else 0,
        recent_active_runs=0,
        configured_concurrency=4,
        oldest_queued_at=None,
        oldest_queued_age_seconds=None,
        watchdog_after_seconds=15,
        queue_moving_at_capacity=False,
        stale_queued_backlog=stale_queued_backlog,
    )


def test_require_embedding_backend_ready_skips_non_gpu():
    from brain.systems.cortex.worker import _require_embedding_backend_ready

    with patch("brain.kernel.config.EMBEDDING_BACKEND", "cpu"):
        _require_embedding_backend_ready()


def test_require_embedding_backend_ready_passes_when_embedder_ready():
    from brain.systems.cortex.worker import _require_embedding_backend_ready

    with patch("brain.kernel.config.EMBEDDING_BACKEND", "gpu"), \
         patch("brain.systems.memory.embeddings.wait_for_embedding_backend_ready", return_value=True):
        _require_embedding_backend_ready()


def test_require_embedding_backend_ready_raises_when_embedder_not_ready():
    from brain.systems.cortex.worker import _require_embedding_backend_ready

    with patch("brain.kernel.config.EMBEDDING_BACKEND", "gpu"), \
         patch("brain.systems.memory.embeddings.wait_for_embedding_backend_ready", return_value=False), \
         patch("brain.systems.memory.embeddings.server_health", return_value={"workers": {"embedding": {"status": "loading"}}}):
        with pytest.raises(RuntimeError, match="Embedding backend not ready before worker start"):
            _require_embedding_backend_ready()


def test_shutdown_drain_timeout_defaults_to_indefinite(monkeypatch):
    from brain.systems.cortex.worker import _shutdown_drain_timeout_seconds

    monkeypatch.delenv("ILLO_AGENT_RUNNER_DRAIN_TIMEOUT_SECONDS", raising=False)

    assert _shutdown_drain_timeout_seconds() is None


def test_shutdown_drain_timeout_accepts_numeric_override(monkeypatch):
    from brain.systems.cortex.worker import _shutdown_drain_timeout_seconds

    monkeypatch.setenv("ILLO_AGENT_RUNNER_DRAIN_TIMEOUT_SECONDS", "42.5")

    assert _shutdown_drain_timeout_seconds() == 42.5


def test_runner_health_grace_accepts_numeric_override(monkeypatch):
    from brain.systems.cortex.worker import _runner_health_grace_seconds

    monkeypatch.setenv("ILLO_AGENT_RUNNER_HEALTH_GRACE_SECONDS", "3.5")

    assert _runner_health_grace_seconds() == 3.5


def test_runner_health_grace_has_safe_minimum(monkeypatch):
    from brain.systems.cortex.worker import _runner_health_grace_seconds

    monkeypatch.setenv("ILLO_AGENT_RUNNER_HEALTH_GRACE_SECONDS", "0")

    assert _runner_health_grace_seconds() == 1.0


def test_queue_health_check_interval_accepts_numeric_override(monkeypatch):
    from brain.systems.cortex.worker import _queue_health_check_interval_seconds

    monkeypatch.setenv("ILLO_AGENT_RUN_QUEUE_HEALTH_CHECK_SECONDS", "2.5")

    assert _queue_health_check_interval_seconds() == 2.5


def test_queue_stall_grace_has_safe_minimum(monkeypatch):
    from brain.systems.cortex.worker import _queue_stall_grace_seconds

    monkeypatch.setenv("ILLO_AGENT_RUN_QUEUE_STALL_GRACE_SECONDS", "0")

    assert _queue_stall_grace_seconds() == 5.0


def test_queue_stall_monitor_checks_on_interval():
    from brain.systems.runs.cortex.queue_health import QueueStallMonitor

    monitor = QueueStallMonitor(check_interval_seconds=5.0, stall_grace_seconds=10.0)

    assert monitor.should_check(now=4.0) is False
    assert monitor.should_check(now=5.0) is True
    monitor.observe(_queue_health(stale_queued_backlog=False), now=5.0)
    assert monitor.should_check(now=9.0) is False
    assert monitor.should_check(now=10.0) is True


def test_queue_stall_monitor_tracks_stale_backlog():
    from brain.systems.runs.cortex.queue_health import QueueStallMonitor

    monitor = QueueStallMonitor(check_interval_seconds=5.0, stall_grace_seconds=10.0)

    stalled = _queue_health(stale_queued_backlog=True)
    healthy = _queue_health(stale_queued_backlog=False)

    assert monitor.observe(stalled, now=10.0) is None
    assert monitor.stale_since == 10.0
    assert monitor.observe(stalled, now=19.0) is None
    assert monitor.observe(stalled, now=20.0) == 10
    assert monitor.observe(healthy, now=21.0) is None
    assert monitor.stale_since is None


def test_cycle_scheduler_can_be_disabled_for_handoff_worker(monkeypatch):
    from brain.systems.cortex.worker import _cycle_scheduler_enabled

    monkeypatch.setenv("ILLO_WORKER_DISABLE_CYCLE_SCHEDULER", "1")
    monkeypatch.setenv("ILLO_WORKER_ENABLE_CYCLE_SCHEDULER", "1")

    assert _cycle_scheduler_enabled() is False


def test_cycle_scheduler_is_disabled_by_default_in_agent_worker(monkeypatch):
    from brain.systems.cortex.worker import _cycle_scheduler_enabled

    monkeypatch.delenv("ILLO_WORKER_DISABLE_CYCLE_SCHEDULER", raising=False)
    monkeypatch.delenv("ILLO_WORKER_ENABLE_CYCLE_SCHEDULER", raising=False)

    assert _cycle_scheduler_enabled() is False


def test_cycle_scheduler_can_be_explicitly_enabled(monkeypatch):
    from brain.systems.cortex.worker import _cycle_scheduler_enabled

    monkeypatch.delenv("ILLO_WORKER_DISABLE_CYCLE_SCHEDULER", raising=False)
    monkeypatch.setenv("ILLO_WORKER_ENABLE_CYCLE_SCHEDULER", "1")

    assert _cycle_scheduler_enabled() is True


def test_cycle_scheduler_stall_grace_defaults_to_five_minutes(monkeypatch):
    from brain.systems.cortex.worker import _cycle_scheduler_stall_grace_seconds

    monkeypatch.delenv("ILLO_CYCLE_SCHEDULER_STALL_GRACE_SECONDS", raising=False)

    assert _cycle_scheduler_stall_grace_seconds() == 300.0


def test_worker_exits_when_cycle_scheduler_heartbeat_is_stale(monkeypatch, caplog):
    from brain.contracts.worker_lifecycle import WorkerLifecyclePhase
    from brain.systems.cortex import worker

    calls = []
    terminate_calls = []

    class QueueStallMonitorStub:
        def should_check(self, *, now):
            return False

    monkeypatch.setattr(worker, "_running", True)
    monkeypatch.setattr(
        worker,
        "_publish_worker_lifecycle_phase",
        lambda phase: calls.append(("phase", phase)),
    )
    monkeypatch.setattr(
        worker,
        "request_runner_stop",
        lambda: calls.append(("request_runner_stop", None)),
    )
    monkeypatch.setattr(worker, "_require_embedding_backend_ready", lambda: None)
    monkeypatch.setattr(worker, "_cycle_scheduler_enabled", lambda: True)
    monkeypatch.setattr(worker, "start_cycle_scheduler", lambda: None)
    monkeypatch.setattr(worker, "start_runner", lambda: None)
    monkeypatch.setattr(worker, "runner_health_snapshot", lambda: {"runner_running": True})
    monkeypatch.setattr(worker, "QueueStallMonitor", lambda **_kwargs: QueueStallMonitorStub())
    monkeypatch.setattr(worker, "seconds_since_last_cycle_tick", lambda: 301.0)
    monkeypatch.setattr(
        worker,
        "stop_runner",
        lambda **_kwargs: calls.append(("stop_runner", None)) or worker.DrainResult(),
    )
    monkeypatch.setattr(worker, "stop_cycle_scheduler", lambda: None)
    monkeypatch.setattr(worker.logging, "shutdown", lambda: None)
    monkeypatch.setattr(worker, "_terminate_process", terminate_calls.append)

    with pytest.raises(SystemExit) as exc_info:
        worker.main()

    assert exc_info.value.code == 1
    assert terminate_calls == [1]
    assert calls == [
        ("phase", WorkerLifecyclePhase.STARTING),
        ("phase", WorkerLifecyclePhase.CLAIMING),
        ("phase", WorkerLifecyclePhase.DRAINING),
        ("request_runner_stop", None),
        ("stop_runner", None),
        ("phase", WorkerLifecyclePhase.STOPPED),
    ]
    assert "cycle scheduler wedged; exiting for restart" in caplog.text


def test_worker_term_path_calls_terminate_process_with_zero(monkeypatch):
    from brain.systems.cortex import worker

    calls = []

    monkeypatch.setattr(worker, "_running", True)
    monkeypatch.setattr(worker, "_require_embedding_backend_ready", lambda: None)
    monkeypatch.setattr(worker, "_cycle_scheduler_enabled", lambda: True)
    monkeypatch.setattr(worker, "start_cycle_scheduler", lambda: None)
    monkeypatch.setattr(worker, "start_runner", lambda: setattr(worker, "_running", False))
    monkeypatch.setattr(worker, "_publish_worker_lifecycle_phase", lambda _phase: None)
    monkeypatch.setattr(
        worker,
        "request_runner_stop",
        lambda: calls.append("request_runner_stop"),
    )
    monkeypatch.setattr(
        worker,
        "stop_runner",
        lambda **_kwargs: calls.append("stop_runner") or worker.DrainResult(),
    )
    monkeypatch.setattr(worker, "stop_cycle_scheduler", lambda: calls.append("stop_cycle_scheduler"))
    monkeypatch.setattr(worker.logging, "shutdown", lambda: calls.append("logging.shutdown"))
    monkeypatch.setattr(worker, "_terminate_process", lambda code: calls.append(("terminate", code)))

    worker.main()

    assert calls == [
        "request_runner_stop",
        "stop_runner",
        "stop_cycle_scheduler",
        "logging.shutdown",
        ("terminate", 0),
    ]


def test_worker_publishes_draining_before_unconditional_runner_stop(monkeypatch):
    from brain.contracts.worker_lifecycle import WorkerLifecyclePhase
    from brain.systems.cortex import worker

    phases = []
    terminate_calls = []

    def require_embedding_backend_ready():
        assert phases == [WorkerLifecyclePhase.STARTING]

    def start_runner():
        assert phases == [WorkerLifecyclePhase.STARTING]

    def stop_after_first_poll(_seconds):
        worker._running = False

    def stop_runner(**_kwargs):
        assert phases[-1] is WorkerLifecyclePhase.DRAINING
        return worker.DrainResult()

    monkeypatch.setattr(worker, "_running", True)
    monkeypatch.setattr(worker, "_publish_worker_lifecycle_phase", phases.append)
    monkeypatch.setattr(
        worker,
        "_require_embedding_backend_ready",
        require_embedding_backend_ready,
    )
    monkeypatch.setattr(worker, "_cycle_scheduler_enabled", lambda: False)
    monkeypatch.setattr(worker, "start_runner", start_runner)
    monkeypatch.setattr(worker, "request_runner_stop", lambda: None)
    monkeypatch.setattr(
        worker,
        "runner_health_snapshot",
        lambda: {"runner_running": True},
    )
    monkeypatch.setattr(
        worker.QueueStallMonitor,
        "should_check",
        lambda _self, *, now: False,
    )
    monkeypatch.setattr(worker.time, "sleep", stop_after_first_poll)
    monkeypatch.setattr(worker, "stop_runner", stop_runner)
    monkeypatch.setattr(worker.logging, "shutdown", lambda: None)
    monkeypatch.setattr(worker, "_terminate_process", terminate_calls.append)

    worker.main()

    assert phases == [
        WorkerLifecyclePhase.STARTING,
        WorkerLifecyclePhase.CLAIMING,
        WorkerLifecyclePhase.DRAINING,
        WorkerLifecyclePhase.STOPPED,
    ]
    assert terminate_calls == [0]


def test_worker_sigterm_during_startup_never_publishes_claiming(monkeypatch):
    from brain.contracts.worker_lifecycle import WorkerLifecyclePhase
    from brain.systems.cortex import worker

    phases = []
    terminate_calls = []

    def interrupt_embedding_startup():
        worker._signal_handler(signal.SIGTERM, None)

    monkeypatch.setattr(worker, "_running", True)
    monkeypatch.setattr(worker, "_publish_worker_lifecycle_phase", phases.append)
    monkeypatch.setattr(worker, "request_runner_stop", lambda: None)
    monkeypatch.setattr(
        worker,
        "_require_embedding_backend_ready",
        interrupt_embedding_startup,
    )
    monkeypatch.setattr(
        worker,
        "stop_runner",
        lambda **_kwargs: worker.DrainResult(),
    )
    monkeypatch.setattr(worker.logging, "shutdown", lambda: None)
    monkeypatch.setattr(worker, "_terminate_process", terminate_calls.append)

    worker.main()

    assert phases == [
        WorkerLifecyclePhase.STARTING,
        WorkerLifecyclePhase.DRAINING,
        WorkerLifecyclePhase.STOPPED,
    ]
    assert terminate_calls == [0]


def test_worker_entry_point_recovers_timed_out_runs(monkeypatch, caplog):
    from brain.systems.cortex import worker
    from brain.systems.runs.interruption import RunInterruption

    calls = []
    occurred_at = datetime(2026, 7, 22, 17, 55, tzinfo=timezone.utc)

    async def interrupt(run_ids, *, reason):
        calls.append((run_ids, reason))
        return (
            RunInterruption(
                run_id=2330,
                reason=reason,
                interrupted_at=occurred_at,
                requeued=True,
            ),
        )

    monkeypatch.setattr(worker, "interrupt_and_requeue_run_ids", interrupt)

    worker._recover_timed_out_runs(
        worker.DrainResult(timed_out_run_ids=(2330,))
    )

    assert calls == [((2330,), "worker_shutdown_drain_timeout")]
    assert "interrupted and requeued run ids: [2330]" in caplog.text


def test_self_restart_drain_timeout_defaults_to_one_minute(monkeypatch):
    from brain.systems.cortex.worker import _self_restart_drain_timeout_seconds

    monkeypatch.delenv(
        "ILLO_AGENT_RUNNER_SELF_RESTART_DRAIN_TIMEOUT_SECONDS", raising=False
    )

    assert _self_restart_drain_timeout_seconds() == 60.0


def test_self_restart_drain_timeout_accepts_numeric_override(monkeypatch):
    from brain.systems.cortex.worker import _self_restart_drain_timeout_seconds

    monkeypatch.setenv("ILLO_AGENT_RUNNER_SELF_RESTART_DRAIN_TIMEOUT_SECONDS", "12.5")

    assert _self_restart_drain_timeout_seconds() == 12.5


def test_self_restart_drain_timeout_is_never_indefinite(monkeypatch):
    from brain.systems.cortex.worker import _self_restart_drain_timeout_seconds

    monkeypatch.setenv("ILLO_AGENT_RUNNER_SELF_RESTART_DRAIN_TIMEOUT_SECONDS", "infinity")

    assert _self_restart_drain_timeout_seconds() == 60.0


def _run_main_until_queue_stall(monkeypatch, *, stop_runner):
    """Drive ``main`` through the queue-stall watchdog with the deploy env set."""
    from brain.systems.cortex import worker

    class StalledQueueMonitor:
        def should_check(self, *, now):
            return True

        def observe(self, _queue_health, *, now):
            return 90

    monkeypatch.setenv("ILLO_AGENT_RUNNER_DRAIN_TIMEOUT_SECONDS", "infinity")
    monkeypatch.setattr(worker, "_running", True)
    monkeypatch.setattr(worker, "_publish_worker_lifecycle_phase", lambda _phase: None)
    monkeypatch.setattr(worker, "_require_embedding_backend_ready", lambda: None)
    monkeypatch.setattr(worker, "_cycle_scheduler_enabled", lambda: False)
    monkeypatch.setattr(worker, "start_runner", lambda: None)
    monkeypatch.setattr(worker, "request_runner_stop", lambda: None)
    monkeypatch.setattr(worker, "runner_health_snapshot", lambda: {"runner_running": True})
    monkeypatch.setattr(worker, "QueueStallMonitor", lambda **_kwargs: StalledQueueMonitor())
    monkeypatch.setattr(
        worker,
        "queued_backlog_health_snapshot_async",
        lambda: _async_queue_health(),
    )
    monkeypatch.setattr(worker, "stop_runner", stop_runner)
    monkeypatch.setattr(worker.logging, "shutdown", lambda: None)
    return worker


async def _async_queue_health():
    return _queue_health(stale_queued_backlog=True)


def test_health_exit_drains_with_a_floor_even_when_deploy_drain_is_infinite(monkeypatch):
    """A wedged run must not hold the process open when nothing else claims.

    On 2026-08-08 the queue-stall watchdog fired, the drain inherited the
    deploy's ``infinity`` setting, and the run that wedged the worker kept
    ``runner_in_flight_count()`` above zero forever. The process never exited,
    so ``restart: unless-stopped`` never fired and the queue stayed dead behind
    a green ``docker ps``.
    """
    from brain.systems.cortex import worker

    drain_timeouts = []
    terminate_calls = []

    def stop_runner(*, drain_timeout_seconds):
        drain_timeouts.append(drain_timeout_seconds)
        if drain_timeout_seconds is None:
            raise AssertionError("unbounded drain would wait on the wedged run forever")
        return worker.DrainResult(timed_out_run_ids=(15747,))

    worker_module = _run_main_until_queue_stall(monkeypatch, stop_runner=stop_runner)
    recovered = []
    monkeypatch.setattr(worker_module, "_recover_timed_out_runs", recovered.append)
    monkeypatch.setattr(worker_module, "_terminate_process", terminate_calls.append)

    with pytest.raises(SystemExit) as exc_info:
        worker_module.main()

    assert exc_info.value.code == 1
    assert drain_timeouts == [60.0]
    assert [result.timed_out_run_ids for result in recovered] == [(15747,)]
    assert terminate_calls == [1]


def test_deploy_sigterm_keeps_the_unbounded_drain(monkeypatch):
    """The handoff worker is already claiming, so long runs may finish."""
    from brain.systems.cortex import worker

    drain_timeouts = []

    monkeypatch.setenv("ILLO_AGENT_RUNNER_DRAIN_TIMEOUT_SECONDS", "infinity")
    monkeypatch.setattr(worker, "_running", True)
    monkeypatch.setattr(worker, "_publish_worker_lifecycle_phase", lambda _phase: None)
    monkeypatch.setattr(worker, "_require_embedding_backend_ready", lambda: None)
    monkeypatch.setattr(worker, "_cycle_scheduler_enabled", lambda: False)
    monkeypatch.setattr(
        worker,
        "start_runner",
        lambda: worker._signal_handler(signal.SIGTERM, None),
    )
    monkeypatch.setattr(worker, "request_runner_stop", lambda: None)
    monkeypatch.setattr(
        worker,
        "stop_runner",
        lambda *, drain_timeout_seconds: drain_timeouts.append(drain_timeout_seconds)
        or worker.DrainResult(),
    )
    monkeypatch.setattr(worker.logging, "shutdown", lambda: None)
    monkeypatch.setattr(worker, "_terminate_process", lambda _code: None)

    worker.main()

    assert drain_timeouts == [None]


def test_health_exit_terminates_even_when_the_shutdown_sequence_hangs(monkeypatch):
    """Only the exit restores claiming capacity, so no step may hold it hostage."""
    from brain.systems.cortex import worker

    terminated = threading.Event()
    terminate_calls = []

    def stop_runner(*, drain_timeout_seconds):
        # Stands in for a drain, requeue or scheduler stop that never returns.
        terminated.wait(timeout=5.0)
        return worker.DrainResult()

    def terminate(code):
        terminate_calls.append(code)
        terminated.set()

    worker_module = _run_main_until_queue_stall(monkeypatch, stop_runner=stop_runner)
    monkeypatch.setattr(worker_module, "_SELF_RESTART_SHUTDOWN_GRACE_SECONDS", 0.05)
    monkeypatch.setenv("ILLO_AGENT_RUNNER_SELF_RESTART_DRAIN_TIMEOUT_SECONDS", "0")
    monkeypatch.setattr(worker_module, "_terminate_process", terminate)

    with pytest.raises(SystemExit):
        worker_module.main()

    assert terminated.is_set()
    assert terminate_calls[0] == 1


def test_signal_handler_requests_runner_stop(monkeypatch):
    from brain.contracts.worker_lifecycle import WorkerLifecyclePhase
    from brain.systems.cortex import worker

    calls = []
    previous_running = worker._running
    previous_draining = worker._draining
    monkeypatch.setattr(
        worker,
        "_publish_worker_lifecycle_phase",
        lambda phase: calls.append(("phase", phase)),
    )
    monkeypatch.setattr(worker, "request_runner_stop", lambda: calls.append(("stop", True)))
    try:
        worker._running = True
        worker._draining = False

        worker._signal_handler(signal.SIGTERM, None)
        worker._signal_handler(signal.SIGTERM, None)

        assert worker._running is False
        assert calls == [
            ("phase", WorkerLifecyclePhase.DRAINING),
            ("stop", True),
        ]
    finally:
        worker._running = previous_running
        worker._draining = previous_draining
