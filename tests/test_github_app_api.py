from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest

from brain.contracts.github import GitHubConnectorError
from brain.platform.integrations.github_app import GITHUB_API_BASE, GitHubAppAPIClient

EXPIRES_AT = datetime(2026, 7, 8, 13, 0, tzinfo=timezone.utc)


class _HTTPClient:
    def __init__(
        self,
        outcomes: list[httpx.Response | httpx.HTTPError],
        requests: list[dict],
    ) -> None:
        self._outcomes = outcomes
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
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, httpx.HTTPError):
            raise outcome
        return outcome


def _client(
    outcomes: list[httpx.Response | httpx.HTTPError],
) -> tuple[GitHubAppAPIClient, list[dict]]:
    requests: list[dict] = []
    http_client = _HTTPClient(outcomes, requests)
    return (
        GitHubAppAPIClient(transport_factory=lambda **_kwargs: http_client),
        requests,
    )


@pytest.mark.asyncio
async def test_client_owns_request_construction_headers_and_response_decoding():
    client, requests = _client(
        [
            httpx.Response(200, json={"id": "007"}),
            httpx.Response(200, json={"account": {"login": " Illospace "}}),
            httpx.Response(
                201,
                json={
                    "token": " minted-token ",
                    "expires_at": "2026-07-08T13:00:00Z",
                },
            ),
        ]
    )

    installation_id = await client.find_repository_installation(
        owner="illospace",
        repository="illospace",
        app_jwt="signed-app-jwt",
    )
    owner = await client.get_installation_owner(
        installation_id="7",
        app_jwt="signed-app-jwt",
    )
    token = await client.create_installation_token(
        installation_id="7",
        repositories=["illospace"],
        permissions={"contents": "read"},
        app_jwt="signed-app-jwt",
    )

    assert installation_id == "7"
    assert owner == "Illospace"
    assert token == ("minted-token", EXPIRES_AT)
    assert [(request["method"], request["url"]) for request in requests] == [
        ("GET", f"{GITHUB_API_BASE}/repos/illospace/illospace/installation"),
        ("GET", f"{GITHUB_API_BASE}/app/installations/7"),
        ("POST", f"{GITHUB_API_BASE}/app/installations/7/access_tokens"),
    ]
    for request in requests:
        assert request["headers"] == {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "illo-brain-project-context",
            "Authorization": "Bearer signed-app-jwt",
        }
    assert requests[2]["json"] == {
        "repositories": ["illospace"],
        "permissions": {"contents": "read"},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [401, 403, 404, 422])
async def test_token_exchange_errors_are_sanitized(status_code):
    forbidden = "minted-token-that-must-not-leak"
    client, requests = _client(
        [httpx.Response(status_code, json={"message": forbidden})]
    )

    with pytest.raises(GitHubConnectorError) as exc_info:
        await client.create_installation_token(
            installation_id="456",
            repositories=["uwear-backend"],
            permissions={"issues": "write"},
            app_jwt="signed-app-jwt-secret",
        )

    assert exc_info.value.status_code == status_code
    assert forbidden not in exc_info.value.message
    assert "signed-app-jwt-secret" not in exc_info.value.message
    assert requests


@pytest.mark.asyncio
async def test_discovery_error_is_sanitized():
    client, _requests = _client(
        [httpx.Response(404, json={"message": "private repository name"})]
    )

    with pytest.raises(
        GitHubConnectorError,
        match="installation was not found for the repository",
    ) as exc_info:
        await client.find_repository_installation(
            owner="illospace",
            repository="private-repository",
            app_jwt="signed-app-jwt",
        )

    assert exc_info.value.status_code == 404
    assert "private repository name" not in exc_info.value.message


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "message"),
    [
        (
            "discovery",
            "Could not reach GitHub while finding the repository installation.",
        ),
        ("exchange", "Could not reach GitHub."),
    ],
)
async def test_transport_errors_are_sanitized(operation, message):
    client, _requests = _client([httpx.ConnectError("secret network details")])

    with pytest.raises(GitHubConnectorError) as exc_info:
        if operation == "discovery":
            await client.find_repository_installation(
                owner="illospace",
                repository="illospace",
                app_jwt="signed-app-jwt",
            )
        else:
            await client.create_installation_token(
                installation_id="456",
                repositories=["illospace"],
                permissions={"contents": "read"},
                app_jwt="signed-app-jwt",
            )

    assert exc_info.value.status_code == 502
    assert exc_info.value.message == message
    assert "secret network details" not in exc_info.value.message


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"expires_at": "2026-07-08T13:00:00Z"}, "missing token"),
        ({"token": "minted-token"}, "missing expires_at"),
        (
            {"token": "minted-token", "expires_at": "not-a-date"},
            "invalid expires_at",
        ),
    ],
)
async def test_token_response_fields_fail_closed(payload, message):
    client, _requests = _client([httpx.Response(201, json=payload)])

    with pytest.raises(GitHubConnectorError, match=message) as exc_info:
        await client.create_installation_token(
            installation_id="456",
            repositories=["illospace"],
            permissions={"contents": "read"},
            app_jwt="signed-app-jwt",
        )

    assert exc_info.value.status_code == 502


@pytest.mark.asyncio
async def test_invalid_json_fails_closed():
    client, _requests = _client(
        [httpx.Response(201, content=b"not-json")]
    )

    with pytest.raises(GitHubConnectorError, match="not valid JSON") as exc_info:
        await client.create_installation_token(
            installation_id="456",
            repositories=["illospace"],
            permissions={"contents": "read"},
            app_jwt="signed-app-jwt",
        )

    assert exc_info.value.status_code == 502
