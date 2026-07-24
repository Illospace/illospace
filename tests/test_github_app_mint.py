from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jose import jwt

from brain.systems.cortex.project_context.github import GITHUB_API_BASE, GitHubConnectorError
from brain.systems.vault import github_app_mint as mint
from brain.systems.vault.github_app_mint import (
    GitHubAppCredential,
    _build_app_jwt,
    _exchange_installation_token,
    async_mint_installation_token,
)
from brain.systems.vault.runtime_secrets import RuntimeSecretUnavailable

NOW = datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def clear_mint_cache():
    mint._TOKEN_CACHE.clear()
    mint._MINT_LOCKS.clear()
    yield
    mint._TOKEN_CACHE.clear()
    mint._MINT_LOCKS.clear()


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


class _HTTPClient:
    def __init__(self, responses: list[httpx.Response], requests: list[dict], *, delay: float = 0) -> None:
        self._responses = responses
        self._requests = requests
        self._delay = delay

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def request(self, method, url, *, headers=None, json=None, params=None):
        if self._delay:
            await asyncio.sleep(self._delay)
        self._requests.append({
            "method": method,
            "url": url,
            "headers": dict(headers or {}),
            "json": json,
            "params": params,
        })
        return self._responses.pop(0)


def _blob(private_key_pem: str, *, app_id=123, installation_id=456, client_id="Iv23.client") -> str:
    payload = {
        "app_id": app_id,
        "client_id": client_id,
        "installation_id": installation_id,
        "private_key_pem": private_key_pem,
    }
    return json.dumps(payload)


def _token_response(token: str, expires_at: datetime) -> httpx.Response:
    return httpx.Response(
        201,
        json={"token": token, "expires_at": expires_at.isoformat().replace("+00:00", "Z")},
    )


def _patch_http(monkeypatch, responses: list[httpx.Response], *, delay: float = 0) -> list[dict]:
    requests: list[dict] = []
    client = _HTTPClient(responses, requests, delay=delay)
    monkeypatch.setattr(mint, "async_http_client", lambda **_kwargs: client)
    return requests


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
async def test_exchange_installation_token_request_shape(monkeypatch, rsa_keypair):
    private_pem, public_pem = rsa_keypair
    cred = GitHubAppCredential.from_blob(_blob(private_pem))
    requests = _patch_http(
        monkeypatch,
        [_token_response("minted-token", NOW + timedelta(hours=1))],
    )

    minted = await _exchange_installation_token(
        cred,
        repositories=["uwear-backend"],
        permissions={"issues": "write"},
        now=NOW,
    )

    assert minted.token == "minted-token"
    assert len(requests) == 1
    request = requests[0]
    assert request["method"] == "POST"
    assert request["url"] == f"{GITHUB_API_BASE}/app/installations/456/access_tokens"
    assert request["headers"]["Accept"] == "application/vnd.github+json"
    assert request["headers"]["X-GitHub-Api-Version"] == "2022-11-28"
    assert request["json"] == {
        "repositories": ["uwear-backend"],
        "permissions": {"issues": "write"},
    }
    app_jwt = request["headers"]["Authorization"].removeprefix("Bearer ")
    assert _decode(app_jwt, public_pem)["iss"] == "Iv23.client"


@pytest.mark.asyncio
async def test_mint_cache_reuses_until_expiry_window_then_remints(monkeypatch, rsa_keypair):
    private_pem, _public_pem = rsa_keypair
    current = {"now": NOW}
    monkeypatch.setattr(mint, "_now", lambda: current["now"])
    requests = _patch_http(
        monkeypatch,
        [
            _token_response("token-1", NOW + timedelta(hours=1)),
            _token_response("token-2", NOW + timedelta(hours=2)),
        ],
    )
    blob = _blob(private_pem)

    assert await async_mint_installation_token(blob, repositories=["uwear-backend"]) == "token-1"
    assert await async_mint_installation_token(blob, repositories=["uwear-backend"]) == "token-1"
    assert len(requests) == 1

    current["now"] = NOW + timedelta(minutes=56)
    assert await async_mint_installation_token(blob, repositories=["uwear-backend"]) == "token-2"
    assert len(requests) == 2


@pytest.mark.asyncio
async def test_mint_cache_separates_repositories_and_permissions(monkeypatch, rsa_keypair):
    private_pem, _public_pem = rsa_keypair
    monkeypatch.setattr(mint, "_now", lambda: NOW)
    requests = _patch_http(
        monkeypatch,
        [
            _token_response("issues-token", NOW + timedelta(hours=1)),
            _token_response("other-repo-token", NOW + timedelta(hours=1)),
            _token_response("metadata-token", NOW + timedelta(hours=1)),
        ],
    )
    blob = _blob(private_pem)

    assert await async_mint_installation_token(blob, repositories=["uwear-backend"]) == "issues-token"
    assert await async_mint_installation_token(blob, repositories=["uwear-mobile"]) == "other-repo-token"
    assert await async_mint_installation_token(
        blob,
        repositories=["uwear-backend"],
        permissions={"metadata": "read"},
    ) == "metadata-token"

    default_scope = {"issues": "write", "contents": "read", "pull_requests": "write", "checks": "read"}
    assert [request["json"] for request in requests] == [
        {"repositories": ["uwear-backend"], "permissions": default_scope},
        {"repositories": ["uwear-mobile"], "permissions": default_scope},
        {"repositories": ["uwear-backend"], "permissions": {"metadata": "read"}},
    ]


@pytest.mark.asyncio
async def test_mint_falls_back_to_read_only_pull_requests_for_older_installation(
    monkeypatch,
    rsa_keypair,
):
    private_pem, _public_pem = rsa_keypair
    monkeypatch.setattr(mint, "_now", lambda: NOW)
    requests = _patch_http(
        monkeypatch,
        [
            httpx.Response(422, json={"message": "Validation Failed"}),
            _token_response("read-only-pr-token", NOW + timedelta(hours=1)),
        ],
    )
    blob = _blob(private_pem)

    token = await async_mint_installation_token(
        blob,
        repositories=["uwear-backend"],
    )

    assert token == "read-only-pr-token"
    assert [request["json"]["permissions"]["pull_requests"] for request in requests] == [
        "write",
        "read",
    ]


@pytest.mark.asyncio
async def test_concurrent_mint_calls_share_one_http_exchange(monkeypatch, rsa_keypair):
    private_pem, _public_pem = rsa_keypair
    monkeypatch.setattr(mint, "_now", lambda: NOW)
    requests = _patch_http(
        monkeypatch,
        [_token_response("shared-token", NOW + timedelta(hours=1))],
        delay=0.01,
    )
    blob = _blob(private_pem)

    tokens = await asyncio.gather(*[
        async_mint_installation_token(blob, repositories=["uwear-backend"])
        for _ in range(8)
    ])

    assert tokens == ["shared-token"] * 8
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_pem_rotation_misses_cache(monkeypatch, rsa_keypair):
    first_private_pem, _public_pem = rsa_keypair
    second_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    second_private_pem = second_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ).decode("ascii")
    monkeypatch.setattr(mint, "_now", lambda: NOW)
    requests = _patch_http(
        monkeypatch,
        [
            _token_response("token-before-rotation", NOW + timedelta(hours=1)),
            _token_response("token-after-rotation", NOW + timedelta(hours=1)),
        ],
    )

    assert await async_mint_installation_token(_blob(first_private_pem), repositories=["uwear-backend"]) == "token-before-rotation"
    assert await async_mint_installation_token(_blob(second_private_pem), repositories=["uwear-backend"]) == "token-after-rotation"
    assert len(requests) == 2


def test_from_blob_fails_closed_without_echoing_bad_secret_values():
    non_pem_blob = json.dumps({
        "app_id": "123",
        "installation_id": "456",
        "private_key_pem": "not-a-pem-secret-value",
    })
    wrong_key_type = json.dumps({
        "app_id": "123",
        "installation_id": "456",
        "private_key_pem": "-----BEGIN CERTIFICATE-----\nsecret-cert\n-----END CERTIFICATE-----",
    })
    public_key = json.dumps({
        "app_id": "123",
        "installation_id": "456",
        "private_key_pem": "-----BEGIN PUBLIC KEY-----\nsecret-public\n-----END PUBLIC KEY-----",
    })

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
@pytest.mark.parametrize("status_code", [401, 403, 404, 422])
async def test_exchange_errors_are_sanitized(monkeypatch, rsa_keypair, status_code):
    private_pem, _public_pem = rsa_keypair
    cred = GitHubAppCredential.from_blob(_blob(private_pem))
    forbidden_token = "minted-token-that-must-not-leak"
    requests = _patch_http(
        monkeypatch,
        [httpx.Response(status_code, json={"message": forbidden_token})],
    )

    with pytest.raises(GitHubConnectorError) as exc:
        await _exchange_installation_token(
            cred,
            repositories=["uwear-backend"],
            permissions={"issues": "write"},
            now=NOW,
        )

    assert exc.value.status_code == status_code
    assert forbidden_token not in exc.value.message
    assert private_pem not in exc.value.message
    assert requests


@pytest.mark.asyncio
async def test_mint_does_not_log_pem_jwt_or_minted_token(monkeypatch, caplog, rsa_keypair):
    private_pem, _public_pem = rsa_keypair
    monkeypatch.setattr(mint, "_now", lambda: NOW)
    requests = _patch_http(
        monkeypatch,
        [_token_response("minted-token-secret", NOW + timedelta(hours=1))],
    )

    with caplog.at_level("DEBUG"):
        token = await async_mint_installation_token(_blob(private_pem), repositories=["uwear-backend"])

    app_jwt = requests[0]["headers"]["Authorization"].removeprefix("Bearer ")
    assert token == "minted-token-secret"
    assert private_pem not in caplog.text
    assert app_jwt not in caplog.text
    assert "minted-token-secret" not in caplog.text
