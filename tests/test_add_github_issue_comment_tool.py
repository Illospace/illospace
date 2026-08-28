from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from brain.systems.cortex.project_context.github import (
    GitHubConnectorError,
    async_add_repo_issue_comment,
)
from brain.systems.runs.execution_context import bind_agent_context
from brain.systems.runs.tool_catalog.definitions.github import GITHUB_TOOLS
from brain.systems.runs.tool_catalog.handlers.github import (
    _handle_add_github_issue_comment,
    _handle_update_github_issue,
)


_C = "brain.systems.cortex.project_context.github"
_H = "brain.systems.runs.tool_catalog.handlers.github"


def test_add_github_issue_comment_is_registered_as_a_separate_append_only_tool():
    from brain.systems.runs.tool_catalog.registry import get_tool_registration
    from brain.systems.runs.tool_definitions import COORDINATOR_TOOLS, WORKER_TOOLS
    from brain.systems.runs.tool_handlers import _get_tool_handlers

    definition = next(tool for tool in GITHUB_TOOLS if tool["name"] == "add_github_issue_comment")
    registration = get_tool_registration("add_github_issue_comment")
    required = definition["input_schema"]["required"]

    assert required == ["repo", "issue_number", "body"]
    assert definition["input_schema"]["properties"]["issue_number"]["minimum"] == 1
    assert "append-only" in definition["description"].lower()
    assert "add_github_issue_comment" in {tool["name"] for tool in COORDINATOR_TOOLS}
    assert "add_github_issue_comment" in {tool["name"] for tool in WORKER_TOOLS}
    assert "add_github_issue_comment" in _get_tool_handlers()
    assert registration is not None
    assert registration.permission == "write_workspace"
    assert registration.risk_class == "high"
    assert registration.side_effect_class == "append_only"
    assert registration.reversibility == "append_only"
    assert registration.action_manifest is True
    assert registration.output_budget_chars == 8_000


@pytest.mark.asyncio
async def test_connector_posts_comment_to_the_dedicated_issue_comments_endpoint():
    created = {
        "id": 1234,
        "node_id": "IC_example",
        "html_url": "https://github.com/Illospace/illospace/issues/390#issuecomment-1234",
        "body": "Resolution note\n\n- cleanup complete",
        "user": {"login": "illo-bot[bot]", "id": 7, "html_url": "https://github.com/apps/illo-bot"},
        "created_at": "2026-07-19T20:00:00Z",
        "updated_at": "2026-07-19T20:00:00Z",
    }
    with patch(f"{_C}._async_request", new=AsyncMock(return_value=created)) as request:
        payload = await async_add_repo_issue_comment(
            "Illospace/illospace",
            390,
            body="Resolution note\n\n- cleanup complete",
            token="installation-token",
        )

    assert payload["repo"] == "Illospace/illospace"
    assert payload["issue_number"] == 390
    assert payload["comment"]["id"] == 1234
    assert payload["comment"]["body"] == "Resolution note\n\n- cleanup complete"
    assert payload["comment"]["body_total_chars"] == 35
    request.assert_awaited_once()
    assert request.await_args.args[1:3] == (
        "POST",
        "/repos/Illospace/illospace/issues/390/comments",
    )
    assert request.await_args.kwargs == {
        "token": "installation-token",
        "json": {"body": "Resolution note\n\n- cleanup complete"},
    }


@pytest.mark.asyncio
async def test_comment_receipt_stays_within_tool_budget_for_escape_heavy_markdown():
    raw_body = "\x01" * 5_000
    created = {
        "id": 1234,
        "html_url": "https://github.com/Illospace/illospace/issues/390#issuecomment-1234",
        "body": raw_body,
    }
    with patch(f"{_C}._async_request", new=AsyncMock(return_value=created)):
        payload = await async_add_repo_issue_comment(
            "Illospace/illospace",
            390,
            body=raw_body,
            token="installation-token",
        )

    serialized = json.dumps(payload)
    assert len(serialized) < 8_000
    assert payload["comment"]["body_total_chars"] == len(raw_body)
    assert payload["comment"]["body_truncated"] is True


@pytest.mark.asyncio
async def test_handler_uses_project_app_write_identity_and_auth_fallback():
    attempts: list[str | None] = []

    async def request(_client, _method, _path, *, token=None, **_kwargs):
        attempts.append(token)
        if token == "rejected-token":
            raise GitHubConnectorError(status_code=403, message="Forbidden")
        return {
            "id": 4321,
            "html_url": "https://github.com/Illospace/illospace/issues/390#issuecomment-4321",
            "body": "Added separately",
        }

    with bind_agent_context({"user_id": "u", "org_id": "o"}), patch(
        f"{_H}.async_resolve_project_bound_env_tokens",
        new=AsyncMock(
            return_value={
                "GITHUB_TOKEN": "rejected-token",
                "GITHUB_TOKEN__SECONDARY": "healthy-token",
            }
        ),
    ), patch(f"{_C}._async_request", new=AsyncMock(side_effect=request)):
        payload = json.loads(
            await _handle_add_github_issue_comment(
                repo="Illospace/illospace",
                issue_number=390,
                body="Added separately",
            )
        )

    assert payload["comment"]["id"] == 4321
    assert payload["mutated_target_refs"] == [
        {
            "kind": "github_issue_comment",
            "id": "Illospace/illospace#390:comment:4321",
        }
    ]
    assert payload["token_source"] == "project_binding:GITHUB_TOKEN__SECONDARY"
    assert payload["fallback_from_status_code"] == 403
    assert attempts == ["rejected-token", "healthy-token"]


@pytest.mark.asyncio
async def test_comment_body_must_be_non_empty_before_resolving_a_write_identity():
    with patch(f"{_H}._github_token_candidates", new=AsyncMock()) as candidates:
        payload = json.loads(
            await _handle_add_github_issue_comment(
                repo="Illospace/illospace",
                issue_number=390,
                body=" \n\t ",
            )
        )

    assert payload["status_code"] == 422
    assert "non-empty body" in payload["error"]
    candidates.assert_not_awaited()

    with pytest.raises(GitHubConnectorError) as exc:
        await async_add_repo_issue_comment(
            "Illospace/illospace",
            390,
            body="   ",
            token="installation-token",
        )
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_closing_an_issue_keeps_its_body_and_commenting_is_a_separate_write():
    original_body = "Durable issue description that must survive closure."
    calls: list[tuple[str, str, dict | None]] = []

    async def request(_client, method, path, *, json=None, **_kwargs):
        calls.append((method, path, json))
        if method == "PATCH":
            return {}
        if method == "GET":
            return {
                "id": 390,
                "number": 390,
                "title": "Comment tool gap",
                "body": original_body,
                "state": "closed",
                "html_url": "https://github.com/Illospace/illospace/issues/390",
                "assignees": [],
                "labels": [],
            }
        if method == "POST" and path.endswith("/comments"):
            return {
                "id": 9001,
                "html_url": "https://github.com/Illospace/illospace/issues/390#issuecomment-9001",
                "body": "Closed after verification.",
            }
        raise AssertionError((method, path))

    with bind_agent_context({"user_id": "u", "org_id": "o"}), patch(
        f"{_H}.async_resolve_project_bound_env_tokens",
        new=AsyncMock(return_value={"GITHUB_TOKEN": "installation-token"}),
    ), patch(f"{_C}._async_request", new=AsyncMock(side_effect=request)):
        closed = json.loads(
            await _handle_update_github_issue(
                repo="Illospace/illospace",
                issue_number=390,
                state="closed",
            )
        )
        commented = json.loads(
            await _handle_add_github_issue_comment(
                repo="Illospace/illospace",
                issue_number=390,
                body="Closed after verification.",
            )
        )

    assert closed["applied"] == {"state": "closed"}
    assert closed["issue"]["body"] == original_body
    assert commented["comment"]["id"] == 9001
    assert calls == [
        ("PATCH", "/repos/Illospace/illospace/issues/390", {"state": "closed"}),
        ("GET", "/repos/Illospace/illospace/issues/390", None),
        (
            "POST",
            "/repos/Illospace/illospace/issues/390/comments",
            {"body": "Closed after verification."},
        ),
    ]
