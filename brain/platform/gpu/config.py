"""GPU server configuration: worker manifests and server settings."""

import os
from dataclasses import dataclass, field
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_EMBEDDING_MODEL = "Qwen/Qwen3-Embedding-8B"
DEFAULT_LLM_MODEL = "Qwen/Qwen3.5-4B"
LOCAL_EMBEDDING_MODEL_DIR = "qwen3-embedding-8b"
LOCAL_LLM_MODEL_DIR = "qwen3.5-4b"


def _repo_env_candidates(repo_root: Path | None = None) -> tuple[Path, ...]:
    root = repo_root or _REPO_ROOT
    return (
        root / ".env",
        root / "brain" / ".env",
        root / "core" / ".env",
    )


def _read_repo_env(repo_root: Path | None = None) -> dict[str, str]:
    """Read repo .env values without mutating process environment."""
    root = repo_root or _REPO_ROOT
    try:
        from dotenv import dotenv_values
    except ImportError:
        return {}

    for candidate in _repo_env_candidates(root):
        if candidate.exists():
            values = dotenv_values(candidate)
            return {key: value for key, value in values.items() if value}
    return {}


def _config_value(*keys: str, repo_root: Path | None = None, default: str) -> str:
    file_values = _read_repo_env(repo_root)
    for key in keys:
        env_value = os.environ.get(key)
        if env_value:
            return env_value
        file_value = file_values.get(key)
        if file_value:
            return file_value
    return default


def _local_model_path(repo_root: Path, model_dir: str) -> str | None:
    candidate = repo_root / "models" / model_dir
    if not candidate.is_dir():
        return None
    if not any((candidate / name).exists() for name in ("config.json", "modules.json")):
        return None
    return str(candidate)


def _config_int(
    *keys: str,
    repo_root: Path | None = None,
    default: int,
    minimum: int = 0,
) -> int:
    raw = _config_value(*keys, repo_root=repo_root, default=str(default))
    try:
        return max(minimum, int(raw))
    except (TypeError, ValueError):
        return default


def _config_bool(*keys: str, repo_root: Path | None = None, default: bool = False) -> bool:
    raw = _config_value(*keys, repo_root=repo_root, default="1" if default else "0")
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _resolve_llm_model_path(repo_root: Path | None = None) -> str:
    root = repo_root or _REPO_ROOT
    configured = _config_value("LLM_MODEL_PATH", "LLM_MODEL", repo_root=root, default="")
    local = _local_model_path(root, LOCAL_LLM_MODEL_DIR)
    if configured:
        if configured == DEFAULT_LLM_MODEL and local:
            return local
        return configured
    return local or DEFAULT_LLM_MODEL


def _resolve_embedding_model_path(repo_root: Path | None = None) -> str:
    root = repo_root or _REPO_ROOT
    configured = _config_value(
        "EMBEDDING_MODEL_PATH",
        "EMBEDDING_MODEL",
        repo_root=root,
        default="",
    )
    local = _local_model_path(root, LOCAL_EMBEDDING_MODEL_DIR)
    if configured:
        if configured == DEFAULT_EMBEDDING_MODEL and local:
            return local
        return configured
    return local or DEFAULT_EMBEDDING_MODEL


@dataclass
class WorkerManifest:
    """Declares a model worker's requirements and behavior."""
    name: str
    model_path: str
    vram_mb: int
    priority: int = 5
    idle_timeout: int = 900
    preload: bool = False
    max_batch_size: int = 64
    load_timeout: int = 45
    worker_module: str = ""
    api_fallback: dict = field(default_factory=dict)


@dataclass
class ServerConfig:
    """GPU server settings."""
    host: str = "127.0.0.1"
    port: int = int(_config_value("GPU_SERVER_PORT", default="9800"))
    socket_dir: str = _config_value("GPU_SOCKET_DIR", default="/tmp")
    reconciliation_interval: int = 60
    max_restart_attempts: int = 5
    restart_window: int = 300
    poll_interval: int = 5
    reclaim_conflicting_processes: bool = _config_bool("GPU_RECLAIM_CONFLICTING_PROCESSES", default=True)
    embedding_request_timeout: int = _config_int("GPU_EMBEDDING_REQUEST_TIMEOUT_SECONDS", default=60, minimum=1)
    llm_request_timeout: int = _config_int("GPU_LLM_REQUEST_TIMEOUT_SECONDS", default=120, minimum=1)


def build_worker_manifests(repo_root: Path | None = None) -> list[WorkerManifest]:
    root = repo_root or _REPO_ROOT
    embedding_vram_mb = _config_int(
        "GPU_EMBEDDING_VRAM_MB",
        "EMBEDDING_VRAM_MB",
        repo_root=root,
        default=15000,
        minimum=1,
    )
    llm_vram_mb = _config_int(
        "GPU_LLM_VRAM_MB",
        "LLM_VRAM_MB",
        repo_root=root,
        default=9000,
        minimum=1,
    )
    embedding_load_timeout = _config_int(
        "GPU_EMBEDDING_LOAD_TIMEOUT_SECONDS",
        "EMBEDDING_LOAD_TIMEOUT_SECONDS",
        repo_root=root,
        default=7200,
        minimum=1,
    )
    llm_load_timeout = _config_int(
        "GPU_LLM_LOAD_TIMEOUT_SECONDS",
        "LLM_LOAD_TIMEOUT_SECONDS",
        repo_root=root,
        default=300,
        minimum=1,
    )
    embedding_batch_size = _config_int(
        "GPU_EMBEDDING_MAX_BATCH_SIZE",
        "EMBEDDING_MAX_BATCH_SIZE",
        repo_root=root,
        default=16,
        minimum=1,
    )
    embedding_backend = _config_value("EMBEDDING_BACKEND", repo_root=root, default="api").strip().lower()
    preload_embedding = (
        embedding_backend == "gpu"
        and _config_bool("GPU_PRELOAD_EMBEDDING", repo_root=root, default=True)
    )
    preload_llm = _config_bool("GPU_PRELOAD_LLM", repo_root=root, default=True)

    return [
        WorkerManifest(
            name="embedding",
            model_path=_resolve_embedding_model_path(root),
            vram_mb=embedding_vram_mb, priority=10, idle_timeout=0, preload=preload_embedding,
            max_batch_size=embedding_batch_size, load_timeout=embedding_load_timeout,
            worker_module="brain.platform.gpu.workers.embedding",
        ),
        WorkerManifest(
            name="llm",
            model_path=_resolve_llm_model_path(root),
            vram_mb=llm_vram_mb, priority=5, idle_timeout=900, preload=preload_llm,
            max_batch_size=1, load_timeout=llm_load_timeout,
            worker_module="brain.platform.gpu.workers.llm",
        ),
    ]
