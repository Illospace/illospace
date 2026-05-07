"""Integration tests for the unified GPU server stack.

Tests the full request path: GPUClient → GPU Server → Worker Manager → Mock Worker.
Does NOT require a GPU — workers are mocked.
"""

import json
import threading
import time

import pytest
import numpy as np
from unittest.mock import patch, MagicMock

from brain.platform.gpu.config import ServerConfig, WorkerManifest
from brain.platform.gpu.server import GPUServer, RequestHandler
from http.server import HTTPServer


@pytest.fixture
def gpu_server():
    """Start a GPU server on a random port with no real workers."""
    config = ServerConfig(port=0)  # port 0 = OS assigns random port
    srv = GPUServer(config)

    for m in [
        WorkerManifest(name="embedding", model_path="/tmp", vram_mb=5000, worker_module="brain.platform.gpu.workers.embedding"),
        WorkerManifest(name="llm", model_path="/tmp", vram_mb=3000, worker_module="brain.platform.gpu.workers.llm"),
    ]:
        srv.manager.register(m)

    RequestHandler.server_instance = srv
    httpd = HTTPServer(("127.0.0.1", 0), RequestHandler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield srv, f"http://127.0.0.1:{port}"
    httpd.shutdown()


class TestHealthEndpoint:
    def test_health_returns_json(self, gpu_server):
        _, base_url = gpu_server
        import urllib.request
        resp = urllib.request.urlopen(f"{base_url}/health", timeout=2)
        data = json.loads(resp.read())
        assert "status" in data
        assert "workers" in data
        assert "embedding" in data["workers"]

    def test_health_shows_down_when_no_workers_running(self, gpu_server):
        _, base_url = gpu_server
        import urllib.request
        resp = urllib.request.urlopen(f"{base_url}/health", timeout=2)
        data = json.loads(resp.read())
        assert data["status"] == "down"


class TestModelsEndpoint:
    def test_models_list(self, gpu_server):
        _, base_url = gpu_server
        import urllib.request
        resp = urllib.request.urlopen(f"{base_url}/models", timeout=2)
        data = json.loads(resp.read())
        names = [m["name"] for m in data]
        assert "embedding" in names
        assert "llm" in names


class TestEmbedEndpointWithoutWorker:
    def test_embed_returns_503_when_worker_not_ready(self, gpu_server):
        _, base_url = gpu_server
        import urllib.request
        req = urllib.request.Request(
            f"{base_url}/embed",
            data=json.dumps({"texts": ["hello"], "mode": "document"}).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            urllib.request.urlopen(req, timeout=5)
            assert False, "Should have raised"
        except urllib.error.HTTPError as e:
            assert e.code == 503


class TestGPUClientIntegration:
    def test_client_health_against_real_server(self, gpu_server):
        _, base_url = gpu_server
        from brain.platform.gpu_client import GPUClient
        client = GPUClient(base_url=base_url)
        health = client.health()
        assert health["status"] in ("ok", "degraded", "down")
