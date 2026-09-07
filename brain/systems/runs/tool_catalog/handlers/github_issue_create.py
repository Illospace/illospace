"""Create-issue policies; token selection and auth fallback belong to the caller."""

import json
from typing import Any

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


def plain_issue_failure(exc: Exception, repo: str, key_name: str | None) -> str:
    """Connector failures are terminal; unexpected failures propagate."""
    if isinstance(exc, GitHubConnectorError):
        return json.dumps(_connector_failure(exc, repo, key_name))
    raise exc


def provider_alert_issue_failure(exc: Exception, repo: str, key_name: str | None) -> str:
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
