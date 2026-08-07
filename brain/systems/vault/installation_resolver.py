"""Resolve GitHub App installations for canonical repository slugs."""
from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Protocol

from brain.contracts.github import GitHubConnectorError, parse_github_repo_slug
from brain.platform.integrations.github_app import github_app_api_client
from brain.systems.vault.runtime_secrets import RuntimeSecretUnavailable

_CACHE_FRESHNESS_WINDOW = timedelta(minutes=5)
_CACHE_LIFETIME = timedelta(hours=1)


@dataclass(frozen=True)
class RepositoryInstallationResolverInput:
    app_id: str
    client_id: str | None
    default_installation_id: str
    private_key_sha256: str


@dataclass(frozen=True)
class ResolvedRepositoryInstallation:
    installation_id: str
    repository_name: str
    repository_slug: str


@dataclass(frozen=True)
class _CachedRepositoryInstallation:
    installation_id: str
    expires_at: datetime


class RepositoryInstallationTransport(Protocol):
    async def find_repository_installation(
        self,
        *,
        owner: str,
        repository: str,
        app_jwt: str,
    ) -> str: ...

    async def get_installation_owner(
        self,
        *,
        installation_id: str,
        app_jwt: str,
    ) -> str: ...


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_repository_slugs(
    repositories: list[str] | tuple[str, ...],
) -> list[tuple[str, str]]:
    normalized: list[tuple[str, str]] = []
    seen: set[str] = set()
    for repository in repositories:
        value = str(repository or "").strip()
        if not value:
            continue
        if value.count("/") != 1:
            raise RuntimeSecretUnavailable(
                "GitHub App token field 'repositories' must contain owner/repository names"
            )
        canonical = parse_github_repo_slug(value)
        if canonical is None:
            raise RuntimeSecretUnavailable(
                "GitHub App token field 'repositories' contained an invalid owner/repository name"
            )
        owner, name = canonical.split("/", 1)
        if canonical.lower() in seen:
            continue
        seen.add(canonical.lower())
        normalized.append((owner, name))
    if not normalized:
        raise RuntimeSecretUnavailable(
            "GitHub App token field 'repositories' is required"
        )
    return normalized


class RepositoryInstallationResolver:
    """Own repository installation discovery, caching, and fallback policy."""

    def __init__(
        self,
        *,
        clock: Callable[[], datetime] | None = None,
        transport: RepositoryInstallationTransport | None = None,
    ) -> None:
        self._clock = clock
        self._transport = transport or github_app_api_client
        self._cache: dict[str, _CachedRepositoryInstallation] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def reset(self) -> None:
        """Clear cached resolutions and their lock registry."""
        self._cache.clear()
        self._locks.clear()

    async def resolve_many(
        self,
        resolver_input: RepositoryInstallationResolverInput,
        *,
        repositories: list[str] | tuple[str, ...],
        app_jwt: str,
    ) -> list[ResolvedRepositoryInstallation]:
        repository_refs = _normalize_repository_slugs(repositories)
        now = self._current_time()
        installation_ids = await asyncio.gather(
            *(
                self._resolve(
                    resolver_input,
                    owner=owner,
                    repository=repository,
                    app_jwt=app_jwt,
                    now=now,
                )
                for owner, repository in repository_refs
            )
        )
        return [
            ResolvedRepositoryInstallation(
                installation_id=installation_id,
                repository_name=repository,
                repository_slug=f"{owner}/{repository}",
            )
            for (owner, repository), installation_id in zip(
                repository_refs,
                installation_ids,
                strict=True,
            )
        ]

    def _current_time(self) -> datetime:
        current = self._clock() if self._clock is not None else _now()
        if current.tzinfo is None:
            return current.replace(tzinfo=timezone.utc)
        return current.astimezone(timezone.utc)

    def _lock_for(self, cache_key: str) -> asyncio.Lock:
        lock = self._locks.get(cache_key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[cache_key] = lock
        return lock

    async def _resolve(
        self,
        resolver_input: RepositoryInstallationResolverInput,
        *,
        owner: str,
        repository: str,
        app_jwt: str,
        now: datetime,
    ) -> str:
        cache_key = self._cache_key(
            resolver_input,
            owner=owner,
            repository=repository,
        )
        cached = self._cache.get(cache_key)
        if self._cache_is_fresh(cached, now=now):
            assert cached is not None
            return cached.installation_id

        async with self._lock_for(cache_key):
            cached = self._cache.get(cache_key)
            if self._cache_is_fresh(cached, now=now):
                assert cached is not None
                return cached.installation_id
            try:
                installation_id = await self._transport.find_repository_installation(
                    owner=owner,
                    repository=repository,
                    app_jwt=app_jwt,
                )
            except GitHubConnectorError as discovery_error:
                installation_id = await self._stored_installation_fallback(
                    resolver_input,
                    requested_owner=owner,
                    app_jwt=app_jwt,
                    discovery_error=discovery_error,
                )
            self._cache[cache_key] = _CachedRepositoryInstallation(
                installation_id=installation_id,
                expires_at=now + _CACHE_LIFETIME,
            )
            return installation_id

    async def _stored_installation_fallback(
        self,
        resolver_input: RepositoryInstallationResolverInput,
        *,
        requested_owner: str,
        app_jwt: str,
        discovery_error: GitHubConnectorError,
    ) -> str:
        """Use the stored id only after GitHub confirms its account owner."""
        try:
            fallback_owner = await self._transport.get_installation_owner(
                installation_id=resolver_input.default_installation_id,
                app_jwt=app_jwt,
            )
        except GitHubConnectorError:
            raise discovery_error
        if fallback_owner.lower() != requested_owner.lower():
            raise discovery_error
        return resolver_input.default_installation_id

    @staticmethod
    def _cache_key(
        resolver_input: RepositoryInstallationResolverInput,
        *,
        owner: str,
        repository: str,
    ) -> str:
        payload = {
            "app_id": resolver_input.app_id,
            "client_id": resolver_input.client_id,
            "default_installation_id": resolver_input.default_installation_id,
            "private_key_sha256": resolver_input.private_key_sha256,
            "repository": f"{owner}/{repository}".lower(),
        }
        serialized = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    @staticmethod
    def _cache_is_fresh(
        cached: _CachedRepositoryInstallation | None,
        *,
        now: datetime,
    ) -> bool:
        return (
            cached is not None
            and cached.expires_at - now > _CACHE_FRESHNESS_WINDOW
        )


repository_installation_resolver = RepositoryInstallationResolver()


__all__ = [
    "RepositoryInstallationResolver",
    "RepositoryInstallationResolverInput",
    "ResolvedRepositoryInstallation",
    "repository_installation_resolver",
]
