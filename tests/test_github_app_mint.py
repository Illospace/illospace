from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import fields
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jose import jwt

from brain.contracts.github import GitHubConnectorError
from brain.platform.integrations.github_app import GITHUB_API_BASE, GitHubAppAPIClient
from brain.systems.vault import github_app_mint as mint
from brain.systems.vault.github_app_mint import (
    GitHubAppCredential,
    MintedInstallationToken,
    _build_app_jwt,
    _exchange_installation_token,
    async_mint_installation_token,
)
from brain.systems.vault.installation_resolver import (
    RepositoryInstallationResolver,
    ResolvedRepositoryInstallation,
)
from brain.systems.vault.runtime_secrets import RuntimeSecretUnavailable

NOW = datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def clear_mint_cache():
    mint.reset_cache()
    yield
    mint.reset_cache()


@pytest.fixture
def rsa_keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ).decode("ascii")
    public_pem = key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("ascii")
    return private_pem, public_pem


class _FakeResolver:
    def __init__(
        self,
        results: list[ResolvedRepositoryInstallation],
    ) -> None:
        self.results = results
        self.calls: list[dict] = []

    async def resolve_many(self, resolver_input, *, repositories, app_jwt):
        self.calls.append(
            {
                "resolver_input": resolver_input,
                "repositories": list(repositories),
                "app_jwt": app_jwt,
            }
        )
        return list(self.results)


class _FakeAPIClient:
    def __init__(
        self,
        outcomes: list[tuple[str, datetime] | GitHubConnectorError],
        *,
        delay: float = 0,
    ) -> None:
        self.outcomes = outcomes
        self.delay = delay
        self.calls: list[dict] = []

    async def create_installation_token(
        self,
        *,
        installation_id,
        repositories,
        permissions,
        app_jwt,
    ):
        if self.delay:
            await asyncio.sleep(self.delay)
        self.calls.append(
            {
                "installation_id": installation_id,
                "repositories": list(repositories),
                "permissions": dict(permissions),
                "app_jwt": app_jwt,
            }
        )
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, GitHubConnectorError):
            raise outcome
        return outcome


class _HTTPClient:
    def __init__(
        self,
        responses: list[httpx.Response],
        requests: list[dict],
    ) -> None:
        self._responses = responses
        self._requests = requests

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def request(self, method, url, *, headers=None, json=None):
        self._requests.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers or {}),
                "json": json,
            }
        )
        return self._responses.pop(0)


def _blob(
    private_key_pem: str,
    *,
    app_id=123,
    installation_id=456,
    client_id="Iv23.client",
) -> str:
    return json.dumps(
        {
            "app_id": app_id,
            "client_id": client_id,
            "installation_id": installation_id,
            "private_key_pem": private_key_pem,
        }
    )


def _resolved(
    repository_slug: str,
    *,
    installation_id: str = "456",
) -> ResolvedRepositoryInstallation:
    _owner, repository_name = repository_slug.split("/", 1)
    return ResolvedRepositoryInstallation(
        installation_id=installation_id,
        repository_name=repository_name,
        repository_slug=repository_slug,
    )


def _install_fakes(
    monkeypatch,
    *,
    resolved: list[ResolvedRepositoryInstallation],
    outcomes: list[tuple[str, datetime] | GitHubConnectorError],
    clock=lambda: NOW,
    delay: float = 0,
) -> tuple[_FakeResolver, _FakeAPIClient]:
    resolver = _FakeResolver(resolved)
    api_client = _FakeAPIClient(outcomes, delay=delay)
    monkeypatch.setattr(mint, "repository_installation_resolver", resolver)
    monkeypatch.setattr(mint, "github_app_api_client", api_client)
    monkeypatch.setattr(mint, "_now", clock)
    return resolver, api_client


def _decode(token: str, public_pem: str) -> dict:
    return jwt.decode(
        token,
        public_pem,
        algorithms=["RS256"],
        options={"verify_aud": False, "verify_exp": False},
    )


def test_build_app_jwt_claims_and_signature_use_client_id(rsa_keypair):
    private_pem, public_pem = rsa_keypair
    cred = GitHubAppCredential.from_blob(_blob(private_pem, client_id="Iv23.client"))

    token = _build_app_jwt(cred, now=NOW)
    claims = _decode(token, public_pem)

    now_ts = int(NOW.timestamp())
    assert jwt.get_unverified_header(token)["alg"] == "RS256"
    assert claims["iss"] == "Iv23.client"
    assert claims["iat"] == now_ts - 60
    assert claims["exp"] == now_ts + 540
    assert claims["exp"] - now_ts < 600
    assert claims["exp"] - claims["iat"] == 600


def test_build_app_jwt_uses_app_id_when_client_id_absent(rsa_keypair):
    private_pem, public_pem = rsa_keypair
    cred = GitHubAppCredential.from_blob(_blob(private_pem, client_id=None))

    token = _build_app_jwt(cred, now=NOW)

    assert _decode(token, public_pem)["iss"] == "123"


@pytest.mark.asyncio
async def test_exchange_returns_only_token_and_expiry(monkeypatch):
    api_client = _FakeAPIClient([("minted-token", NOW + timedelta(hours=1))])
    monkeypatch.setattr(mint, "github_app_api_client", api_client)

    minted = await _exchange_installation_token(
        installation_id="456",
        repositories=["uwear-backend"],
        permissions={"issues": "write"},
        app_jwt="signed-app-jwt",
        now=NOW,
    )

    assert [field.name for field in fields(MintedInstallationToken)] == [
        "token",
        "expires_at",
    ]
    assert minted == MintedInstallationToken(
        token="minted-token",
        expires_at=NOW + timedelta(hours=1),
    )
    assert api_client.calls == [
        {
            "installation_id": "456",
            "repositories": ["uwear-backend"],
            "permissions": {"issues": "write"},
            "app_jwt": "signed-app-jwt",
        }
    ]


@pytest.mark.asyncio
async def test_mint_discovers_full_slug_then_exchanges_bare_name(
    monkeypatch,
    rsa_keypair,
):
    private_pem, _public_pem = rsa_keypair
    requests: list[dict] = []
    http_client = _HTTPClient(
        [
            httpx.Response(200, json={"id": 789}),
            httpx.Response(
                201,
                json={
                    "token": "illospace-token",
                    "expires_at": (NOW + timedelta(hours=1))
                    .isoformat()
                    .replace("+00:00", "Z"),
                },
            ),
        ],
        requests,
    )
    api_client = GitHubAppAPIClient(
        transport_factory=lambda **_kwargs: http_client
    )
    resolver = RepositoryInstallationResolver(
        clock=lambda: NOW,
        transport=api_client,
    )
    monkeypatch.setattr(mint, "repository_installation_resolver", resolver)
    monkeypatch.setattr(mint, "github_app_api_client", api_client)
    monkeypatch.setattr(mint, "_now", lambda: NOW)

    token = await async_mint_installation_token(
        _blob(private_pem),
        repositories=["illospace/illospace"],
        permissions={"contents": "write"},
    )

    assert token == "illospace-token"
    assert [(request["method"], request["url"]) for request in requests] == [
        ("GET", f"{GITHUB_API_BASE}/repos/illospace/illospace/installation"),
        ("POST", f"{GITHUB_API_BASE}/app/installations/789/access_tokens"),
    ]
    assert requests[1]["json"] == {
        "repositories": ["illospace"],
        "permissions": {"contents": "write"},
    }


@pytest.mark.asyncio
async def test_mint_does_not_pass_private_key_pem_to_resolver(
    monkeypatch,
    rsa_keypair,
):
    private_pem, _public_pem = rsa_keypair
    resolver, _api_client = _install_fakes(
        monkeypatch,
        resolved=[_resolved("illospace/illospace", installation_id="789")],
        outcomes=[("illospace-token", NOW + timedelta(hours=1))],
    )

    await async_mint_installation_token(
        _blob(private_pem),
        repositories=["illospace/illospace"],
    )

    resolver_input = resolver.calls[0]["resolver_input"]
    assert not hasattr(resolver_input, "private_key_pem")
    assert vars(resolver_input) == {
        "app_id": "123",
        "client_id": "Iv23.client",
        "default_installation_id": "456",
        "private_key_sha256": hashlib.sha256(
            private_pem.strip().encode("utf-8")
        ).hexdigest(),
    }


@pytest.mark.asyncio
async def test_mint_rejects_repositories_across_installations_before_exchange(
    monkeypatch,
    rsa_keypair,
):
    private_pem, _public_pem = rsa_keypair
    _resolver, api_client = _install_fakes(
        monkeypatch,
        resolved=[
            _resolved("first-org/backend", installation_id="111"),
            _resolved("second-org/frontend", installation_id="222"),
        ],
        outcomes=[],
    )

    with pytest.raises(GitHubConnectorError, match="span multiple installations"):
        await async_mint_installation_token(
            _blob(private_pem),
            repositories=["first-org/backend", "second-org/frontend"],
            permissions={"contents": "read"},
        )

    assert api_client.calls == []


@pytest.mark.asyncio
async def test_mint_cache_reuses_until_expiry_window_then_remints(
    monkeypatch,
    rsa_keypair,
):
    private_pem, _public_pem = rsa_keypair
    current = {"now": NOW}
    _resolver, api_client = _install_fakes(
        monkeypatch,
        resolved=[_resolved("uwear-ai/uwear-backend")],
        outcomes=[
            ("token-1", NOW + timedelta(hours=1)),
            ("token-2", NOW + timedelta(hours=2)),
        ],
        clock=lambda: current["now"],
    )
    blob = _blob(private_pem)

    assert await async_mint_installation_token(
        blob,
        repositories=["uwear-ai/uwear-backend"],
    ) == "token-1"
    assert await async_mint_installation_token(
        blob,
        repositories=["uwear-ai/uwear-backend"],
    ) == "token-1"
    assert len(api_client.calls) == 1

    current["now"] = NOW + timedelta(minutes=56)
    assert await async_mint_installation_token(
        blob,
        repositories=["uwear-ai/uwear-backend"],
    ) == "token-2"
    assert len(api_client.calls) == 2


@pytest.mark.asyncio
async def test_mint_cache_separates_repositories_and_permissions(
    monkeypatch,
    rsa_keypair,
):
    private_pem, _public_pem = rsa_keypair
    resolver = _FakeResolver([])

    async def resolve_many(resolver_input, *, repositories, app_jwt):
        resolver.calls.append(
            {
                "resolver_input": resolver_input,
                "repositories": list(repositories),
                "app_jwt": app_jwt,
            }
        )
        return [_resolved(repository) for repository in repositories]

    resolver.resolve_many = resolve_many
    api_client = _FakeAPIClient(
        [
            ("issues-token", NOW + timedelta(hours=1)),
            ("other-repo-token", NOW + timedelta(hours=1)),
            ("metadata-token", NOW + timedelta(hours=1)),
        ]
    )
    monkeypatch.setattr(mint, "repository_installation_resolver", resolver)
    monkeypatch.setattr(mint, "github_app_api_client", api_client)
    monkeypatch.setattr(mint, "_now", lambda: NOW)
    blob = _blob(private_pem)

    assert await async_mint_installation_token(
        blob,
        repositories=["uwear-ai/uwear-backend"],
    ) == "issues-token"
    assert await async_mint_installation_token(
        blob,
        repositories=["uwear-ai/uwear-mobile"],
    ) == "other-repo-token"
    assert await async_mint_installation_token(
        blob,
        repositories=["uwear-ai/uwear-backend"],
        permissions={"metadata": "read"},
    ) == "metadata-token"

    assert [call["repositories"] for call in api_client.calls] == [
        ["uwear-backend"],
        ["uwear-mobile"],
        ["uwear-backend"],
    ]


@pytest.mark.asyncio
async def test_mint_falls_back_to_read_only_pull_requests_for_older_installation(
    monkeypatch,
    rsa_keypair,
):
    private_pem, _public_pem = rsa_keypair
    _resolver, api_client = _install_fakes(
        monkeypatch,
        resolved=[_resolved("uwear-ai/uwear-backend")],
        outcomes=[
            GitHubConnectorError(
                status_code=422,
                message="GitHub could not mint an installation token.",
            ),
            ("read-only-pr-token", NOW + timedelta(hours=1)),
        ],
    )

    token = await async_mint_installation_token(
        _blob(private_pem),
        repositories=["uwear-ai/uwear-backend"],
    )

    assert token == "read-only-pr-token"
    assert [call["permissions"]["pull_requests"] for call in api_client.calls] == [
        "write",
        "read",
    ]


@pytest.mark.asyncio
async def test_concurrent_mint_calls_share_one_exchange(monkeypatch, rsa_keypair):
    private_pem, _public_pem = rsa_keypair
    _resolver, api_client = _install_fakes(
        monkeypatch,
        resolved=[_resolved("uwear-ai/uwear-backend")],
        outcomes=[("shared-token", NOW + timedelta(hours=1))],
        delay=0.01,
    )
    blob = _blob(private_pem)

    tokens = await asyncio.gather(
        *[
            async_mint_installation_token(
                blob,
                repositories=["uwear-ai/uwear-backend"],
            )
            for _ in range(8)
        ]
    )

    assert tokens == ["shared-token"] * 8
    assert len(api_client.calls) == 1


@pytest.mark.asyncio
async def test_pem_rotation_misses_mint_cache(monkeypatch, rsa_keypair):
    first_private_pem, _public_pem = rsa_keypair
    second_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    second_private_pem = second_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ).decode("ascii")
    _resolver, api_client = _install_fakes(
        monkeypatch,
        resolved=[_resolved("uwear-ai/uwear-backend")],
        outcomes=[
            ("token-before-rotation", NOW + timedelta(hours=1)),
            ("token-after-rotation", NOW + timedelta(hours=1)),
        ],
    )

    assert await async_mint_installation_token(
        _blob(first_private_pem),
        repositories=["uwear-ai/uwear-backend"],
    ) == "token-before-rotation"
    assert await async_mint_installation_token(
        _blob(second_private_pem),
        repositories=["uwear-ai/uwear-backend"],
    ) == "token-after-rotation"
    assert len(api_client.calls) == 2


def test_from_blob_fails_closed_without_echoing_bad_secret_values():
    non_pem_blob = json.dumps(
        {
            "app_id": "123",
            "installation_id": "456",
            "private_key_pem": "not-a-pem-secret-value",
        }
    )
    wrong_key_type = json.dumps(
        {
            "app_id": "123",
            "installation_id": "456",
            "private_key_pem": (
                "-----BEGIN CERTIFICATE-----\nsecret-cert\n-----END CERTIFICATE-----"
            ),
        }
    )
    public_key = json.dumps(
        {
            "app_id": "123",
            "installation_id": "456",
            "private_key_pem": (
                "-----BEGIN PUBLIC KEY-----\nsecret-public\n-----END PUBLIC KEY-----"
            ),
        }
    )

    for blob, forbidden in [
        (non_pem_blob, "not-a-pem-secret-value"),
        (wrong_key_type, "secret-cert"),
        (public_key, "secret-public"),
    ]:
        with pytest.raises(RuntimeSecretUnavailable) as exc:
            GitHubAppCredential.from_blob(blob)
        message = str(exc.value)
        assert "private_key_pem" in message
        assert forbidden not in message
        assert blob not in message


@pytest.mark.asyncio
async def test_mint_does_not_log_pem_jwt_or_minted_token(
    monkeypatch,
    caplog,
    rsa_keypair,
):
    private_pem, _public_pem = rsa_keypair
    resolver, api_client = _install_fakes(
        monkeypatch,
        resolved=[_resolved("uwear-ai/uwear-backend")],
        outcomes=[("minted-token-secret", NOW + timedelta(hours=1))],
    )

    with caplog.at_level("DEBUG"):
        token = await async_mint_installation_token(
            _blob(private_pem),
            repositories=["uwear-ai/uwear-backend"],
        )

    app_jwt = resolver.calls[0]["app_jwt"]
    assert token == "minted-token-secret"
    assert api_client.calls
    assert private_pem not in caplog.text
    assert app_jwt not in caplog.text
    assert "minted-token-secret" not in caplog.text
