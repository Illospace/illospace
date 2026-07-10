"""Illo Brain — embedding client.

Routes to the configured backend (gpu, cpu, or api).
All other modules import embedding functions from here.

Runtime config is DB-backed once an admin saves memory settings. Process
configuration only seeds first-boot non-secret defaults.
"""

from __future__ import annotations

import logging
import os
import threading
import time

import numpy as np

from brain.platform.async_io import http_post

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
    if _runtime_embedding_config().backend != "gpu":
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
_cpu_model_name = None
_cpu_lock = threading.Lock()


def _runtime_embedding_config(runtime_config=None):
    if runtime_config is not None:
        return runtime_config
    try:
        from brain.systems.runtime_settings.memory import get_embedding_runtime_config

        return get_embedding_runtime_config()
    except Exception:
        logger.debug("Could not load DB-backed embedding runtime config; using process config", exc_info=True)
        import brain.kernel.config as _cfg
        from brain.systems.runtime_settings.memory import EmbeddingRuntimeConfig

        return EmbeddingRuntimeConfig(
            backend=str(_cfg.EMBEDDING_BACKEND),
            provider=str(_cfg.EMBEDDING_API_PROVIDER),
            api_model=str(_cfg.EMBEDDING_API_MODEL),
            cpu_model=str(_cfg.EMBEDDING_CPU_MODEL),
            dimensions=int(_cfg.EMBEDDING_DIM),
            reranker=str(getattr(_cfg, "MEMORY_RERANKER", "weighted")),
            api_key="",
        )


def _get_cpu_model(runtime_config=None):
    global _cpu_model, _cpu_model_name
    model_name = _runtime_embedding_config(runtime_config).cpu_model
    if _cpu_model is None or _cpu_model_name != model_name:
        with _cpu_lock:
            if _cpu_model is None or _cpu_model_name != model_name:
                from sentence_transformers import SentenceTransformer
                logger.info("Loading CPU embedding model: %s", model_name)
                _cpu_model = SentenceTransformer(model_name)
                _cpu_model_name = model_name
    return _cpu_model


def _embed_cpu(texts: list[str], mode: str, runtime_config=None) -> np.ndarray:
    model = _get_cpu_model(runtime_config)
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


def _embed_api_gemini(texts: list[str], mode: str, runtime_config=None) -> np.ndarray:
    runtime = _runtime_embedding_config(runtime_config)
    api_key = runtime.api_key
    embedding_dim = runtime.dimensions

    if not api_key:
        raise RuntimeError(
            "Gemini embedding credentials are not configured. "
            "Add them in System/Access."
        )

    model = runtime.api_model or "gemini-embedding-2"
    task_type = "RETRIEVAL_QUERY" if mode == "query" else "RETRIEVAL_DOCUMENT"
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:embedContent"

    embeddings = []
    for text in texts:
        payload = {
            "content": {"parts": [{"text": _prepare_gemini_text(text, mode, model)}]},
            "output_dimensionality": embedding_dim,
        }
        if model != "gemini-embedding-2":
            payload["taskType"] = task_type
        resp = http_post(
            url,
            headers={
                "Content-Type": "application/json",
                "x-goog-api-key": api_key,
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


def _embed_api_openai(texts: list[str], mode: str, runtime_config=None) -> np.ndarray:
    runtime = _runtime_embedding_config(runtime_config)

    api_key = runtime.api_key or ""
    if not api_key:
        raise RuntimeError(
            "OpenAI embedding credentials are not configured. "
            "Add them in System/Access."
        )

    model = runtime.api_model or "text-embedding-3-small"
    resp = http_post(
        "https://api.openai.com/v1/embeddings",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        json={"model": model, "input": texts, "dimensions": runtime.dimensions},
        timeout=30,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"OpenAI embedding API error {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    return np.array([item["embedding"] for item in data["data"]], dtype=np.float32)


def _embed_api(texts: list[str], mode: str, runtime_config=None) -> np.ndarray:
    runtime = _runtime_embedding_config(runtime_config)
    if runtime.provider == "openai":
        return _embed_api_openai(texts, mode, runtime)
    return _embed_api_gemini(texts, mode, runtime)


# ---------------------------------------------------------------------------
# Public API — routes to configured backend
# ---------------------------------------------------------------------------

_BACKENDS = {
    "gpu": _embed_gpu,
    "cpu": _embed_cpu,
    "api": _embed_api,
}


def embed_batch(texts: list[str], mode: str = "document", runtime_config=None) -> np.ndarray:
    """Embed multiple texts. Returns (N, dims) numpy array."""
    runtime = _runtime_embedding_config(runtime_config)
    backend = runtime.backend
    fn = _BACKENDS.get(backend)
    if not fn:
        raise ValueError(f"Unknown EMBEDDING_BACKEND: {backend!r}. Use gpu, cpu, or api.")
    if backend in {"cpu", "api"}:
        return fn(texts, mode, runtime)
    return fn(texts, mode)


def embed_document(text: str, runtime_config=None) -> np.ndarray:
    """Embed a document (memory content)."""
    return embed_batch([text], mode="document", runtime_config=runtime_config)[0]


def embed_query(text: str, runtime_config=None) -> np.ndarray:
    """Embed a query (with retrieval instruction)."""
    return embed_batch([text], mode="query", runtime_config=runtime_config)[0]


# ---------------------------------------------------------------------------
# Server health (for status reporting)
# ---------------------------------------------------------------------------

def server_health() -> dict | None:
    """Return embedding backend health info."""
    try:
        runtime = _runtime_embedding_config()
        backend = runtime.backend
        if backend == "gpu":
            from brain.platform.gpu_client import get_client
            return get_client().health()
        elif backend == "cpu":
            model = _get_cpu_model()
            return {"status": "ok", "backend": "cpu", "model": model.get_config_dict().get("model_name_or_path", "?")}
        elif backend == "api":
            return {
                "status": "ok",
                "backend": "api",
                "provider": runtime.provider,
                "model": runtime.api_model,
            }
    except Exception:
        return None
