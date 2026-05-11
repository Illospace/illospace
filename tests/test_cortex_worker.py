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


def test_cycle_scheduler_can_be_disabled_for_handoff_worker(monkeypatch):
    from brain.systems.cortex.worker import _cycle_scheduler_enabled

    monkeypatch.setenv("ILLO_WORKER_DISABLE_CYCLE_SCHEDULER", "1")

    assert _cycle_scheduler_enabled() is False
