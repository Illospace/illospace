"""Illo Brain — Unified LLM Client Factory.

Single entry point for creating LLM clients. All auth resolution, header
injection, and provider-specific logic lives here. No other module should
create Anthropic/OpenAI clients directly.

Usage:
    from brain.platform.integrations.llm import resolve_llm_client

    result = resolve_llm_client(user_id="u-123")
    response = result.call(model="openai/gpt-5.4", messages=[...], ...)

To fix provider auth issues, change ONE file: this one.
To add another LLM provider, add ONE branch: in this file.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any, Callable
from dataclasses import dataclass
from pathlib import Path

from brain.kernel.common.env import env_flag as _shared_env_flag
from brain.platform.integrations.anthropic_adapter import (
    OAUTH_SYSTEM_PROMPT_PREFIX,
    build_auth_adapter,
    get_oauth_betas,
)
from brain.platform.integrations.openai_cache import (
    build_openai_extra_headers,
    normalize_openai_request_kwargs,
)
from brain.platform.integrations.openai_codex_auth import (
    OpenAICodexCredential,
    encode_codex_auth_payload,
    refresh_codex_access_token,
    load_codex_auth_json,
    parse_codex_auth_payload,
)
from brain.platform.integrations.openai_codex_client import OpenAICodexClient

logger = logging.getLogger("brain.platform.integrations.llm")

# ── Beta flag management (OAuth setup-tokens) ────────────────────
# Essential flags MUST always be present for setup-token auth.
# Optional flags can be stripped on 500 errors (degradation).

_ESSENTIAL_BETAS = ("claude-code-20250219", "oauth-2025-04-20")
_active_betas: list[str] = []
_active_betas_lock = threading.Lock()


def _get_active_betas() -> list[str]:
    """Return the current active beta flag list, fetching if needed."""
    global _active_betas
    with _active_betas_lock:
        if not _active_betas:
            _active_betas = get_oauth_betas()
        return list(_active_betas)


def _degrade_betas() -> bool:
    """Strip one optional beta flag after an API 500. Returns True if degraded."""
    global _active_betas
    with _active_betas_lock:
        optional = [b for b in _active_betas if b not in _ESSENTIAL_BETAS]
        if optional:
            removed = optional[-1]
            _active_betas = [b for b in _active_betas if b != removed]
            logger.warning("Stripped beta '%s' after API 500 — remaining: %s", removed, _active_betas)
            return True
        if set(_active_betas) != set(_ESSENTIAL_BETAS):
            _active_betas = list(_ESSENTIAL_BETAS)
            logger.warning("Reduced to essential betas only: %s", _active_betas)
            return True
        return False


def refresh_betas() -> None:
    """Reset OAuth beta flags to the configured fallback set."""
    global _active_betas
    with _active_betas_lock:
        _active_betas = get_oauth_betas()


# ── LLM Client Result ────────────────────────────────────────────

@dataclass(frozen=True)
class LLMClient:
    """Resolved LLM client ready for API calls."""
    client: Any
    provider: str                     # "anthropic" or "openai"
    source: str                       # "user_default", "org_main", "env", "none"
    auth_mode: str | None             # "api_key", "chatgpt", etc.
    is_oauth: bool                    # True for setup-tokens (sk-ant-oat*)
    extra_headers: dict[str, str]     # Per-request headers (OAuth betas)
    token_prefix: str = ""            # First 18 chars (for logging)
    system_prompt_prefix: str = ""    # Required prefix for OAuth (Claude Code identity)

    def get_extra_headers(self) -> dict[str, str]:
        """Get current extra headers. Re-reads active betas for OAuth clients
        so degradation is picked up on the next call."""
        if not self.is_oauth or self.provider != "anthropic":
            return dict(self.extra_headers)
        betas = _get_active_betas()
        headers = dict(self.extra_headers)
        if betas:
            headers["anthropic-beta"] = ",".join(betas)
        return headers

    def build_request_headers(
        self,
        *,
        session_id: str | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, str]:
        """Return final request headers after provider-specific normalization."""
        base_headers = self.get_extra_headers()
        if self.provider == "openai":
            return build_openai_extra_headers(
                base_headers,
                auth_mode=self.auth_mode,
                session_id=session_id,
                extra_headers=extra_headers,
            )

        headers = dict(base_headers)
        if extra_headers:
            headers.update(extra_headers)
        return headers


@dataclass(frozen=True)
class ResolvedProviderAuth:
    """Resolved provider credential in a provider-neutral form."""

    token: str
    source: str
    auth_mode: str
    account_id: str = ""
    external_source_path: str = ""


# ── Resolution ───────────────────────────────────────────────────

def _resolve_key_from_db(
    user_id: str | None = None,
    org_id: str | None = None,
    provider: str = "anthropic",
) -> tuple[str | None, str]:
    """Resolve API key from DB. Returns (key, source)."""
    try:
        from brain.systems.vault import resolve_api_key
        return resolve_api_key(user_id=user_id, org_id=org_id, provider=provider)
    except Exception as exc:
        logger.warning("DB key resolution failed: %s", exc)
        return None, "none"


def _resolve_key_from_env(provider: str = "anthropic") -> tuple[str | None, str]:
    """Fallback: resolve from env var (dev convenience only)."""
    env_name = f"{provider.upper()}_API_KEY"
    key = os.environ.get(env_name)
    if key:
        return key, "env"
    # Try .env file
    env_path = Path(__file__).resolve().parents[3] / ".env"
    if env_path.is_file():
        try:
            for line in env_path.read_text().splitlines():
                line = line.strip()
                if line.startswith(f"{env_name}="):
                    val = line.split("=", 1)[1].strip()
                    if val:
                        return val, "dotenv"
        except OSError:
            pass
    return None, "none"


def _env_flag(name: str, default: bool) -> bool:
    return _shared_env_flag(name, default=default, false_values={"0", "false", "no", "off", ""})


def _llm_timeout_seconds() -> float:
    try:
        return max(30.0, float(os.environ.get("ILLO_LLM_TIMEOUT_SECONDS", "240")))
    except Exception:
        return 240.0


def _allow_local_codex_auth_fallback() -> bool:
    """Whether host-local Codex auth can be used as a runtime fallback.

    This is convenient for single-user laptop development, but it should not be
    the default behavior for shared multiplayer deployments.
    """
    default = os.environ.get("ILLO_ENV", "development") != "production"
    return _env_flag("ILLO_ALLOW_LOCAL_CODEX_AUTH_FALLBACK", default)


def _import_openai_sdk():
    """Import the OpenAI SDK lazily with a clean error if missing."""
    try:
        import openai
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "OpenAI support requires the 'openai' package to be installed in this environment."
        ) from exc
    return openai


def _build_openai_client(token: str, source: str = "") -> LLMClient:
    """Build an OpenAI LLMClient from a resolved token."""
    openai = _import_openai_sdk()
    client = openai.OpenAI(api_key=token, timeout=_llm_timeout_seconds())
    _wrap_openai_request_kwargs(client)
    return LLMClient(
        client=client,
        provider="openai",
        source=source,
        auth_mode="api_key",
        is_oauth=False,
        extra_headers={},
        token_prefix=token[:18] if token else "",
        system_prompt_prefix="",
    )


def _wrap_openai_request_kwargs(client: Any) -> Any:
    """Normalize OpenAI request kwargs on raw SDK calls that bypass our provider layer."""

    def _wrap_create(api: Any) -> None:
        original = getattr(api, "create", None)
        if not callable(original) or getattr(original, "_illo_request_kwargs_wrapped", False) is True:
            return

        def wrapped_create(*args: Any, **kwargs: Any):
            return original(*args, **normalize_openai_request_kwargs(kwargs))

        setattr(wrapped_create, "_illo_request_kwargs_wrapped", True)
        api.create = wrapped_create

    responses_api = getattr(client, "responses", None)
    if responses_api is not None:
        _wrap_create(responses_api)

    chat_api = getattr(client, "chat", None)
    completions_api = getattr(chat_api, "completions", None) if chat_api is not None else None
    if completions_api is not None:
        _wrap_create(completions_api)

    return client


def _build_anthropic_client(token: str) -> LLMClient:
    """Build an Anthropic LLMClient from a resolved token."""
    adapter = build_auth_adapter(token, timeout=_llm_timeout_seconds())
    extra_headers = dict(adapter.extra_headers)
    # For OAuth tokens, ensure betas are current
    if adapter.is_oauth:
        betas = _get_active_betas()
        if betas:
            extra_headers["anthropic-beta"] = ",".join(betas)
    return LLMClient(
        client=adapter.client,
        provider="anthropic",
        source="",  # Caller sets this
        auth_mode="api_key",
        is_oauth=adapter.is_oauth,
        extra_headers=extra_headers,
        token_prefix=token[:18] if token else "",
        system_prompt_prefix=OAUTH_SYSTEM_PROMPT_PREFIX if adapter.is_oauth else "",
    )


def _build_openai_codex_client(auth: ResolvedProviderAuth) -> LLMClient:
    """Build an OpenAI Codex ChatGPT-backed client."""
    if not auth.account_id:
        raise RuntimeError("OpenAI Codex auth is missing a ChatGPT account ID")

    originator = os.environ.get("OPENAI_CODEX_ORIGINATOR", "illo-brain")
    client = OpenAICodexClient(
        auth.token,
        auth.account_id,
        originator=originator,
        timeout=_llm_timeout_seconds(),
    )
    return LLMClient(
        client=client,
        provider="openai",
        source=auth.source,
        auth_mode="chatgpt",
        is_oauth=True,
        extra_headers={
            "chatgpt-account-id": auth.account_id,
            "originator": originator,
        },
        token_prefix=auth.token[:18] if auth.token else "",
        system_prompt_prefix="",
    )


def _codex_access_token_expired(cred: OpenAICodexCredential, *, skew_seconds: int = 60) -> bool:
    if cred.expires_at is None:
        return False
    try:
        return float(cred.expires_at) <= time.time() + skew_seconds
    except (TypeError, ValueError):
        return False


def _refresh_codex_credential_if_needed(
    cred: OpenAICodexCredential,
    *,
    source: str,
    on_refresh: Callable[[OpenAICodexCredential], None] | None = None,
) -> OpenAICodexCredential:
    if cred.auth_mode != "chatgpt" or not _codex_access_token_expired(cred):
        return cred
    if not cred.refresh_token:
        raise RuntimeError("OpenAI Codex token expired and no refresh token is available. Please sign in again.")
    try:
        refreshed = refresh_codex_access_token(cred.refresh_token)
    except Exception as exc:
        raise RuntimeError("OpenAI Codex token expired and refresh failed. Please sign in again.") from exc

    refreshed.source = source
    refreshed.external_source_path = refreshed.external_source_path or cred.external_source_path
    refreshed.account_id = refreshed.account_id or cred.account_id
    refreshed.refresh_token = refreshed.refresh_token or cred.refresh_token
    refreshed.id_token = refreshed.id_token or cred.id_token
    refreshed.email = refreshed.email or cred.email
    refreshed.plan_type = refreshed.plan_type or cred.plan_type
    refreshed.auth_mode = refreshed.auth_mode or cred.auth_mode
    if on_refresh is not None:
        try:
            on_refresh(refreshed)
        except Exception as exc:
            logger.warning(
                "Failed to persist refreshed OpenAI Codex credential for source=%s: %s",
                source,
                exc,
                exc_info=True,
            )
    return refreshed


def _persist_refreshed_openai_codex_db_credential(
    *,
    user_id: str | None,
    org_id: str | None,
    source: str,
    cred: OpenAICodexCredential,
) -> None:
    if source not in {"user_default", "org_main"}:
        return

    from brain.systems.vault import update_resolved_api_key

    stored_payload = json.dumps(encode_codex_auth_payload(cred))
    updated = update_resolved_api_key(
        user_id=user_id,
        org_id=org_id,
        provider="openai",
        source=source,
        api_key=stored_payload,
    )
    if not updated:
        logger.warning(
            "Refreshed OpenAI Codex credential could not be persisted; "
            "no matching DB key row for source=%s user_id=%s org_id=%s",
            source,
            user_id,
            org_id,
        )


def _coerce_openai_db_auth(
    raw_value: str | None,
    source: str,
    auth_mode: str | None = None,
    on_refresh: Callable[[OpenAICodexCredential], None] | None = None,
) -> ResolvedProviderAuth | None:
    """Interpret a DB-stored OpenAI credential as API key or Codex auth."""
    token = (raw_value or "").strip()
    if not token:
        return None

    parsed_cred = None
    try:
        parsed_cred = parse_codex_auth_payload(token, source=source)
    except Exception:
        parsed_cred = None

    if auth_mode != "api_key":
        if parsed_cred and parsed_cred.auth_mode == "chatgpt":
            parsed_cred = _refresh_codex_credential_if_needed(
                parsed_cred,
                source=source,
                on_refresh=on_refresh,
            )
        if (
            parsed_cred
            and parsed_cred.auth_mode == "chatgpt"
            and parsed_cred.access_token
            and parsed_cred.account_id
        ):
            return ResolvedProviderAuth(
                token=parsed_cred.access_token,
                source=source,
                auth_mode="chatgpt",
                account_id=parsed_cred.account_id,
                external_source_path=parsed_cred.external_source_path or "",
            )

    if auth_mode in (None, "api_key"):
        if parsed_cred and parsed_cred.auth_mode == "api_key" and parsed_cred.access_token:
            return ResolvedProviderAuth(
                token=parsed_cred.access_token,
                source=source,
                auth_mode="api_key",
            )
        return ResolvedProviderAuth(
            token=token,
            source=source,
            auth_mode="api_key",
        )

    return None


def _resolve_openai_auth(
    user_id: str | None = None,
    org_id: str | None = None,
    auth_mode: str | None = None,
) -> ResolvedProviderAuth | None:
    """Resolve OpenAI auth with DB-backed user/org state first.

    Machine-local Codex auth is a development fallback only unless explicitly
    re-enabled by environment override.
    """
    db_value, db_source = _resolve_key_from_db(user_id=user_id, org_id=org_id, provider="openai")
    db_auth = _coerce_openai_db_auth(
        db_value,
        db_source,
        auth_mode=auth_mode,
        on_refresh=lambda cred: _persist_refreshed_openai_codex_db_credential(
            user_id=user_id,
            org_id=org_id,
            source=db_source,
            cred=cred,
        ),
    )
    if db_auth is not None:
        return db_auth

    if auth_mode != "api_key" and _allow_local_codex_auth_fallback():
        codex_auth = load_codex_auth_json()
        if (
            isinstance(codex_auth, OpenAICodexCredential)
            and codex_auth.auth_mode == "chatgpt"
            and codex_auth.access_token
            and codex_auth.account_id
        ):
            return ResolvedProviderAuth(
                token=codex_auth.access_token,
                source="codex_cache",
                auth_mode="chatgpt",
                account_id=codex_auth.account_id,
                external_source_path=codex_auth.external_source_path or "",
            )

    if auth_mode in (None, "api_key"):
        env_key, env_source = _resolve_key_from_env(provider="openai")
        if env_key:
            return ResolvedProviderAuth(
                token=env_key,
                source=env_source,
                auth_mode="api_key",
            )

    return None


def resolve_llm_client(
    user_id: str | None = None,
    org_id: str | None = None,
    provider: str | None = None,
    auth_mode: str | None = None,
) -> LLMClient:
    """Resolve an LLM client from DB-stored keys.

    Priority:
        1. User's default key (set via Settings UI)
        2. Org main key (set by org owner)
        3. Environment variable (dev convenience)

    Anthropic stays DB/env-only. OpenAI may additionally use a machine-local
    Codex auth cache as an explicit development fallback.

    Raises RuntimeError if no key can be resolved.
    """
    if not user_id and not org_id:
        import traceback
        caller = "".join(traceback.format_stack(limit=4)[:-1])
        logger.warning(
            "resolve_llm_client called without user_id or org_id — "
            "will fall back to env key. Thread user_id from the caller.\n%s",
            caller,
        )

    provider_from_default = provider is None
    if provider_from_default:
        from brain.platform.providers.model_policy import resolve_default_provider

        provider = resolve_default_provider(user_id=user_id, org_id=org_id)
    from brain.platform.providers.model_policy import normalize_default_provider, normalize_runtime_provider

    provider = normalize_default_provider(provider) if provider_from_default else normalize_runtime_provider(provider)

    if provider not in ("anthropic", "openai"):
        raise NotImplementedError(f"Provider '{provider}' not yet supported. Add it here.")

    if provider == "openai":
        resolved_auth = _resolve_openai_auth(
            user_id=user_id,
            org_id=org_id,
            auth_mode=auth_mode,
        )
        if not resolved_auth:
            shared_hint = (
                "Add a user-scoped OpenAI/Codex credential in Illo."
                if os.environ.get("ILLO_ENV", "development") == "production"
                else "Add a user-scoped OpenAI/Codex credential in Illo, or set OPENAI_API_KEY in dev. Machine-local Codex login is only a local fallback."
            )
            raise RuntimeError(
                f"No OpenAI auth found. {shared_hint}"
            )
        if resolved_auth.auth_mode == "chatgpt":
            return _build_openai_codex_client(resolved_auth)
        return _build_openai_client(resolved_auth.token, resolved_auth.source)

    # 1+2. DB keys (user default → org main → org vault → env fallback inside resolve_api_key)
    key, source = _resolve_key_from_db(user_id=user_id, org_id=org_id, provider=provider)

    # 3. Env fallback (for dev — no DB key yet)
    if not key:
        key, source = _resolve_key_from_env(provider=provider)

    if not key:
        raise RuntimeError(
            f"No API key found for {provider}. Add one in Settings. Environment keys are only a development fallback."
        )

    result = _build_anthropic_client(key)
    # Replace source with the resolved one (frozen dataclass, so reconstruct)
    return LLMClient(
        client=result.client,
        provider=result.provider,
        source=source,
        auth_mode=result.auth_mode,
        is_oauth=result.is_oauth,
        extra_headers=result.extra_headers,
        token_prefix=result.token_prefix,
        system_prompt_prefix=result.system_prompt_prefix,
    )
