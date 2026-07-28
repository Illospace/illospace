"""Read-only check_fix_deploy_state tool tests."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from brain.systems.cortex.project_context.github import GitHubConnectorError
from brain.systems.deploy_state_github import (
    AncestryObservation,
    is_ancestor_of,
    observe_ancestry,
)
from brain.systems.runs.tool_catalog.handlers.github import _handle_check_fix_deploy_state


_H = "brain.systems.runs.tool_catalog.handlers.github"


@pytest.mark.asyncio
async def test_check_fix_deploy_state_uses_pr_merge_sha_and_shared_deriver(monkeypatch):
    async def ancestry(repo, sha, branch, *, token=None):
        assert repo == "uwear-ai/uwear-backend"
        assert sha == "merge-sha"
        assert token is None
        return AncestryObservation(
            branch=branch,
            is_ancestor=branch == "staging",
        )

    with patch(
        f"{_H}.async_get_pull_request_deploy_info",
        new=AsyncMock(
            return_value={
                "repo": "uwear-ai/uwear-backend",
                "pull_request": {
                    "merged": True,
                    "merge_commit_sha": "merge-sha",
                    "merged_at": "2026-07-01T00:00:00Z",
                    "base": {"ref": "staging"},
                    "head": {"sha": "head-sha"},
                },
            }
        ),
    ), patch(
        "brain.systems.deploy_state.observe_ancestry",
        new=AsyncMock(side_effect=ancestry),
    ):
        payload = json.loads(
            await _handle_check_fix_deploy_state(
                repo="https://github.com/uwear-ai/uwear-backend.git",
                pr_number=905,
            )
        )

    assert payload["merged"] is True
    assert payload["base_ref"] == "staging"
    assert payload["in_staging"] is True
    assert payload["in_main"] is False
    assert payload["deploy_state"] == "staging"
    assert payload["indeterminate"] is False


@pytest.mark.asyncio
async def test_check_fix_deploy_state_sha_in_main_degrades_open_on_unknown_deploy_time(monkeypatch):
    async def ancestry(repo, sha, branch, *, token=None):
        return AncestryObservation(branch=branch, is_ancestor=True)

    with patch(
        "brain.systems.deploy_state.observe_ancestry",
        new=AsyncMock(side_effect=ancestry),
    ):
        payload = json.loads(
            await _handle_check_fix_deploy_state(
                repo="uwear-ai/uwear-backend",
                sha="abc123",
            )
        )

    assert payload["merged"] is None
    assert payload["in_main"] is True
    assert payload["deploy_state"] == "deployed"


@pytest.mark.asyncio
async def test_staging_pr_now_in_main_does_not_use_old_staging_merge_as_deploy_time(monkeypatch):
    async def ancestry(repo, sha, branch, *, token=None):
        return AncestryObservation(branch=branch, is_ancestor=True)

    with patch(
        f"{_H}.async_get_pull_request_deploy_info",
        new=AsyncMock(
            return_value={
                "pull_request": {
                    "merged": True,
                    "merge_commit_sha": "merge-sha",
                    "merged_at": "2026-01-01T00:00:00Z",
                    "base": {"ref": "staging"},
                    "head": {"sha": "head-sha"},
                }
            }
        ),
    ), patch(
        "brain.systems.deploy_state.observe_ancestry",
        new=AsyncMock(side_effect=ancestry),
    ):
        payload = json.loads(
            await _handle_check_fix_deploy_state(
                repo="uwear-ai/uwear-backend",
                pr_number=905,
            )
        )

    assert payload["deploy_state"] == "deployed"


@pytest.mark.asyncio
async def test_check_fix_deploy_state_no_token_visibility_degrades_open(monkeypatch):
    with patch(
        f"{_H}.async_get_pull_request_deploy_info",
        new=AsyncMock(
            side_effect=GitHubConnectorError(
                status_code=404,
                message="Repository not found or not visible to this token.",
            )
        ),
    ):
        payload = json.loads(
            await _handle_check_fix_deploy_state(
                repo="uwear-ai/uwear-backend",
                pr_number=905,
            )
        )

    assert payload["merged"] is None
    assert payload["in_staging"] is None
    assert payload["in_main"] is None
    assert payload["deploy_state"] == "unknown"
    assert payload["indeterminate"] is True
    assert payload["status_code"] == 404


@pytest.mark.asyncio
async def test_check_fix_deploy_state_is_always_on(monkeypatch):
    async def ancestry(_repo, _sha, branch, *, token=None):
        return AncestryObservation(branch=branch, is_ancestor=False)

    with patch(
        "brain.systems.deploy_state.observe_ancestry",
        new=AsyncMock(side_effect=ancestry),
    ):
        payload = json.loads(
            await _handle_check_fix_deploy_state(
                repo="uwear-ai/uwear-backend",
                sha="abc123",
            )
        )
    assert payload["deploy_state"] == "unmerged"
    assert payload.get("disabled") is None


@pytest.mark.asyncio
async def test_check_fix_deploy_state_surfaces_compare_failures():
    async def ancestry(_repo, _sha, branch, *, token=None):
        return AncestryObservation(
            branch=branch,
            is_ancestor=None,
            error_category="github_http_503",
            status_code=503,
        )

    with patch(
        "brain.systems.deploy_state.observe_ancestry",
        new=AsyncMock(side_effect=ancestry),
    ):
        payload = json.loads(
            await _handle_check_fix_deploy_state(
                repo="uwear-ai/uwear-backend",
                sha="abc123",
            )
        )

    assert payload["deploy_state"] == "unknown"
    assert payload["indeterminate"] is True
    assert payload["compare_failures"] == [
        {
            "branch": "staging",
            "category": "github_http_503",
            "status_code": 503,
        },
        {
            "branch": "main",
            "category": "github_http_503",
            "status_code": 503,
        },
    ]


def test_check_fix_deploy_state_tool_is_registered_and_read_only():
    from brain.systems.runs.tool_catalog.registry import get_tool_registration
    from brain.systems.runs.tool_definitions import COORDINATOR_TOOLS, WORKER_TOOLS
    from brain.systems.runs.tool_handlers import _get_tool_handlers

    name = "check_fix_deploy_state"
    assert name in {tool["name"] for tool in COORDINATOR_TOOLS}
    assert name in {tool["name"] for tool in WORKER_TOOLS}
    assert name in _get_tool_handlers()
    registration = get_tool_registration(name)
    assert registration is not None
    assert registration.risk_class == "low"
    assert registration.side_effect_class == "read_only"
    assert registration.action_manifest is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "expected"),
    [("identical", True), ("behind", True), ("ahead", False), ("diverged", False)],
)
async def test_ancestry_helper_maps_github_compare_status(status, expected):
    with patch(
        "brain.systems.deploy_state_github.async_compare_commits",
        new=AsyncMock(return_value=status),
    ) as compare:
        assert await is_ancestor_of("uwear-ai/uwear-backend", "abc123", "main") is expected
    compare.assert_awaited_once_with(
        "uwear-ai/uwear-backend",
        "main",
        "abc123",
        token=None,
    )


@pytest.mark.asyncio
async def test_ancestry_helper_failure_is_indeterminate():
    with patch(
        "brain.systems.deploy_state_github.async_compare_commits",
        new=AsyncMock(side_effect=GitHubConnectorError(503, "offline")),
    ):
        assert await is_ancestor_of("uwear-ai/uwear-backend", "abc123", "main") is None
        observation = await observe_ancestry(
            "uwear-ai/uwear-backend",
            "abc123",
            "main",
        )

    assert observation.error_category == "github_http_503"
    assert observation.status_code == 503
