"""Illo Brain — embedding client.

Routes to the configured backend (gpu, cpu, or api).
All other modules import embedding functions from here.

Config (brain/kernel/config.py / env vars):
    EMBEDDING_BACKEND = "gpu" | "cpu" | "api"
    EMBEDDING_DIM     = vector dimensions (must match DB)
"""

from __future__ import annotations

import logging
import os
import threading
import time

import numpy as np

from brain.kernel.config import (
    EMBEDDING_BACKEND,
    EMBEDDING_DIM,
    MEMORY_EMOTIONAL_EMBEDDING_DIM,
)

logger = logging.getLogger("brain.systems.memory.embeddings")

# ---------------------------------------------------------------------------
# Vector helpers
# ---------------------------------------------------------------------------

def vec_to_pg(arr: np.ndarray) -> str:
    """Convert numpy array to pgvector string format."""
    return "[" + ",".join(f"{x:.8f}" for x in arr.flat) + "]"


# ---------------------------------------------------------------------------
# Backend: GPU server
# ---------------------------------------------------------------------------

def _embed_gpu(texts: list[str], mode: str) -> np.ndarray:
    from brain.platform.gpu_client import get_client
    client = get_client()
    try:
        return client.embed(texts, mode=mode)
    except RuntimeError as exc:
        if _is_embedding_worker_unavailable_error(exc):
            recovery_request_failed = False
            if _embedding_worker_needs_load_request(exc):
                restart_timeout = _embedding_restart_timeout_seconds()
                try:
                    logger.info(
                        "Embedding worker is unavailable; requesting GPU server recovery (timeout=%.1fs)",
                        restart_timeout,
                    )
                    recovery_request_failed = not client.load_worker("embedding", timeout_s=restart_timeout)
                except Exception as load_exc:
                    recovery_request_failed = True
                    logger.warning("Embedding worker recovery request failed: %s", load_exc)
            if recovery_request_failed:
                raise

            wait_seconds = _embedding_warmup_wait_seconds()
            if wait_seconds > 0:
                logger.info(
                    "Embedding worker is warming/recovering; waiting up to %.1fs before retrying %s embedding request",
                    wait_seconds,
                    mode,
                )
                if wait_for_embedding_backend_ready(
                    timeout_s=wait_seconds,
                    poll_s=_embedding_warmup_poll_seconds(),
                    client=client,
                ):
                    logger.info("Embedding worker became ready; retrying %s embedding request", mode)
                    return client.embed(texts, mode=mode)
        raise


def _is_embedding_worker_unavailable_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return (
        "worker_unavailable" in message
        and "embedding" in message
        and any(state in message for state in ("loading", "failed", "stopped", "registered", "unloading"))
    )


def _is_embedding_worker_loading_error(exc: Exception) -> bool:
    """Backward-compatible name for older tests/callers."""
    return _is_embedding_worker_unavailable_error(exc)


def _embedding_worker_needs_load_request(exc: Exception) -> bool:
    message = str(exc).lower()
    return any(state in message for state in ("failed", "stopped", "registered"))


def _embedding_warmup_wait_seconds() -> float:
    try:
        return max(0.0, float(os.getenv("EMBEDDING_WORKER_WARMUP_WAIT_SECONDS", "180")))
    except Exception:
        return 180.0


def _embedding_restart_timeout_seconds() -> float:
    try:
        return max(1.0, float(os.getenv("EMBEDDING_WORKER_RESTART_TIMEOUT_SECONDS", "300")))
    except Exception:
        return 300.0


def _embedding_warmup_poll_seconds() -> float:
    try:
        return max(0.05, float(os.getenv("EMBEDDING_WORKER_WARMUP_POLL_SECONDS", "0.5")))
    except Exception:
        return 0.5


def _wait_for_gpu_worker_ready(client, worker_name: str, *, timeout_s: float, poll_s: float | None = None) -> bool:
    deadline = time.monotonic() + timeout_s
    poll_s = poll_s or _embedding_warmup_poll_seconds()

    while time.monotonic() < deadline:
        try:
            if client.is_ready(worker_name):
                return True
        except Exception:
            pass
        time.sleep(poll_s)

    try:
        return client.is_ready(worker_name)
    except Exception:
        return False


def wait_for_embedding_backend_ready(
    *,
    timeout_s: float | None = None,
    poll_s: float | None = None,
    client=None,
) -> bool:
    """Block until the embedding backend is ready, or timeout.

    Returns True when embeddings are ready to serve requests. For non-GPU
    backends, this is always True because those backends are loaded inline.
    """
    import brain.kernel.config as _cfg

    if _cfg.EMBEDDING_BACKEND != "gpu":
        return True

    if client is None:
        from brain.platform.gpu_client import get_client
        client = get_client()

    return _wait_for_gpu_worker_ready(
        client,
        "embedding",
        timeout_s=_embedding_warmup_wait_seconds() if timeout_s is None else timeout_s,
        poll_s=poll_s,
    )


# ---------------------------------------------------------------------------
# Backend: CPU (sentence-transformers)
# ---------------------------------------------------------------------------

_cpu_model = None
_cpu_lock = threading.Lock()


def _get_cpu_model():
    global _cpu_model
    if _cpu_model is None:
        with _cpu_lock:
            if _cpu_model is None:
                from sentence_transformers import SentenceTransformer
                from brain.kernel.config import EMBEDDING_CPU_MODEL
                logger.info("Loading CPU embedding model: %s", EMBEDDING_CPU_MODEL)
                _cpu_model = SentenceTransformer(EMBEDDING_CPU_MODEL)
    return _cpu_model


def _embed_cpu(texts: list[str], mode: str) -> np.ndarray:
    model = _get_cpu_model()
    prefix = "search_query: " if mode == "query" else "search_document: "
    # MiniLM doesn't use prefixes, but larger models (e5, gte) do
    if "e5" in model.get_config_dict().get("model_name_or_path", "").lower():
        texts = [prefix + t for t in texts]
    return np.array(model.encode(texts, normalize_embeddings=True), dtype=np.float32)


# ---------------------------------------------------------------------------
# Backend: API (Gemini or OpenAI)
# ---------------------------------------------------------------------------

def _prepare_gemini_text(text: str, mode: str, model: str) -> str:
    if model != "gemini-embedding-2":
        return text
    if mode == "query":
        return f"task: search result | query: {text}"
    return f"title: none | text: {text}"


def _embed_api_gemini(texts: list[str], mode: str) -> np.ndarray:
    import httpx
    from brain.kernel.config import EMBEDDING_API_KEY, EMBEDDING_API_MODEL

    if not EMBEDDING_API_KEY:
        raise RuntimeError(
            "Gemini embedding credentials are not configured. "
            "Add them in System/Access or use a development-only EMBEDDING_API_KEY."
        )

    model = EMBEDDING_API_MODEL or "gemini-embedding-2"
    task_type = "RETRIEVAL_QUERY" if mode == "query" else "RETRIEVAL_DOCUMENT"
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:embedContent"

    embeddings = []
    for text in texts:
        payload = {
            "content": {"parts": [{"text": _prepare_gemini_text(text, mode, model)}]},
            "output_dimensionality": EMBEDDING_DIM,
        }
        if model != "gemini-embedding-2":
            payload["taskType"] = task_type
        resp = httpx.post(
            url,
            headers={
                "Content-Type": "application/json",
                "x-goog-api-key": EMBEDDING_API_KEY,
            },
            json=payload,
            timeout=30,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"Gemini embedding API error {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        values = data.get("embedding", {}).get("values", [])
        if not values:
            raise RuntimeError(f"Gemini returned empty embedding: {resp.text[:200]}")
        embeddings.append(values)

    return np.array(embeddings, dtype=np.float32)


def _embed_api_openai(texts: list[str], mode: str) -> np.ndarray:
    import httpx
    from brain.kernel.config import EMBEDDING_API_KEY, EMBEDDING_API_MODEL

    api_key = EMBEDDING_API_KEY or ""
    if not api_key:
        raise RuntimeError(
            "OpenAI embedding credentials are not configured. "
            "Add them in System/Access or use a development-only EMBEDDING_API_KEY."
        )

    model = EMBEDDING_API_MODEL or "text-embedding-3-small"
    resp = httpx.post(
        "https://api.openai.com/v1/embeddings",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        json={"model": model, "input": texts, "dimensions": EMBEDDING_DIM},
        timeout=30,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"OpenAI embedding API error {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    return np.array([item["embedding"] for item in data["data"]], dtype=np.float32)


def _embed_api(texts: list[str], mode: str) -> np.ndarray:
    from brain.kernel.config import EMBEDDING_API_PROVIDER
    if EMBEDDING_API_PROVIDER == "openai":
        return _embed_api_openai(texts, mode)
    return _embed_api_gemini(texts, mode)


# ---------------------------------------------------------------------------
# Public API — routes to configured backend
# ---------------------------------------------------------------------------

_BACKENDS = {
    "gpu": _embed_gpu,
    "cpu": _embed_cpu,
    "api": _embed_api,
}


def embed_batch(texts: list[str], mode: str = "document") -> np.ndarray:
    """Embed multiple texts. Returns (N, dims) numpy array."""
    # Read backend dynamically so hot-reload from settings works
    import brain.kernel.config as _cfg
    backend = _cfg.EMBEDDING_BACKEND
    fn = _BACKENDS.get(backend)
    if not fn:
        raise ValueError(f"Unknown EMBEDDING_BACKEND: {backend!r}. Use gpu, cpu, or api.")
    return fn(texts, mode)


def embed_document(text: str) -> np.ndarray:
    """Embed a document (memory content)."""
    return embed_batch([text], mode="document")[0]


def embed_query(text: str) -> np.ndarray:
    """Embed a query (with retrieval instruction)."""
    return embed_batch([text], mode="query")[0]


# ---------------------------------------------------------------------------
# Emotional embeddings (32-dim compact representation)
# ---------------------------------------------------------------------------

EMOTION_MAP = {
    "frustrated":   (-0.8, 0.7, 0.3),
    "angry":        (-0.9, 0.9, 0.6),
    "disappointed": (-0.6, 0.3, 0.2),
    "confused":     (-0.3, 0.5, 0.2),
    "anxious":      (-0.5, 0.8, 0.2),
    "neutral":      ( 0.0, 0.2, 0.5),
    "curious":      ( 0.3, 0.6, 0.5),
    "satisfied":    ( 0.6, 0.3, 0.6),
    "happy":        ( 0.8, 0.6, 0.6),
    "excited":      ( 0.7, 0.9, 0.7),
    "proud":        ( 0.8, 0.5, 0.8),
    "urgent":       (-0.2, 0.9, 0.4),
    "relieved":     ( 0.5, 0.2, 0.5),
    "impressed":    ( 0.7, 0.6, 0.4),
    "teaching":     ( 0.4, 0.4, 0.7),
    "directing":    ( 0.2, 0.5, 0.6),
    "encouraging":  ( 0.6, 0.5, 0.7),
    "delegating":   ( 0.5, 0.3, 0.6),
}

_EMOTION_CATEGORIES = {
    "negative_high": ["frustrated", "angry", "anxious", "urgent"],
    "negative_low":  ["disappointed", "confused"],
    "neutral":       ["neutral", "curious", "directing"],
    "positive_low":  ["satisfied", "relieved", "delegating"],
    "positive_high": ["happy", "excited", "proud", "impressed", "teaching", "encouraging"],
}


def make_emotional_embedding(
    valence: float = 0.0, arousal: float = 0.0, label: str = "neutral",
) -> np.ndarray:
    """Create a fixed-dimension emotional embedding from valence/arousal/label."""
    vec = np.zeros(MEMORY_EMOTIONAL_EMBEDDING_DIM, dtype=np.float32)

    base = EMOTION_MAP.get(label, (valence, arousal, 0.5))
    vec[0], vec[1], vec[2] = base

    for i, (_, labels) in enumerate(_EMOTION_CATEGORIES.items()):
        if label in labels:
            vec[3 + i] = 1.0

    rng = np.random.RandomState(hash(label) % 2**31)
    vec[8:] = rng.randn(MEMORY_EMOTIONAL_EMBEDDING_DIM - 8) * 0.3
    vec[8:] += vec[0] * 0.2

    norm = np.linalg.norm(vec)
    if norm > 0:
        vec /= norm

    return vec


# ---------------------------------------------------------------------------
# Server health (for status reporting)
# ---------------------------------------------------------------------------

def server_health() -> dict | None:
    """Return embedding backend health info."""
    try:
        if EMBEDDING_BACKEND == "gpu":
            from brain.platform.gpu_client import get_client
            return get_client().health()
        elif EMBEDDING_BACKEND == "cpu":
            model = _get_cpu_model()
            return {"status": "ok", "backend": "cpu", "model": model.get_config_dict().get("model_name_or_path", "?")}
        elif EMBEDDING_BACKEND == "api":
            from brain.kernel.config import EMBEDDING_API_PROVIDER, EMBEDDING_API_MODEL
            return {"status": "ok", "backend": "api", "provider": EMBEDDING_API_PROVIDER, "model": EMBEDDING_API_MODEL}
    except Exception:
        return None
