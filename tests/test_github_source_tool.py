from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from brain.systems.cortex.project_context.github import (
    GitHubConnectorError,
    _issue_payload,
    async_list_repo_issues,
    async_list_repo_pull_requests,
)
from brain.systems.runs.execution_context import bind_agent_context, snapshot_agent_context
from brain.systems.runs.tool_catalog.handlers.github import (
    _github_token_candidates,
    _handle_read_github_source,
)


def test_issue_payload_normalizes_github_issue_shape():
    payload = _issue_payload(
        {
            "id": 1,
            "node_id": "I_1",
            "number": 42,
            "title": "Fix scanner",
            "state": "open",
            "html_url": "https://github.com/acme/app/issues/42",
            "user": {"login": "octo", "id": 7, "html_url": "https://github.com/octo"},
            "assignees": [{"login": "reda", "id": 8, "html_url": "https://github.com/reda"}],
            "labels": [{"name": "bug", "color": "ff0000", "description": "Bug"}],
            "comments": 3,
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-02T00:00:00Z",
            "body": " ".join(["body"] * 400),
        }
    )

    assert payload["type"] == "issue"
    assert payload["number"] == 42
    assert payload["user"]["login"] == "octo"
    assert payload["assignees"][0]["login"] == "reda"
    assert payload["labels"] == [{"name": "bug", "color": "ff0000", "description": "Bug"}]
    assert payload["body"].endswith("...")


@pytest.mark.asyncio
async def test_read_github_source_handler_lists_issues_with_canonical_repo_and_bounded_limit():
    with patch(
        "brain.systems.runs.tool_catalog.handlers.github.async_list_repo_issues",
        new=AsyncMock(return_value={"repo": "uwear-ai/uwear-backend", "issues": []}),
    ) as list_issues:
        result = await _handle_read_github_source(
            repo="https://github.com/uwear-ai/uwear-backend.git",
            labels=["bug"],
            assignee="redawear",
            limit=250,
        )

    assert json.loads(result) == {
        "repo": "uwear-ai/uwear-backend",
        "issues": [],
        "token_secret_key_used": False,
        "token_source": "public",
    }
    list_issues.assert_awaited_once()
    assert list_issues.await_args.args == ("uwear-ai/uwear-backend",)
    assert list_issues.await_args.kwargs["labels"] == ["bug"]
    assert list_issues.await_args.kwargs["assignee"] == "redawear"
    assert list_issues.await_args.kwargs["limit"] == 100


@pytest.mark.asyncio
async def test_github_issue_and_pull_request_lists_return_working_next_page_tokens():
    issues = [
        {"id": number, "number": number, "title": f"Issue {number}", "state": "open"}
        for number in range(1, 5)
    ]
    pulls = [
        {"id": number, "number": number, "title": f"PR {number}", "state": "open"}
        for number in range(1, 4)
    ]

    with patch(
        "brain.systems.cortex.project_context.github._async_request",
        new=AsyncMock(return_value=issues),
    ):
        first_issues = await async_list_repo_issues("acme/widgets", limit=2)
        second_issues = await async_list_repo_issues(
            "acme/widgets",
            limit=2,
            cursor=first_issues["next_page"],
        )

    assert [item["number"] for item in first_issues["issues"]] == [1, 2]
    assert first_issues["truncated"] is True
    assert [item["number"] for item in second_issues["issues"]] == [3, 4]
    assert second_issues["truncated"] is False
    assert second_issues["evidence_health"] == {"status": "ok", "completeness": "complete"}

    with patch(
        "brain.systems.cortex.project_context.github._async_request",
        new=AsyncMock(return_value=pulls),
    ):
        first_pulls = await async_list_repo_pull_requests("acme/widgets", limit=2)
        second_pulls = await async_list_repo_pull_requests(
            "acme/widgets",
            limit=2,
            cursor=first_pulls["next_page"],
        )

    assert [item["number"] for item in first_pulls["pull_requests"]] == [1, 2]
    assert first_pulls["next_page"]
    assert [item["number"] for item in second_pulls["pull_requests"]] == [3]
    assert second_pulls["next_page"] is None


@pytest.mark.asyncio
async def test_read_github_source_falls_back_from_bad_explicit_token_to_available_github_token():
    async def fake_get_secret(key_name, *args, **kwargs):
        return {
            "BAD_GITHUB_TOKEN": "bad-token",
            "GITHUB_TOKEN": "good-token",
        }.get(key_name)

    async def fake_list_secrets(*args, **kwargs):
        return [
            {
                "key_name": "GITHUB_TOKEN",
                "category": "general",
                "agent_access_level": "available",
            }
        ]

    with bind_agent_context({"user_id": "user-1", "org_id": "org-1"}), patch(
        "brain.systems.runs.tool_catalog.handlers.github.async_get_secret",
        new=AsyncMock(side_effect=fake_get_secret),
    ), patch(
        "brain.systems.runs.tool_catalog.handlers.github.async_list_secrets",
        new=AsyncMock(side_effect=fake_list_secrets),
    ), patch(
        "brain.systems.runs.tool_catalog.handlers.github.async_resolve_project_bound_env_tokens",
        new=AsyncMock(return_value={}),
    ), patch(
        "brain.systems.runs.tool_catalog.handlers.github.async_list_repo_issues",
        new=AsyncMock(
            side_effect=[
                GitHubConnectorError(
                    status_code=404,
                    message="Repository not found or not visible to this token.",
                ),
                {"repo": "uwear-ai/uwear-backend", "issues": []},
            ]
        ),
    ) as list_issues:
        result = await _handle_read_github_source(
            repo="uwear-ai/uwear-backend",
            token_secret_key="BAD_GITHUB_TOKEN",
        )

    payload = json.loads(result)
    assert payload == {
        "repo": "uwear-ai/uwear-backend",
        "issues": [],
        "token_secret_key_used": True,
        "token_source": "vault_inventory",
        "fallback_from_status_code": 404,
    }
    assert [call.kwargs["token"] for call in list_issues.await_args_list] == [
        "bad-token",
        "good-token",
    ]


@pytest.mark.asyncio
async def test_read_github_source_falls_back_from_project_binding_to_available_github_token():
    async def fake_get_secret(key_name, *args, **kwargs):
        return {"GITHUB_TOKEN": "good-token"}.get(key_name)

    async def fake_list_secrets(*args, **kwargs):
        return [
            {
                "key_name": "GITHUB_TOKEN",
                "category": "general",
                "agent_access_level": "available",
            }
        ]

    with bind_agent_context({"user_id": "user-1", "org_id": "org-1"}), patch(
        "brain.systems.runs.tool_catalog.handlers.github.async_get_secret",
        new=AsyncMock(side_effect=fake_get_secret),
    ), patch(
        "brain.systems.runs.tool_catalog.handlers.github.async_list_secrets",
        new=AsyncMock(side_effect=fake_list_secrets),
    ), patch(
        "brain.systems.runs.tool_catalog.handlers.github.async_resolve_project_bound_env_tokens",
        new=AsyncMock(return_value={"GH_TOKEN": "expired-token"}),
    ), patch(
        "brain.systems.runs.tool_catalog.handlers.github.async_list_repo_pull_requests",
        new=AsyncMock(
            side_effect=[
                GitHubConnectorError(
                    status_code=401,
                    message="GitHub rejected this Vault token.",
                ),
                {"repo": "uwear-ai/uwear-backend", "pull_requests": []},
            ]
        ),
    ) as list_prs:
        result = await _handle_read_github_source(
            action="list_pull_requests",
            repo="uwear-ai/uwear-backend",
        )

    payload = json.loads(result)
    assert payload == {
        "repo": "uwear-ai/uwear-backend",
        "pull_requests": [],
        "token_secret_key_used": True,
        "token_source": "vault_inventory",
        "fallback_from_status_code": 401,
    }
    assert [call.kwargs["token"] for call in list_prs.await_args_list] == [
        "expired-token",
        "good-token",
    ]


@pytest.mark.asyncio
async def test_github_token_candidates_drop_known_legacy_and_put_primary_before_aliases():
    async def fake_get_secret(key_name, *args, **kwargs):
        return {"GITHUB_TOKEN": "primary-token"}.get(key_name)

    with bind_agent_context({"user_id": "user-1", "org_id": "org-1"}), patch(
        "brain.systems.runs.tool_catalog.handlers.github.async_get_secret",
        new=AsyncMock(side_effect=fake_get_secret),
    ), patch(
        "brain.systems.runs.tool_catalog.handlers.github.async_list_secrets",
        new=AsyncMock(
            return_value=[
                {
                    "key_name": "GITHUB_TOKEN",
                    "category": "github",
                    "agent_access_level": "available",
                }
            ]
        ),
    ), patch(
        "brain.systems.runs.tool_catalog.handlers.github.async_resolve_project_bound_env_tokens",
        new=AsyncMock(
            return_value={
                "GITHUB_TOKEN__JB_LEGACY": "legacy-token",
                "GITHUB_TOKEN__AXEL": "secondary-token",
            }
        ),
    ):
        candidates = await _github_token_candidates(
            repo_slug="uwear-ai/uwear-backend",
            token_secret_key=None,
        )

    assert [candidate["token"] for candidate in candidates] == [
        "primary-token",
        "secondary-token",
    ]
    assert all("LEGACY" not in candidate["source"] for candidate in candidates)


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [401, 404])
async def test_read_github_source_does_not_retry_rejected_token_within_run(status_code: int):
    attempts: list[str | None] = []

    async def fake_list_issues(*args, token=None, **kwargs):
        attempts.append(token)
        if token == "bad-token":
            raise GitHubConnectorError(status_code=status_code, message="Not visible")
        return {"repo": "uwear-ai/uwear-backend", "issues": []}

    bound_tokens = {
        "GITHUB_TOKEN": "bad-token",
        "GITHUB_TOKEN__AXEL": "good-token",
    }
    with bind_agent_context({"user_id": "user-1", "org_id": "org-1"}), patch(
        "brain.systems.runs.tool_catalog.handlers.github.async_resolve_project_bound_env_tokens",
        new=AsyncMock(return_value=bound_tokens),
    ), patch(
        "brain.systems.runs.tool_catalog.handlers.github.async_list_secrets",
        new=AsyncMock(return_value=[]),
    ), patch(
        "brain.systems.runs.tool_catalog.handlers.github.async_list_repo_issues",
        new=AsyncMock(side_effect=fake_list_issues),
    ):
        tool_context = snapshot_agent_context()
        with bind_agent_context(tool_context):
            first = json.loads(await _handle_read_github_source(repo="uwear-ai/uwear-backend"))
        with bind_agent_context(tool_context):
            second = json.loads(await _handle_read_github_source(repo="uwear-ai/uwear-backend"))

    assert first["fallback_from_status_code"] == status_code
    assert "fallback_from_status_code" not in second
    assert attempts == ["bad-token", "good-token", "good-token"]


@pytest.mark.asyncio
async def test_read_github_source_prefers_repo_winner_for_follow_up_pull_checks():
    attempts: list[tuple[str, str | None]] = []

    async def fake_list_prs(*args, token=None, **kwargs):
        attempts.append(("list", token))
        if token == "explicit-token":
            raise GitHubConnectorError(status_code=403, message="Forbidden")
        return {"repo": "uwear-ai/uwear-backend", "pull_requests": [{"number": 42}]}

    async def fake_get_pull(*args, token=None, **kwargs):
        attempts.append(("detail", token))
        return {
            "repo": "uwear-ai/uwear-backend",
            "pull_request": {"number": 42},
            "checks": {"status": "success", "total_count": 1, "check_runs": []},
        }

    async def fake_get_secret(key_name, *args, **kwargs):
        return {"EXPLICIT_TOKEN": "explicit-token"}.get(key_name)

    with bind_agent_context({"user_id": "user-1", "org_id": "org-1"}), patch(
        "brain.systems.runs.tool_catalog.handlers.github.async_get_secret",
        new=AsyncMock(side_effect=fake_get_secret),
    ), patch(
        "brain.systems.runs.tool_catalog.handlers.github.async_resolve_project_bound_env_tokens",
        new=AsyncMock(return_value={"GITHUB_TOKEN": "winning-token"}),
    ), patch(
        "brain.systems.runs.tool_catalog.handlers.github.async_list_secrets",
        new=AsyncMock(return_value=[]),
    ), patch(
        "brain.systems.runs.tool_catalog.handlers.github.async_list_repo_pull_requests",
        new=AsyncMock(side_effect=fake_list_prs),
    ), patch(
        "brain.systems.runs.tool_catalog.handlers.github.async_get_pull_request",
        new=AsyncMock(side_effect=fake_get_pull),
    ):
        listed = json.loads(
            await _handle_read_github_source(
                action="list_pull_requests",
                repo="uwear-ai/uwear-backend",
                token_secret_key="EXPLICIT_TOKEN",
            )
        )
        detailed = json.loads(
            await _handle_read_github_source(
                action="get_pull_request",
                repo="uwear-ai/uwear-backend",
                pull_number=42,
                token_secret_key="EXPLICIT_TOKEN",
            )
        )

    assert listed["fallback_from_status_code"] == 403
    assert detailed["checks"]["status"] == "success"
    assert attempts == [
        ("list", "explicit-token"),
        ("list", "winning-token"),
        ("detail", "winning-token"),
    ]


@pytest.mark.asyncio
async def test_pull_check_read_uses_primary_without_trying_bound_legacy_token():
    attempts: list[str | None] = []

    async def fake_get_secret(key_name, *args, **kwargs):
        return {"GITHUB_TOKEN": "primary-token"}.get(key_name)

    async def fake_get_pull(*args, token=None, **kwargs):
        attempts.append(token)
        if token == "legacy-token":
            raise GitHubConnectorError(status_code=404, message="Not visible")
        return {
            "repo": "uwear-ai/uwear-backend",
            "pull_request": {"number": 42, "mergeable": True},
            "checks": {"status": "success", "total_count": 2, "check_runs": []},
        }

    with bind_agent_context({"user_id": "user-1", "org_id": "org-1"}), patch(
        "brain.systems.runs.tool_catalog.handlers.github.async_get_secret",
        new=AsyncMock(side_effect=fake_get_secret),
    ), patch(
        "brain.systems.runs.tool_catalog.handlers.github.async_resolve_project_bound_env_tokens",
        new=AsyncMock(return_value={"GITHUB_TOKEN__JB_LEGACY": "legacy-token"}),
    ), patch(
        "brain.systems.runs.tool_catalog.handlers.github.async_list_secrets",
        new=AsyncMock(
            return_value=[
                {
                    "key_name": "GITHUB_TOKEN",
                    "category": "github",
                    "agent_access_level": "available",
                }
            ]
        ),
    ), patch(
        "brain.systems.runs.tool_catalog.handlers.github.async_get_pull_request",
        new=AsyncMock(side_effect=fake_get_pull),
    ):
        result = json.loads(
            await _handle_read_github_source(
                action="get_pull_request",
                repo="uwear-ai/uwear-backend",
                pull_number=42,
            )
        )

    assert result["checks"]["status"] == "success"
    assert result["pull_request"]["mergeable"] is True
    assert "fallback_from_status_code" not in result
    assert attempts == ["primary-token"]


@pytest.mark.asyncio
async def test_read_github_source_still_falls_back_to_normal_secondary_token():
    attempts: list[str | None] = []

    async def fake_list_prs(*args, token=None, **kwargs):
        attempts.append(token)
        if token == "primary-token":
            raise GitHubConnectorError(status_code=401, message="Expired")
        return {"repo": "uwear-ai/uwear-backend", "pull_requests": []}

    with bind_agent_context({"user_id": "user-1", "org_id": "org-1"}), patch(
        "brain.systems.runs.tool_catalog.handlers.github.async_resolve_project_bound_env_tokens",
        new=AsyncMock(
            return_value={
                "GITHUB_TOKEN": "primary-token",
                "GITHUB_TOKEN__AXEL": "secondary-token",
            }
        ),
    ), patch(
        "brain.systems.runs.tool_catalog.handlers.github.async_list_secrets",
        new=AsyncMock(return_value=[]),
    ), patch(
        "brain.systems.runs.tool_catalog.handlers.github.async_list_repo_pull_requests",
        new=AsyncMock(side_effect=fake_list_prs),
    ):
        result = json.loads(
            await _handle_read_github_source(
                action="list_pull_requests",
                repo="uwear-ai/uwear-backend",
            )
        )

    assert result["token_source"] == "project_binding:GITHUB_TOKEN__AXEL"
    assert attempts == ["primary-token", "secondary-token"]


@pytest.mark.asyncio
async def test_pull_request_reader_fetches_details_and_checks_with_same_token():
    from brain.systems.cortex.project_context.github import async_get_pull_request

    with patch(
        "brain.systems.cortex.project_context.github._async_request",
        new=AsyncMock(
            side_effect=[
                {
                    "number": 42,
                    "state": "open",
                    "mergeable": True,
                    "mergeable_state": "clean",
                    "head": {"ref": "fix-ci", "sha": "abc123"},
                    "base": {"ref": "staging", "sha": "def456"},
                },
                {
                    "total_count": 2,
                    "check_runs": [
                        {"name": "unit", "status": "completed", "conclusion": "success"},
                        {"name": "lint", "status": "completed", "conclusion": "failure"},
                    ],
                },
                {"state": "failure", "statuses": []},
            ]
        ),
    ) as request:
        result = await async_get_pull_request(
            "uwear-ai/uwear-backend",
            42,
            token="primary-token",
        )

    assert result["pull_request"]["mergeable"] is True
    assert result["pull_request"]["mergeable_state"] == "clean"
    assert result["checks"]["status"] == "failure"
    assert result["checks"]["total_count"] == 2
    assert {
        key: result["checks"][key]
        for key in ("total", "success", "failure", "pending")
    } == {"total": 2, "success": 1, "failure": 1, "pending": 0}
    assert result["combined_status"] == "failure"
    assert [call.args[2] for call in request.await_args_list] == [
        "/repos/uwear-ai/uwear-backend/pulls/42",
        "/repos/uwear-ai/uwear-backend/commits/abc123/check-runs",
        "/repos/uwear-ai/uwear-backend/commits/abc123/status",
    ]
    assert [call.kwargs["token"] for call in request.await_args_list] == [
        "primary-token",
        "primary-token",
        "primary-token",
    ]


@pytest.mark.asyncio
async def test_get_pull_request_uses_project_bound_token_for_mergeability_and_ci():
    with bind_agent_context({"user_id": "user-1", "org_id": "org-1"}), patch(
        "brain.systems.runs.tool_catalog.handlers.github.async_resolve_project_bound_env_tokens",
        new=AsyncMock(return_value={"GITHUB_TOKEN": "app-token"}),
    ), patch(
        "brain.systems.runs.tool_catalog.handlers.github.async_list_secrets",
        new=AsyncMock(return_value=[]),
    ), patch(
        "brain.systems.cortex.project_context.github._async_request",
        new=AsyncMock(
            side_effect=[
                {
                    "number": 859,
                    "state": "open",
                    "mergeable": True,
                    "mergeable_state": "clean",
                    "head": {"sha": "head859"},
                },
                {
                    "total_count": 2,
                    "check_runs": [
                        {"name": "unit", "status": "completed", "conclusion": "success"},
                        {"name": "lint", "status": "completed", "conclusion": "success"},
                    ],
                },
                {"state": "success", "statuses": []},
            ]
        ),
    ) as request:
        result = json.loads(
            await _handle_read_github_source(
                action="get_pull_request",
                repo="uwear-ai/uwear-backend",
                number=859,
            )
        )

    assert result["pull_request"]["mergeable_state"] == "clean"
    assert result["checks"] == {
        "status": "success",
        "total_count": 2,
        "total": 2,
        "success": 2,
        "failure": 0,
        "pending": 0,
        "check_runs": [
            {
                "name": "unit",
                "status": "completed",
                "conclusion": "success",
                "details_url": None,
                "started_at": None,
                "completed_at": None,
            },
            {
                "name": "lint",
                "status": "completed",
                "conclusion": "success",
                "details_url": None,
                "started_at": None,
                "completed_at": None,
            },
        ],
    }
    assert result["combined_status"] == "success"
    assert result["token_source"] == "project_binding:GITHUB_TOKEN"
    assert [call.args[2] for call in request.await_args_list] == [
        "/repos/uwear-ai/uwear-backend/pulls/859",
        "/repos/uwear-ai/uwear-backend/commits/head859/check-runs",
        "/repos/uwear-ai/uwear-backend/commits/head859/status",
    ]
    assert all(call.kwargs["token"] == "app-token" for call in request.await_args_list)


@pytest.mark.asyncio
async def test_pull_request_checks_action_reads_checks_and_combined_status():
    with patch(
        "brain.systems.runs.tool_catalog.handlers.github.async_get_pull_request_checks",
        new=AsyncMock(
            return_value={
                "repo": "uwear-ai/uwear-backend",
                "sha": "abc123",
                "checks": {"total": 1, "success": 0, "failure": 0, "pending": 1},
                "combined_status": "pending",
            }
        ),
    ) as get_checks:
        result = json.loads(
            await _handle_read_github_source(
                action="pull_request_checks",
                repo="uwear-ai/uwear-backend",
                sha="abc123",
            )
        )

    assert result["checks"] == {"total": 1, "success": 0, "failure": 0, "pending": 1}
    assert result["combined_status"] == "pending"
    get_checks.assert_awaited_once_with(
        "uwear-ai/uwear-backend",
        "abc123",
        token=None,
    )


@pytest.mark.asyncio
async def test_pull_request_detail_reports_all_denied_token_sources():
    with bind_agent_context({"user_id": "user-1", "org_id": "org-1"}), patch(
        "brain.systems.runs.tool_catalog.handlers.github.async_resolve_project_bound_env_tokens",
        new=AsyncMock(
            return_value={
                "GITHUB_TOKEN": "primary-token",
                "GITHUB_TOKEN__AXEL": "secondary-token",
            }
        ),
    ), patch(
        "brain.systems.runs.tool_catalog.handlers.github.async_list_secrets",
        new=AsyncMock(return_value=[]),
    ), patch(
        "brain.systems.runs.tool_catalog.handlers.github.async_get_pull_request",
        new=AsyncMock(
            side_effect=[
                GitHubConnectorError(status_code=403, message="Forbidden"),
                GitHubConnectorError(status_code=404, message="Not visible"),
            ]
        ),
    ):
        result = json.loads(
            await _handle_read_github_source(
                action="get_pull_request",
                repo="uwear-ai/uwear-backend",
                number=859,
            )
        )

    assert result == {
        "error": "Not visible",
        "status_code": 404,
        "attempted_token_sources": [
            "project_binding:GITHUB_TOKEN",
            "project_binding:GITHUB_TOKEN__AXEL",
        ],
    }


@pytest.mark.asyncio
async def test_authenticated_github_search_reader_returns_exact_repo_counts():
    from brain.systems.cortex.project_context.github import async_get_repo_counts

    with patch(
        "brain.systems.cortex.project_context.github._async_request",
        new=AsyncMock(
            side_effect=[
                {"total_count": 17, "items": []},
                {"total_count": 4, "items": []},
            ]
        ),
    ) as request:
        result = await async_get_repo_counts(
            "uwear-ai/uwear-backend",
            token="primary-token",
            state="open",
        )

    assert result == {
        "repo": "uwear-ai/uwear-backend",
        "state": "open",
        "counts": {"issues": 17, "pull_requests": 4},
        "total_count": 21,
    }
    assert [call.kwargs["token"] for call in request.await_args_list] == [
        "primary-token",
        "primary-token",
    ]
    assert [call.kwargs["params"]["q"] for call in request.await_args_list] == [
        "repo:uwear-ai/uwear-backend is:issue is:open",
        "repo:uwear-ai/uwear-backend is:pr is:open",
    ]


@pytest.mark.asyncio
async def test_read_github_source_exact_counts_action_uses_authenticated_reader():
    with bind_agent_context({"user_id": "user-1", "org_id": "org-1"}), patch(
        "brain.systems.runs.tool_catalog.handlers.github.async_resolve_project_bound_env_tokens",
        new=AsyncMock(return_value={"GITHUB_TOKEN": "primary-token"}),
    ), patch(
        "brain.systems.runs.tool_catalog.handlers.github.async_list_secrets",
        new=AsyncMock(return_value=[]),
    ), patch(
        "brain.systems.runs.tool_catalog.handlers.github.async_get_repo_counts",
        new=AsyncMock(
            return_value={
                "repo": "uwear-ai/uwear-backend",
                "state": "open",
                "counts": {"issues": 17, "pull_requests": 4},
                "total_count": 21,
            }
        ),
    ) as get_counts:
        result = json.loads(
            await _handle_read_github_source(
                action="get_counts",
                repo="uwear-ai/uwear-backend",
            )
        )

    assert result["counts"] == {"issues": 17, "pull_requests": 4}
    assert result["total_count"] == 21
    get_counts.assert_awaited_once_with(
        "uwear-ai/uwear-backend",
        token="primary-token",
        state="open",
    )


@pytest.mark.asyncio
async def test_exact_counts_retry_search_422_with_healthy_secondary_token():
    attempts: list[str | None] = []

    async def fake_get_counts(repo, *, token, state):
        attempts.append(token)
        if token == "primary-token":
            raise GitHubConnectorError(status_code=422, message="Validation Failed")
        return {
            "repo": repo,
            "state": state,
            "counts": {"issues": 17, "pull_requests": 4},
            "total_count": 21,
        }

    with bind_agent_context({"user_id": "user-1", "org_id": "org-1"}), patch(
        "brain.systems.runs.tool_catalog.handlers.github.async_resolve_project_bound_env_tokens",
        new=AsyncMock(
            return_value={
                "GITHUB_TOKEN": "primary-token",
                "GITHUB_TOKEN__AXEL": "secondary-token",
            }
        ),
    ), patch(
        "brain.systems.runs.tool_catalog.handlers.github.async_list_secrets",
        new=AsyncMock(return_value=[]),
    ), patch(
        "brain.systems.runs.tool_catalog.handlers.github.async_get_repo_counts",
        new=AsyncMock(side_effect=fake_get_counts),
    ):
        result = json.loads(
            await _handle_read_github_source(
                action="get_counts",
                repo="uwear-ai/uwear-backend",
            )
        )

    assert result["counts"] == {"issues": 17, "pull_requests": 4}
    assert result["fallback_from_status_code"] == 422
    assert attempts == ["primary-token", "secondary-token"]
