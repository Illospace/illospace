"""Thin HTTP client for the unified GPU server.

Replaces all embed server lifecycle management and Ollama coordination.
Brain modules call this instead of managing GPU processes directly.
"""

import os
import logging

import httpx
import numpy as np

logger = logging.getLogger("brain.platform.gpu_client")

_GPU_SERVER_URL = os.environ.get("GPU_SERVER_URL", "http://127.0.0.1:9800")


class GPUClient:
    """Client for the unified GPU model server."""

    def __init__(self, base_url: str = _GPU_SERVER_URL):
        self.base_url = base_url
        self._session = httpx.Client(
            base_url=base_url,
            timeout=60.0,
            headers={"Content-Type": "application/json"},
        )

    def embed(self, texts: list[str], mode: str = "document") -> np.ndarray:
        """Embed texts. Returns (N, dims) numpy array."""
        resp = self._session.post("/embed", json={"texts": texts, "mode": mode})
        if resp.status_code != 200:
            data = resp.json()
            raise RuntimeError(f"{data.get('error', 'unknown')}: {data.get('message', '')}")
        return np.array(resp.json()["embeddings"], dtype=np.float32)

    def embed_one(self, text: str, mode: str = "document") -> np.ndarray:
        """Embed a single text. Returns (dims,) numpy array."""
        return self.embed([text], mode=mode)[0]

    def generate(self, prompt: str, fallback_policy: str = "local-only", **kwargs) -> str:
        """Generate text with the local LLM. Returns the generated string."""
        headers = {"X-Fallback-Policy": fallback_policy}
        kwargs.setdefault("think", False)
        payload = {"prompt": prompt, **kwargs}
        resp = self._session.post("/generate", json=payload, headers=headers)
        if resp.status_code != 200:
            data = resp.json()
            raise RuntimeError(f"{data.get('error', 'unknown')}: {data.get('message', '')}")
        return resp.json()["text"]

    def load_worker(self, worker: str, timeout_s: float = 300.0) -> bool:
        """Ask the GPU server to load or recover a worker."""
        resp = self._session.post(f"/models/{worker}/load", timeout=timeout_s + 5)
        if resp.status_code != 200:
            data = resp.json()
            raise RuntimeError(f"{data.get('error', 'unknown')}: {data.get('message', '')}")
        return bool(resp.json().get("ok"))

    def health(self) -> dict:
        """Get server health info."""
        resp = self._session.get("/health")
        return resp.json()

    def is_ready(self, worker: str | None = None) -> bool:
        """Check if server (or specific worker) is ready."""
        try:
            h = self.health()
            if worker:
                return h.get("workers", {}).get(worker, {}).get("status") == "ready"
            return h.get("status") in ("ok", "degraded")
        except Exception:
            return False


_client: GPUClient | None = None


def get_client() -> GPUClient:
    """Get or create the module-level GPU client singleton."""
    global _client
    if _client is None:
        _client = GPUClient()
    return _client
