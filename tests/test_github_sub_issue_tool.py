from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from brain.systems.cortex.project_context.github import (
    GitHubConnectorError,
    async_add_repo_sub_issue,
    async_get_repo_issue_parent,
    async_list_repo_sub_issues,
    async_remove_repo_sub_issue,
)
from brain.systems.runs.execution_context import bind_agent_context
from brain.systems.runs.tool_catalog.handlers.github import (
    _handle_add_github_sub_issue,
    _handle_list_github_sub_issues,
)


_C = "brain.systems.cortex.project_context.github"
_H = "brain.systems.runs.tool_catalog.handlers.github"


def _issue(repo: str, number: int, issue_id: int, title: str = "Issue") -> dict:
    return {
        "id": issue_id,
        "node_id": f"I_{issue_id}",
        "number": number,
        "title": title,
        "state": "open",
        "url": f"https://api.github.com/repos/{repo}/issues/{number}",
        "repository_url": f"https://api.github.com/repos/{repo}",
        "html_url": f"https://github.com/{repo}/issues/{number}",
    }


def _graphql_parent(repo: str, number: int) -> dict:
    return {
        "data": {
            "node": {
                "parent": {
                    "number": number,
                    "repository": {"nameWithOwner": repo},
                }
            }
        }
    }


def _vault_patches(*, bound_env: dict[str, str]):
    return (
        patch(
            f"{_H}.async_resolve_project_bound_env_tokens",
            new=AsyncMock(return_value=bound_env),
        ),
        patch(f"{_H}.async_list_secrets", new=AsyncMock(return_value=[])),
        patch(f"{_H}.async_get_secret", new=AsyncMock(return_value=None)),
    )


@pytest.mark.asyncio
async def test_add_sub_issue_resolves_cross_repo_issue_number_to_numeric_id():
    child = _issue("uwear-ai/uwear-backend", 42, 987654, "Backend ticket")
    linked = dict(child)
    request = AsyncMock(side_effect=[child, [], linked, [child]])

    with patch(f"{_C}._async_request", new=request):
        payload = await async_add_repo_sub_issue(
            "uwear-ai/uwear-coordination",
            7,
            "uwear-ai/uwear-backend",
            42,
            token="app-token",
        )

    assert payload["action"] == "linked"
    assert payload["changed"] is True
    assert payload["verified"] is True
    assert payload["verification_source"] == "parent_sub_issues"
    assert payload["parent"]["repo"] == "uwear-ai/uwear-coordination"
    assert payload["parent"]["number"] == 7
    assert payload["child"]["repo"] == "uwear-ai/uwear-backend"
    assert [call.args[1:3] for call in request.await_args_list] == [
        ("GET", "/repos/uwear-ai/uwear-backend/issues/42"),
        ("GET", "/repos/uwear-ai/uwear-coordination/issues/7/sub_issues"),
        ("POST", "/repos/uwear-ai/uwear-coordination/issues/7/sub_issues"),
        ("GET", "/repos/uwear-ai/uwear-coordination/issues/7/sub_issues"),
    ]
    assert request.await_args_list[2].kwargs["json"] == {"sub_issue_id": 987654}
    assert all(call.kwargs["token"] == "app-token" for call in request.await_args_list)


@pytest.mark.asyncio
async def test_add_same_repo_sub_issue_still_links_and_verifies():
    child = _issue("uwear-ai/uwear-backend", 42, 987654, "Backend ticket")
    request = AsyncMock(side_effect=[child, [], child, [child]])

    with patch(f"{_C}._async_request", new=request):
        payload = await async_add_repo_sub_issue(
            "uwear-ai/uwear-backend",
            7,
            "uwear-ai/uwear-backend",
            42,
            token="app-token",
        )

    assert payload["action"] == "linked"
    assert payload["changed"] is True
    assert payload["verified"] is True
    assert payload["parent"]["repo"] == "uwear-ai/uwear-backend"
    assert payload["child"]["repo"] == "uwear-ai/uwear-backend"
    assert request.await_args_list[-1].args[1:3] == (
        "GET",
        "/repos/uwear-ai/uwear-backend/issues/7/sub_issues",
    )


@pytest.mark.asyncio
async def test_add_sub_issue_is_idempotent_when_child_is_already_linked():
    child = _issue("uwear-ai/uwear-backend", 42, 987654)
    request = AsyncMock(side_effect=[child, [child]])

    with patch(f"{_C}._async_request", new=request):
        payload = await async_add_repo_sub_issue(
            "uwear-ai/uwear-coordination",
            7,
            "uwear-ai/uwear-backend",
            42,
            token="app-token",
        )

    assert payload["action"] == "already_linked"
    assert payload["changed"] is False
    assert payload["already_linked"] is True
    assert payload["verified"] is True
    assert payload["verification_source"] == "parent_sub_issues"
    assert request.await_count == 2
    assert all(call.args[1] == "GET" for call in request.await_args_list)


@pytest.mark.asyncio
async def test_remove_sub_issue_uses_official_singular_endpoint_and_numeric_id():
    child = _issue("uwear-ai/uwear-backend", 42, 987654)
    request = AsyncMock(side_effect=[child, [child], child])

    with patch(f"{_C}._async_request", new=request):
        payload = await async_remove_repo_sub_issue(
            "uwear-ai/uwear-coordination",
            7,
            "uwear-ai/uwear-backend",
            42,
            token="app-token",
        )

    assert payload["action"] == "unlinked"
    delete = request.await_args_list[2]
    assert delete.args[1:3] == (
        "DELETE",
        "/repos/uwear-ai/uwear-coordination/issues/7/sub_issue",
    )
    assert delete.kwargs["json"] == {"sub_issue_id": 987654}


@pytest.mark.asyncio
async def test_list_sub_issues_and_same_repo_parent_lookup_are_unchanged():
    child = _issue("uwear-ai/uwear-backend", 42, 987654)

    with patch(f"{_C}._async_request", new=AsyncMock(return_value=[child])) as request:
        listed = await async_list_repo_sub_issues(
            "uwear-ai/uwear-coordination",
            7,
            token="read-token",
        )

    assert listed["sub_issues"][0]["id"] == 987654
    assert request.await_args.args[1:3] == (
        "GET",
        "/repos/uwear-ai/uwear-coordination/issues/7/sub_issues",
    )

    with patch(
        f"{_C}._async_request",
        new=AsyncMock(
            side_effect=[
                child,
                _graphql_parent("uwear-ai/uwear-backend", 7),
            ]
        ),
    ) as request:
        lookup = await async_get_repo_issue_parent(
            "uwear-ai/uwear-backend",
            42,
            token="read-token",
        )

    assert lookup["parent"]["number"] == 7
    assert lookup["parent"]["repo"] == "uwear-ai/uwear-backend"
    assert request.await_args_list[1].args[1:3] == ("POST", "/graphql")
    assert request.await_args_list[1].kwargs["json"]["variables"] == {"issueId": "I_987654"}
    assert request.await_count == 2


@pytest.mark.asyncio
async def test_cross_repo_parent_lookup_resolves_via_child_node_id():
    child = _issue("uwear-ai/uwear-aiapp", 648, 987654, "App ticket")
    request = AsyncMock(
        side_effect=[
            child,
            _graphql_parent("uwear-ai/uwear-backend", 1102),
        ]
    )

    with patch(f"{_C}._async_request", new=request):
        lookup = await async_get_repo_issue_parent(
            "uwear-ai/uwear-aiapp",
            648,
            token="read-token",
        )

    assert lookup["parent"]["repo"] == "uwear-ai/uwear-backend"
    assert lookup["parent"]["number"] == 1102
    assert [call.args[1:3] for call in request.await_args_list] == [
        ("GET", "/repos/uwear-ai/uwear-aiapp/issues/648"),
        ("POST", "/graphql"),
    ]
    assert request.await_args_list[1].kwargs["json"]["variables"] == {"issueId": "I_987654"}


@pytest.mark.asyncio
async def test_public_parent_lookup_preserves_native_rest_reader():
    child = _issue("uwear-ai/uwear-backend", 42, 987654)
    parent = _issue("uwear-ai/uwear-backend", 7, 123456, "Chantier")
    request = AsyncMock(side_effect=[child, parent])

    with patch(f"{_C}._async_request", new=request):
        lookup = await async_get_repo_issue_parent(
            "uwear-ai/uwear-backend",
            42,
            token=None,
        )

    assert lookup["parent"]["repo"] == "uwear-ai/uwear-backend"
    assert lookup["parent"]["number"] == 7
    assert request.await_args_list[1].args[1:3] == (
        "GET",
        "/repos/uwear-ai/uwear-backend/issues/42/parent",
    )


@pytest.mark.asyncio
async def test_add_sub_issue_missing_scope_403_degrades_loudly():
    p1, p2, p3 = _vault_patches(bound_env={"GITHUB_TOKEN": "app-token"})
    with bind_agent_context({"user_id": "u", "org_id": "o"}), p1 as resolve, p2, p3, patch(
        f"{_C}._async_request",
        new=AsyncMock(
            side_effect=GitHubConnectorError(
                status_code=403,
                message="Resource not accessible by integration",
            )
        ),
    ):
        result = await _handle_add_github_sub_issue(
            parent_repo="uwear-ai/uwear-coordination",
            parent_issue_number=7,
            child_repo="uwear-ai/uwear-backend",
            child_issue_number=42,
        )

    payload = json.loads(result)
    assert payload["status_code"] == 403
    assert payload["no_write_token"] is True
    assert payload["missing_scope"] is True
    assert "Issues: Read and write" in payload["error"]
    assert "both uwear-ai/uwear-coordination and uwear-ai/uwear-backend" in payload["error"]
    assert "reapprove or reconnect" in payload["error"]
    assert resolve.await_args.kwargs["project_slugs"] == [
        "uwear-ai/uwear-coordination",
        "uwear-ai/uwear-backend",
    ]
    assert resolve.await_args.kwargs["github_app_only"] is True


@pytest.mark.asyncio
async def test_list_tool_get_parent_is_callable_with_public_reader():
    parent_payload = {
        "repo": "uwear-ai/uwear-backend",
        "issue": _issue("uwear-ai/uwear-backend", 42, 987654),
        "parent": _issue("uwear-ai/uwear-coordination", 7, 123456),
    }
    with patch(
        f"{_H}.async_get_repo_issue_parent",
        new=AsyncMock(return_value=parent_payload),
    ) as get_parent:
        result = await _handle_list_github_sub_issues(
            action="get_parent",
            repo="uwear-ai/uwear-backend",
            issue_number=42,
        )

    payload = json.loads(result)
    assert payload["parent"]["number"] == 7
    assert payload["token_source"] == "public"
    get_parent.assert_awaited_once_with(
        "uwear-ai/uwear-backend",
        42,
        token=None,
    )


@pytest.mark.asyncio
async def test_list_tool_cross_repo_parent_works_with_child_scoped_app_token():
    child = _issue("uwear-ai/uwear-aiapp", 648, 987654, "App ticket")
    request = AsyncMock(
        side_effect=[
            child,
            _graphql_parent("uwear-ai/uwear-backend", 1102),
        ]
    )
    p1, p2, p3 = _vault_patches(
        bound_env={"GITHUB_TOKEN": "child-scoped-app-token"}
    )

    with bind_agent_context({"user_id": "u", "org_id": "o"}), p1, p2, p3, patch(
        f"{_C}._async_request",
        new=request,
    ):
        result = await _handle_list_github_sub_issues(
            action="get_parent",
            repo="uwear-ai/uwear-aiapp",
            issue_number=648,
        )

    payload = json.loads(result)
    assert payload["parent"] == {
        "repo": "uwear-ai/uwear-backend",
        "number": 1102,
        "html_url": "https://github.com/uwear-ai/uwear-backend/issues/1102",
    }
    assert payload["token_source"] == "project_binding:GITHUB_TOKEN"
    assert [call.args[1:3] for call in request.await_args_list] == [
        ("GET", "/repos/uwear-ai/uwear-aiapp/issues/648"),
        ("POST", "/graphql"),
    ]


def test_chantier_parent_conventions_are_in_tool_guidance():
    from brain.systems.runs.tool_catalog.definitions.github import GITHUB_TOOLS

    create = next(tool for tool in GITHUB_TOOLS if tool["name"] == "create_github_issue")
    guidance = " ".join([
        create["description"],
        create["input_schema"]["properties"]["body"]["description"],
    ])
    assert "[Chantier] <title>" in guidance
    assert "Done means" in guidance
    assert "chantier slug" in guidance
    assert "key refs" in guidance or "key references" in guidance
    assert "checklist" in guidance
