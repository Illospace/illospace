"""In-memory OAuth credentials and trusted, org-scoped vault reads."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import math
import json
import re
import time

import httpx

from brain.systems.vault.runtime_secrets import RuntimeSecretContext, RuntimeSecretUnavailable, read_runtime_secret
from .client import McpAuthError, McpMissingSecret, McpTimeout, McpUnavailable, quiet_http_logs
from .config import validate_url


@dataclass(repr=False)
class _Token:
    value: str
    expires_at: float
    refresh_margin: float
    updated_at: object


class TokenCache:
    def __init__(self):
        self._tokens: dict[tuple, _Token] = {}
        self._locks: dict[tuple, asyncio.Lock] = {}

    @staticmethod
    def key(server) -> tuple:
        return (server.org_id, server.id, server.token_url, server.client_id_key, server.scope, server.resource)

    def invalidate(self, server) -> None:
        self._tokens.pop(self.key(server), None)

    async def get(self, server, *, actor_user_id: str | None, force_refresh: bool = False, rejected_token: str | None = None) -> str:
        key = self.key(server)
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            cached = self._tokens.get(key)
            # Parallel 401 responses can share a refresh that replaced their rejected token.
            must_refresh = force_refresh and (not cached or rejected_token is None or cached.value == rejected_token)
            if not must_refresh and cached and cached.updated_at == server.updated_at and time.monotonic() < cached.expires_at - cached.refresh_margin:
                return cached.value
            self._tokens.pop(key, None)
            value, lifetime = await _fetch_token(server, actor_user_id=actor_user_id)
            now = time.monotonic()
            for old_key in list(self._tokens):
                if self._tokens[old_key].expires_at <= now or (old_key[:2] == key[:2] and old_key != key):
                    self._tokens.pop(old_key, None)
            if len(self._tokens) >= 512:
                self._tokens.pop(next(iter(self._tokens)))
            self._tokens[key] = _Token(value, time.monotonic() + lifetime, min(60, lifetime * .2), server.updated_at)
            return value


async def _fetch_token(server, *, actor_user_id: str | None) -> tuple[str, float]:
    client_id = await service_secret(server.client_id_key, server, actor_user_id=actor_user_id)
    client_secret = await service_secret(server.client_secret_key, server, actor_user_id=actor_user_id)
    form = {"grant_type": "client_credentials"}
    if server.scope:
        form["scope"] = server.scope
    if server.resource:
        form["resource"] = server.resource
    try:
        async with asyncio.timeout(server.timeout_seconds):
            await validate_url(server.token_url)
            with quiet_http_logs():
                async with httpx.AsyncClient(timeout=server.timeout_seconds, follow_redirects=False, trust_env=False) as client:
                    async with client.stream("POST", server.token_url, data=form, auth=httpx.BasicAuth(client_id, client_secret)) as response:
                        raw = bytearray()
                        async for chunk in response.aiter_bytes():
                            raw.extend(chunk)
                            if len(raw) > 64 * 1024:
                                raise McpAuthError("Token endpoint response is too large")
                        try:
                            data = json.loads(raw)
                        except (ValueError, UnicodeError):
                            data = {}
                        if not isinstance(data, dict):
                            data = {}
                        if not response.is_success:
                            # Only standardized OAuth codes are safe. Never echo an arbitrary error string.
                            code = data.get("error")
                            safe_codes = {"invalid_request", "invalid_client", "invalid_grant", "unauthorized_client", "unsupported_grant_type", "invalid_scope", "invalid_target", "server_error", "temporarily_unavailable"}
                            suffix = f" ({code})" if isinstance(code, str) and code in safe_codes else ""
                            raise McpAuthError(f"Token endpoint returned HTTP {response.status_code}{suffix}")
    except (TimeoutError, httpx.TimeoutException):
        raise McpTimeout("Token endpoint timed out") from None
    except httpx.HTTPError:
        raise McpUnavailable("Token endpoint is unavailable") from None
    except ValueError:
        raise McpAuthError("Invalid token endpoint URL") from None
    value = data.get("access_token")
    token_type = data.get("token_type", "Bearer")
    if not isinstance(value, str) or not value or not re.fullmatch(r"[A-Za-z0-9\-._~+/]+=*", value) or str(token_type).lower() != "bearer":
        raise McpAuthError("Token endpoint did not return a bearer token")
    try:
        lifetime = float(data.get("expires_in", 600))
        if not math.isfinite(lifetime) or lifetime <= 0:
            raise ValueError
    except (ValueError, TypeError):
        raise McpAuthError("Token endpoint returned an invalid token lifetime") from None
    return value, lifetime


async def service_secret(key: str, server, *, actor_user_id: str | None) -> str:
    try:
        return await read_runtime_secret(key, context=RuntimeSecretContext(
            actor_user_id=actor_user_id or server.updated_by or server.created_by, org_id=server.org_id),
            reason="Use an admin-configured MCP service credential.", requested_by="external_mcp",
            access="service", allow_env_fallback=False)
    except RuntimeSecretUnavailable:
        raise McpMissingSecret(f"Missing vault key: {key}") from None


TOKEN_CACHE = TokenCache()


def auth_provider(server, *, actor_user_id: str | None = None):
    previous_token: str | None = None

    async def resolve(force_refresh: bool = False) -> str | None:
        nonlocal previous_token
        if server.auth_mode == "none":
            return None
        if server.auth_mode == "bearer":
            return await service_secret(server.bearer_token_key, server, actor_user_id=actor_user_id)
        previous_token = await TOKEN_CACHE.get(server, actor_user_id=actor_user_id, force_refresh=force_refresh,
                                               rejected_token=previous_token)
        return previous_token
    return resolve
