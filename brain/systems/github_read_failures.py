"""Stable failure vocabulary for authenticated GitHub reads."""

from __future__ import annotations

from brain.systems.cortex.project_context.github import GitHubConnectorError


GITHUB_READ_AUTHENTICATION_REQUIRED = "github_authentication_required"
GITHUB_READ_ACCESS_FORBIDDEN = "github_access_forbidden"
GITHUB_READ_CONNECTOR_ERROR = "github_connector_error"
GITHUB_READ_AUTH_FAILURE_REASONS = frozenset(
    {
        GITHUB_READ_AUTHENTICATION_REQUIRED,
        GITHUB_READ_ACCESS_FORBIDDEN,
    }
)


def github_read_reason_code(exc: GitHubConnectorError) -> str:
    """Translate connector statuses into the shared stable read vocabulary."""

    if exc.status_code == 401:
        return GITHUB_READ_AUTHENTICATION_REQUIRED
    if exc.status_code == 403:
        return GITHUB_READ_ACCESS_FORBIDDEN
    return GITHUB_READ_CONNECTOR_ERROR


__all__ = [
    "GITHUB_READ_ACCESS_FORBIDDEN",
    "GITHUB_READ_AUTHENTICATION_REQUIRED",
    "GITHUB_READ_AUTH_FAILURE_REASONS",
    "GITHUB_READ_CONNECTOR_ERROR",
    "github_read_reason_code",
]
