from __future__ import annotations

import base64
import hashlib
import json
from unittest.mock import AsyncMock, patch

import httpx
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


class _SubIssueHTTPClient:
    def __init__(self, responses: list[httpx.Response]) -> None:
        self._responses = responses
        self.requests: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def request(self, method, url, *, headers=None, params=None, json=None):
        self.requests.append({
            "method": method,
            "url": url,
            "headers": dict(headers or {}),
            "params": params,
            "json": json,
        })
        return self._responses.pop(0)


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
                [child],
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
    assert request.await_args_list[2].args[1:3] == (
        "GET",
        "/repos/uwear-ai/uwear-backend/issues/7/sub_issues",
    )
    assert lookup["verified"] is True
    assert request.await_count == 3


@pytest.mark.asyncio
async def test_cross_repo_parent_lookup_resolves_via_child_node_id():
    child = _issue("uwear-ai/uwear-aiapp", 648, 987654, "App ticket")
    request = AsyncMock(
        side_effect=[
            child,
            _graphql_parent("uwear-ai/uwear-backend", 1102),
            [child],
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
        ("GET", "/repos/uwear-ai/uwear-backend/issues/1102/sub_issues"),
    ]
    assert request.await_args_list[1].kwargs["json"]["variables"] == {"issueId": "I_987654"}
    assert lookup["verified"] is True


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
async def test_list_tool_cross_repo_parent_scopes_app_token_to_both_repos():
    child = _issue("uwear-ai/uwear-aiapp", 648, 987654, "App ticket")
    request = AsyncMock(
        side_effect=[
            child,
            _graphql_parent("uwear-ai/uwear-backend", 1102),
            [child],
        ]
    )
    p1, p2, p3 = _vault_patches(
        bound_env={"GITHUB_TOKEN": "both-repos-app-token"}
    )

    with bind_agent_context({"user_id": "u", "org_id": "o"}), p1 as resolve, p2, p3, patch(
        f"{_C}._async_request",
        new=request,
    ):
        result = await _handle_list_github_sub_issues(
            action="get_parent",
            repo="uwear-ai/uwear-aiapp",
            issue_number=648,
            counterpart_repo="uwear-ai/uwear-backend",
        )

    payload = json.loads(result)
    assert payload["parent"] == {
        "repo": "uwear-ai/uwear-backend",
        "number": 1102,
        "html_url": "https://github.com/uwear-ai/uwear-backend/issues/1102",
    }
    assert payload["token_source"] == "project_binding:GITHUB_TOKEN"
    assert payload["verified"] is True
    assert resolve.await_args.kwargs["project_slugs"] == [
        "uwear-ai/uwear-aiapp",
        "uwear-ai/uwear-backend",
    ]
    assert [call.args[1:3] for call in request.await_args_list] == [
        ("GET", "/repos/uwear-ai/uwear-aiapp/issues/648"),
        ("POST", "/graphql"),
        ("GET", "/repos/uwear-ai/uwear-backend/issues/1102/sub_issues"),
    ]


@pytest.mark.asyncio
async def test_cross_repo_parent_404_does_not_reject_token_for_child_repo():
    child_repo = "uwear-ai/uwearaiapp"
    parent_repo = "uwear-ai/uwear-backend"
    child = _issue(child_repo, 653, 987654, "App ticket")
    request = AsyncMock(
        side_effect=[
            child,
            _graphql_parent(parent_repo, 1125),
            GitHubConnectorError(status_code=404, message="Parent repo not visible"),
            [],
        ]
    )
    p1, p2, p3 = _vault_patches(
        bound_env={"GITHUB_TOKEN": "child-readable-token"}
    )

    with bind_agent_context({"user_id": "u", "org_id": "o"}), p1, p2, p3, patch(
        f"{_C}._async_request",
        new=request,
    ):
        failed_parent_read = json.loads(
            await _handle_list_github_sub_issues(
                action="get_parent",
                repo=child_repo,
                issue_number=653,
            )
        )
        healthy_child_read = json.loads(
            await _handle_list_github_sub_issues(
                action="list",
                repo=child_repo,
                issue_number=653,
            )
        )

    assert failed_parent_read["status_code"] == 404
    assert healthy_child_read["token_source"] == "project_binding:GITHUB_TOKEN"
    assert healthy_child_read["sub_issues"] == []
    assert request.await_count == 4


@pytest.mark.asyncio
async def test_get_parent_continues_after_unverified_candidate_to_verified_app_token():
    child_repo = "uwear-ai/uwearaiapp"
    parent_repo = "uwear-ai/uwear-backend"
    child = _issue(child_repo, 653, 987654, "App ticket")
    request = AsyncMock(
        side_effect=[
            child,
            _graphql_parent(parent_repo, 1125),
            [],
            child,
            _graphql_parent(parent_repo, 1125),
            [child],
        ]
    )

    async def get_secret(key_name, *args, **kwargs):
        return "child-scoped-token" if key_name == "EXPLICIT_GITHUB_TOKEN" else None

    with bind_agent_context({"user_id": "u", "org_id": "o"}), patch(
        f"{_H}.async_get_secret",
        new=AsyncMock(side_effect=get_secret),
    ), patch(
        f"{_H}.async_resolve_project_bound_env_tokens",
        new=AsyncMock(return_value={"GITHUB_TOKEN": "both-repos-app-token"}),
    ), patch(
        f"{_H}.async_list_secrets",
        new=AsyncMock(return_value=[]),
    ), patch(
        f"{_C}._async_request",
        new=request,
    ):
        result = json.loads(
            await _handle_list_github_sub_issues(
                action="get_parent",
                repo=child_repo,
                issue_number=653,
                counterpart_repo=parent_repo,
                token_secret_key="EXPLICIT_GITHUB_TOKEN",
            )
        )

    assert result["verified"] is True
    assert result["parent"]["repo"] == parent_repo
    assert result["token_source"] == "project_binding:GITHUB_TOKEN"
    assert result["attempted_token_sources"] == [
        "explicit",
        "project_binding:GITHUB_TOKEN",
    ]
    assert request.await_count == 6


@pytest.mark.asyncio
async def test_get_parent_returns_honest_unverified_result_after_candidates_exhausted():
    child_repo = "uwear-ai/uwearaiapp"
    parent_repo = "uwear-ai/uwear-backend"
    child = _issue(child_repo, 653, 987654, "App ticket")
    request = AsyncMock(
        side_effect=[
            child,
            _graphql_parent(parent_repo, 1125),
            [],
            GitHubConnectorError(status_code=403, message="App candidate rejected"),
        ]
    )

    async def get_secret(key_name, *args, **kwargs):
        return "child-scoped-token" if key_name == "EXPLICIT_GITHUB_TOKEN" else None

    with bind_agent_context({"user_id": "u", "org_id": "o"}), patch(
        f"{_H}.async_get_secret",
        new=AsyncMock(side_effect=get_secret),
    ), patch(
        f"{_H}.async_resolve_project_bound_env_tokens",
        new=AsyncMock(return_value={"GITHUB_TOKEN": "both-repos-app-token"}),
    ), patch(
        f"{_H}.async_list_secrets",
        new=AsyncMock(return_value=[]),
    ), patch(
        f"{_C}._async_request",
        new=request,
    ):
        result = json.loads(
            await _handle_list_github_sub_issues(
                action="get_parent",
                repo=child_repo,
                issue_number=653,
                counterpart_repo=parent_repo,
                token_secret_key="EXPLICIT_GITHUB_TOKEN",
            )
        )

    assert result["verified"] is False
    assert result["parent"] is None
    assert result["candidate_parent"]["repo"] == parent_repo
    assert result["token_source"] == "explicit"
    assert result["attempted_token_sources"] == [
        "explicit",
        "project_binding:GITHUB_TOKEN",
    ]
    assert request.await_count == 4


@pytest.mark.asyncio
async def test_get_parent_exhausts_candidates_after_unverified_result_and_validation_error():
    child_repo = "uwear-ai/uwearaiapp"
    parent_repo = "uwear-ai/uwear-backend"
    child = _issue(child_repo, 653, 987654, "App ticket")
    request = AsyncMock(
        side_effect=[
            child,
            _graphql_parent(parent_repo, 1125),
            [],
            GitHubConnectorError(status_code=422, message="Candidate-specific failure"),
            child,
            _graphql_parent(parent_repo, 1125),
            [child],
        ]
    )

    async def get_secret(key_name, *args, **kwargs):
        return "child-scoped-token" if key_name == "EXPLICIT_GITHUB_TOKEN" else None

    with bind_agent_context({"user_id": "u", "org_id": "o"}), patch(
        f"{_H}.async_get_secret",
        new=AsyncMock(side_effect=get_secret),
    ), patch(
        f"{_H}.async_resolve_project_bound_env_tokens",
        new=AsyncMock(
            return_value={
                "GITHUB_TOKEN": "validation-error-token",
                "GITHUB_TOKEN__SECONDARY": "both-repos-app-token",
            }
        ),
    ), patch(
        f"{_H}.async_list_secrets",
        new=AsyncMock(return_value=[]),
    ), patch(
        f"{_C}._async_request",
        new=request,
    ):
        result = json.loads(
            await _handle_list_github_sub_issues(
                action="get_parent",
                repo=child_repo,
                issue_number=653,
                counterpart_repo=parent_repo,
                token_secret_key="EXPLICIT_GITHUB_TOKEN",
            )
        )

    assert result["verified"] is True
    assert result["token_source"] == "project_binding:GITHUB_TOKEN__SECONDARY"
    assert result["attempted_token_sources"] == [
        "explicit",
        "project_binding:GITHUB_TOKEN",
        "project_binding:GITHUB_TOKEN__SECONDARY",
    ]
    assert request.await_count == 7


@pytest.mark.asyncio
async def test_get_parent_retries_after_unexpected_success_response_shape():
    child_repo = "uwear-ai/uwearaiapp"
    parent_repo = "uwear-ai/uwear-backend"
    child = _issue(child_repo, 653, 987654, "App ticket")
    client = _SubIssueHTTPClient([
        httpx.Response(200, json=child),
        httpx.Response(200, json=_graphql_parent(parent_repo, 1125)),
        httpx.Response(200, json={"unexpected": "object"}),
        httpx.Response(200, json=child),
        httpx.Response(200, json=_graphql_parent(parent_repo, 1125)),
        httpx.Response(200, json=[child]),
    ])

    async def get_secret(key_name, *args, **kwargs):
        return "shape-drift-token" if key_name == "EXPLICIT_GITHUB_TOKEN" else None

    with bind_agent_context({"user_id": "u", "org_id": "o"}), patch(
        f"{_H}.async_get_secret",
        new=AsyncMock(side_effect=get_secret),
    ), patch(
        f"{_H}.async_resolve_project_bound_env_tokens",
        new=AsyncMock(return_value={"GITHUB_TOKEN": "both-repos-app-token"}),
    ), patch(
        f"{_H}.async_list_secrets",
        new=AsyncMock(return_value=[]),
    ), patch(
        f"{_C}.async_http_client",
        return_value=client,
    ):
        result = json.loads(
            await _handle_list_github_sub_issues(
                action="get_parent",
                repo=child_repo,
                issue_number=653,
                counterpart_repo=parent_repo,
                token_secret_key="EXPLICIT_GITHUB_TOKEN",
            )
        )

    assert result["verified"] is True
    assert result["token_source"] == "project_binding:GITHUB_TOKEN"
    assert result["fallback_from_status_code"] == 502
    assert [request["headers"].get("Authorization") for request in client.requests] == [
        "Bearer shape-drift-token",
        "Bearer shape-drift-token",
        "Bearer shape-drift-token",
        "Bearer both-repos-app-token",
        "Bearer both-repos-app-token",
        "Bearer both-repos-app-token",
    ]


@pytest.mark.asyncio
async def test_parent_list_retries_after_unexpected_success_response_shape():
    parent_repo = "uwear-ai/uwear-backend"
    child_repo = "uwear-ai/uwearaiapp"
    child = _issue(child_repo, 653, 987654, "App ticket")
    client = _SubIssueHTTPClient([
        httpx.Response(200, json={"unexpected": "object"}),
        httpx.Response(200, json=[child]),
    ])

    async def get_secret(key_name, *args, **kwargs):
        return "shape-drift-token" if key_name == "EXPLICIT_GITHUB_TOKEN" else None

    with bind_agent_context({"user_id": "u", "org_id": "o"}), patch(
        f"{_H}.async_get_secret",
        new=AsyncMock(side_effect=get_secret),
    ), patch(
        f"{_H}.async_resolve_project_bound_env_tokens",
        new=AsyncMock(return_value={"GITHUB_TOKEN": "both-repos-app-token"}),
    ), patch(
        f"{_H}.async_list_secrets",
        new=AsyncMock(return_value=[]),
    ), patch(
        f"{_C}.async_http_client",
        return_value=client,
    ):
        result = json.loads(
            await _handle_list_github_sub_issues(
                action="list",
                repo=parent_repo,
                issue_number=1125,
                counterpart_repo=child_repo,
                token_secret_key="EXPLICIT_GITHUB_TOKEN",
            )
        )

    assert result["sub_issues"][0]["repo"] == child_repo
    assert result["token_source"] == "project_binding:GITHUB_TOKEN"
    assert result["fallback_from_status_code"] == 502


@pytest.mark.asyncio
async def test_get_parent_scopes_app_token_to_both_repos_and_verifies_parent_rollup(
    caplog,
):
    child_repo = "uwear-ai/uwearaiapp"
    parent_repo = "uwear-ai/uwear-backend"
    child = _issue(child_repo, 653, 987654, "private child title")
    raw_child = {**child, "body": "raw child body must not leak"}
    client = _SubIssueHTTPClient([
        httpx.Response(200, json=child),
        httpx.Response(200, json=_graphql_parent(parent_repo, 1125)),
        httpx.Response(200, json=[raw_child]),
    ])
    p1, p2, p3 = _vault_patches(
        bound_env={"GITHUB_TOKEN": "both-repos-app-token"}
    )

    with bind_agent_context({"user_id": "u", "org_id": "o"}), p1 as resolve, p2, p3, patch(
        f"{_C}.async_http_client",
        return_value=client,
    ), caplog.at_level("INFO", logger=_C):
        result = await _handle_list_github_sub_issues(
            action="get_parent",
            repo=child_repo,
            issue_number=653,
            counterpart_repo=parent_repo,
        )

    payload = json.loads(result)
    assert payload["parent"] == {
        "repo": parent_repo,
        "number": 1125,
        "html_url": f"https://github.com/{parent_repo}/issues/1125",
    }
    assert payload["verified"] is True
    assert payload["verification_source"] == "parent_sub_issues"
    assert resolve.await_args.kwargs["project_slugs"] == [child_repo, parent_repo]
    assert [request["url"].removeprefix("https://api.github.com") for request in client.requests] == [
        f"/repos/{child_repo}/issues/653",
        "/graphql",
        f"/repos/{parent_repo}/issues/1125/sub_issues",
    ]

    diagnostic_record = next(
        record
        for record in caplog.records
        if record.getMessage().startswith("github_sub_issue_read_response ")
    )
    diagnostic = json.loads(diagnostic_record.getMessage().partition(" ")[2])
    assert diagnostic["body_bytes"] > 0
    assert len(diagnostic["body_sha256"]) == 64
    assert diagnostic["items"] == [{
        "api_url": f"https://api.github.com/repos/{child_repo}/issues/653",
        "html_url": f"https://github.com/{child_repo}/issues/653",
        "id": 987654,
        "node_id": "I_987654",
        "number": 653,
        "repository_slug": child_repo,
    }]
    assert "both-repos-app-token" not in caplog.text
    assert "private child title" not in caplog.text
    assert "raw child body must not leak" not in caplog.text


@pytest.mark.asyncio
async def test_cross_repo_project_app_logs_lossless_raw_parent_response_without_credentials(
    caplog,
):
    child_repo = "uwear-ai/uwearaiapp"
    parent_repo = "uwear-ai/uwear-backend"
    child = _issue(child_repo, 653, 987654, "private child title")
    raw_child = {**child, "body": "private body retained only inside encoded raw evidence"}
    raw_body = json.dumps([raw_child], separators=(",", ":")).encode()
    client = _SubIssueHTTPClient([
        httpx.Response(200, json=child),
        httpx.Response(200, json=_graphql_parent(parent_repo, 1125)),
        httpx.Response(
            200,
            content=raw_body,
            headers={"content-type": "application/json"},
        ),
    ])
    p1, p2, p3 = _vault_patches(
        bound_env={"GITHUB_TOKEN": "both-repos-app-token"}
    )

    with bind_agent_context({"user_id": "u", "org_id": "o"}), p1, p2, p3, patch(
        f"{_C}.async_http_client",
        return_value=client,
    ), caplog.at_level("INFO", logger=_C):
        result = json.loads(
            await _handle_list_github_sub_issues(
                action="get_parent",
                repo=child_repo,
                issue_number=653,
                counterpart_repo=parent_repo,
            )
        )

    assert result["verified"] is True
    raw_record = next(
        record
        for record in caplog.records
        if record.getMessage().startswith("github_sub_issue_raw_response ")
    )
    raw_event = json.loads(raw_record.getMessage().partition(" ")[2])
    assert set(raw_event) == {
        "auth_source",
        "body_base64",
        "path",
        "status_code",
    }
    assert raw_event["auth_source"] == "project_binding:GITHUB_TOKEN"
    assert raw_event["path"] == f"/repos/{parent_repo}/issues/1125/sub_issues"
    assert raw_event["status_code"] == 200
    assert base64.b64decode(raw_event["body_base64"]) == raw_body
    assert "both-repos-app-token" not in raw_record.getMessage()
    assert "Authorization" not in raw_record.getMessage()


@pytest.mark.asyncio
async def test_list_parent_scopes_app_token_to_both_repos_and_retains_child_identity(
    caplog,
):
    parent_repo = "uwear-ai/uwear-backend"
    child_repo = "uwear-ai/uwearaiapp"
    child = {
        **_issue(child_repo, 653, 987654, "private child title"),
        "body": "raw child body must not leak",
        "user": {"login": "private-user"},
    }
    raw_body = json.dumps([child], separators=(",", ":")).encode()
    client = _SubIssueHTTPClient([
        httpx.Response(
            200,
            content=raw_body,
            headers={"content-type": "application/json"},
        ),
    ])
    p1, p2, p3 = _vault_patches(
        bound_env={"GITHUB_TOKEN": "both-repos-app-token"}
    )

    with bind_agent_context({"user_id": "u", "org_id": "o"}), p1 as resolve, p2, p3, patch(
        f"{_C}.async_http_client",
        return_value=client,
    ), caplog.at_level("INFO", logger=_C):
        result = await _handle_list_github_sub_issues(
            action="list",
            repo=parent_repo,
            issue_number=1125,
            counterpart_repo=child_repo,
        )

    payload = json.loads(result)
    assert resolve.await_args.kwargs["project_slugs"] == [parent_repo, child_repo]
    assert payload["sub_issues"][0]["repo"] == child_repo
    assert client.requests[0]["url"].removeprefix("https://api.github.com") == (
        f"/repos/{parent_repo}/issues/1125/sub_issues"
    )
    diagnostic_record = next(
        record
        for record in caplog.records
        if record.getMessage().startswith("github_sub_issue_read_response ")
    )
    diagnostic = json.loads(diagnostic_record.getMessage().partition(" ")[2])
    assert diagnostic["body_sha256"] == hashlib.sha256(raw_body).hexdigest()
    assert diagnostic["items"] == [{
        "api_url": f"https://api.github.com/repos/{child_repo}/issues/653",
        "html_url": f"https://github.com/{child_repo}/issues/653",
        "id": 987654,
        "node_id": "I_987654",
        "number": 653,
        "repository_slug": child_repo,
    }]
    for private_value in (
        "both-repos-app-token",
        "private child title",
        "raw child body must not leak",
        "private-user",
    ):
        assert private_value not in caplog.text


def test_list_sub_issue_schema_documents_cross_repo_app_scope():
    from brain.systems.runs.tool_catalog.definitions.github import GITHUB_TOOLS

    tool = next(tool for tool in GITHUB_TOOLS if tool["name"] == "list_github_sub_issues")
    counterpart = tool["input_schema"]["properties"]["counterpart_repo"]
    guidance = " ".join([tool["description"], counterpart["description"]])

    assert counterpart["type"] == "string"
    assert "action='list'" in guidance
    assert "action='get_parent'" in guidance
    assert "one GitHub App installation token" in guidance
    assert "both repositories" in guidance


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
