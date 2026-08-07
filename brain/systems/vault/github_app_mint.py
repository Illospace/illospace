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
    parse_github_repo_slug,
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
_INSTALLATION_CACHE_LIFETIME = timedelta(hours=1)
_PEM_HASH_PREFIX_LENGTH = 16
_MINT_STATUS_MESSAGES = {
    401: "GitHub rejected the GitHub App JWT.",
    403: "GitHub denied the GitHub App installation token request.",
    404: "GitHub App installation was not found.",
    422: "GitHub could not mint an installation token for the requested repositories or permissions.",
}
_DISCOVERY_STATUS_MESSAGES = {
    401: "GitHub rejected the GitHub App JWT while finding the repository installation.",
    403: "GitHub denied the GitHub App repository installation request.",
    404: "GitHub App installation was not found for the repository.",
    422: "GitHub could not find an installation for the repository.",
}


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


@dataclass(frozen=True)
class CachedRepositoryInstallation:
    installation_id: str
    expires_at: datetime


_TOKEN_CACHE: dict[str, MintedInstallationToken] = {}
_MINT_LOCKS: dict[str, asyncio.Lock] = {}
_INSTALLATION_CACHE: dict[str, CachedRepositoryInstallation] = {}
_INSTALLATION_LOCKS: dict[str, asyncio.Lock] = {}


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


def _normalize_repository_refs(
    repositories: list[str] | tuple[str, ...],
) -> list[tuple[str | None, str]]:
    cleaned: list[tuple[str | None, str]] = []
    seen: set[str] = set()
    for repository in repositories:
        value = str(repository or "").strip()
        if not value:
            continue
        parts = value.split("/")
        if len(parts) == 1:
            canonical = parse_github_repo_slug(f"placeholder/{value}")
            if canonical is None:
                raise RuntimeSecretUnavailable(
                    "GitHub App token field 'repositories' contained an invalid repository name"
                )
            owner = None
            _placeholder, name = canonical.split("/", 1)
        elif len(parts) == 2:
            canonical = parse_github_repo_slug(value)
            if canonical is None:
                raise RuntimeSecretUnavailable(
                    "GitHub App token field 'repositories' contained an invalid owner/repository name"
                )
            owner, name = canonical.split("/", 1)
        else:
            raise RuntimeSecretUnavailable(
                "GitHub App token field 'repositories' must contain repository names or owner/repository names"
            )
        cache_identity = f"{owner}/{name}" if owner is not None else name
        if cache_identity.lower() in seen:
            continue
        seen.add(cache_identity.lower())
        cleaned.append((owner, name))
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
    installation_id: str | None = None,
    repositories: list[str],
    permissions: dict[str, str],
) -> str:
    private_key_hash = hashlib.sha256(cred.private_key_pem.encode("utf-8")).hexdigest()
    payload = {
        "installation_id": installation_id or cred.installation_id,
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


def _installation_cache_key(
    cred: GitHubAppCredential,
    *,
    owner: str,
    repository: str,
) -> str:
    payload = {
        "app_id": cred.app_id,
        "client_id": cred.client_id,
        "default_installation_id": cred.installation_id,
        "private_key_sha256": hashlib.sha256(
            cred.private_key_pem.encode("utf-8")
        ).hexdigest(),
        "repository": f"{owner}/{repository}".lower(),
    }
    serialized = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _lock_for_installation(cache_key: str) -> asyncio.Lock:
    lock = _INSTALLATION_LOCKS.get(cache_key)
    if lock is None:
        lock = asyncio.Lock()
        _INSTALLATION_LOCKS[cache_key] = lock
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


def _github_app_error(
    response: httpx.Response,
    *,
    status_messages: dict[int, str],
    default_action: str,
) -> GitHubConnectorError:
    return GitHubConnectorError(
        status_code=response.status_code,
        message=status_messages.get(
            response.status_code,
            f"GitHub returned {response.status_code} while {default_action}.",
        ),
    )


def _mint_error(response: httpx.Response) -> GitHubConnectorError:
    return _github_app_error(
        response,
        status_messages=_MINT_STATUS_MESSAGES,
        default_action="minting an installation token",
    )


def _discovery_error(response: httpx.Response) -> GitHubConnectorError:
    return _github_app_error(
        response,
        status_messages=_DISCOVERY_STATUS_MESSAGES,
        default_action="finding the repository installation",
    )


async def _fetch_repository_installation(
    *,
    owner: str,
    repository: str,
    app_jwt: str,
) -> str:
    path = f"/repos/{owner}/{repository}/installation"
    try:
        async with async_http_client(timeout=httpx.Timeout(12.0, connect=5.0)) as client:
            response = await client.request(
                "GET",
                f"{GITHUB_API_BASE}{path}",
                headers=_headers(app_jwt),
            )
    except httpx.HTTPError:
        raise GitHubConnectorError(
            status_code=502,
            message="Could not reach GitHub while finding the repository installation.",
        ) from None

    if response.status_code != 200:
        raise _discovery_error(response)
    try:
        payload = response.json()
    except Exception:
        raise GitHubConnectorError(
            status_code=502,
            message="GitHub repository installation response was not valid JSON.",
        ) from None
    installation_id = payload.get("id") if isinstance(payload, dict) else None
    if (
        installation_id is None
        or installation_id is True
        or installation_id is False
        or not str(installation_id).strip()
    ):
        raise GitHubConnectorError(
            status_code=502,
            message="GitHub repository installation response was missing id.",
        )
    try:
        return str(int(str(installation_id).strip()))
    except (TypeError, ValueError):
        raise GitHubConnectorError(
            status_code=502,
            message="GitHub repository installation response had an invalid id.",
        ) from None


async def _fetch_installation_owner(
    *,
    installation_id: str,
    app_jwt: str,
) -> str:
    path = f"/app/installations/{installation_id}"
    try:
        async with async_http_client(timeout=httpx.Timeout(12.0, connect=5.0)) as client:
            response = await client.request(
                "GET",
                f"{GITHUB_API_BASE}{path}",
                headers=_headers(app_jwt),
            )
    except httpx.HTTPError:
        raise GitHubConnectorError(
            status_code=502,
            message="Could not reach GitHub while verifying the fallback installation.",
        ) from None

    if response.status_code != 200:
        raise _discovery_error(response)
    try:
        payload = response.json()
    except Exception:
        raise GitHubConnectorError(
            status_code=502,
            message="GitHub fallback installation response was not valid JSON.",
        ) from None
    account = payload.get("account") if isinstance(payload, dict) else None
    owner = account.get("login") if isinstance(account, dict) else None
    if not isinstance(owner, str) or not owner.strip():
        raise GitHubConnectorError(
            status_code=502,
            message="GitHub fallback installation response was missing account login.",
        )
    return owner.strip()


async def _resolve_repository_installation(
    cred: GitHubAppCredential,
    *,
    owner: str | None,
    repository: str,
    app_jwt: str,
    now: datetime,
) -> str:
    if owner is None:
        return cred.installation_id

    cache_key = _installation_cache_key(
        cred,
        owner=owner,
        repository=repository,
    )
    cached = _INSTALLATION_CACHE.get(cache_key)
    if cached is not None and cached.expires_at - now > _CACHE_FRESHNESS_WINDOW:
        return cached.installation_id

    async with _lock_for_installation(cache_key):
        cached = _INSTALLATION_CACHE.get(cache_key)
        if cached is not None and cached.expires_at - now > _CACHE_FRESHNESS_WINDOW:
            return cached.installation_id
        try:
            installation_id = await _fetch_repository_installation(
                owner=owner,
                repository=repository,
                app_jwt=app_jwt,
            )
        except GitHubConnectorError as discovery_error:
            try:
                fallback_owner = await _fetch_installation_owner(
                    installation_id=cred.installation_id,
                    app_jwt=app_jwt,
                )
            except GitHubConnectorError:
                raise discovery_error
            if fallback_owner.lower() != owner.lower():
                raise discovery_error
            installation_id = cred.installation_id
        _INSTALLATION_CACHE[cache_key] = CachedRepositoryInstallation(
            installation_id=installation_id,
            expires_at=now + _INSTALLATION_CACHE_LIFETIME,
        )
        return installation_id


async def _exchange_installation_token(
    cred: GitHubAppCredential,
    *,
    repositories: list[str],
    permissions: dict[str, str],
    installation_id: str | None = None,
    app_jwt: str | None = None,
    now: datetime | int | float | None = None,
) -> MintedInstallationToken:
    clean_repositories = _normalize_repositories(repositories)
    clean_permissions = _normalize_permissions(permissions)
    resolved_installation_id = installation_id or cred.installation_id
    scope_key = _scope_key(
        cred,
        installation_id=resolved_installation_id,
        repositories=clean_repositories,
        permissions=clean_permissions,
    )
    exchange_jwt = app_jwt or _build_app_jwt(cred, now=now)
    path = f"/app/installations/{resolved_installation_id}/access_tokens"
    try:
        async with async_http_client(timeout=httpx.Timeout(12.0, connect=5.0)) as client:
            response = await client.request(
                "POST",
                f"{GITHUB_API_BASE}{path}",
                headers=_headers(exchange_jwt),
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
        installation_id=resolved_installation_id,
        scope_key=scope_key,
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
                cred,
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
                cred,
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
    repository_refs = _normalize_repository_refs(repositories)
    clean_permissions = _normalize_permissions(permissions)
    current = _utc_datetime(None)
    app_jwt = _build_app_jwt(cred, now=current)
    repositories_by_installation: dict[str, list[tuple[str, str]]] = {}
    for owner, repository in repository_refs:
        installation_id = await _resolve_repository_installation(
            cred,
            owner=owner,
            repository=repository,
            app_jwt=app_jwt,
            now=current,
        )
        installation_repositories = repositories_by_installation.setdefault(
            installation_id,
            [],
        )
        repository_identity = f"{owner}/{repository}" if owner is not None else repository
        if (repository, repository_identity) not in installation_repositories:
            installation_repositories.append((repository, repository_identity))

    minted_tokens = [
        await _mint_for_installation(
            cred,
            installation_id=installation_id,
            repositories=[repository for repository, _identity in repository_refs],
            scope_repositories=[identity for _repository, identity in repository_refs],
            permissions=clean_permissions,
            app_jwt=app_jwt,
            now=current,
        )
        for installation_id, repository_refs in repositories_by_installation.items()
    ]
    if len(minted_tokens) != 1:
        raise GitHubConnectorError(
            status_code=422,
            message=(
                "GitHub App repositories span multiple installations and cannot share "
                "one installation token."
            ),
        )
    return minted_tokens[0].token


__all__ = [
    "DEFAULT_INSTALLATION_PERMISSIONS",
    "CachedRepositoryInstallation",
    "GitHubAppCredential",
    "MintedInstallationToken",
    "_build_app_jwt",
    "_exchange_installation_token",
    "async_mint_installation_token",
]
