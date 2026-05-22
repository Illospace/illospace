from __future__ import annotations

import base64
import json
import logging
import time
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from brain.kernel import config as cfg
from brain.platform.async_io import http_get, http_post
from brain.platform.db.models.org import User
from brain.platform.db.models.vault import VaultConfig

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
RUNTIME_MEMORY_SECRET_PREFIX = "runtime_memory_api_key_"
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
VALID_EMBEDDING_BACKENDS = {"api", "cpu", "gpu"}
GPU_EMBEDDING_INFO_TIMEOUT_SECONDS = 1.0


@dataclass(frozen=True)
class EmbeddingRuntimeConfig:
    backend: str
    provider: str
    api_model: str
    cpu_model: str
    dimensions: int
    reranker: str = "weighted"
    api_key: str = ""

    def stored_settings(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "provider": self.provider,
            "api_model": self.api_model,
            "cpu_model": self.cpu_model,
            "dimensions": self.dimensions,
            "reranker": self.reranker,
        }


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
    logger.debug("Sync runtime memory config read skipped for key %s; use async runtime settings APIs.", key)
    return None


async def _async_read_runtime_config_value(session: AsyncSession, key: str) -> str | None:
    try:
        config = (await session.scalars(select(VaultConfig).where(VaultConfig.key == key))).first()
        return config.value if config else None
    except Exception:
        logger.debug("Could not read runtime memory config key %s", key, exc_info=True)
        return None


def _write_runtime_config_value(key: str, value: str) -> None:
    logger.warning("Sync runtime memory config write skipped for key %s; use async runtime settings APIs.", key)
    raise HTTPException(status_code=503, detail=RUNTIME_SETTINGS_UNAVAILABLE_DETAIL)


async def _async_write_runtime_config_value(session: AsyncSession, key: str, value: str) -> None:
    try:
        config = (await session.scalars(select(VaultConfig).where(VaultConfig.key == key))).first()
        if config:
            config.value = value
        else:
            session.add(VaultConfig(key=key, value=value))
        await session.flush()
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("Could not persist runtime memory config key %s: %s", key, exc)
        raise HTTPException(status_code=503, detail=RUNTIME_SETTINGS_UNAVAILABLE_DETAIL) from exc


def _read_persisted_runtime_settings() -> dict[str, Any]:
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
    return {str(key): value for key, value in data.items() if value is not None}


async def _async_read_persisted_runtime_settings(session: AsyncSession) -> dict[str, Any]:
    raw = await _async_read_runtime_config_value(session, RUNTIME_MEMORY_SETTINGS_KEY)
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("Ignoring invalid runtime memory settings JSON")
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(key): value for key, value in data.items() if value is not None}


def _persist_runtime_settings(config: EmbeddingRuntimeConfig) -> None:
    _write_runtime_config_value(RUNTIME_MEMORY_SETTINGS_KEY, json.dumps(config.stored_settings(), sort_keys=True))


async def _async_persist_runtime_settings(session: AsyncSession, config: EmbeddingRuntimeConfig) -> None:
    await _async_write_runtime_config_value(
        session,
        RUNTIME_MEMORY_SETTINGS_KEY,
        json.dumps(config.stored_settings(), sort_keys=True),
    )


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


async def _async_persist_embedding_api_key(session: AsyncSession, provider: str, api_key: str) -> None:
    if not api_key:
        return
    try:
        from brain.systems.vault import _encrypt

        encrypted = base64.b64encode(_encrypt(api_key)).decode("ascii")
    except RuntimeError as exc:
        if "VAULT_MASTER_KEY is required" in str(exc):
            raise HTTPException(status_code=503, detail=VAULT_NOT_CONFIGURED_DETAIL) from exc
        raise
    await _async_write_runtime_config_value(session, _runtime_secret_config_key(provider), encrypted)


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


async def _async_read_persisted_embedding_api_key(session: AsyncSession, provider: str) -> str | None:
    raw = await _async_read_runtime_config_value(session, _runtime_secret_config_key(provider))
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


def _stored_str(settings: dict[str, Any], key: str, default: Any) -> str:
    value = settings.get(key)
    if value in (None, ""):
        value = default
    return str(value if value is not None else "")


def _stored_int(settings: dict[str, Any], key: str, default: int) -> int:
    try:
        return int(settings.get(key) or default)
    except (TypeError, ValueError):
        return default


def _default_runtime_config() -> EmbeddingRuntimeConfig:
    provider = _provider_key(getattr(cfg, "EMBEDDING_API_PROVIDER", "gemini"))
    api_model = str(getattr(cfg, "EMBEDDING_API_MODEL", "") or default_embedding_model(provider) or "gemini-embedding-2")
    return EmbeddingRuntimeConfig(
        backend=str(getattr(cfg, "EMBEDDING_BACKEND", "api") or "api").lower(),
        provider=provider,
        api_model=api_model,
        cpu_model=str(getattr(cfg, "EMBEDDING_CPU_MODEL", "") or "all-MiniLM-L6-v2"),
        dimensions=int(getattr(cfg, "EMBEDDING_DIM", 0) or embedding_dimensions(provider, api_model)),
        reranker=str(getattr(cfg, "MEMORY_RERANKER", "") or "weighted"),
    )


def get_embedding_runtime_config(*, include_secret: bool = True) -> EmbeddingRuntimeConfig:
    """Return the process-default memory runtime config for legacy sync callers.

    Persisted runtime settings are DB-backed and are loaded through
    ``async_get_embedding_runtime_config``. This sync fallback is for older
    local/CLI-style embedding calls that have not been threaded with an async
    session yet.
    """
    defaults = _default_runtime_config()
    settings = _read_persisted_runtime_settings()
    provider = _provider_key(_stored_str(settings, "provider", defaults.provider))
    config = EmbeddingRuntimeConfig(
        backend=_stored_str(settings, "backend", defaults.backend).lower(),
        provider=provider,
        api_model=_stored_str(settings, "api_model", defaults.api_model),
        cpu_model=_stored_str(settings, "cpu_model", defaults.cpu_model),
        dimensions=_stored_int(settings, "dimensions", defaults.dimensions),
        reranker=_stored_str(settings, "reranker", defaults.reranker) or "weighted",
        api_key=_read_persisted_embedding_api_key(provider) if include_secret else "",
    )
    return config


async def async_get_embedding_runtime_config(
    session: AsyncSession,
    *,
    include_secret: bool = True,
) -> EmbeddingRuntimeConfig:
    """Return installation memory runtime config using async DB access."""
    defaults = _default_runtime_config()
    settings = await _async_read_persisted_runtime_settings(session)
    provider = _provider_key(_stored_str(settings, "provider", defaults.provider))
    return EmbeddingRuntimeConfig(
        backend=_stored_str(settings, "backend", defaults.backend).lower(),
        provider=provider,
        api_model=_stored_str(settings, "api_model", defaults.api_model),
        cpu_model=_stored_str(settings, "cpu_model", defaults.cpu_model),
        dimensions=_stored_int(settings, "dimensions", defaults.dimensions),
        reranker=_stored_str(settings, "reranker", defaults.reranker) or "weighted",
        api_key=await _async_read_persisted_embedding_api_key(session, provider) if include_secret else "",
    )


def _sync_gpu_embedding_worker(backend: str) -> None:
    if backend not in {"gpu", "cpu", "api"}:
        return
    unload_url = f"{cfg.GPU_SERVER_URL}/models/embedding/unload"
    load_url = f"{cfg.GPU_SERVER_URL}/models/embedding/load"
    try:
        http_post(unload_url, timeout=15)
        if backend == "gpu":
            http_post(load_url, timeout=120)
    except Exception as exc:  # pragma: no cover - worker may not be running locally.
        logger.info("Could not sync embedding worker after settings update: %s", exc)


def _clear_embedding_runtime_caches() -> None:
    try:
        from brain.systems.memory import embeddings as emb_mod

        emb_mod._cpu_model = None
        emb_mod._cpu_model_name = None
    except Exception:
        pass


def _save_embedding_runtime_config(
    config: EmbeddingRuntimeConfig,
    *,
    api_key: str | None = None,
    sync_worker: bool = True,
) -> None:
    if api_key:
        _persist_embedding_api_key(config.provider, api_key)
    _persist_runtime_settings(config)
    _clear_embedding_runtime_caches()
    if sync_worker:
        _sync_gpu_embedding_worker(config.backend)


async def _async_save_embedding_runtime_config(
    session: AsyncSession,
    config: EmbeddingRuntimeConfig,
    *,
    api_key: str | None = None,
    sync_worker: bool = True,
) -> None:
    if api_key:
        await _async_persist_embedding_api_key(session, config.provider, api_key)
    await _async_persist_runtime_settings(session, config)
    _clear_embedding_runtime_caches()
    if sync_worker:
        _sync_gpu_embedding_worker(config.backend)


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
    return get_embedding_runtime_config(include_secret=False).provider


async def _async_current_api_provider(session: AsyncSession) -> str:
    return (await async_get_embedding_runtime_config(session, include_secret=False)).provider


def _installation_embedding_api_key(provider: str) -> str | None:
    key = _read_persisted_embedding_api_key(_provider_key(provider)) or ""
    key = (key or "").strip()
    if not key:
        return None
    if _provider_key(provider) == "openai":
        return _standard_openai_api_key(key)
    return key


async def _async_installation_embedding_api_key(session: AsyncSession, provider: str) -> str | None:
    key = await _async_read_persisted_embedding_api_key(session, _provider_key(provider)) or ""
    key = (key or "").strip()
    if not key:
        return None
    if _provider_key(provider) == "openai":
        return _standard_openai_api_key(key)
    return key


def configure_openai_embedding_api_key(api_key: str) -> None:
    """Make a verified OpenAI API key available to the installation memory embedder."""
    key = _standard_openai_api_key(api_key)
    if not key:
        return
    current = get_embedding_runtime_config(include_secret=False)
    _save_embedding_runtime_config(
        EmbeddingRuntimeConfig(
            backend="api",
            provider="openai",
            api_model="text-embedding-3-small",
            cpu_model=current.cpu_model,
            dimensions=embedding_dimensions("openai", "text-embedding-3-small"),
            reranker=current.reranker,
        ),
        api_key=key,
    )


async def async_configure_openai_embedding_api_key(session: AsyncSession, api_key: str) -> None:
    """Make a verified OpenAI API key available via async DB access."""
    key = _standard_openai_api_key(api_key)
    if not key:
        return
    current = await async_get_embedding_runtime_config(session, include_secret=False)
    await _async_save_embedding_runtime_config(
        session,
        EmbeddingRuntimeConfig(
            backend="api",
            provider="openai",
            api_model="text-embedding-3-small",
            cpu_model=current.cpu_model,
            dimensions=embedding_dimensions("openai", "text-embedding-3-small"),
            reranker=current.reranker,
        ),
        api_key=key,
    )


def configure_gemini_embedding_api_key(api_key: str) -> None:
    """Make a Gemini API key available to the installation memory embedder."""
    key = (api_key or "").strip()
    if not key:
        return
    model = default_embedding_model("gemini") or "gemini-embedding-2"
    current = get_embedding_runtime_config(include_secret=False)
    _save_embedding_runtime_config(
        EmbeddingRuntimeConfig(
            backend="api",
            provider="gemini",
            api_model=model,
            cpu_model=current.cpu_model,
            dimensions=embedding_dimensions("gemini", model),
            reranker=current.reranker,
        ),
        api_key=key,
    )


async def async_configure_gemini_embedding_api_key(session: AsyncSession, api_key: str) -> None:
    """Make a Gemini API key available via async DB access."""
    key = (api_key or "").strip()
    if not key:
        return
    model = default_embedding_model("gemini") or "gemini-embedding-2"
    current = await async_get_embedding_runtime_config(session, include_secret=False)
    await _async_save_embedding_runtime_config(
        session,
        EmbeddingRuntimeConfig(
            backend="api",
            provider="gemini",
            api_model=model,
            cpu_model=current.cpu_model,
            dimensions=embedding_dimensions("gemini", model),
            reranker=current.reranker,
        ),
        api_key=key,
    )


def _embedder_from_backend_provider(backend: str, provider: str | None) -> str:
    backend = (backend or "gpu").lower()
    provider = (provider or "").lower()
    if backend == "api":
        return "gemini" if provider in {"gemini", "google"} else "openai"
    if backend == "cpu":
        return "local_cpu"
    return "local_gpu"


def _embedding_credentials_detail(provider: str) -> str:
    try:
        from brain.systems.memory.embedding_service import embedding_credentials_message

        return embedding_credentials_message(provider)
    except Exception:
        if provider in {"gemini", "google"}:
            return "Gemini embedding credentials are not configured. Add them in System/Access."
        if provider == "openai":
            return "OpenAI embedding credentials are not configured. Add them in System/Access."
        return "Embedding credentials are not configured. Add them in System/Access."


def _finalize_embedding_info(info: dict[str, Any]) -> dict[str, Any]:
    detail = _embedding_detail(info)
    remediation = _embedding_remediation(info)
    if detail:
        info["detail"] = detail
    if remediation:
        info["remediation"] = remediation
    info["ready"] = str(info.get("status") or "") == "ready"
    return info


def _apply_gpu_embedding_info(info: dict[str, Any]) -> None:
    try:
        url = f"{cfg.GPU_SERVER_URL.rstrip('/')}/health"
        response = http_get(url, timeout=GPU_EMBEDDING_INFO_TIMEOUT_SECONDS)
        health = response.json()
        worker = health.get("workers", {}).get("embedding", {})
        worker_status = worker.get("status")
        server_status = health.get("status")
        ready = bool(health.get("ready")) or worker_status == "ready" or server_status in {"ok", "healthy"}
        info.update(
            {
                "status": "ready" if ready else "initializing",
                "model": health.get("model") or info["model"],
                "loaded": ready,
                "gpu_server_status": server_status,
                "gpu_worker_status": worker_status,
            }
        )
    except Exception:
        info.update({"status": "unavailable", "loaded": False})


def get_embedding_info(user: User | None = None) -> dict[str, Any]:
    runtime = get_embedding_runtime_config()
    backend = runtime.backend.lower()
    provider = runtime.provider.lower()
    api_key_set = bool(runtime.api_key)
    info: dict[str, Any] = {
        "backend": backend,
        "provider": provider,
        "model": runtime.api_model if backend == "api" else runtime.cpu_model if backend == "cpu" else "local-gpu",
        "dimensions": runtime.dimensions,
        "api_key_set": api_key_set,
    }
    if backend not in VALID_EMBEDDING_BACKENDS:
        info.update({"status": "invalid_config", "loaded": False})
    elif backend == "gpu":
        _apply_gpu_embedding_info(info)
    elif backend == "api":
        info["status"] = "ready" if info["api_key_set"] else "missing_key"
        info["loaded"] = info["api_key_set"]
    else:
        info["status"] = "ready"
        info["loaded"] = True
    return _finalize_embedding_info(info)


async def async_get_embedding_info(session: AsyncSession, user: User | None = None) -> dict[str, Any]:
    runtime = await async_get_embedding_runtime_config(session)
    backend = runtime.backend.lower()
    provider = runtime.provider.lower()
    api_key_set = bool(runtime.api_key)
    info: dict[str, Any] = {
        "backend": backend,
        "provider": provider,
        "model": runtime.api_model if backend == "api" else runtime.cpu_model if backend == "cpu" else "local-gpu",
        "dimensions": runtime.dimensions,
        "api_key_set": api_key_set,
    }
    if backend not in VALID_EMBEDDING_BACKENDS:
        info.update({"status": "invalid_config", "loaded": False})
    elif backend == "gpu":
        _apply_gpu_embedding_info(info)
    elif backend == "api":
        info["status"] = "ready" if info["api_key_set"] else "missing_key"
        info["loaded"] = info["api_key_set"]
    else:
        info["status"] = "ready"
        info["loaded"] = True
    return _finalize_embedding_info(info)


def _embedder_from_info(info: dict[str, Any]) -> str:
    return _embedder_from_backend_provider(
        str(info.get("backend") or "gpu"),
        str(info.get("provider") or ""),
    )


def _embedding_detail(info: dict[str, Any]) -> str | None:
    provider = str(info.get("provider") or "").lower()
    status = info.get("status")
    if status == "missing_key":
        return _embedding_credentials_detail(provider)
    if status == "invalid_config":
        return f"Unknown embedding backend {info.get('backend')!r}. Use gpu, cpu, or api."
    if status == "unavailable":
        return "The local GPU embedding worker is not responding."
    if status == "initializing":
        return "The local GPU embedding worker is still initializing."
    return None


def _embedding_remediation(info: dict[str, Any]) -> str | None:
    status = info.get("status")
    if status == "missing_key":
        return "Add embedding credentials in System/Access, or choose Local CPU."
    if status == "invalid_config":
        return "Set the memory embedder to Local GPU, Local CPU, Gemini, or OpenAI."
    if status == "unavailable":
        return "Start or inspect the GPU embedding worker, or switch memory to Local CPU/API."
    if status == "initializing":
        return "Wait for the GPU embedding worker to finish loading before running semantic work."
    return None


def _indexed_vector_count() -> int:
    logger.debug("Sync indexed memory vector count skipped; use async runtime settings APIs.")
    return 0


async def _async_indexed_vector_count(session: AsyncSession) -> int:
    try:
        return int(
            (
                await session.execute(
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
                )
            ).scalar()
            or 0
        )
    except Exception:
        logger.debug("Could not count indexed memory vectors", exc_info=True)
        return 0


def get_runtime_memory(user: User) -> RuntimeMemoryRead:
    info = get_embedding_info(user)
    runtime = get_embedding_runtime_config(include_secret=False)
    reranker = runtime.reranker or "weighted"
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
        embedding_remediation=_embedding_remediation(info),
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


async def async_get_runtime_memory(session: AsyncSession, user: User) -> RuntimeMemoryRead:
    info = await async_get_embedding_info(session, user)
    runtime = await async_get_embedding_runtime_config(session, include_secret=False)
    reranker = runtime.reranker or "weighted"
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
        embedding_remediation=_embedding_remediation(info),
        indexed_vectors=await _async_indexed_vector_count(session),
        api_key_statuses={
            "openai": bool(await _async_installation_embedding_api_key(session, "openai")),
            "gemini": bool(await _async_installation_embedding_api_key(session, "gemini")),
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

    runtime = get_embedding_runtime_config(include_secret=False)
    if embedder_spec.backend == "api":
        backend = "api"
        provider = embedder_spec.provider or "openai"
        cpu_model = runtime.cpu_model
        model = api_model or embedder_spec.default_model
    elif embedder == "local_cpu":
        backend = "cpu"
        provider = runtime.provider
        model = runtime.api_model
        cpu_model = runtime.cpu_model
    else:
        backend = "gpu"
        provider = runtime.provider
        model = runtime.api_model
        cpu_model = runtime.cpu_model

    dimensions = embedding_dimensions(embedder, model)
    next_config = EmbeddingRuntimeConfig(
        backend=backend,
        provider=_provider_key(provider or "openai"),
        api_model=model or "text-embedding-3-small",
        cpu_model=cpu_model or "all-MiniLM-L6-v2",
        dimensions=dimensions,
        reranker=update.reranker,
    )
    api_key = None
    if embedder_spec.backend == "api":
        api_key = _installation_embedding_api_key(provider)
    _save_embedding_runtime_config(next_config, api_key=api_key)

    return get_runtime_memory(user)


async def async_update_runtime_memory(
    session: AsyncSession,
    user: User,
    update: RuntimeMemoryUpdate,
) -> RuntimeMemoryRead:
    embedder = update.embedder
    embedder_spec = EMBEDDER_SPECS.get(embedder)
    if not embedder_spec:
        raise HTTPException(status_code=400, detail="Unsupported memory embedder")
    api_model = update.embedding_model or default_embedding_model(embedder)

    if not embedding_model_supported(embedder, api_model):
        provider_label = "Gemini" if embedder == "gemini" else "OpenAI"
        raise HTTPException(status_code=400, detail=f"Unsupported {provider_label} embedding model")

    current_info = await async_get_embedding_info(session)
    current_embedder = _embedder_from_info(current_info)
    current_model = current_info.get("model")
    if not embedding_model_supported(current_embedder, str(current_model or "")):
        current_model = default_embedding_model(current_embedder)
    proposed_model = api_model if embedder_spec.backend == "api" else default_embedding_model(embedder)
    indexed_vectors = await _async_indexed_vector_count(session)
    if indexed_vectors > 0 and (embedder != current_embedder or proposed_model != current_model):
        raise HTTPException(
            status_code=409,
            detail=(
                f"{indexed_vectors} installation memory vectors already exist. "
                "Rebuild the memory index before changing embedder or embedding model."
            ),
        )

    runtime = await async_get_embedding_runtime_config(session, include_secret=False)
    if embedder_spec.backend == "api":
        backend = "api"
        provider = embedder_spec.provider or "openai"
        cpu_model = runtime.cpu_model
        model = api_model or embedder_spec.default_model
    elif embedder == "local_cpu":
        backend = "cpu"
        provider = runtime.provider
        model = runtime.api_model
        cpu_model = runtime.cpu_model
    else:
        backend = "gpu"
        provider = runtime.provider
        model = runtime.api_model
        cpu_model = runtime.cpu_model

    dimensions = embedding_dimensions(embedder, model)
    next_config = EmbeddingRuntimeConfig(
        backend=backend,
        provider=_provider_key(provider or "openai"),
        api_model=model or "text-embedding-3-small",
        cpu_model=cpu_model or "all-MiniLM-L6-v2",
        dimensions=dimensions,
        reranker=update.reranker,
    )
    api_key = None
    if embedder_spec.backend == "api":
        api_key = await _async_installation_embedding_api_key(session, provider)
    await _async_save_embedding_runtime_config(session, next_config, api_key=api_key)

    return await async_get_runtime_memory(session, user)


def check_runtime_memory(user: User) -> RuntimeMemoryCheckRead:
    started = time.perf_counter()
    try:
        from brain.systems.memory.embedding_service import EmbeddingService

        embedding_service = EmbeddingService.from_legacy_sync_config()
        vector = embedding_service.query("illo brain memory setup check")
        shape = getattr(vector, "shape", None)
        dimensions = int(shape[0] if shape else len(vector))
        expected = embedding_service.runtime_config.dimensions
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
        return RuntimeMemoryCheckRead(
            status="error",
            detail=_memory_check_error_detail(exc),
            duration_ms=int(round((time.perf_counter() - started) * 1000)),
        )


async def async_check_runtime_memory(
    session: AsyncSession,
    user: User,
) -> RuntimeMemoryCheckRead:
    started = time.perf_counter()
    try:
        from brain.systems.memory.embedding_service import EmbeddingService

        embedding_service = await EmbeddingService.from_session(session)
        vector = embedding_service.query("illo brain memory setup check")
        shape = getattr(vector, "shape", None)
        dimensions = int(shape[0] if shape else len(vector))
        expected = embedding_service.runtime_config.dimensions
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
        return RuntimeMemoryCheckRead(
            status="error",
            detail=_memory_check_error_detail(exc),
            duration_ms=int(round((time.perf_counter() - started) * 1000)),
        )


def _memory_check_error_detail(exc: Exception) -> str:
    detail = str(exc)
    if "EMBEDDING_API_KEY" not in detail:
        return detail
    try:
        provider = get_embedding_runtime_config(include_secret=False).provider.lower()
    except Exception:
        provider = ""
    if provider in {"gemini", "google"}:
        return _embedding_credentials_detail(provider)
    if provider == "openai":
        return _embedding_credentials_detail(provider)
    return detail
