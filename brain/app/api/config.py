"""API configuration — reads from brain.kernel.config + API-specific env vars."""
import logging
import os
import secrets

from brain.kernel import config as brain_config
from brain.kernel.common.env import env_flag as _shared_env_flag

logger = logging.getLogger(__name__)


def _env_flag(name: str, default: bool = False) -> bool:
    return _shared_env_flag(name, default=default, true_only=True)

# Re-export database config
DB_URL = brain_config.DB_URL
DB_POOL_MAX = brain_config.DB_POOL_MAX

# API-specific
# In production (ILLO_ENV=production), SECRET_KEY env var is REQUIRED.
# In dev, prefer the legacy FLASK_SECRET_KEY if present so local sessions survive
# server restarts; otherwise fall back to a per-process random key.
_ENV = os.getenv("ILLO_ENV", "development").strip().lower()
_secret_from_env = os.getenv("SECRET_KEY") or os.getenv("FLASK_SECRET_KEY")
if _ENV == "production" and not _secret_from_env:
    raise RuntimeError(
        "SECRET_KEY environment variable is required in production. "
        "Generate one with: python3 -c \"import secrets; print(secrets.token_urlsafe(64))\""
    )
SECRET_KEY = _secret_from_env or secrets.token_urlsafe(32)
if not _secret_from_env:
    logger.warning(
        "SECRET_KEY/FLASK_SECRET_KEY not set — using ephemeral random key "
        "(sessions won't survive restarts)"
    )
CORS_ORIGINS = os.getenv("CORS_ORIGINS", "http://localhost:5173").split(",")
RATE_LIMIT = int(os.getenv("RATE_LIMIT", "300"))
RATE_LIMIT_GLOBAL = int(os.getenv("RATE_LIMIT_GLOBAL", str(max(RATE_LIMIT, RATE_LIMIT * 4))))
RATE_WINDOW = int(os.getenv("RATE_WINDOW", "60"))

# Localhost auth fallback exists for local CLI/dev ergonomics only.
# Production startup validation rejects this flag if it is forced on.
AUTH_DEV_FALLBACK_ENABLED = _env_flag(
    "AUTH_DEV_FALLBACK_ENABLED",
    default=_ENV in {"development", "test"},
)

# Internal bearer tokens (for agent/worker calls)
_INTERNAL_TOKEN_ENV_NAMES = ("ILLO_API_TOKEN",)
INTERNAL_BEARER_TOKEN_SOURCES: dict[str, str] = {
    token: env_name.lower()
    for env_name in _INTERNAL_TOKEN_ENV_NAMES
    for token in [os.getenv(env_name, "")]
    if token
}
INTERNAL_BEARER_TOKENS: set[str] = set(INTERNAL_BEARER_TOKEN_SOURCES)


def validate_auth_config(
    *,
    env: str | None = None,
    auth_dev_fallback_enabled: bool | None = None,
) -> None:
    """Reject auth settings that are unsafe for shared/prod deployments."""
    effective_env = (env or _ENV).strip().lower()
    fallback_enabled = (
        AUTH_DEV_FALLBACK_ENABLED
        if auth_dev_fallback_enabled is None
        else auth_dev_fallback_enabled
    )
    if effective_env not in {"development", "test", "local"} and fallback_enabled:
        raise RuntimeError(
            "AUTH_DEV_FALLBACK_ENABLED can only be enabled in local development/test. "
            "Use session auth or an explicit internal service-principal token."
        )
