"""GitHub App installation-token minting for Vault-stored credentials."""
from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from jose import jwt

from brain.contracts.github import GitHubConnectorError
from brain.platform.integrations.github_app import github_app_api_client
from brain.systems.vault.installation_resolver import (
    RepositoryInstallationResolverInput,
    repository_installation_resolver,
)
from brain.systems.vault.runtime_secrets import RuntimeSecretUnavailable

# Minted installation tokens carry issues:write (to file issues and manage
# native parent/sub-issue relationships), pull_requests:write (to open, but
# never merge, PRs), and read-only contents/checks. The same App token serves
# read_github_source PR-listing/detail (including check-runs) and project-bound
# git-clone/source reads. This lets the legacy personal-PAT read fallbacks
# (GITHUB_TOKEN__AXEL_LEGACY via GH_TOKEN, and the static GITHUB_TOKEN) be
# retired. Widening the minted token from pull_requests:read to :write does not
# require GitHub re-approval for current Illo installations: the installation
# already holds Contents/Pull-requests read&write and Checks read. GitHub rejects
# a mint that exceeds an older installation's approved permissions, so a 422
# retries the prior read-only PR scope. Existing issue/source tools keep working;
# PR creation alone then reports that pull_requests:write is required.
DEFAULT_INSTALLATION_PERMISSIONS: dict[str, str] = {
    "issues": "write",
    "contents": "read",
    "pull_requests": "write",
    "checks": "read",
}
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
    installation_id: str,
    repositories: list[str],
    permissions: dict[str, str],
) -> str:
    payload = {
        "installation_id": installation_id,
        "repositories": sorted(repositories),
        "permissions": sorted(permissions.items()),
        "private_key_sha256": _private_key_sha256(cred)[:_PEM_HASH_PREFIX_LENGTH],
    }
    serialized = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _private_key_sha256(cred: GitHubAppCredential) -> str:
    return hashlib.sha256(cred.private_key_pem.encode("utf-8")).hexdigest()


def _lock_for_scope(scope_key: str) -> asyncio.Lock:
    lock = _MINT_LOCKS.get(scope_key)
    if lock is None:
        lock = asyncio.Lock()
        _MINT_LOCKS[scope_key] = lock
    return lock


def reset_cache() -> None:
    """Clear minted tokens and their lock registry."""
    _TOKEN_CACHE.clear()
    _MINT_LOCKS.clear()


def _cache_is_fresh(
    minted: MintedInstallationToken | None,
    *,
    now: datetime,
) -> bool:
    if minted is None:
        return False
    return minted.expires_at - now > _CACHE_FRESHNESS_WINDOW


async def _exchange_installation_token(
    *,
    repositories: list[str],
    permissions: dict[str, str],
    installation_id: str,
    app_jwt: str,
    now: datetime,
) -> MintedInstallationToken:
    token, expires_at = await github_app_api_client.create_installation_token(
        installation_id=installation_id,
        repositories=repositories,
        permissions=permissions,
        app_jwt=app_jwt,
    )
    return MintedInstallationToken(
        token=token,
        expires_at=expires_at,
    )


async def _mint_for_installation(
    cred: GitHubAppCredential,
    *,
    installation_id: str,
    repositories: list[str],
    scope_repositories: list[str],
    permissions: dict[str, str],
    app_jwt: str,
    now: datetime,
) -> MintedInstallationToken:
    scope_key = _scope_key(
        cred,
        installation_id=installation_id,
        repositories=scope_repositories,
        permissions=permissions,
    )
    cached = _TOKEN_CACHE.get(scope_key)
    if _cache_is_fresh(cached, now=now):
        return cached

    async with _lock_for_scope(scope_key):
        current = _utc_datetime(None)
        cached = _TOKEN_CACHE.get(scope_key)
        if _cache_is_fresh(cached, now=current):
            return cached
        try:
            minted = await _exchange_installation_token(
                repositories=repositories,
                permissions=permissions,
                installation_id=installation_id,
                app_jwt=app_jwt,
                now=current,
            )
        except GitHubConnectorError as exc:
            if exc.status_code != 422 or permissions.get("pull_requests") != "write":
                raise
            fallback_permissions = dict(permissions)
            fallback_permissions["pull_requests"] = "read"
            minted = await _exchange_installation_token(
                repositories=repositories,
                permissions=fallback_permissions,
                installation_id=installation_id,
                app_jwt=app_jwt,
                now=current,
            )
        _TOKEN_CACHE[scope_key] = minted
        return minted


async def async_mint_installation_token(
    decrypted_blob: str,
    *,
    repositories: list[str],
    permissions: dict[str, str] = DEFAULT_INSTALLATION_PERMISSIONS,
) -> str:
    cred = GitHubAppCredential.from_blob(decrypted_blob)
    clean_permissions = _normalize_permissions(permissions)
    current = _utc_datetime(None)
    app_jwt = _build_app_jwt(cred, now=current)
    resolved_repositories = await repository_installation_resolver.resolve_many(
        RepositoryInstallationResolverInput(
            app_id=cred.app_id,
            client_id=cred.client_id,
            default_installation_id=cred.installation_id,
            private_key_sha256=_private_key_sha256(cred),
        ),
        repositories=repositories,
        app_jwt=app_jwt,
    )
    installation_ids = {
        resolved.installation_id for resolved in resolved_repositories
    }
    if len(installation_ids) != 1:
        raise GitHubConnectorError(
            status_code=422,
            message=(
                "GitHub App repositories span multiple installations and cannot share "
                "one installation token."
            ),
        )
    minted = await _mint_for_installation(
        cred,
        installation_id=installation_ids.pop(),
        repositories=[
            resolved.repository_name for resolved in resolved_repositories
        ],
        scope_repositories=[
            resolved.repository_slug for resolved in resolved_repositories
        ],
        permissions=clean_permissions,
        app_jwt=app_jwt,
        now=current,
    )
    return minted.token


__all__ = [
    "DEFAULT_INSTALLATION_PERMISSIONS",
    "GitHubAppCredential",
    "MintedInstallationToken",
    "_build_app_jwt",
    "_exchange_installation_token",
    "async_mint_installation_token",
]
