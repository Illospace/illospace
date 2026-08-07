"""Small HTTP client for the GitHub App calls used by Vault."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

import httpx

from brain.contracts.github import GitHubConnectorError
from brain.platform.async_io import async_http_client

GITHUB_API_BASE = "https://api.github.com"

_DISCOVERY_STATUS_MESSAGES = {
    401: "GitHub rejected the GitHub App JWT while finding the repository installation.",
    403: "GitHub denied the GitHub App repository installation request.",
    404: "GitHub App installation was not found for the repository.",
    422: "GitHub could not find an installation for the repository.",
}
_MINT_STATUS_MESSAGES = {
    401: "GitHub rejected the GitHub App JWT.",
    403: "GitHub denied the GitHub App installation token request.",
    404: "GitHub App installation was not found.",
    422: "GitHub could not mint an installation token for the requested repositories or permissions.",
}


class GitHubAppAPIClient:
    """Own HTTP construction and decoding for Vault's GitHub App calls."""

    def __init__(
        self,
        *,
        transport_factory: Callable[..., Any] | None = None,
    ) -> None:
        self._transport_factory = transport_factory

    async def find_repository_installation(
        self,
        *,
        owner: str,
        repository: str,
        app_jwt: str,
    ) -> str:
        payload = await self._request_json(
            "GET",
            f"/repos/{owner}/{repository}/installation",
            app_jwt=app_jwt,
            expected_status=200,
            status_messages=_DISCOVERY_STATUS_MESSAGES,
            status_action="finding the repository installation",
            transport_message=(
                "Could not reach GitHub while finding the repository installation."
            ),
            invalid_json_message=(
                "GitHub repository installation response was not valid JSON."
            ),
        )
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

    async def get_installation_owner(
        self,
        *,
        installation_id: str,
        app_jwt: str,
    ) -> str:
        payload = await self._request_json(
            "GET",
            f"/app/installations/{installation_id}",
            app_jwt=app_jwt,
            expected_status=200,
            status_messages=_DISCOVERY_STATUS_MESSAGES,
            status_action="finding the repository installation",
            transport_message=(
                "Could not reach GitHub while verifying the fallback installation."
            ),
            invalid_json_message=(
                "GitHub fallback installation response was not valid JSON."
            ),
        )
        account = payload.get("account") if isinstance(payload, dict) else None
        owner = account.get("login") if isinstance(account, dict) else None
        if not isinstance(owner, str) or not owner.strip():
            raise GitHubConnectorError(
                status_code=502,
                message=(
                    "GitHub fallback installation response was missing account login."
                ),
            )
        return owner.strip()

    async def create_installation_token(
        self,
        *,
        installation_id: str,
        repositories: list[str],
        permissions: dict[str, str],
        app_jwt: str,
    ) -> tuple[str, datetime]:
        payload = await self._request_json(
            "POST",
            f"/app/installations/{installation_id}/access_tokens",
            app_jwt=app_jwt,
            json={
                "repositories": repositories,
                "permissions": permissions,
            },
            expected_status=201,
            status_messages=_MINT_STATUS_MESSAGES,
            status_action="minting an installation token",
            transport_message="Could not reach GitHub.",
            invalid_json_message=(
                "GitHub installation token response was not valid JSON."
            ),
        )
        token = payload.get("token") if isinstance(payload, dict) else None
        if not isinstance(token, str) or not token.strip():
            raise GitHubConnectorError(
                status_code=502,
                message="GitHub installation token response was missing token.",
            )
        return token.strip(), _parse_expires_at(payload.get("expires_at"))

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        app_jwt: str,
        expected_status: int,
        status_messages: dict[int, str],
        status_action: str,
        transport_message: str,
        invalid_json_message: str,
        json: dict[str, Any] | None = None,
    ) -> Any:
        transport_factory = self._transport_factory or async_http_client
        try:
            async with transport_factory(
                timeout=httpx.Timeout(12.0, connect=5.0)
            ) as client:
                response = await client.request(
                    method,
                    f"{GITHUB_API_BASE}{path}",
                    headers=_headers(app_jwt),
                    json=json,
                )
        except httpx.HTTPError:
            raise GitHubConnectorError(
                status_code=502,
                message=transport_message,
            ) from None

        if response.status_code != expected_status:
            raise GitHubConnectorError(
                status_code=response.status_code,
                message=status_messages.get(
                    response.status_code,
                    f"GitHub returned {response.status_code} while {status_action}.",
                ),
            )
        try:
            return response.json()
        except Exception:
            raise GitHubConnectorError(
                status_code=502,
                message=invalid_json_message,
            ) from None


def _headers(app_jwt: str) -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "illo-brain-project-context",
        "Authorization": f"Bearer {app_jwt}",
    }


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


github_app_api_client = GitHubAppAPIClient()


__all__ = [
    "GITHUB_API_BASE",
    "GitHubAppAPIClient",
    "github_app_api_client",
]
