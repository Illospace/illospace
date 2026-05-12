from __future__ import annotations

import json
import logging
import os
import time
from urllib.parse import urlparse, urlunparse

from fastapi import HTTPException, Request
from sqlalchemy import select

from brain.app.api.routers.cortex._key_utils import (
    parse_provider_connect_token,
    store_org_api_key,
    verify_provider_api_key,
)
from brain.platform.integrations.openai_codex_auth import (
    build_codex_oauth_authorize_url,
    encode_codex_auth_payload,
    exchange_codex_authorization_code,
    parse_codex_oauth_callback,
)
from brain.platform.db.models.org import User
from brain.platform.db.repositories.unit_of_work import UnitOfWork, open_unit_of_work
from brain.systems.services.runtime_introspection import get_provider_auth_status
from brain.systems.vault import set_api_key

from .oauth_callback_server import (
    CALLBACK_REDIRECT_URI,
    clear_callback_target,
    ensure_callback_server,
    register_callback_target,
)
from .schemas import RuntimeConnectionRead, RuntimeMemoryRead

logger = logging.getLogger(__name__)

OPENAI_PROVIDER = "openai"
OPENAI_ORG_KEY_LABEL = "Workspace OpenAI key"
OPENAI_OAUTH_SESSION_KEY = "openai_oauth_state"
OPENAI_OAUTH_TTL_SEC = 30 * 60
OPENAI_OAUTH_REDIRECT_URI = CALLBACK_REDIRECT_URI
VAULT_NOT_CONFIGURED_DETAIL = (
    "Vault master key is not configured. Set VAULT_MASTER_KEY before saving Codex or API keys."
)
REMOTE_OAUTH_CALLBACK_DETAIL = (
    "OpenAI returns Codex sign-in to localhost:1455 in the browser. "
    "This Illospace deployment cannot receive that browser-local callback automatically, "
    "so paste the final localhost callback URL from the sign-in tab into Illospace."
)
SERVER_OAUTH_CALLBACK_DETAIL = "OpenAI will return directly to this Illo server after sign-in."


def _connection_label(method: str | None) -> str:
    if method == "chatgpt":
        return "Codex / ChatGPT"
    if method == "api_key":
        return "OpenAI API key"
    return "OpenAI"


def _can_manage_installation_memory(user: User) -> bool:
    return getattr(user, "role", None) in {"owner", "admin"}


def _raise_if_vault_not_configured(exc: RuntimeError) -> None:
    if "VAULT_MASTER_KEY is required" not in str(exc):
        return
    raise HTTPException(status_code=503, detail=VAULT_NOT_CONFIGURED_DETAIL) from exc


def _store_org_openai_api_key(user: User, api_token: str, *, required: bool = True) -> bool:
    """Store a standard OpenAI key as the workspace credential for owner/admin setup."""
    org_id = getattr(user, "org_id", None)
    if not org_id or not _can_manage_installation_memory(user):
        return False
    try:
        from brain.systems.vault import _encrypt

        store_org_api_key(
            str(org_id),
            OPENAI_PROVIDER,
            _encrypt(api_token),
            label=OPENAI_ORG_KEY_LABEL,
            uow_factory=UnitOfWork,
        )
        return True
    except RuntimeError as exc:
        if not required:
            logger.warning("OpenAI workspace credential was not stored: %s", exc)
            return False
        _raise_if_vault_not_configured(exc)
        raise


def get_openai_connection(user: User) -> RuntimeConnectionRead:
    status = get_provider_auth_status(
        user_id=user.id,
        org_id=user.org_id,
        provider=OPENAI_PROVIDER,
    )
    connected = bool(status.get("runtime_key_available"))
    method = status.get("method") if connected else None
    source = status.get("runtime_key_source") if connected else None
    return RuntimeConnectionRead(
        status="connected" if connected else "missing",
        setup_required=not connected,
        method=method,
        source=source,
        label=_connection_label(method),
        detail=None if connected else "Connect Codex or an OpenAI API key to run models.",
    )


def store_openai_connection(user: User, token: str, *, label: str | None = None) -> RuntimeConnectionRead:
    try:
        api_token, method = parse_provider_connect_token(token, OPENAI_PROVIDER)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        verify_provider_api_key(api_token, OPENAI_PROVIDER)
    except Exception as exc:  # pragma: no cover - exact SDK exception type varies.
        logger.warning("OpenAI runtime credential verification failed: %s", exc)
        raise HTTPException(status_code=400, detail=f"OpenAI credential verification failed: {exc}") from exc

    try:
        key_id = set_api_key(str(user.id), api_token, provider=OPENAI_PROVIDER, label=label or _connection_label(method))
    except RuntimeError as exc:
        _raise_if_vault_not_configured(exc)
        raise
    with open_unit_of_work(UnitOfWork) as uow:
        refreshed = uow.session.get(User, user.id)
        if not refreshed:
            raise HTTPException(status_code=404, detail="User not found")
        refreshed.default_provider = OPENAI_PROVIDER
        refreshed.default_api_key_id = key_id
        uow.commit()
        uow.session.refresh(refreshed)
        if method == "api_key" and _can_manage_installation_memory(refreshed):
            _store_org_openai_api_key(refreshed, api_token)
            from .memory import configure_openai_embedding_api_key

            configure_openai_embedding_api_key(api_token)
        return get_openai_connection(refreshed)


def _origin_from_header(raw: str | None) -> str | None:
    if not raw:
        return None
    parsed = urlparse(raw.strip())
    if not parsed.scheme or not parsed.netloc:
        return None
    return urlunparse((parsed.scheme, parsed.netloc, "", "", "", ""))


def _is_loopback_origin(origin: str | None) -> bool:
    if not origin:
        return False
    parsed = urlparse(origin)
    hostname = (parsed.hostname or "").lower()
    return parsed.scheme in {"http", "https"} and hostname in {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


def _env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _local_bridge_enabled() -> bool:
    return _env_flag("ILLO_OPENAI_OAUTH_LOCAL_BRIDGE", True)


def _request_origin(request: Request) -> str | None:
    candidates = [
        _origin_from_header(request.headers.get("origin")),
        _origin_from_header(request.headers.get("referer")),
        _origin_from_header(str(request.base_url)),
    ]
    return next((candidate for candidate in candidates if candidate), None)


def _oauth_callback_return_url(request: Request) -> str:
    candidates = [
        _origin_from_header(request.headers.get("origin")),
        _origin_from_header(request.headers.get("referer")),
        _origin_from_header(str(request.base_url)),
    ]
    origin = next((candidate for candidate in candidates if candidate), None)
    if origin is None:
        origin = "http://localhost:8000"
    return f"{origin.rstrip('/')}/auth/callback"


def _oauth_server_redirect_uri(request: Request) -> str | None:
    origin = _request_origin(request)
    if not origin or _is_loopback_origin(origin):
        return None
    return f"{origin.rstrip('/')}/auth/callback"


def _oauth_callback_mode(request: Request, requested_mode: str) -> str:
    # The Codex OAuth client is registered for the localhost callback. A custom
    # self-hosted callback must stay opt-in because providers can reject it
    # before Illo receives a recoverable callback URL.
    if requested_mode == "local_bridge":
        return "local_bridge"
    if requested_mode == "server" and _env_flag("ILLO_OPENAI_OAUTH_SERVER_CALLBACK", False):
        return "server" if _oauth_server_redirect_uri(request) else "local_bridge"
    return "local_bridge"


def start_openai_oauth(request: Request, *, callback_mode: str = "auto") -> dict[str, object]:
    mode = _oauth_callback_mode(request, callback_mode)
    return_url = _oauth_callback_return_url(request)
    redirect_uri = _oauth_server_redirect_uri(request) if mode == "server" else OPENAI_OAUTH_REDIRECT_URI
    if not redirect_uri:
        mode = "local_bridge"
        redirect_uri = OPENAI_OAUTH_REDIRECT_URI

    local_browser_callback_available = (
        mode == "local_bridge"
        and _local_bridge_enabled()
        and _is_loopback_origin(_request_origin(request))
    )
    callback_status = None
    if local_browser_callback_available:
        callback_status = ensure_callback_server()
    authorize_url, state, code_verifier = build_codex_oauth_authorize_url(redirect_uri=redirect_uri)
    request.session[OPENAI_OAUTH_SESSION_KEY] = {
        "state": state,
        "code_verifier": code_verifier,
        "created_at": time.time(),
        "redirect_uri": redirect_uri,
        "return_url": return_url,
        "callback_mode": mode,
    }
    if local_browser_callback_available:
        register_callback_target(state, return_url)

    callback_available = True if mode == "server" else bool(callback_status and callback_status.available and local_browser_callback_available)
    if mode == "server":
        callback_detail = SERVER_OAUTH_CALLBACK_DETAIL
    else:
        callback_detail = callback_status.detail if local_browser_callback_available and callback_status else REMOTE_OAUTH_CALLBACK_DETAIL
    return {
        "url": authorize_url,
        "state": state,
        "redirect_uri": redirect_uri,
        "expires_in_seconds": OPENAI_OAUTH_TTL_SEC,
        "callback_available": callback_available,
        "callback_detail": callback_detail,
        "callback_mode": mode,
    }


def exchange_openai_oauth(request: Request, user: User, callback: str) -> RuntimeConnectionRead:
    pending = request.session.get(OPENAI_OAUTH_SESSION_KEY)
    if not isinstance(pending, dict):
        raise HTTPException(status_code=400, detail="Start the Codex sign-in flow first")
    try:
        age_seconds = time.time() - float(pending.get("created_at"))
    except (TypeError, ValueError):
        age_seconds = OPENAI_OAUTH_TTL_SEC + 1
    if age_seconds > OPENAI_OAUTH_TTL_SEC:
        request.session.pop(OPENAI_OAUTH_SESSION_KEY, None)
        clear_callback_target(str(pending.get("state") or ""))
        raise HTTPException(status_code=400, detail="This Codex sign-in expired. Start it again.")

    try:
        code, returned_state = parse_codex_oauth_callback(callback)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    expected_state = str(pending.get("state") or "").strip()
    if returned_state and expected_state and returned_state != expected_state:
        raise HTTPException(status_code=400, detail="OAuth state mismatch. Start the flow again.")

    code_verifier = str(pending.get("code_verifier") or "").strip()
    redirect_uri = str(pending.get("redirect_uri") or OPENAI_OAUTH_REDIRECT_URI).strip()
    if not code_verifier:
        request.session.pop(OPENAI_OAUTH_SESSION_KEY, None)
        raise HTTPException(status_code=400, detail="OAuth session is missing its verifier. Start it again.")

    try:
        cred = exchange_codex_authorization_code(code, code_verifier=code_verifier, redirect_uri=redirect_uri)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if cred.auth_mode != "chatgpt" or not cred.access_token or not cred.account_id:
        raise HTTPException(status_code=400, detail="OpenAI did not return a usable Codex session")

    token_payload = json.dumps(encode_codex_auth_payload(cred))
    request.session.pop(OPENAI_OAUTH_SESSION_KEY, None)
    clear_callback_target(expected_state)
    return store_openai_connection(user, token_payload, label="Codex / ChatGPT")


def connect_openai_api_key(user: User, api_key: str) -> RuntimeConnectionRead:
    return store_openai_connection(user, api_key.strip(), label="OpenAI API key")


def connect_openai_embedding_api_key(user: User, api_key: str) -> RuntimeMemoryRead:
    if not _can_manage_installation_memory(user):
        raise HTTPException(status_code=403, detail="You need owner or admin access to manage installation memory")
    token = (api_key or "").strip()
    if not token:
        raise HTTPException(status_code=400, detail="Paste an OpenAI API key")
    try:
        api_token, method = parse_provider_connect_token(token, OPENAI_PROVIDER)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if method != "api_key" or not api_token.startswith("sk-"):
        raise HTTPException(status_code=400, detail="Memory and retrieval need a standard OpenAI API key")
    try:
        verify_provider_api_key(api_token, OPENAI_PROVIDER)
    except Exception as exc:  # pragma: no cover - exact SDK exception type varies.
        logger.warning("OpenAI embedding credential verification failed: %s", exc)
        raise HTTPException(status_code=400, detail=f"OpenAI credential verification failed: {exc}") from exc

    from .memory import configure_openai_embedding_api_key, get_runtime_memory

    configure_openai_embedding_api_key(api_token)
    _store_org_openai_api_key(user, api_token, required=False)
    return get_runtime_memory(user)


def connect_gemini_api_key(user: User, api_key: str) -> RuntimeMemoryRead:
    if not _can_manage_installation_memory(user):
        raise HTTPException(status_code=403, detail="You need owner or admin access to manage installation memory")
    token = (api_key or "").strip()
    if not token:
        raise HTTPException(status_code=400, detail="Paste a Gemini API key")

    from .memory import configure_gemini_embedding_api_key, get_runtime_memory

    configure_gemini_embedding_api_key(token)
    return get_runtime_memory(user)


def refresh_user(user_id: str) -> User:
    with open_unit_of_work(UnitOfWork) as uow:
        user = uow.session.execute(select(User).where(User.id == user_id)).scalar_one_or_none()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        uow.session.expunge(user)
        return user
