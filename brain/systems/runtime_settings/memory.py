from __future__ import annotations

import base64
import json
import logging
import os
import time
from typing import Any

import httpx
from fastapi import HTTPException
from sqlalchemy import select

from brain.kernel import config as cfg
from brain.platform.db.models.org import User
from brain.platform.db.models.vault import VaultConfig
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

RUNTIME_MEMORY_SETTINGS_KEY = "runtime_memory"
RUNTIME_MEMORY_SECRET_PREFIX = "runtime_memory_key_"
RUNTIME_SETTINGS_UNAVAILABLE_DETAIL = (
    "Runtime memory settings could not be saved. Check that Postgres is available and try again."
)
VAULT_NOT_CONFIGURED_DETAIL = (
    "Vault master key is not configured. Set VAULT_MASTER_KEY before saving memory API keys."
)
EMBEDDING_MODEL_OPTIONS = embedding_model_options()
EMBEDDER_OPTIONS = embedder_options()
RERANKER_OPTIONS = [
    RuntimeOption(key="weighted", label="Built-in ranking", description="Use the current memory ranking stack."),
]


def _provider_key(provider: str | None) -> str:
    provider = (provider or "").strip().lower()
    if provider in {"google", "gemini"}:
        return "gemini"
    if provider == "openai":
        return "openai"
    return provider


def _runtime_secret_config_key(provider: str | None) -> str:
    return f"{RUNTIME_MEMORY_SECRET_PREFIX}{_provider_key(provider)}"


def _read_runtime_config_value(key: str) -> str | None:
    try:
        with UnitOfWork() as uow:
            config = uow.session.scalars(select(VaultConfig).where(VaultConfig.key == key)).first()
            return config.value if config else None
    except Exception:
        logger.debug("Could not read runtime memory config key %s", key, exc_info=True)
        return None


def _write_runtime_config_value(key: str, value: str) -> None:
    try:
        with UnitOfWork() as uow:
            config = uow.session.scalars(select(VaultConfig).where(VaultConfig.key == key)).first()
            if config:
                config.value = value
            else:
                uow.session.add(VaultConfig(key=key, value=value))
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("Could not persist runtime memory config key %s: %s", key, exc)
        raise HTTPException(status_code=503, detail=RUNTIME_SETTINGS_UNAVAILABLE_DETAIL) from exc


def _read_persisted_runtime_settings() -> dict[str, str]:
    raw = _read_runtime_config_value(RUNTIME_MEMORY_SETTINGS_KEY)
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("Ignoring invalid runtime memory settings JSON")
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(key): str(value) for key, value in data.items() if value is not None}


def _persist_runtime_settings(updates: dict[str, str]) -> None:
    current = _read_persisted_runtime_settings()
    current.update({key: str(value) for key, value in updates.items() if value is not None})
    _write_runtime_config_value(RUNTIME_MEMORY_SETTINGS_KEY, json.dumps(current, sort_keys=True))


def _persist_embedding_api_key(provider: str, api_key: str) -> None:
    if not api_key:
        return
    try:
        from brain.systems.vault import _encrypt

        encrypted = base64.b64encode(_encrypt(api_key)).decode("ascii")
    except RuntimeError as exc:
        if "VAULT_MASTER_KEY is required" in str(exc):
            raise HTTPException(status_code=503, detail=VAULT_NOT_CONFIGURED_DETAIL) from exc
        raise
    _write_runtime_config_value(_runtime_secret_config_key(provider), encrypted)


def _read_persisted_embedding_api_key(provider: str) -> str | None:
    raw = _read_runtime_config_value(_runtime_secret_config_key(provider))
    if not raw:
        return None
    try:
        from brain.systems.vault import _decrypt

        return _decrypt(base64.b64decode(raw.encode("ascii"))).strip() or None
    except RuntimeError as exc:
        if "VAULT_MASTER_KEY is required" in str(exc):
            logger.warning("Memory API key is encrypted, but VAULT_MASTER_KEY is not configured")
            return None
        raise
    except Exception:
        logger.warning("Could not decrypt persisted %s memory API key", provider, exc_info=True)
        return None


def _runtime_setting(key: str, persisted: dict[str, str], default: Any) -> str:
    value = persisted.get(key)
    if value not in (None, ""):
        return str(value)
    if default not in (None, ""):
        return str(default)
    return os.getenv(key, "")


def get_effective_embedding_runtime_config() -> dict[str, str]:
    """Return durable runtime memory settings with env/config fallbacks.

    This is intentionally read dynamically by embedding callers so API, worker,
    and scheduler processes observe settings stored through the UI without
    mutating the application checkout or relying on one process' environment.
    """
    persisted = _read_persisted_runtime_settings()
    backend = _runtime_setting("EMBEDDING_BACKEND", persisted, cfg.EMBEDDING_BACKEND).lower()
    provider = _provider_key(_runtime_setting("EMBEDDING_API_PROVIDER", persisted, cfg.EMBEDDING_API_PROVIDER))
    api_model = _runtime_setting("EMBEDDING_API_MODEL", persisted, cfg.EMBEDDING_API_MODEL)
    cpu_model = _runtime_setting("EMBEDDING_CPU_MODEL", persisted, cfg.EMBEDDING_CPU_MODEL)
    dim = _runtime_setting("EMBEDDING_DIM", persisted, cfg.EMBEDDING_DIM)
    reranker = _runtime_setting("MEMORY_RERANKER", persisted, getattr(cfg, "MEMORY_RERANKER", "weighted"))
    api_key = _installation_embedding_api_key(provider, persisted=persisted)
    return {
        "EMBEDDING_BACKEND": backend,
        "EMBEDDING_API_PROVIDER": provider,
        "EMBEDDING_API_MODEL": api_model,
        "EMBEDDING_CPU_MODEL": cpu_model,
        "EMBEDDING_DIM": str(dim),
        "EMBEDDING_API_KEY": api_key or "",
        "MEMORY_RERANKER": reranker,
    }


def _sync_process_embedding_config(runtime: dict[str, str]) -> None:
    """Keep legacy cfg readers in this process aligned with DB-backed settings."""
    semantic_dim = int(runtime["EMBEDDING_DIM"])
    cfg.EMBEDDING_BACKEND = runtime["EMBEDDING_BACKEND"]
    cfg.EMBEDDING_API_PROVIDER = runtime["EMBEDDING_API_PROVIDER"]
    cfg.EMBEDDING_API_MODEL = runtime["EMBEDDING_API_MODEL"]
    cfg.EMBEDDING_CPU_MODEL = runtime["EMBEDDING_CPU_MODEL"]
    cfg.EMBEDDING_API_KEY = runtime.get("EMBEDDING_API_KEY", "")
    cfg.EMBEDDING_DIM = semantic_dim
    cfg.MEMORY_RERANKER = runtime["MEMORY_RERANKER"]
    cfg.MEMORY_SEMANTIC_EMBEDDING_DIM = semantic_dim
    cfg.SUMMARY_SEMANTIC_EMBEDDING_DIM = semantic_dim
    cfg.NARRATIVE_SEMANTIC_EMBEDDING_DIM = semantic_dim
    cfg.SKILL_SEMANTIC_EMBEDDING_DIM = semantic_dim
    cfg.SKILL_TASK_CENTROID_EMBEDDING_DIM = semantic_dim


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
    updates = dict(updates)
    secret = updates.pop("EMBEDDING_API_KEY", None)
    provider = _provider_key(updates.get("EMBEDDING_API_PROVIDER") or _current_api_provider())
    if secret:
        _persist_embedding_api_key(provider, secret)
    _persist_runtime_settings(updates)
    runtime = get_effective_embedding_runtime_config()
    _sync_process_embedding_config(runtime)

    try:
        from brain.systems.memory import embeddings as emb_mod

        emb_mod._cpu_model = None
        emb_mod._cpu_model_name = None
    except Exception:
        pass
    if sync_worker:
        _sync_gpu_embedding_worker(runtime.get("EMBEDDING_BACKEND", str(cfg.EMBEDDING_BACKEND)))


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
    persisted = _read_persisted_runtime_settings()
    return _provider_key(_runtime_setting("EMBEDDING_API_PROVIDER", persisted, cfg.EMBEDDING_API_PROVIDER or "gemini"))


def _installation_embedding_api_key(provider: str, *, persisted: dict[str, str] | None = None) -> str | None:
    provider = _provider_key(provider)
    key = _read_persisted_embedding_api_key(provider) or ""
    if not key:
        persisted = persisted if persisted is not None else _read_persisted_runtime_settings()
        if _provider_key(_runtime_setting("EMBEDDING_API_PROVIDER", persisted, cfg.EMBEDDING_API_PROVIDER)) == provider:
            key = os.getenv("EMBEDDING_API_KEY", "")
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
    runtime = get_effective_embedding_runtime_config()
    backend = runtime["EMBEDDING_BACKEND"].lower()
    provider = runtime["EMBEDDING_API_PROVIDER"].lower()
    api_model = runtime["EMBEDDING_API_MODEL"]
    cpu_model = runtime["EMBEDDING_CPU_MODEL"]
    dim = int(runtime["EMBEDDING_DIM"])
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
    runtime = get_effective_embedding_runtime_config()
    reranker = runtime["MEMORY_RERANKER"] or "weighted"
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

    runtime = get_effective_embedding_runtime_config()
    if embedder_spec.backend == "api":
        backend = "api"
        provider = embedder_spec.provider or "openai"
        cpu_model = runtime["EMBEDDING_CPU_MODEL"]
        model = api_model or embedder_spec.default_model
    elif embedder == "local_cpu":
        backend = "cpu"
        provider = runtime["EMBEDDING_API_PROVIDER"]
        model = runtime["EMBEDDING_API_MODEL"]
        cpu_model = runtime["EMBEDDING_CPU_MODEL"]
    else:
        backend = "gpu"
        provider = runtime["EMBEDDING_API_PROVIDER"]
        model = runtime["EMBEDDING_API_MODEL"]
        cpu_model = runtime["EMBEDDING_CPU_MODEL"]

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
        expected = int(get_effective_embedding_runtime_config()["EMBEDDING_DIM"])
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
        provider = get_effective_embedding_runtime_config()["EMBEDDING_API_PROVIDER"].lower()
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
