from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from brain.contracts.github import GitHubConnectorError
from brain.systems.vault.installation_resolver import (
    RepositoryInstallationResolver,
    RepositoryInstallationResolverInput,
    ResolvedRepositoryInstallation,
)
from brain.systems.vault.runtime_secrets import RuntimeSecretUnavailable

NOW = datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc)


class _FakeTransport:
    def __init__(
        self,
        *,
        installations: dict[str, list[str | GitHubConnectorError]],
        owners: dict[str, list[str | GitHubConnectorError]] | None = None,
        delay: float = 0,
    ) -> None:
        self.installations = installations
        self.owners = owners or {}
        self.delay = delay
        self.installation_calls: list[dict] = []
        self.owner_calls: list[dict] = []

    async def find_repository_installation(
        self,
        *,
        owner: str,
        repository: str,
        app_jwt: str,
    ) -> str:
        if self.delay:
            await asyncio.sleep(self.delay)
        self.installation_calls.append(
            {
                "owner": owner,
                "repository": repository,
                "app_jwt": app_jwt,
            }
        )
        outcome = self.installations[f"{owner}/{repository}"].pop(0)
        if isinstance(outcome, GitHubConnectorError):
            raise outcome
        return outcome

    async def get_installation_owner(
        self,
        *,
        installation_id: str,
        app_jwt: str,
    ) -> str:
        self.owner_calls.append(
            {
                "installation_id": installation_id,
                "app_jwt": app_jwt,
            }
        )
        outcome = self.owners[installation_id].pop(0)
        if isinstance(outcome, GitHubConnectorError):
            raise outcome
        return outcome


def _resolver_input(
    *,
    private_key_sha256: str = "pem-sha-1",
) -> RepositoryInstallationResolverInput:
    return RepositoryInstallationResolverInput(
        app_id="123",
        client_id="Iv23.client",
        default_installation_id="456",
        private_key_sha256=private_key_sha256,
    )


@pytest.mark.asyncio
async def test_resolve_many_normalizes_full_slugs_and_deduplicates_case():
    transport = _FakeTransport(
        installations={"Illospace/Repo.Name": ["789"]}
    )
    resolver = RepositoryInstallationResolver(
        clock=lambda: NOW,
        transport=transport,
    )

    resolved = await resolver.resolve_many(
        _resolver_input(),
        repositories=[" Illospace/Repo.Name ", "illospace/repo.name"],
        app_jwt="signed-app-jwt",
    )

    assert resolved == [
        ResolvedRepositoryInstallation(
            installation_id="789",
            repository_name="Repo.Name",
            repository_slug="Illospace/Repo.Name",
        )
    ]
    assert transport.installation_calls == [
        {
            "owner": "Illospace",
            "repository": "Repo.Name",
            "app_jwt": "signed-app-jwt",
        }
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "repositories",
    [
        ["uwear-backend"],
        ["owner/repository/extra"],
        ["bad owner/repository"],
        [""],
    ],
)
async def test_resolve_many_requires_canonical_repository_slugs(repositories):
    transport = _FakeTransport(installations={})
    resolver = RepositoryInstallationResolver(
        clock=lambda: NOW,
        transport=transport,
    )

    with pytest.raises(RuntimeSecretUnavailable, match="owner/repository|required"):
        await resolver.resolve_many(
            _resolver_input(),
            repositories=repositories,
            app_jwt="signed-app-jwt",
        )

    assert transport.installation_calls == []


@pytest.mark.asyncio
async def test_resolution_cache_uses_injected_clock_and_refreshes_near_expiry():
    current = {"now": NOW}
    transport = _FakeTransport(
        installations={"illospace/illospace": ["789", "987"]}
    )
    resolver = RepositoryInstallationResolver(
        clock=lambda: current["now"],
        transport=transport,
    )

    first = await resolver.resolve_many(
        _resolver_input(),
        repositories=["illospace/illospace"],
        app_jwt="signed-app-jwt",
    )
    cached = await resolver.resolve_many(
        _resolver_input(),
        repositories=["illospace/illospace"],
        app_jwt="signed-app-jwt",
    )

    assert first[0].installation_id == "789"
    assert cached[0].installation_id == "789"
    assert len(transport.installation_calls) == 1

    current["now"] = NOW + timedelta(minutes=56)
    refreshed = await resolver.resolve_many(
        _resolver_input(),
        repositories=["illospace/illospace"],
        app_jwt="signed-app-jwt",
    )

    assert refreshed[0].installation_id == "987"
    assert len(transport.installation_calls) == 2


@pytest.mark.asyncio
async def test_resolution_cache_misses_after_private_key_rotation():
    transport = _FakeTransport(
        installations={"illospace/illospace": ["789", "987"]}
    )
    resolver = RepositoryInstallationResolver(
        clock=lambda: NOW,
        transport=transport,
    )

    first = await resolver.resolve_many(
        _resolver_input(private_key_sha256="first-pem-sha"),
        repositories=["illospace/illospace"],
        app_jwt="first-app-jwt",
    )
    rotated = await resolver.resolve_many(
        _resolver_input(private_key_sha256="second-pem-sha"),
        repositories=["illospace/illospace"],
        app_jwt="second-app-jwt",
    )

    assert first[0].installation_id == "789"
    assert rotated[0].installation_id == "987"
    assert [call["app_jwt"] for call in transport.installation_calls] == [
        "first-app-jwt",
        "second-app-jwt",
    ]


@pytest.mark.asyncio
async def test_fallback_verifies_matching_owner_before_using_stored_installation():
    discovery_error = GitHubConnectorError(
        status_code=404,
        message="GitHub App installation was not found for the repository.",
    )
    transport = _FakeTransport(
        installations={"uwear-ai/uwear-backend": [discovery_error]},
        owners={"456": ["UWEAR-AI"]},
    )
    resolver = RepositoryInstallationResolver(
        clock=lambda: NOW,
        transport=transport,
    )

    first = await resolver.resolve_many(
        _resolver_input(),
        repositories=["uwear-ai/uwear-backend"],
        app_jwt="signed-app-jwt",
    )
    cached = await resolver.resolve_many(
        _resolver_input(),
        repositories=["uwear-ai/uwear-backend"],
        app_jwt="signed-app-jwt",
    )

    assert first[0].installation_id == "456"
    assert cached[0].installation_id == "456"
    assert len(transport.installation_calls) == 1
    assert transport.owner_calls == [
        {
            "installation_id": "456",
            "app_jwt": "signed-app-jwt",
        }
    ]


@pytest.mark.asyncio
async def test_fallback_fails_closed_when_stored_installation_owner_differs():
    discovery_error = GitHubConnectorError(
        status_code=404,
        message="GitHub App installation was not found for the repository.",
    )
    transport = _FakeTransport(
        installations={"illospace/illospace": [discovery_error]},
        owners={"456": ["uwear-ai"]},
    )
    resolver = RepositoryInstallationResolver(
        clock=lambda: NOW,
        transport=transport,
    )

    with pytest.raises(GitHubConnectorError) as exc_info:
        await resolver.resolve_many(
            _resolver_input(),
            repositories=["illospace/illospace"],
            app_jwt="signed-app-jwt",
        )

    assert exc_info.value is discovery_error
    assert transport.owner_calls


@pytest.mark.asyncio
async def test_fallback_preserves_discovery_error_when_owner_lookup_fails():
    discovery_error = GitHubConnectorError(
        status_code=404,
        message="GitHub App installation was not found for the repository.",
    )
    owner_error = GitHubConnectorError(
        status_code=502,
        message="Could not reach GitHub while verifying the fallback installation.",
    )
    transport = _FakeTransport(
        installations={"illospace/illospace": [discovery_error]},
        owners={"456": [owner_error]},
    )
    resolver = RepositoryInstallationResolver(
        clock=lambda: NOW,
        transport=transport,
    )

    with pytest.raises(GitHubConnectorError) as exc_info:
        await resolver.resolve_many(
            _resolver_input(),
            repositories=["illospace/illospace"],
            app_jwt="signed-app-jwt",
        )

    assert exc_info.value is discovery_error


@pytest.mark.asyncio
async def test_concurrent_resolution_calls_share_one_transport_request():
    transport = _FakeTransport(
        installations={"illospace/illospace": ["789"]},
        delay=0.01,
    )
    resolver = RepositoryInstallationResolver(
        clock=lambda: NOW,
        transport=transport,
    )

    results = await asyncio.gather(
        *[
            resolver.resolve_many(
                _resolver_input(),
                repositories=["illospace/illospace"],
                app_jwt="signed-app-jwt",
            )
            for _ in range(8)
        ]
    )

    assert [result[0].installation_id for result in results] == ["789"] * 8
    assert len(transport.installation_calls) == 1
