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
