from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any

import httpx
from fastapi import HTTPException

from brain.kernel import config as cfg
from brain.platform.db.models.org import User
from brain.platform.db.repositories.unit_of_work import UnitOfWork

from .embedding_registry import (
    EMBEDDER_SPECS,
    default_embedding_model,
    embedder_options,
    embedding_dimensions,
    embedding_model_options,
    embedding_model_supported,
)
from .schemas import RuntimeMemoryCheckRead, RuntimeMemoryRead, RuntimeMemoryUpdate, RuntimeOption

logger = logging.getLogger(__name__)

ENV_PATH = Path(__file__).resolve().parents[3] / ".env"
EMBEDDING_MODEL_OPTIONS = embedding_model_options()
EMBEDDER_OPTIONS = embedder_options()
RERANKER_OPTIONS = [
    RuntimeOption(key="weighted", label="Built-in ranking", description="Use the current memory ranking stack."),
]


def _update_env_var(key: str, value: str | None) -> None:
    lines: list[str] = []
    if ENV_PATH.exists():
        lines = ENV_PATH.read_text().splitlines()

    written = False
    updated: list[str] = []
    for line in lines:
        if line.startswith(f"{key}="):
            if value is not None:
                updated.append(f"{key}={value}")
            written = True
        else:
            updated.append(line)
    if not written and value is not None:
        updated.append(f"{key}={value}")
    ENV_PATH.write_text("\n".join(updated) + ("\n" if updated else ""))


def _sync_gpu_embedding_worker(backend: str) -> None:
    if backend not in {"gpu", "cpu", "api"}:
        return
    unload_url = f"{cfg.GPU_SERVER_URL}/models/embedding/unload"
    load_url = f"{cfg.GPU_SERVER_URL}/models/embedding/load"
    try:
        httpx.post(unload_url, timeout=15)
        if backend == "gpu":
            httpx.post(load_url, timeout=120)
    except Exception as exc:  # pragma: no cover - worker may not be running locally.
        logger.info("Could not sync embedding worker after settings update: %s", exc)


def _apply_embedding_runtime_settings(updates: dict[str, str], *, sync_worker: bool = True) -> None:
    for key, value in updates.items():
        _update_env_var(key, value)
        os.environ[key] = value
        if key == "EMBEDDING_DIM":
            setattr(cfg, key, int(value))
        else:
            setattr(cfg, key, value)

    try:
        from brain.systems.memory import embeddings as emb_mod

        emb_mod._cpu_model = None
    except Exception:
        pass
    if sync_worker:
        _sync_gpu_embedding_worker(updates.get("EMBEDDING_BACKEND", str(cfg.EMBEDDING_BACKEND)))


def _standard_openai_api_key(token: str | None) -> str | None:
    value = (token or "").strip()
    if not value:
        return None
    if value.startswith("sk-"):
        return value
    try:
        from brain.platform.integrations.openai_codex_auth import parse_codex_auth_payload

        cred = parse_codex_auth_payload(value, source="runtime_memory")
        if cred.auth_mode == "api_key" and cred.access_token and cred.access_token.startswith("sk-"):
            return cred.access_token
    except Exception:
        return None
    return None


def _current_api_provider() -> str:
    return (os.getenv("EMBEDDING_API_PROVIDER") or cfg.EMBEDDING_API_PROVIDER or "gemini").lower()


def _installation_embedding_api_key(provider: str) -> str | None:
    provider = (provider or "").lower()
    key = ""
    if _current_api_provider() == provider:
        key = os.getenv("EMBEDDING_API_KEY") or cfg.EMBEDDING_API_KEY
    if not key and provider == "openai":
        key = os.getenv("OPENAI_API_KEY", "")
    elif not key and provider == "gemini":
        key = os.getenv("GEMINI_API_KEY", "") or os.getenv("GOOGLE_API_KEY", "")

    key = (key or "").strip()
    if not key:
        return None
    if provider == "openai":
        return _standard_openai_api_key(key)
    return key


def configure_openai_embedding_api_key(api_key: str) -> None:
    """Make a verified OpenAI API key available to the installation memory embedder."""
    key = _standard_openai_api_key(api_key)
    if not key:
        return
    _apply_embedding_runtime_settings(
        {
            "EMBEDDING_BACKEND": "api",
            "EMBEDDING_DIM": str(embedding_dimensions("openai", "text-embedding-3-small")),
            "EMBEDDING_API_PROVIDER": "openai",
            "EMBEDDING_API_MODEL": "text-embedding-3-small",
            "EMBEDDING_API_KEY": key,
        }
    )


def configure_gemini_embedding_api_key(api_key: str) -> None:
    """Make a Gemini API key available to the installation memory embedder."""
    key = (api_key or "").strip()
    if not key:
        return
    model = default_embedding_model("gemini") or "gemini-embedding-2"
    _apply_embedding_runtime_settings(
        {
            "EMBEDDING_BACKEND": "api",
            "EMBEDDING_DIM": str(embedding_dimensions("gemini", model)),
            "EMBEDDING_API_PROVIDER": "gemini",
            "EMBEDDING_API_MODEL": model,
            "EMBEDDING_API_KEY": key,
        }
    )


def _embedder_from_backend_provider(backend: str, provider: str | None) -> str:
    backend = (backend or "gpu").lower()
    provider = (provider or "").lower()
    if backend == "api":
        return "gemini" if provider in {"gemini", "google"} else "openai"
    if backend == "cpu":
        return "local_cpu"
    return "local_gpu"


def get_embedding_info(user: User | None = None) -> dict[str, Any]:
    backend = (os.getenv("EMBEDDING_BACKEND") or cfg.EMBEDDING_BACKEND or "gpu").lower()
    provider = (os.getenv("EMBEDDING_API_PROVIDER") or cfg.EMBEDDING_API_PROVIDER or "gemini").lower()
    api_model = os.getenv("EMBEDDING_API_MODEL") or cfg.EMBEDDING_API_MODEL
    cpu_model = os.getenv("EMBEDDING_CPU_MODEL") or cfg.EMBEDDING_CPU_MODEL
    dim = int(os.getenv("EMBEDDING_DIM") or cfg.EMBEDDING_DIM)
    api_key_set = bool(_installation_embedding_api_key(provider))
    info: dict[str, Any] = {
        "backend": backend,
        "provider": provider,
        "model": api_model if backend == "api" else cpu_model if backend == "cpu" else "local-gpu",
        "dimensions": dim,
        "api_key_set": api_key_set,
    }
    if backend == "gpu":
        try:
            from brain.platform.gpu_client import get_client

            health = get_client().health()
            info.update(
                {
                    "status": "ready" if health.get("ready") or health.get("status") == "ok" else "initializing",
                    "model": health.get("model") or info["model"],
                    "loaded": bool(health.get("loaded", health.get("ready", False))),
                }
            )
        except Exception:
            info.update({"status": "unavailable", "loaded": False})
    elif backend == "api":
        info["status"] = "ready" if info["api_key_set"] else "missing_key"
        info["loaded"] = info["api_key_set"]
    else:
        info["status"] = "ready"
        info["loaded"] = True
    return info


def _embedder_from_info(info: dict[str, Any]) -> str:
    return _embedder_from_backend_provider(
        str(info.get("backend") or "gpu"),
        str(info.get("provider") or ""),
    )


def _embedding_detail(info: dict[str, Any]) -> str | None:
    provider = str(info.get("provider") or "").lower()
    status = info.get("status")
    if status == "missing_key":
        if provider in {"gemini", "google"}:
            return "Gemini memory needs a Google AI Studio API key. Add a Gemini key in Access or choose Local CPU."
        return "OpenAI memory needs an OpenAI API key. Connect an API key in Access or choose Local CPU."
    if status == "unavailable":
        return "The local GPU embedding worker is not responding."
    return None


def _indexed_vector_count() -> int:
    try:
        from sqlalchemy import text

        with UnitOfWork() as uow:
            return int(
                uow.session.execute(
                    text(
                        """
                        SELECT
                          (SELECT count(*) FROM memories
                           WHERE semantic_embedding IS NOT NULL) +
                          (SELECT count(*) FROM memory_summaries
                           WHERE semantic_embedding IS NOT NULL) +
                          (SELECT count(*) FROM project_narratives
                           WHERE semantic_embedding IS NOT NULL) +
                          (SELECT count(*) FROM skills
                           WHERE embedding IS NOT NULL)
                        """
                    ),
                ).scalar()
                or 0
            )
    except Exception:
        logger.debug("Could not count indexed memory vectors", exc_info=True)
        return 0


def get_runtime_memory(user: User) -> RuntimeMemoryRead:
    info = get_embedding_info(user)
    reranker = os.getenv("MEMORY_RERANKER") or getattr(cfg, "MEMORY_RERANKER", "weighted") or "weighted"
    if reranker != "weighted":
        reranker = "weighted"
    embedder = _embedder_from_info(info)
    embedding_model = info.get("model")
    if not embedding_model_supported(embedder, str(embedding_model or "")):
        embedding_model = default_embedding_model(embedder)

    return RuntimeMemoryRead(
        scope="installation",
        embedder=embedder,
        embedding_model=embedding_model,
        embedding_dimensions=info.get("dimensions"),
        embedding_status=str(info.get("status") or "unknown"),
        embedding_detail=_embedding_detail(info),
        indexed_vectors=_indexed_vector_count(),
        api_key_statuses={
            "openai": bool(_installation_embedding_api_key("openai")),
            "gemini": bool(_installation_embedding_api_key("gemini")),
        },
        reranker=reranker,
        embedder_options=EMBEDDER_OPTIONS,
        embedding_model_options=EMBEDDING_MODEL_OPTIONS,
        reranker_options=RERANKER_OPTIONS,
    )


def update_runtime_memory(user: User, update: RuntimeMemoryUpdate) -> RuntimeMemoryRead:
    embedder = update.embedder
    embedder_spec = EMBEDDER_SPECS.get(embedder)
    if not embedder_spec:
        raise HTTPException(status_code=400, detail="Unsupported memory embedder")
    api_model = update.embedding_model or default_embedding_model(embedder)

    if not embedding_model_supported(embedder, api_model):
        provider_label = "Gemini" if embedder == "gemini" else "OpenAI"
        raise HTTPException(status_code=400, detail=f"Unsupported {provider_label} embedding model")

    current_info = get_embedding_info()
    current_embedder = _embedder_from_info(current_info)
    current_model = current_info.get("model")
    if not embedding_model_supported(current_embedder, str(current_model or "")):
        current_model = default_embedding_model(current_embedder)
    proposed_model = api_model if embedder_spec.backend == "api" else default_embedding_model(embedder)
    indexed_vectors = _indexed_vector_count()
    if indexed_vectors > 0 and (embedder != current_embedder or proposed_model != current_model):
        raise HTTPException(
            status_code=409,
            detail=(
                f"{indexed_vectors} installation memory vectors already exist. "
                "Rebuild the memory index before changing embedder or embedding model."
            ),
        )

    if embedder_spec.backend == "api":
        backend = "api"
        provider = embedder_spec.provider or "openai"
        cpu_model = os.getenv("EMBEDDING_CPU_MODEL") or cfg.EMBEDDING_CPU_MODEL
        model = api_model or embedder_spec.default_model
    elif embedder == "local_cpu":
        backend = "cpu"
        provider = os.getenv("EMBEDDING_API_PROVIDER") or cfg.EMBEDDING_API_PROVIDER
        model = os.getenv("EMBEDDING_API_MODEL") or cfg.EMBEDDING_API_MODEL
        cpu_model = os.getenv("EMBEDDING_CPU_MODEL") or cfg.EMBEDDING_CPU_MODEL
    else:
        backend = "gpu"
        provider = os.getenv("EMBEDDING_API_PROVIDER") or cfg.EMBEDDING_API_PROVIDER
        model = os.getenv("EMBEDDING_API_MODEL") or cfg.EMBEDDING_API_MODEL
        cpu_model = os.getenv("EMBEDDING_CPU_MODEL") or cfg.EMBEDDING_CPU_MODEL

    dimensions = embedding_dimensions(embedder, model)
    updates = {
        "EMBEDDING_BACKEND": backend,
        "EMBEDDING_DIM": str(dimensions),
        "EMBEDDING_API_PROVIDER": provider or "openai",
        "EMBEDDING_API_MODEL": model or "text-embedding-3-small",
        "EMBEDDING_CPU_MODEL": cpu_model or "all-MiniLM-L6-v2",
        "MEMORY_RERANKER": update.reranker,
    }
    if embedder_spec.backend == "api":
        api_key = _installation_embedding_api_key(provider)
        updates["EMBEDDING_API_KEY"] = api_key or ""
    _apply_embedding_runtime_settings(updates)

    return get_runtime_memory(user)


def check_runtime_memory(user: User) -> RuntimeMemoryCheckRead:
    started = time.perf_counter()
    try:
        from brain.systems.memory.embeddings import embed_query

        vector = embed_query("illo brain memory setup check")
        shape = getattr(vector, "shape", None)
        dimensions = int(shape[0] if shape else len(vector))
        expected = int(os.getenv("EMBEDDING_DIM") or cfg.EMBEDDING_DIM)
        duration_ms = int(round((time.perf_counter() - started) * 1000))
        if dimensions != expected:
            return RuntimeMemoryCheckRead(
                status="error",
                detail=f"Embedding returned {dimensions} dimensions, expected {expected}.",
                dimensions=dimensions,
                duration_ms=duration_ms,
            )
        return RuntimeMemoryCheckRead(
            status="ok",
            detail=f"Embedding check returned {dimensions} dimensions.",
            dimensions=dimensions,
            duration_ms=duration_ms,
        )
    except Exception as exc:
        detail = str(exc)
        provider = (os.getenv("EMBEDDING_API_PROVIDER") or cfg.EMBEDDING_API_PROVIDER or "").lower()
        if "EMBEDDING_API_KEY" in detail:
            if provider in {"gemini", "google"}:
                detail = "Gemini memory needs a Google AI Studio API key. Add a Gemini key in Access or choose Local CPU."
            elif provider == "openai":
                detail = "OpenAI memory needs an OpenAI API key. Connect an API key in Access or choose Local CPU."
        return RuntimeMemoryCheckRead(
            status="error",
            detail=detail,
            duration_ms=int(round((time.perf_counter() - started) * 1000)),
        )
