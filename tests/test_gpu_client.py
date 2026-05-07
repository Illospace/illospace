import pytest
from unittest.mock import patch, MagicMock
import json
import numpy as np


class TestGPUClient:
    def test_client_defaults(self):
        from brain.platform.gpu_client import GPUClient
        c = GPUClient()
        assert c.base_url == "http://127.0.0.1:9800"

    def test_embed_returns_numpy(self):
        from brain.platform.gpu_client import GPUClient
        c = GPUClient()
        fake_response = MagicMock()
        fake_response.status_code = 200
        fake_response.json.return_value = {
            "embeddings": [[0.1] * 2000, [0.2] * 2000],
            "dims": 2000,
            "count": 2,
        }
        with patch.object(c._session, "post", return_value=fake_response):
            result = c.embed(["hello", "world"])
        assert isinstance(result, np.ndarray)
        assert result.shape == (2, 2000)

    def test_embed_one(self):
        from brain.platform.gpu_client import GPUClient
        c = GPUClient()
        fake_response = MagicMock()
        fake_response.status_code = 200
        fake_response.json.return_value = {
            "embeddings": [[0.1] * 2000],
            "dims": 2000,
            "count": 1,
        }
        with patch.object(c._session, "post", return_value=fake_response):
            result = c.embed_one("hello")
        assert result.shape == (2000,)

    def test_generate_returns_text(self):
        from brain.platform.gpu_client import GPUClient
        c = GPUClient()
        fake_response = MagicMock()
        fake_response.status_code = 200
        fake_response.json.return_value = {"text": "compressed output", "elapsed_ms": 100}
        with patch.object(c._session, "post", return_value=fake_response):
            result = c.generate("compress this text")
        assert result == "compressed output"

    def test_generate_with_fallback_header(self):
        from brain.platform.gpu_client import GPUClient
        c = GPUClient()
        fake_response = MagicMock()
        fake_response.status_code = 200
        fake_response.json.return_value = {"text": "result", "elapsed_ms": 50}

        with patch.object(c._session, "post", return_value=fake_response) as mock_post:
            c.generate("test", fallback_policy="auto")
            call_kwargs = mock_post.call_args
            assert call_kwargs[1]["headers"]["X-Fallback-Policy"] == "auto"
            assert call_kwargs[1]["json"]["think"] is False

    def test_load_worker_requests_model_load(self):
        from brain.platform.gpu_client import GPUClient
        c = GPUClient()
        fake_response = MagicMock()
        fake_response.status_code = 200
        fake_response.json.return_value = {"ok": True}

        with patch.object(c._session, "post", return_value=fake_response) as mock_post:
            assert c.load_worker("embedding", timeout_s=10) is True

        mock_post.assert_called_once_with("/models/embedding/load", timeout=15)

    def test_health(self):
        from brain.platform.gpu_client import GPUClient
        c = GPUClient()
        fake_response = MagicMock()
        fake_response.status_code = 200
        fake_response.json.return_value = {"status": "ok", "workers": {}}
        with patch.object(c._session, "get", return_value=fake_response):
            result = c.health()
        assert result["status"] == "ok"

    def test_is_ready(self):
        from brain.platform.gpu_client import GPUClient
        c = GPUClient()
        fake_response = MagicMock()
        fake_response.status_code = 200
        fake_response.json.return_value = {
            "status": "ok",
            "workers": {"embedding": {"status": "ready"}, "llm": {"status": "ready"}},
        }
        with patch.object(c._session, "get", return_value=fake_response):
            assert c.is_ready() is True
            assert c.is_ready("embedding") is True

    def test_is_ready_false_when_down(self):
        from brain.platform.gpu_client import GPUClient
        c = GPUClient()
        with patch.object(c._session, "get", side_effect=Exception("connection refused")):
            assert c.is_ready() is False

    def test_embed_raises_on_server_error(self):
        from brain.platform.gpu_client import GPUClient
        c = GPUClient()
        fake_response = MagicMock()
        fake_response.status_code = 503
        fake_response.json.return_value = {"error": "worker_unavailable", "message": "loading"}
        with patch.object(c._session, "post", return_value=fake_response):
            with pytest.raises(RuntimeError, match="worker_unavailable"):
                c.embed(["hello"])
