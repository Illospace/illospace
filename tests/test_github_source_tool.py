from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from brain.systems.cortex.project_context.github import _issue_payload
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
    }
    list_issues.assert_awaited_once()
    assert list_issues.await_args.args == ("uwear-ai/uwear-backend",)
    assert list_issues.await_args.kwargs["labels"] == ["bug"]
    assert list_issues.await_args.kwargs["assignee"] == "redawear"
    assert list_issues.await_args.kwargs["limit"] == 100
