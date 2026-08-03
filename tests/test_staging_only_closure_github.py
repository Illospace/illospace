"""GitHub client-boundary coverage for staging-only closure reads."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from brain.systems.cortex.project_context.github import (
    GitHubConnectorError,
    GithubFixingPullRequest,
    GithubIssueClosure,
    async_get_issue_closure_info,
)
from brain.systems.deploy_state import DeployStateBatch
from brain.systems.deploy_state_github import ancestry_failure
from brain.systems.production_gate_github import (
    CLOSURE_READ_ACCESS_FORBIDDEN,
    CLOSURE_READ_AUTHENTICATION_REQUIRED,
    CLOSURE_READ_CONNECTOR_ERROR,
    BackendClosureGithubClient,
    ClosureReadFailure,
)


@pytest.mark.asyncio
async def test_issue_closure_read_resolves_closer_and_fixing_pr_deploy_facts():
    issue = {
        "number": 1281,
        "title": "PostgreSQL deadlock",
        "state": "closed",
        "closed_at": "2026-07-27T09:08:19Z",
        "closed_by": {"login": "uwear-claw"},
    }
    graphql = {
        "data": {
            "repository": {
                "issue": {
                    "closedByPullRequestsReferences": {
                        "nodes": [
                            {
                                "number": 1305,
                                "baseRefName": "staging",
                                "mergedAt": "2026-07-27T09:01:00Z",
                                "mergeCommit": {"oid": "a" * 40},
                                "repository": {
                                    "nameWithOwner": "uwear-ai/uwear-backend"
                                },
                            }
                        ]
                    }
                }
            }
        }
    }

    with patch(
        "brain.systems.cortex.project_context.github._async_request",
        new=AsyncMock(side_effect=[issue, graphql]),
    ) as request:
        result = await async_get_issue_closure_info(
            "uwear-ai/uwear-backend",
            1281,
            token="read-token",
        )

    assert result == GithubIssueClosure(
        repo="uwear-ai/uwear-backend",
        number=1281,
        title="PostgreSQL deadlock",
        state="closed",
        closed_at=datetime(2026, 7, 27, 9, 8, 19, tzinfo=timezone.utc),
        closed_by="uwear-claw",
        fixing_pull_requests=(
            GithubFixingPullRequest(
                repo="uwear-ai/uwear-backend",
                number=1305,
                base_ref_name="staging",
                merge_commit_sha="a" * 40,
                merged_at=datetime(2026, 7, 27, 9, 1, tzinfo=timezone.utc),
            ),
        ),
    )
    assert [call.args[1:3] for call in request.await_args_list] == [
        ("GET", "/repos/uwear-ai/uwear-backend/issues/1281"),
        ("POST", "/graphql"),
    ]
    assert [call.kwargs["token"] for call in request.await_args_list] == [
        "read-token",
        "read-token",
    ]


@pytest.mark.asyncio
async def test_org_only_backend_closure_read_resolves_authenticated_project_token(
    monkeypatch,
):
    import brain.systems.runs.tool_catalog.handlers.github as handler

    closure = GithubIssueClosure(
        repo="uwear-ai/uwear-backend",
        number=1281,
        title="PostgreSQL deadlock",
        state="closed",
        closed_at=datetime(2026, 7, 27, 9, 8, 19, tzinfo=timezone.utc),
        closed_by="uwear-claw",
        fixing_pull_requests=(),
    )
    resolve_bound = AsyncMock(
        return_value={"GITHUB_TOKEN": "installation-token"}
    )
    closure_read = AsyncMock(return_value=closure)
    monkeypatch.setattr(
        handler,
        "async_resolve_org_project_bound_env_tokens",
        resolve_bound,
    )
    monkeypatch.setattr(handler, "async_get_issue_closure_info", closure_read)

    result = await handler.github_issue_closure_for_backend(
        repo_slug="uwear-ai/uwear-backend",
        issue_number=1281,
        org_id="org-1",
        caller_label="staging_only_closure_sweep",
    )

    assert result is closure
    resolve_bound.assert_awaited_once_with(
        org_id="org-1",
        accessed_by="staging_only_closure_sweep",
        project_slug="uwear-ai/uwear-backend",
        project_slugs=None,
        github_app_only=False,
    )
    closure_read.assert_awaited_once_with(
        "uwear-ai/uwear-backend",
        1281,
        token="installation-token",
    )


@pytest.mark.asyncio
async def test_org_only_backend_closure_read_never_falls_back_to_public(
    monkeypatch,
):
    import brain.systems.runs.tool_catalog.handlers.github as handler

    monkeypatch.setattr(
        handler,
        "async_resolve_org_project_bound_env_tokens",
        AsyncMock(return_value={}),
    )
    closure_read = AsyncMock()
    monkeypatch.setattr(handler, "async_get_issue_closure_info", closure_read)

    with pytest.raises(
        GitHubConnectorError,
        match="No GitHub token candidates were available",
    ):
        await handler.github_issue_closure_for_backend(
            repo_slug="uwear-ai/uwear-backend",
            issue_number=1281,
            org_id="org-1",
            caller_label="staging_only_closure_sweep",
        )

    closure_read.assert_not_awaited()


@pytest.mark.asyncio
async def test_backend_adapter_uses_shared_closure_and_deploy_state_reads(monkeypatch):
    import brain.systems.runs.tool_catalog.handlers.github as handler

    closure_read = AsyncMock(
        return_value=GithubIssueClosure(
            repo="uwear-ai/uwear-backend",
            number=1281,
            title="PostgreSQL deadlock",
            state="closed",
            closed_at=datetime(2026, 7, 27, 9, 8, 19, tzinfo=timezone.utc),
            closed_by="uwear-claw",
            fixing_pull_requests=(
                GithubFixingPullRequest(
                    repo="uwear-ai/uwear-backend",
                    number=1305,
                    base_ref_name="staging",
                    merge_commit_sha="a" * 40,
                    merged_at=datetime(2026, 7, 27, 9, 1, tzinfo=timezone.utc),
                ),
            ),
        )
    )
    expected_batch = DeployStateBatch(
        {},
        observations_by_key={},
        observations_by_ref={},
    )
    deploy_read = AsyncMock(return_value=expected_batch)
    monkeypatch.setattr(
        handler,
        "github_issue_closure_for_backend",
        closure_read,
    )
    monkeypatch.setattr(
        handler,
        "github_deploy_states_for_backend",
        deploy_read,
    )
    client = BackendClosureGithubClient(
        org_id="org-1",
        user_id="user-1",
        caller_label="staging_only_closure_sweep",
    )

    closure = await client.get_issue_closure(
        repo="uwear-ai/uwear-backend",
        issue_number=1281,
    )
    batch = await client.derive_deploy_states(
        {"key": ("uwear-ai/uwear-backend", "a" * 40)}
    )

    assert closure is not None
    assert closure.fixing_pull_requests[0].base_ref_name == "staging"
    assert closure.fixing_pull_requests[0].merge_commit_sha == "a" * 40
    assert batch is expected_batch
    closure_read.assert_awaited_once_with(
        repo_slug="uwear-ai/uwear-backend",
        issue_number=1281,
        org_id="org-1",
        user_id="user-1",
        caller_label="staging_only_closure_sweep",
    )
    deploy_read.assert_awaited_once_with(
        {"key": ("uwear-ai/uwear-backend", "a" * 40)},
        org_id="org-1",
        user_id="user-1",
        caller_label="staging_only_closure_sweep",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "reason_code"),
    [
        (401, CLOSURE_READ_AUTHENTICATION_REQUIRED),
        (403, CLOSURE_READ_ACCESS_FORBIDDEN),
        (404, CLOSURE_READ_ACCESS_FORBIDDEN),
        (503, CLOSURE_READ_CONNECTOR_ERROR),
    ],
)
async def test_backend_adapter_translates_connector_errors(
    monkeypatch,
    status_code,
    reason_code,
):
    import brain.systems.runs.tool_catalog.handlers.github as handler

    monkeypatch.setattr(
        handler,
        "github_issue_closure_for_backend",
        AsyncMock(
            side_effect=GitHubConnectorError(
                status_code=status_code,
                message="stable test message",
            )
        ),
    )
    client = BackendClosureGithubClient(
        org_id="org-1",
        caller_label="staging_only_closure_sweep",
    )

    with pytest.raises(ClosureReadFailure) as exc_info:
        await client.get_issue_closure(
            repo="uwear-ai/uwear-backend",
            issue_number=1281,
        )

    assert exc_info.value.reason_code == reason_code
    assert exc_info.value.status_code == status_code
    assert exc_info.value.message == "stable test message"


@pytest.mark.asyncio
async def test_backend_deploy_state_empty_candidates_raise_credential_failure(
    monkeypatch,
):
    import brain.systems.runs.tool_catalog.handlers.github as handler

    candidates = AsyncMock(return_value=[])
    healthy_batch = DeployStateBatch(
        {},
        observations_by_key={},
        observations_by_ref={},
    )
    derive = AsyncMock(return_value=healthy_batch)
    captured_errors = []

    def capture_failure(branch, exc):
        captured_errors.append(exc)
        return ancestry_failure(branch, exc)

    monkeypatch.setattr(handler, "_github_token_candidates", candidates)
    monkeypatch.setattr(handler, "derive_deploy_states", derive)
    monkeypatch.setattr(handler, "ancestry_failure", capture_failure)

    batch = await handler.github_deploy_states_for_backend(
        {"fix": ("uwear-ai/uwear-backend", "a" * 40)},
        org_id="org-1",
        caller_label="staging_only_closure_sweep",
    )

    assert batch["fix"] is None
    assert len(captured_errors) == 1
    assert isinstance(captured_errors[0], GitHubConnectorError)
    assert captured_errors[0].status_code == 401
    assert captured_errors[0].message == "No GitHub token candidates were available"
    derive.assert_awaited_once_with({}, tokens={})
