"""GitHub credential adapter for the production-gate closure sweep."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from brain.systems.cortex.project_context.github import (
    GithubFixingPullRequest as FixingPullRequest,
    GithubIssueClosure as IssueClosure,
)
from brain.systems.deploy_state import DeployStateBatch


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

    def __init__(self, *, org_id: str, user_id: str | None = None) -> None:
        self.org_id = str(org_id)
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

        return await github_issue_closure_for_backend(
            repo_slug=repo,
            issue_number=issue_number,
            org_id=self.org_id,
            user_id=self.user_id,
        )

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
        )
