"""Create-issue policies; token selection and auth fallback belong to the caller."""

from dataclasses import dataclass
import json
from typing import Any, Awaitable, Callable

from brain.systems.cortex.project_context.github import GitHubConnectorError
from brain.systems.slack.provider_alert_filing import FilingPendingError


CREATE_ISSUE_AUTH_STATUSES = frozenset({401, 403, 404})


def _connector_failure(exc: GitHubConnectorError, repo: str, key_name: str | None) -> dict[str, Any]:
    return {
        "error": exc.message,
        "status_code": exc.status_code,
        "no_write_token": exc.status_code in CREATE_ISSUE_AUTH_STATUSES,
        "repo": repo,
        "token_key_name": key_name,
    }


@dataclass(frozen=True)
class PlainIssueCreate:
    create: Callable[..., Awaitable[dict[str, Any]]]

    async def attempt(self, repo: str, **kwargs: Any) -> tuple[dict[str, Any], str]:
        return await self.create(repo, **kwargs), repo

    def failure(self, exc: Exception, repo: str, key_name: str | None) -> str:
        """Connector failures are terminal; unexpected failures propagate."""
        if isinstance(exc, GitHubConnectorError):
            return json.dumps(_connector_failure(exc, repo, key_name))
        if isinstance(exc, FilingPendingError):
            return json.dumps({"error": str(exc), "filing_pending": True, "retryable": True})
        raise exc


@dataclass(frozen=True)
class ProviderAlertIssueCreate:
    create: Callable[..., Awaitable[dict[str, Any]]]

    async def attempt(self, repo: str, **kwargs: Any) -> tuple[dict[str, Any], str]:
        payload = await self.create(repo, **kwargs)
        return payload, payload["repo"]

    def failure(self, exc: Exception, repo: str, key_name: str | None) -> str:
        """Every failure keeps the same claim pending for a safe retry."""
        payload = (
            _connector_failure(exc, repo, key_name)
            if isinstance(exc, GitHubConnectorError) else {"error": str(exc)}
        )
        payload.update(filing_pending=True, retryable=True)
        if not isinstance(exc, (GitHubConnectorError, FilingPendingError)):
            payload["instruction"] = (
                "Retry the same provider_alert claim; do not create another issue or tracker record."
            )
        return json.dumps(payload)
