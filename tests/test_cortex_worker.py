import pytest
from unittest.mock import patch


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
    monitor.observe({"stale_queued_backlog": False}, now=5.0)
    assert monitor.should_check(now=9.0) is False
    assert monitor.should_check(now=10.0) is True


def test_queue_stall_monitor_tracks_stale_backlog():
    from brain.systems.runs.cortex.queue_health import QueueStallMonitor

    monitor = QueueStallMonitor(check_interval_seconds=5.0, stall_grace_seconds=10.0)

    assert monitor.observe({"stale_queued_backlog": True}, now=10.0) is None
    assert monitor.stale_since == 10.0
    assert monitor.observe({"stale_queued_backlog": True}, now=19.0) is None
    assert monitor.observe({"stale_queued_backlog": True}, now=20.0) == 10
    assert monitor.observe({"stale_queued_backlog": False}, now=21.0) is None
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
