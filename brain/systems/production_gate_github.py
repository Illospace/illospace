"""GitHub credential adapter for the production-gate closure sweep."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from brain.systems.cortex.project_context.github import (
    GitHubConnectorError,
    GithubFixingPullRequest as FixingPullRequest,
    GithubIssueClosure as IssueClosure,
)
from brain.systems.deploy_state import DeployStateBatch
from brain.systems.github_read_failures import github_read_reason_code


@dataclass(slots=True)
class ClosureReadFailure(Exception):
    """Stable closure-read failure translated from a connector error."""

    reason_code: str
    status_code: int
    message: str

    def __post_init__(self) -> None:
        Exception.__init__(self, self.message)


class ClosureGithubClient(Protocol):
    async def get_issue_closure(
        self,
        *,
        repo: str,
        issue_number: int,
    ) -> IssueClosure | None: ...

    async def derive_deploy_states(
        self,
        refs: Mapping[object, tuple[str, str]],
    ) -> DeployStateBatch: ...


class BackendClosureGithubClient:
    """Supply backend credentials to shared typed GitHub read primitives."""

    def __init__(
        self,
        *,
        org_id: str,
        caller_label: str,
        user_id: str | None = None,
    ) -> None:
        self.org_id = str(org_id)
        self.caller_label = str(caller_label)
        self.user_id = user_id

    async def get_issue_closure(
        self,
        *,
        repo: str,
        issue_number: int,
    ) -> IssueClosure | None:
        from brain.systems.runs.tool_catalog.handlers.github import (
            github_issue_closure_for_backend,
        )

        try:
            return await github_issue_closure_for_backend(
                repo_slug=repo,
                issue_number=issue_number,
                org_id=self.org_id,
                user_id=self.user_id,
                caller_label=self.caller_label,
            )
        except GitHubConnectorError as exc:
            raise _closure_read_failure(exc) from exc

    async def derive_deploy_states(
        self,
        refs: Mapping[object, tuple[str, str]],
    ) -> DeployStateBatch:
        from brain.systems.runs.tool_catalog.handlers.github import (
            github_deploy_states_for_backend,
        )

        return await github_deploy_states_for_backend(
            refs,
            org_id=self.org_id,
            user_id=self.user_id,
            caller_label=self.caller_label,
        )


def _closure_read_failure(exc: GitHubConnectorError) -> ClosureReadFailure:
    return ClosureReadFailure(
        reason_code=github_read_reason_code(exc),
        status_code=exc.status_code,
        message=exc.message,
    )
