"""Tests for memory.py — pure helper functions."""

import os
import sys
from unittest.mock import patch, MagicMock

import numpy as np
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), *([".."] * 1))))


def test_vec_to_pg():
    """vec_to_pg should format numpy array as PostgreSQL vector string."""
    import numpy as np
    from brain.systems.memory.embeddings import vec_to_pg

    vec = np.array([1.0, 2.5, -0.3], dtype=np.float32)
    result = vec_to_pg(vec)
    assert result.startswith("[")
    assert result.endswith("]")
    assert "1.0" in result
    assert "2.5" in result


def test_emotion_map_has_standard_emotions():
    """EMOTION_MAP should contain standard emotion labels."""
    from brain.systems.memory.embeddings import EMOTION_MAP

    required = ["neutral", "frustrated", "satisfied", "curious"]
    for emotion in required:
        assert emotion in EMOTION_MAP, f"Missing emotion: {emotion}"


def test_make_emotional_embedding_returns_vector():
    """make_emotional_embedding should return a numpy array."""
    import numpy as np
    from brain.systems.memory.embeddings import make_emotional_embedding

    result = make_emotional_embedding(0.5, 0.3, "satisfied")
    assert isinstance(result, np.ndarray)
    assert len(result) > 0


def test_make_emotional_embedding_with_label_only():
    """make_emotional_embedding should work with just a label."""
    import numpy as np
    from brain.systems.memory.embeddings import make_emotional_embedding

    result = make_emotional_embedding(label="frustrated")
    assert isinstance(result, np.ndarray)


class TestEmbeddingsClient:
    def test_embed_document_calls_gpu_client(self):
        from brain.systems.memory.embeddings import embed_document
        fake_arr = np.random.randn(1, 2000).astype(np.float32)
        with patch("brain.platform.gpu_client.get_client") as mock_get, \
             patch("brain.kernel.config.EMBEDDING_BACKEND", "gpu"):
            mock_client = MagicMock()
            mock_client.embed.return_value = fake_arr
            mock_get.return_value = mock_client
            result = embed_document("test text")
            mock_client.embed.assert_called_once_with(["test text"], mode="document")
            assert result.shape == (2000,)

    def test_embed_query_calls_gpu_client(self):
        from brain.systems.memory.embeddings import embed_query
        fake_arr = np.random.randn(1, 2000).astype(np.float32)
        with patch("brain.platform.gpu_client.get_client") as mock_get, \
             patch("brain.kernel.config.EMBEDDING_BACKEND", "gpu"):
            mock_client = MagicMock()
            mock_client.embed.return_value = fake_arr
            mock_get.return_value = mock_client
            embed_query("search query")
            mock_client.embed.assert_called_once_with(["search query"], mode="query")

    def test_embed_query_waits_for_embedding_worker_loading(self):
        from brain.systems.memory.embeddings import embed_query

        fake_arr = np.random.randn(1, 2000).astype(np.float32)
        with patch("brain.platform.gpu_client.get_client") as mock_get, \
             patch("brain.kernel.config.EMBEDDING_BACKEND", "gpu"), \
             patch("brain.systems.memory.embeddings.time.sleep") as mock_sleep, \
             patch.dict(
                 os.environ,
                 {
                     "EMBEDDING_WORKER_WARMUP_WAIT_SECONDS": "1",
                     "EMBEDDING_WORKER_WARMUP_POLL_SECONDS": "0.01",
                 },
                 clear=False,
             ):
            mock_client = MagicMock()
            mock_client.embed.side_effect = [
                RuntimeError("worker_unavailable: Worker 'embedding' is loading"),
                fake_arr,
            ]
            mock_client.is_ready.side_effect = [False, True]
            mock_get.return_value = mock_client

            result = embed_query("search query")

            assert result.shape == (2000,)
            assert mock_client.embed.call_count == 2
            mock_client.is_ready.assert_called_with("embedding")
            mock_sleep.assert_called_once_with(0.05)

    def test_embed_query_raises_when_embedding_worker_stays_loading(self):
        from brain.systems.memory.embeddings import embed_query

        with patch("brain.platform.gpu_client.get_client") as mock_get, \
             patch("brain.kernel.config.EMBEDDING_BACKEND", "gpu"), \
             patch("brain.systems.memory.embeddings.time.sleep"), \
             patch("brain.systems.memory.embeddings._wait_for_gpu_worker_ready", return_value=False), \
             patch.dict(os.environ, {"EMBEDDING_WORKER_WARMUP_WAIT_SECONDS": "0.1"}, clear=False):
            mock_client = MagicMock()
            mock_client.embed.side_effect = RuntimeError("worker_unavailable: Worker 'embedding' is loading")
            mock_get.return_value = mock_client

            with pytest.raises(RuntimeError, match="worker_unavailable"):
                embed_query("search query")

    def test_embed_query_requests_recovery_when_embedding_worker_failed(self):
        from brain.systems.memory.embeddings import embed_query

        fake_arr = np.random.randn(1, 2000).astype(np.float32)
        with patch("brain.platform.gpu_client.get_client") as mock_get, \
             patch("brain.kernel.config.EMBEDDING_BACKEND", "gpu"), \
             patch.dict(
                 os.environ,
                 {
                     "EMBEDDING_WORKER_WARMUP_WAIT_SECONDS": "1",
                     "EMBEDDING_WORKER_RESTART_TIMEOUT_SECONDS": "2",
                 },
                 clear=False,
             ):
            mock_client = MagicMock()
            mock_client.embed.side_effect = [
                RuntimeError("worker_unavailable: Worker 'embedding' is failed | recent crash: startup timeout"),
                fake_arr,
            ]
            mock_client.load_worker.return_value = True
            mock_client.is_ready.return_value = True
            mock_get.return_value = mock_client

            result = embed_query("search query")

        assert result.shape == (2000,)
        mock_client.load_worker.assert_called_once_with("embedding", timeout_s=2.0)
        assert mock_client.embed.call_count == 2

    def test_wait_for_embedding_backend_ready_returns_true_for_non_gpu_backends(self):
        from brain.systems.memory.embeddings import wait_for_embedding_backend_ready

        with patch("brain.kernel.config.EMBEDDING_BACKEND", "cpu"):
            assert wait_for_embedding_backend_ready(timeout_s=0.1) is True

    def test_server_health_calls_gpu_client(self):
        from brain.systems.memory.embeddings import server_health
        with patch("brain.platform.gpu_client.get_client") as mock_get, \
             patch("brain.systems.memory.embeddings.EMBEDDING_BACKEND", "gpu"):
            mock_client = MagicMock()
            mock_client.health.return_value = {"status": "ok"}
            mock_get.return_value = mock_client
            result = server_health()
            assert result["status"] == "ok"

    def test_server_health_returns_none_on_failure(self):
        from brain.systems.memory.embeddings import server_health
        with patch("brain.platform.gpu_client.get_client") as mock_get, \
             patch("brain.systems.memory.embeddings.EMBEDDING_BACKEND", "gpu"):
            mock_get.return_value.health.side_effect = RuntimeError("down")
            result = server_health()
            assert result is None
