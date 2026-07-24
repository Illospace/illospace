from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from brain.systems.cortex.project_context.github import (
    GitHubConnectorError,
    _github_error,
    async_create_repo_pull_request,
)
from brain.systems.runs.execution_context import bind_agent_context
from brain.systems.runs.tool_catalog.handlers.github import (
    _handle_create_github_pull_request,
)


_H = "brain.systems.runs.tool_catalog.handlers.github"


def _vault_patches(*, bound_env: dict, secrets: list, get_secret=None):
    return (
        patch(
            f"{_H}.async_resolve_project_bound_env_tokens",
            new=AsyncMock(return_value=bound_env),
        ),
        patch(f"{_H}.async_list_secrets", new=AsyncMock(return_value=secrets)),
        patch(
            f"{_H}.async_get_secret",
            new=AsyncMock(side_effect=get_secret or (lambda *a, **k: None)),
        ),
    )


def test_create_github_pull_request_definition_and_registry_wiring():
    from brain.systems.runs.tool_catalog.definitions.github import GITHUB_TOOLS
    from brain.systems.runs.tool_catalog.registry import get_tool_registration
    from brain.systems.runs.tool_definitions import COORDINATOR_TOOLS, WORKER_TOOLS
    from brain.systems.runs.tool_handlers import _get_tool_handlers

    name = "create_github_pull_request"
    definition = next(tool for tool in GITHUB_TOOLS if tool["name"] == name)
    description = definition["description"]
    schema = definition["input_schema"]

    assert "REAL GitHub pull request" in description
    assert "public write" in description
    assert "never merges" in description
    assert "Missions must restrict" in description
    assert "staging→main" in description
    assert schema["required"] == ["repo", "base", "head", "title", "body"]
    assert schema["properties"]["draft"]["default"] is False
    assert name in {tool["name"] for tool in COORDINATOR_TOOLS}
    assert name in {tool["name"] for tool in WORKER_TOOLS}
    assert name in _get_tool_handlers()

    registration = get_tool_registration(name)
    issue_registration = get_tool_registration("create_github_issue")
    assert registration is not None
    assert issue_registration is not None
    assert registration.permission == issue_registration.permission == "write_workspace"
    assert registration.risk_class == issue_registration.risk_class == "high"
    assert registration.side_effect_class == issue_registration.side_effect_class == "append_only"
    assert registration.reversibility == issue_registration.reversibility == "append_only"
    assert registration.action_manifest is True


@pytest.mark.asyncio
async def test_create_github_pull_request_happy_path_opens_real_pull_request():
    created = {
        "repo": "uwear-ai/uwear-backend",
        "pull_request": {
            "type": "pull_request",
            "number": 842,
            "html_url": "https://github.com/uwear-ai/uwear-backend/pull/842",
            "state": "open",
            "draft": False,
        },
    }
    p1, p2, p3 = _vault_patches(
        bound_env={"GITHUB_TOKEN": "write-token"},
        secrets=[],
    )
    with bind_agent_context({"user_id": "u", "org_id": "o"}), p1, p2, p3, patch(
        f"{_H}.async_create_repo_pull_request",
        new=AsyncMock(return_value=created),
    ) as create:
        result = await _handle_create_github_pull_request(
            repo="https://github.com/uwear-ai/uwear-backend.git",
            base="main",
            head="staging",
            title="Promote staging to main",
            body="Evergreen promotion pull request.",
        )

    payload = json.loads(result)
    assert payload == {
        "repo": "uwear-ai/uwear-backend",
        "number": 842,
        "html_url": "https://github.com/uwear-ai/uwear-backend/pull/842",
        "state": "open",
        "draft": False,
        "token_secret_key_used": False,
        "token_source": "project_binding:GITHUB_TOKEN",
        "token_key_name": None,
    }
    create.assert_awaited_once_with(
        "uwear-ai/uwear-backend",
        base="main",
        head="staging",
        title="Promote staging to main",
        body="Evergreen promotion pull request.",
        draft=False,
        token="write-token",
    )


@pytest.mark.asyncio
async def test_create_github_pull_request_returns_no_write_token():
    with patch(
        f"{_H}.async_create_repo_pull_request",
        new=AsyncMock(),
    ) as create:
        result = await _handle_create_github_pull_request(
            repo="uwear-ai/uwear-backend",
            base="main",
            head="staging",
            title="Promote staging to main",
            body="Evergreen promotion pull request.",
        )

    payload = json.loads(result)
    assert payload["no_write_token"] is True
    assert payload["status_code"] == 401
    assert payload["repo"] == "uwear-ai/uwear-backend"
    create.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_github_pull_request_maps_no_commits_422():
    p1, p2, p3 = _vault_patches(
        bound_env={"GITHUB_TOKEN": "write-token"},
        secrets=[],
    )
    with bind_agent_context({"user_id": "u", "org_id": "o"}), p1, p2, p3, patch(
        f"{_H}.async_create_repo_pull_request",
        new=AsyncMock(
            side_effect=GitHubConnectorError(
                status_code=422,
                message="Validation Failed: No commits between main and staging",
            )
        ),
    ):
        result = await _handle_create_github_pull_request(
            repo="uwear-ai/uwear-backend",
            base="main",
            head="staging",
            title="Promote staging to main",
            body="Evergreen promotion pull request.",
        )

    payload = json.loads(result)
    assert payload["error"] == "no_commits_between"
    assert payload["status_code"] == 422
    assert payload["base"] == "main"
    assert payload["head"] == "staging"


@pytest.mark.asyncio
async def test_create_github_pull_request_maps_already_exists_422():
    p1, p2, p3 = _vault_patches(
        bound_env={"GITHUB_TOKEN": "write-token"},
        secrets=[],
    )
    with bind_agent_context({"user_id": "u", "org_id": "o"}), p1, p2, p3, patch(
        f"{_H}.async_create_repo_pull_request",
        new=AsyncMock(
            side_effect=GitHubConnectorError(
                status_code=422,
                message=(
                    "Validation Failed: A pull request already exists for "
                    "uwear-ai:staging: https://github.com/uwear-ai/uwear-backend/pull/841"
                ),
            )
        ),
    ):
        result = await _handle_create_github_pull_request(
            repo="uwear-ai/uwear-backend",
            base="main",
            head="staging",
            title="Promote staging to main",
            body="Evergreen promotion pull request.",
        )

    payload = json.loads(result)
    assert payload["error"] == "pull_request_exists"
    assert payload["existing"] == 841
    assert payload["status_code"] == 422


@pytest.mark.asyncio
async def test_connector_posts_pull_request_fields():
    fake_created = {
        "number": 842,
        "html_url": "https://github.com/uwear-ai/uwear-backend/pull/842",
        "state": "open",
        "draft": True,
    }
    with patch(
        "brain.systems.cortex.project_context.github._async_request",
        new=AsyncMock(return_value=fake_created),
    ) as request:
        payload = await async_create_repo_pull_request(
            "uwear-ai/uwear-backend",
            base="main",
            head="staging",
            title="Promote staging to main",
            body="Evergreen promotion pull request.",
            draft=True,
            token="write-token",
        )

    assert payload["repo"] == "uwear-ai/uwear-backend"
    assert payload["pull_request"]["number"] == 842
    request.assert_awaited_once()
    assert request.await_args.args[1] == "POST"
    assert request.await_args.args[2] == "/repos/uwear-ai/uwear-backend/pulls"
    assert request.await_args.kwargs["token"] == "write-token"
    assert request.await_args.kwargs["json"] == {
        "base": "main",
        "head": "staging",
        "title": "Promote staging to main",
        "body": "Evergreen promotion pull request.",
        "draft": True,
    }


def test_github_422_error_includes_nested_validation_message():
    response = httpx.Response(
        422,
        json={
            "message": "Validation Failed",
            "errors": [
                {
                    "resource": "PullRequest",
                    "code": "custom",
                    "message": "No commits between main and staging",
                }
            ],
        },
    )

    error = _github_error(response)

    assert error.status_code == 422
    assert error.message == "Validation Failed: No commits between main and staging"
