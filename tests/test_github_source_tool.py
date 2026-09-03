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
    assert list_issues.await_args.kwargs["token"] is None


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
    ]
    assert [call.kwargs["token"] for call in request.await_args_list] == [
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
    ]
    assert all(call.kwargs["token"] == "app-token" for call in request.await_args_list)


@pytest.mark.asyncio
async def test_pull_request_checks_action_reads_checks_and_combined_status():
    with patch(
        "brain.systems.cortex.project_context.github._async_request",
        new=AsyncMock(
            return_value={
                "total_count": 1,
                "check_runs": [
                    {"name": "unit", "status": "in_progress", "conclusion": None},
                ],
            }
        ),
    ) as request:
        result = json.loads(
            await _handle_read_github_source(
                action="pull_request_checks",
                repo="uwear-ai/uwear-backend",
                sha="abc123",
            )
        )

    assert result["checks"]["status"] == "pending"
    assert result["combined_status"] == "pending"
    requested_urls = [call.args[2] for call in request.await_args_list]
    assert requested_urls == [
        "/repos/uwear-ai/uwear-backend/commits/abc123/check-runs",
    ]
    assert "/repos/uwear-ai/uwear-backend/commits/abc123/status" not in requested_urls


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


def test_read_github_source_schema_exposes_pinned_source_actions():
    from brain.systems.runs.tool_catalog.definitions.github import GITHUB_TOOLS

    definition = next(tool for tool in GITHUB_TOOLS if tool["name"] == "read_github_source")
    properties = definition["input_schema"]["properties"]

    assert {"get_file", "list_tree", "grep"} <= set(properties["action"]["enum"])
    assert properties["ref"]["type"] == "string"
    assert properties["path"]["type"] == "string"
    assert properties["query"]["type"] == "string"
    assert properties["query"]["maxLength"] == 500
    assert properties["ref"]["maxLength"] == 512
    assert properties["path"]["maxLength"] == 4096
    assert properties["case_sensitive"]["default"] is False
    assert properties["line_start"]["minimum"] == 1
    assert properties["line_end"]["minimum"] == 1


def test_read_github_source_registration_advertises_bounded_source_evidence():
    from brain.systems.runs.tool_catalog.registry import get_tool_registration
    from brain.systems.runs.tool_definitions import COORDINATOR_TOOLS, WORKER_TOOLS

    registration = get_tool_registration("read_github_source")
    coordinator_names = {tool["name"] for tool in COORDINATOR_TOOLS}
    worker_names = {tool["name"] for tool in WORKER_TOOLS}

    assert registration is not None
    assert registration.side_effect_class == "read_only"
    assert registration.output_budget_chars == 18_000
    assert "pinned source" in registration.expected_effect
    assert "read_github_source" in coordinator_names
    assert "read_github_source" in worker_names


@pytest.mark.asyncio
async def test_get_file_reads_numbered_lines_at_resolved_ref_with_project_token():
    import base64

    requests: list[tuple[str, str | None, dict | None]] = []
    commit_sha = "a" * 40
    tree_sha = "b" * 40

    async def fake_request(client, method, path, *, token=None, params=None, json=None):
        requests.append((path, token, params))
        if path.endswith("/commits/main"):
            return {"sha": commit_sha, "commit": {"tree": {"sha": tree_sha}}}
        if path.endswith("/contents/brain/app.py"):
            content = b"first\nneedle = True\nlast\n"
            return {
                "type": "file",
                "path": "brain/app.py",
                "sha": "blob-sha",
                "size": len(content),
                "encoding": "base64",
                "content": base64.b64encode(content).decode("ascii"),
            }
        raise AssertionError(path)

    with bind_agent_context({"user_id": "user-1", "org_id": "org-1"}), patch(
        "brain.systems.runs.tool_catalog.handlers.github.async_resolve_project_bound_env_tokens",
        new=AsyncMock(return_value={"GITHUB_TOKEN": "installation-token"}),
    ), patch(
        "brain.systems.runs.tool_catalog.handlers.github.async_list_secrets",
        new=AsyncMock(return_value=[]),
    ), patch(
        "brain.systems.cortex.project_context.github._async_request",
        new=AsyncMock(side_effect=fake_request),
    ):
        payload = json.loads(
            await _handle_read_github_source(
                action="get_file",
                repo="uwear-ai/uwear-backend",
                ref="main",
                path="/brain/app.py",
                line_start=2,
                line_end=3,
            )
        )

    assert payload["resolved_ref"] == commit_sha
    assert payload["path"] == "brain/app.py"
    assert payload["content"] == "2: needle = True\n3: last"
    assert payload["citation_range"] == "brain/app.py:2-3"
    assert payload["truncated"] is False
    assert payload["token_source"] == "project_binding:GITHUB_TOKEN"
    assert requests == [
        ("/repos/uwear-ai/uwear-backend/commits/main", "installation-token", None),
        (
            "/repos/uwear-ai/uwear-backend/contents/brain/app.py",
            "installation-token",
            {"ref": commit_sha},
        ),
    ]


@pytest.mark.asyncio
async def test_get_file_stays_below_tool_output_budget_for_escape_heavy_source():
    import base64

    commit_sha = "1" * 40
    tree_sha = "2" * 40
    source = ("\\" * 20_000).encode("utf-8")
    requested_ref = "r" * 512
    requested_path = f"{'p' * 4092}.txt"

    async def fake_request(client, method, path, *, token=None, params=None, json=None):
        if "/commits/" in path:
            return {"sha": commit_sha, "commit": {"tree": {"sha": tree_sha}}}
        return {
            "type": "file",
            "path": "generated.txt",
            "sha": "blob-sha",
            "size": len(source),
            "encoding": "base64",
            "content": base64.b64encode(source).decode("ascii"),
        }

    with patch(
        "brain.systems.cortex.project_context.github._async_request",
        new=AsyncMock(side_effect=fake_request),
    ):
        result = await _handle_read_github_source(
            action="get_file",
            repo="acme/widgets",
            ref=requested_ref,
            path=requested_path,
        )

    payload = json.loads(result)
    assert len(result) < 18_000
    assert "error" not in payload, payload
    assert payload["requested_ref"] == requested_ref
    assert payload["path"] == requested_path
    assert payload["truncated"] is True
    assert payload["line_truncated"] is True
    assert payload["evidence_health"] == {"status": "warning", "completeness": "line_truncated"}


@pytest.mark.asyncio
async def test_get_file_uses_existing_auth_fallback_for_a_rejected_project_token():
    import base64

    attempts: list[str | None] = []
    commit_sha = "9" * 40
    tree_sha = "0" * 40

    async def fake_request(client, method, path, *, token=None, params=None, json=None):
        attempts.append(token)
        if token == "rejected-token":
            raise GitHubConnectorError(status_code=403, message="Forbidden")
        if "/commits/" in path:
            return {"sha": commit_sha, "commit": {"tree": {"sha": tree_sha}}}
        source = b"healthy\n"
        return {
            "type": "file",
            "path": "health.py",
            "sha": "blob",
            "size": len(source),
            "encoding": "base64",
            "content": base64.b64encode(source).decode("ascii"),
        }

    with bind_agent_context({"user_id": "user-1", "org_id": "org-1"}), patch(
        "brain.systems.runs.tool_catalog.handlers.github.async_resolve_project_bound_env_tokens",
        new=AsyncMock(
            return_value={
                "GITHUB_TOKEN": "rejected-token",
                "GITHUB_TOKEN__SECONDARY": "healthy-token",
            }
        ),
    ), patch(
        "brain.systems.runs.tool_catalog.handlers.github.async_list_secrets",
        new=AsyncMock(return_value=[]),
    ), patch(
        "brain.systems.cortex.project_context.github._async_request",
        new=AsyncMock(side_effect=fake_request),
    ):
        payload = json.loads(
            await _handle_read_github_source(
                action="get_file",
                repo="acme/widgets",
                ref="main",
                path="health.py",
            )
        )

    assert payload["content"] == "1: healthy"
    assert payload["token_source"] == "project_binding:GITHUB_TOKEN__SECONDARY"
    assert payload["fallback_from_status_code"] == 403
    assert attempts == ["rejected-token", "healthy-token", "healthy-token"]


@pytest.mark.asyncio
async def test_missing_source_path_does_not_poison_same_run_token_for_valid_read():
    import base64

    commit_sha = "7" * 40
    tree_sha = "8" * 40
    requested_paths: list[str] = []

    async def fake_request(client, method, path, *, token=None, params=None, json=None):
        if "/commits/" in path:
            return {"sha": commit_sha, "commit": {"tree": {"sha": tree_sha}}}
        requested_paths.append(path)
        if path.endswith("/contents/missing.py"):
            raise GitHubConnectorError(status_code=404, message="Path not found")
        source = b"healthy\n"
        return {
            "type": "file",
            "path": "valid.py",
            "sha": "blob",
            "size": len(source),
            "encoding": "base64",
            "content": base64.b64encode(source).decode("ascii"),
        }

    with bind_agent_context({"user_id": "user-1", "org_id": "org-1"}), patch(
        "brain.systems.runs.tool_catalog.handlers.github.async_resolve_project_bound_env_tokens",
        new=AsyncMock(return_value={"GITHUB_TOKEN": "healthy-token"}),
    ), patch(
        "brain.systems.runs.tool_catalog.handlers.github.async_list_secrets",
        new=AsyncMock(return_value=[]),
    ), patch(
        "brain.systems.cortex.project_context.github._async_request",
        new=AsyncMock(side_effect=fake_request),
    ):
        missing = json.loads(
            await _handle_read_github_source(
                action="get_file",
                repo="acme/widgets",
                ref="main",
                path="missing.py",
            )
        )
        valid = json.loads(
            await _handle_read_github_source(
                action="get_file",
                repo="acme/widgets",
                ref="main",
                path="valid.py",
            )
        )

    assert missing["status_code"] == 404
    assert valid["content"] == "1: healthy"
    assert requested_paths == [
        "/repos/acme/widgets/contents/missing.py",
        "/repos/acme/widgets/contents/valid.py",
    ]


@pytest.mark.asyncio
async def test_get_file_rejects_a_line_start_beyond_end_of_file():
    import base64

    source = b"one\ntwo\nthree\n"

    async def fake_request(client, method, path, *, token=None, params=None, json=None):
        if "/commits/" in path:
            return {"sha": "a" * 40, "commit": {"tree": {"sha": "b" * 40}}}
        return {
            "type": "file",
            "path": "short.py",
            "sha": "blob",
            "size": len(source),
            "encoding": "base64",
            "content": base64.b64encode(source).decode("ascii"),
        }

    with patch(
        "brain.systems.cortex.project_context.github._async_request",
        new=AsyncMock(side_effect=fake_request),
    ):
        payload = json.loads(
            await _handle_read_github_source(
                action="get_file",
                repo="acme/widgets",
                ref="main",
                path="short.py",
                line_start=10,
            )
        )

    assert payload["status_code"] == 416
    assert "line_start" in payload["error"]


@pytest.mark.asyncio
async def test_get_file_returns_a_coherent_empty_result_for_an_empty_file():
    import base64

    async def fake_request(client, method, path, *, token=None, params=None, json=None):
        if "/commits/" in path:
            return {"sha": "a" * 40, "commit": {"tree": {"sha": "b" * 40}}}
        return {
            "type": "file",
            "path": "empty.py",
            "sha": "blob",
            "size": 0,
            "encoding": "base64",
            "content": base64.b64encode(b"").decode("ascii"),
        }

    with patch(
        "brain.systems.cortex.project_context.github._async_request",
        new=AsyncMock(side_effect=fake_request),
    ):
        payload = json.loads(
            await _handle_read_github_source(
                action="get_file",
                repo="acme/widgets",
                ref="main",
                path="empty.py",
            )
        )

    assert payload["total_lines"] == 0
    assert payload["line_start"] == 1
    assert payload["line_end"] is None
    assert payload["content"] == ""
    assert payload["citation_range"] is None
    assert payload["truncated"] is False
    assert payload["evidence_health"] == {"status": "ok", "completeness": "complete"}


@pytest.mark.asyncio
async def test_get_file_fails_boundedly_when_metadata_leaves_no_room_to_advance():
    import base64

    requested_path = "/".join(["\x01" * 97 for _ in range(16)])
    source = b"x\n"

    async def fake_request(client, method, path, *, token=None, params=None, json=None):
        if "/commits/" in path:
            return {"sha": "a" * 40, "commit": {"tree": {"sha": "b" * 40}}}
        return {
            "type": "file",
            "path": requested_path,
            "sha": "blob",
            "size": len(source),
            "encoding": "base64",
            "content": base64.b64encode(source).decode("ascii"),
        }

    with patch(
        "brain.systems.cortex.project_context.github._async_request",
        new=AsyncMock(side_effect=fake_request),
    ):
        result = await _handle_read_github_source(
            action="get_file",
            repo="acme/widgets",
            ref="main",
            path=requested_path,
        )

    payload = json.loads(result)
    assert len(result) < 18_000
    assert payload["status_code"] == 413
    assert "output budget" in payload["error"]


@pytest.mark.asyncio
async def test_list_tree_is_bounded_and_paginates_at_the_resolved_ref():
    commit_sha = "c" * 40
    tree_sha = "d" * 40

    async def fake_request(client, method, path, *, token=None, params=None, json=None):
        if "/commits/" in path:
            return {"sha": commit_sha, "commit": {"tree": {"sha": tree_sha}}}
        if "/git/trees/" in path:
            assert path.endswith(f"/git/trees/{tree_sha}")
            assert params == {"recursive": "1"}
            return {
                "sha": tree_sha,
                "truncated": False,
                "tree": [
                    {"path": "README.md", "type": "blob", "sha": "readme", "size": 12},
                    {"path": "src/a.py", "type": "blob", "sha": "a", "size": 20},
                    {"path": "src/b.py", "type": "blob", "sha": "b", "size": 30},
                ],
            }
        raise AssertionError(path)

    with patch(
        "brain.systems.cortex.project_context.github._async_request",
        new=AsyncMock(side_effect=fake_request),
    ):
        first = json.loads(
            await _handle_read_github_source(
                action="list_tree",
                repo="acme/widgets",
                ref="main",
                path="src",
                limit=1,
            )
        )
        second = json.loads(
            await _handle_read_github_source(
                action="list_tree",
                repo="acme/widgets",
                ref="main",
                path="src",
                limit=1,
                cursor=first["next_page"],
            )
        )

    assert first["resolved_ref"] == commit_sha
    assert [entry["path"] for entry in first["entries"]] == ["src/a.py"]
    assert first["truncated"] is True
    assert first["next_page"]
    assert [entry["path"] for entry in second["entries"]] == ["src/b.py"]
    assert second["truncated"] is False
    assert second["next_page"] is None
    assert second["evidence_health"] == {"status": "ok", "completeness": "complete"}


@pytest.mark.asyncio
async def test_list_tree_reports_upstream_truncation_even_when_another_local_page_exists():
    commit_sha = "c" * 40
    tree_sha = "d" * 40

    async def fake_request(client, method, path, *, token=None, params=None, json=None):
        if "/commits/" in path:
            return {"sha": commit_sha, "commit": {"tree": {"sha": tree_sha}}}
        return {
            "sha": tree_sha,
            "truncated": True,
            "tree": [
                {"path": "a.py", "type": "blob", "sha": "a", "size": 1},
                {"path": "b.py", "type": "blob", "sha": "b", "size": 1},
            ],
        }

    with patch(
        "brain.systems.cortex.project_context.github._async_request",
        new=AsyncMock(side_effect=fake_request),
    ):
        payload = json.loads(
            await _handle_read_github_source(
                action="list_tree",
                repo="acme/widgets",
                ref="main",
                limit=1,
            )
        )

    assert payload["next_page"]
    assert payload["source_truncated"] is True
    assert payload["evidence_health"] == {"status": "warning", "completeness": "source_truncated"}


@pytest.mark.asyncio
async def test_list_tree_rejects_a_non_finite_cursor_position():
    import hashlib

    from brain.kernel.common.pagination import encode_page_token

    commit_sha = "c" * 40
    tree_sha = "d" * 40
    prefix_fingerprint = hashlib.sha256(b"").hexdigest()[:20]
    cursor = encode_page_token(
        f"github_tree:acme/widgets:{commit_sha}:{prefix_fingerprint}",
        {"offset": float("inf")},
    )

    async def fake_request(client, method, path, *, token=None, params=None, json=None):
        assert "/commits/" in path
        return {"sha": commit_sha, "commit": {"tree": {"sha": tree_sha}}}

    with patch(
        "brain.systems.cortex.project_context.github._async_request",
        new=AsyncMock(side_effect=fake_request),
    ):
        payload = json.loads(
            await _handle_read_github_source(
                action="list_tree",
                repo="acme/widgets",
                ref="main",
                cursor=cursor,
            )
        )

    assert payload == {"error": "Invalid pagination cursor"}


@pytest.mark.asyncio
async def test_grep_rejects_a_non_finite_cursor_position():
    import hashlib

    from brain.kernel.common.pagination import encode_page_token

    commit_sha = "e" * 40
    tree_sha = "f" * 40
    fingerprint = hashlib.sha256("needle\0\0False".encode("utf-8")).hexdigest()[:20]
    cursor = encode_page_token(
        f"github_grep:acme/widgets:{commit_sha}:{fingerprint}",
        {"file_index": float("inf"), "line": 1},
    )

    async def fake_request(client, method, path, *, token=None, params=None, json=None):
        assert "/commits/" in path
        return {"sha": commit_sha, "commit": {"tree": {"sha": tree_sha}}}

    with patch(
        "brain.systems.cortex.project_context.github._async_request",
        new=AsyncMock(side_effect=fake_request),
    ):
        payload = json.loads(
            await _handle_read_github_source(
                action="grep",
                repo="acme/widgets",
                ref="main",
                query="needle",
                cursor=cursor,
            )
        )

    assert payload == {"error": "Invalid pagination cursor"}


@pytest.mark.asyncio
async def test_list_tree_pages_before_exceeding_the_tool_output_budget():
    commit_sha = "3" * 40
    tree_sha = "4" * 40
    entries = [
        {
            "path": f"src/{index:03d}-{'nested-' * 45}.py",
            "type": "blob",
            "sha": f"blob-{index}",
            "size": 10,
        }
        for index in range(60)
    ]

    async def fake_request(client, method, path, *, token=None, params=None, json=None):
        if "/commits/" in path:
            return {"sha": commit_sha, "commit": {"tree": {"sha": tree_sha}}}
        return {"sha": tree_sha, "truncated": False, "tree": entries}

    with patch(
        "brain.systems.cortex.project_context.github._async_request",
        new=AsyncMock(side_effect=fake_request),
    ):
        result = await _handle_read_github_source(
            action="list_tree",
            repo="acme/widgets",
            ref="main",
            path="src",
            limit=100,
        )

    payload = json.loads(result)
    assert len(result) < 18_000
    assert 0 < payload["returned"] < 60
    assert payload["truncated"] is True
    assert payload["next_page"]


@pytest.mark.asyncio
async def test_list_tree_fails_boundedly_when_one_entry_cannot_fit_the_envelope():
    requested_path = "/".join(["\x01" * 97 for _ in range(16)])

    async def fake_request(client, method, path, *, token=None, params=None, json=None):
        if "/commits/" in path:
            return {"sha": "a" * 40, "commit": {"tree": {"sha": "b" * 40}}}
        return {
            "sha": "b" * 40,
            "truncated": False,
            "tree": [{"path": requested_path, "type": "blob", "sha": "blob", "size": 1}],
        }

    with patch(
        "brain.systems.cortex.project_context.github._async_request",
        new=AsyncMock(side_effect=fake_request),
    ):
        result = await _handle_read_github_source(
            action="list_tree",
            repo="acme/widgets",
            ref="main",
            path=requested_path,
        )

    payload = json.loads(result)
    assert len(result) < 18_000
    assert payload["status_code"] == 413
    assert "output budget" in payload["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["list_tree", "grep"])
async def test_source_directory_actions_reject_a_missing_path_prefix(action: str):
    async def fake_request(client, method, path, *, token=None, params=None, json=None):
        if "/commits/" in path:
            return {"sha": "a" * 40, "commit": {"tree": {"sha": "b" * 40}}}
        return {
            "sha": "b" * 40,
            "truncated": False,
            "tree": [{"path": "src/app.py", "type": "blob", "sha": "blob", "size": 1}],
        }

    kwargs = {"query": "needle"} if action == "grep" else {}
    with patch(
        "brain.systems.cortex.project_context.github._async_request",
        new=AsyncMock(side_effect=fake_request),
    ):
        payload = json.loads(
            await _handle_read_github_source(
                action=action,
                repo="acme/widgets",
                ref="main",
                path="missing",
                **kwargs,
            )
        )

    assert payload["status_code"] == 404
    assert "prefix" in payload["error"]


@pytest.mark.asyncio
async def test_grep_returns_bounded_path_line_citations_and_resumable_cursor():
    import base64

    commit_sha = "e" * 40
    tree_sha = "f" * 40
    source = b"first\nNeedle here\nsecond NEEDLE here\n"

    async def fake_request(client, method, path, *, token=None, params=None, json=None):
        if "/commits/" in path:
            return {"sha": commit_sha, "commit": {"tree": {"sha": tree_sha}}}
        if "/git/trees/" in path:
            return {
                "sha": tree_sha,
                "truncated": False,
                "tree": [
                    {"path": "src/app.py", "type": "blob", "sha": "blob-1", "size": len(source)},
                    {"path": "vendor/ignored.py", "type": "blob", "sha": "blob-2", "size": 10},
                ],
            }
        if path.endswith("/git/blobs/blob-1"):
            return {
                "sha": "blob-1",
                "encoding": "base64",
                "size": len(source),
                "content": base64.b64encode(source).decode("ascii"),
            }
        raise AssertionError(path)

    with patch(
        "brain.systems.cortex.project_context.github._async_request",
        new=AsyncMock(side_effect=fake_request),
    ):
        first = json.loads(
            await _handle_read_github_source(
                action="grep",
                repo="acme/widgets",
                ref="main",
                path="src",
                query="needle",
                limit=1,
            )
        )
        second = json.loads(
            await _handle_read_github_source(
                action="grep",
                repo="acme/widgets",
                ref=first["resolved_ref"],
                path="src",
                query="needle",
                limit=1,
                cursor=first["next_page"],
            )
        )

    assert first["matches"] == [
        {
            "path": "src/app.py",
            "line": 2,
            "column": 1,
            "text": "Needle here",
            "citation": "src/app.py:2",
            "text_truncated": False,
            "prefix_truncated": False,
            "suffix_truncated": False,
        }
    ]
    assert first["truncated"] is True
    assert first["next_page"]
    assert first["scan_budget"]["max_files"] > 0
    assert first["scan_budget"]["max_bytes"] > 0
    assert second["matches"][0]["citation"] == "src/app.py:3"
    assert second["truncated"] is False
    assert second["next_page"] is None
    assert second["evidence_health"] == {"status": "ok", "completeness": "complete"}


@pytest.mark.asyncio
async def test_grep_centers_its_bounded_snippet_on_a_late_line_match():
    import base64

    source = (("x" * 400) + "needle" + ("y" * 400)).encode("utf-8")

    async def fake_request(client, method, path, *, token=None, params=None, json=None):
        if "/commits/" in path:
            return {"sha": "a" * 40, "commit": {"tree": {"sha": "b" * 40}}}
        if "/git/trees/" in path:
            return {
                "sha": "b" * 40,
                "truncated": False,
                "tree": [{"path": "src/long.py", "type": "blob", "sha": "blob", "size": len(source)}],
            }
        return {
            "sha": "blob",
            "encoding": "base64",
            "size": len(source),
            "content": base64.b64encode(source).decode("ascii"),
        }

    with patch(
        "brain.systems.cortex.project_context.github._async_request",
        new=AsyncMock(side_effect=fake_request),
    ):
        payload = json.loads(
            await _handle_read_github_source(
                action="grep",
                repo="acme/widgets",
                ref="main",
                query="needle",
            )
        )

    match = payload["matches"][0]
    assert "needle" in match["text"]
    assert match["column"] == 401
    assert match["prefix_truncated"] is True
    assert match["suffix_truncated"] is True
    assert len(match["text"]) <= 300


@pytest.mark.asyncio
async def test_grep_fails_boundedly_when_one_match_cannot_fit_the_envelope():
    import base64

    requested_path = "/".join(["\\" * 125 for _ in range(30)])
    source = b"needle\n"

    async def fake_request(client, method, path, *, token=None, params=None, json=None):
        if "/commits/" in path:
            return {"sha": "a" * 40, "commit": {"tree": {"sha": "b" * 40}}}
        if "/git/trees/" in path:
            return {
                "sha": "b" * 40,
                "truncated": False,
                "tree": [{"path": requested_path, "type": "blob", "sha": "blob", "size": len(source)}],
            }
        return {
            "sha": "blob",
            "encoding": "base64",
            "size": len(source),
            "content": base64.b64encode(source).decode("ascii"),
        }

    with patch(
        "brain.systems.cortex.project_context.github._async_request",
        new=AsyncMock(side_effect=fake_request),
    ):
        result = await _handle_read_github_source(
            action="grep",
            repo="acme/widgets",
            ref="main",
            path=requested_path,
            query="needle",
        )

    payload = json.loads(result)
    assert len(result) < 18_000
    assert payload["status_code"] == 413
    assert "output budget" in payload["error"]


@pytest.mark.asyncio
async def test_grep_marks_evidence_incomplete_when_a_source_file_is_skipped():
    commit_sha = "5" * 40
    tree_sha = "6" * 40

    async def fake_request(client, method, path, *, token=None, params=None, json=None):
        if "/commits/" in path:
            return {"sha": commit_sha, "commit": {"tree": {"sha": tree_sha}}}
        if "/git/trees/" in path:
            return {
                "sha": tree_sha,
                "truncated": False,
                "tree": [
                    {
                        "path": "generated/minified.js",
                        "type": "blob",
                        "sha": "large-blob",
                        "size": 200_000,
                    }
                ],
            }
        raise AssertionError("oversized blob should not be fetched")

    with patch(
        "brain.systems.cortex.project_context.github._async_request",
        new=AsyncMock(side_effect=fake_request),
    ):
        payload = json.loads(
            await _handle_read_github_source(
                action="grep",
                repo="acme/widgets",
                ref="main",
                query="needle",
            )
        )

    assert payload["matches"] == []
    assert payload["skipped_large_files"] == 1
    assert payload["truncated"] is True
    assert payload["next_page"] is None
    assert payload["evidence_health"] == {"status": "warning", "completeness": "files_skipped"}


@pytest.mark.asyncio
async def test_grep_skips_nul_containing_blobs_as_incomplete_binary_evidence():
    import base64

    commit_sha = "c" * 40
    tree_sha = "d" * 40
    source = b"\x00needle\n"

    async def fake_request(client, method, path, *, token=None, params=None, json=None):
        if "/commits/" in path:
            return {"sha": commit_sha, "commit": {"tree": {"sha": tree_sha}}}
        if "/git/trees/" in path:
            return {
                "sha": tree_sha,
                "truncated": False,
                "tree": [{"path": "asset.bin", "type": "blob", "sha": "binary", "size": len(source)}],
            }
        return {
            "sha": "binary",
            "encoding": "base64",
            "size": len(source),
            "content": base64.b64encode(source).decode("ascii"),
        }

    with patch(
        "brain.systems.cortex.project_context.github._async_request",
        new=AsyncMock(side_effect=fake_request),
    ):
        payload = json.loads(
            await _handle_read_github_source(
                action="grep",
                repo="acme/widgets",
                ref="main",
                query="needle",
            )
        )

    assert payload["matches"] == []
    assert payload["skipped_binary_files"] == 1
    assert payload["search_incomplete"] is True
    assert payload["evidence_health"] == {"status": "warning", "completeness": "files_skipped"}


@pytest.mark.asyncio
async def test_grep_cursor_carries_skipped_file_evidence_to_the_final_page():
    import base64

    commit_sha = "a" * 40
    tree_sha = "b" * 40
    source = b"needle one\nneedle two\n"

    async def fake_request(client, method, path, *, token=None, params=None, json=None):
        if "/commits/" in path:
            return {"sha": commit_sha, "commit": {"tree": {"sha": tree_sha}}}
        if "/git/trees/" in path:
            return {
                "sha": tree_sha,
                "truncated": False,
                "tree": [
                    {"path": "a-large.txt", "type": "blob", "sha": "large", "size": 200_000},
                    {"path": "b.py", "type": "blob", "sha": "small", "size": len(source)},
                ],
            }
        return {
            "sha": "small",
            "encoding": "base64",
            "size": len(source),
            "content": base64.b64encode(source).decode("ascii"),
        }

    with patch(
        "brain.systems.cortex.project_context.github._async_request",
        new=AsyncMock(side_effect=fake_request),
    ):
        first = json.loads(
            await _handle_read_github_source(
                action="grep",
                repo="acme/widgets",
                ref="main",
                query="needle",
                limit=1,
            )
        )
        final = json.loads(
            await _handle_read_github_source(
                action="grep",
                repo="acme/widgets",
                ref=first["resolved_ref"],
                query="needle",
                limit=1,
                cursor=first["next_page"],
            )
        )

    assert final["matches"][0]["citation"] == "b.py:2"
    assert final["skipped_large_files"] == 1
    assert final["search_incomplete"] is True
    assert final["evidence_health"] == {"status": "warning", "completeness": "files_skipped"}


@pytest.mark.asyncio
async def test_grep_pages_before_match_evidence_exceeds_the_tool_output_budget():
    import base64

    commit_sha = "7" * 40
    tree_sha = "8" * 40
    source = "\n".join(f"needle {index} " + ("\\" * 400) for index in range(60)).encode("utf-8")

    async def fake_request(client, method, path, *, token=None, params=None, json=None):
        if "/commits/" in path:
            return {"sha": commit_sha, "commit": {"tree": {"sha": tree_sha}}}
        if "/git/trees/" in path:
            return {
                "sha": tree_sha,
                "truncated": False,
                "tree": [{"path": "src/generated.py", "type": "blob", "sha": "blob", "size": len(source)}],
            }
        return {
            "sha": "blob",
            "encoding": "base64",
            "size": len(source),
            "content": base64.b64encode(source).decode("ascii"),
        }

    with patch(
        "brain.systems.cortex.project_context.github._async_request",
        new=AsyncMock(side_effect=fake_request),
    ):
        result = await _handle_read_github_source(
            action="grep",
            repo="acme/widgets",
            ref="main",
            query="needle",
            limit=100,
        )

    payload = json.loads(result)
    assert len(result) < 18_000
    assert 0 < payload["returned"] < 50
    assert payload["truncated"] is True
    assert payload["next_page"]


@pytest.mark.asyncio
async def test_get_issue_action_reads_one_issue_with_assignment_provenance_enabled():
    payload = {
        "repo": "uwear-ai/uwear-backend",
        "issue": {"number": 1918, "assignment_provenance": "automation_at_filing"},
    }
    with patch(
        "brain.systems.runs.tool_catalog.handlers.github.async_get_issue",
        new=AsyncMock(return_value=payload),
    ) as get_issue:
        result = await _handle_read_github_source(
            action="get_issue",
            repo="https://github.com/uwear-ai/uwear-backend.git",
            issue_number=1918,
        )

    body = json.loads(result)
    assert body["issue"]["assignment_provenance"] == "automation_at_filing"
    get_issue.assert_awaited_once()
    assert get_issue.await_args.args == ("uwear-ai/uwear-backend", 1918)
    # Provenance is the safety signal clause 3 of #881 depends on. A caller that
    # has to know to ask for it will not ask, so get_issue always enables it.
    assert get_issue.await_args.kwargs["include_assignment_provenance"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("issue_number", [None, 0, -3])
async def test_get_issue_action_requires_a_positive_issue_number(issue_number):
    with patch(
        "brain.systems.runs.tool_catalog.handlers.github.async_get_issue",
        new=AsyncMock(),
    ) as get_issue:
        result = await _handle_read_github_source(
            action="get_issue",
            repo="uwear-ai/uwear-backend",
            issue_number=issue_number,
        )

    assert json.loads(result) == {"error": "get_issue requires a positive issue_number"}
    get_issue.assert_not_awaited()
