"""GitHub App installation-token minting for Vault-stored credentials."""
from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from jose import jwt

from brain.platform.async_io import async_http_client
from brain.systems.cortex.project_context.github import (
    GITHUB_API_BASE,
    GitHubConnectorError,
    _headers,
)
from brain.systems.vault.runtime_secrets import RuntimeSecretUnavailable

DEFAULT_INSTALLATION_PERMISSIONS: dict[str, str] = {"issues": "write"}
_CACHE_FRESHNESS_WINDOW = timedelta(minutes=5)
_PEM_HASH_PREFIX_LENGTH = 16


@dataclass(frozen=True)
class GitHubAppCredential:
    app_id: str
    installation_id: str
    private_key_pem: str
    client_id: str | None = None

    @classmethod
    def from_blob(cls, decrypted_value: str) -> "GitHubAppCredential":
        try:
            payload = json.loads(decrypted_value)
        except Exception:
            raise RuntimeSecretUnavailable(
                "GitHub App credential field 'value' must be valid JSON"
            ) from None
        if not isinstance(payload, dict):
            raise RuntimeSecretUnavailable(
                "GitHub App credential field 'value' must be a JSON object"
            )

        app_id = _required_int_string(payload, "app_id")
        installation_id = _required_int_string(payload, "installation_id")
        private_key_pem = _required_string(payload, "private_key_pem")
        pem_lines = private_key_pem.splitlines()
        first_line = pem_lines[0] if pem_lines else ""
        if not private_key_pem.startswith("-----BEGIN") or "PRIVATE KEY-----" not in first_line:
            raise RuntimeSecretUnavailable(
                "GitHub App credential field 'private_key_pem' must be a PEM private key"
            )

        raw_client_id = payload.get("client_id")
        client_id = str(raw_client_id).strip() if raw_client_id is not None else None
        return cls(
            app_id=app_id,
            installation_id=installation_id,
            private_key_pem=private_key_pem,
            client_id=client_id or None,
        )


@dataclass(frozen=True)
class MintedInstallationToken:
    token: str
    expires_at: datetime
    installation_id: str
    scope_key: str


_TOKEN_CACHE: dict[str, MintedInstallationToken] = {}
_MINT_LOCKS: dict[str, asyncio.Lock] = {}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _required_string(payload: dict[str, Any], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise RuntimeSecretUnavailable(
            f"GitHub App credential field '{field}' is required"
        )
    return value.strip()


def _required_int_string(payload: dict[str, Any], field: str) -> str:
    value = payload.get(field)
    if value is None or value is True or value is False or not str(value).strip():
        raise RuntimeSecretUnavailable(
            f"GitHub App credential field '{field}' is required"
        )
    try:
        return str(int(str(value).strip()))
    except (TypeError, ValueError):
        raise RuntimeSecretUnavailable(
            f"GitHub App credential field '{field}' must be an integer"
        ) from None


def _utc_datetime(value: datetime | int | float | None) -> datetime:
    current = _now() if value is None else value
    if isinstance(current, datetime):
        if current.tzinfo is None:
            return current.replace(tzinfo=timezone.utc)
        return current.astimezone(timezone.utc)
    return datetime.fromtimestamp(float(current), tz=timezone.utc)


def _timestamp(value: datetime | int | float | None) -> int:
    return int(_utc_datetime(value).timestamp())


def _build_app_jwt(
    cred: GitHubAppCredential,
    *,
    now: datetime | int | float | None = None,
) -> str:
    issued_at = _timestamp(now)
    claims = {
        "iss": cred.client_id or cred.app_id,
        "iat": issued_at - 60,
        "exp": issued_at + 540,
    }
    try:
        return jwt.encode(claims, cred.private_key_pem, algorithm="RS256")
    except Exception:
        raise RuntimeSecretUnavailable(
            "GitHub App credential field 'private_key_pem' could not sign an app JWT"
        ) from None


def _normalize_repositories(repositories: list[str] | tuple[str, ...]) -> list[str]:
    cleaned: list[str] = []
    for repo in repositories:
        name = str(repo or "").strip()
        if not name:
            continue
        if "/" in name:
            raise RuntimeSecretUnavailable(
                "GitHub App token field 'repositories' must contain bare repository names"
            )
        if name not in cleaned:
            cleaned.append(name)
    if not cleaned:
        raise RuntimeSecretUnavailable(
            "GitHub App token field 'repositories' is required"
        )
    return cleaned


def _normalize_permissions(permissions: dict[str, str] | None) -> dict[str, str]:
    if permissions is None:
        raise RuntimeSecretUnavailable(
            "GitHub App token field 'permissions' is required"
        )
    cleaned = {
        str(key).strip(): str(value).strip()
        for key, value in permissions.items()
        if str(key).strip() and str(value).strip()
    }
    if not cleaned:
        raise RuntimeSecretUnavailable(
            "GitHub App token field 'permissions' is required"
        )
    return cleaned


def _scope_key(
    cred: GitHubAppCredential,
    *,
    repositories: list[str],
    permissions: dict[str, str],
) -> str:
    private_key_hash = hashlib.sha256(cred.private_key_pem.encode("utf-8")).hexdigest()
    payload = {
        "installation_id": cred.installation_id,
        "repositories": sorted(repositories),
        "permissions": sorted(permissions.items()),
        "private_key_sha256": private_key_hash[:_PEM_HASH_PREFIX_LENGTH],
    }
    serialized = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _lock_for_scope(scope_key: str) -> asyncio.Lock:
    lock = _MINT_LOCKS.get(scope_key)
    if lock is None:
        lock = asyncio.Lock()
        _MINT_LOCKS[scope_key] = lock
    return lock


def _cache_is_fresh(
    minted: MintedInstallationToken | None,
    *,
    now: datetime,
) -> bool:
    if minted is None:
        return False
    return minted.expires_at - now > _CACHE_FRESHNESS_WINDOW


def _parse_expires_at(value: Any) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise GitHubConnectorError(
            status_code=502,
            message="GitHub installation token response was missing expires_at.",
        )
    try:
        expires_at = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        raise GitHubConnectorError(
            status_code=502,
            message="GitHub installation token response had an invalid expires_at.",
        ) from None
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at.astimezone(timezone.utc)


def _mint_error(response: httpx.Response) -> GitHubConnectorError:
    messages = {
        401: "GitHub rejected the GitHub App JWT.",
        403: "GitHub denied the GitHub App installation token request.",
        404: "GitHub App installation was not found.",
        422: "GitHub could not mint an installation token for the requested repositories or permissions.",
    }
    return GitHubConnectorError(
        status_code=response.status_code,
        message=messages.get(
            response.status_code,
            f"GitHub returned {response.status_code} while minting an installation token.",
        ),
    )


async def _exchange_installation_token(
    cred: GitHubAppCredential,
    *,
    repositories: list[str],
    permissions: dict[str, str],
    now: datetime | int | float | None = None,
) -> MintedInstallationToken:
    clean_repositories = _normalize_repositories(repositories)
    clean_permissions = _normalize_permissions(permissions)
    scope_key = _scope_key(
        cred,
        repositories=clean_repositories,
        permissions=clean_permissions,
    )
    app_jwt = _build_app_jwt(cred, now=now)
    path = f"/app/installations/{cred.installation_id}/access_tokens"
    try:
        async with async_http_client(timeout=httpx.Timeout(12.0, connect=5.0)) as client:
            response = await client.request(
                "POST",
                f"{GITHUB_API_BASE}{path}",
                headers=_headers(app_jwt),
                json={
                    "repositories": clean_repositories,
                    "permissions": clean_permissions,
                },
            )
    except httpx.HTTPError:
        raise GitHubConnectorError(
            status_code=502,
            message="Could not reach GitHub.",
        ) from None

    if response.status_code != 201:
        raise _mint_error(response)
    try:
        payload = response.json()
    except Exception:
        raise GitHubConnectorError(
            status_code=502,
            message="GitHub installation token response was not valid JSON.",
        ) from None
    token = payload.get("token") if isinstance(payload, dict) else None
    if not isinstance(token, str) or not token.strip():
        raise GitHubConnectorError(
            status_code=502,
            message="GitHub installation token response was missing token.",
        )
    return MintedInstallationToken(
        token=token.strip(),
        expires_at=_parse_expires_at(payload.get("expires_at")),
        installation_id=cred.installation_id,
        scope_key=scope_key,
    )


async def async_mint_installation_token(
    decrypted_blob: str,
    *,
    repositories: list[str],
    permissions: dict[str, str] = DEFAULT_INSTALLATION_PERMISSIONS,
) -> str:
    cred = GitHubAppCredential.from_blob(decrypted_blob)
    clean_repositories = _normalize_repositories(repositories)
    clean_permissions = _normalize_permissions(permissions)
    scope_key = _scope_key(
        cred,
        repositories=clean_repositories,
        permissions=clean_permissions,
    )

    current = _utc_datetime(None)
    cached = _TOKEN_CACHE.get(scope_key)
    if _cache_is_fresh(cached, now=current):
        return cached.token

    async with _lock_for_scope(scope_key):
        current = _utc_datetime(None)
        cached = _TOKEN_CACHE.get(scope_key)
        if _cache_is_fresh(cached, now=current):
            return cached.token
        minted = await _exchange_installation_token(
            cred,
            repositories=clean_repositories,
            permissions=clean_permissions,
            now=current,
        )
        _TOKEN_CACHE[scope_key] = minted
        return minted.token


__all__ = [
    "DEFAULT_INSTALLATION_PERMISSIONS",
    "GitHubAppCredential",
    "MintedInstallationToken",
    "_build_app_jwt",
    "_exchange_installation_token",
    "async_mint_installation_token",
]
