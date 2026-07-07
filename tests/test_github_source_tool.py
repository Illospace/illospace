from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from brain.systems.cortex.project_context.github import GitHubConnectorError, _issue_payload
from brain.systems.runs.execution_context import bind_agent_context
from brain.systems.runs.tool_catalog.handlers.github import _handle_read_github_source


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
