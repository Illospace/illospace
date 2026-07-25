"""Tests for the scheduled staging-to-main promotion PR reconciler."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from brain.jobs.pipelines import staging_promotion_pr
from brain.systems.cortex.project_context.github import (
    GitHubConnectorError,
    async_compare_repo_branches,
)
from brain.systems.runs.tool_catalog.handlers.github import (
    PROMOTION_PULL_REQUEST_TITLE,
)
from brain.systems.vault.github_app_mint import DEFAULT_INSTALLATION_PERMISSIONS


REPO = "uwear-ai/uwear-backend"
COMPARE_PAYLOAD = {
    "repo": REPO,
    "base": "main",
    "head": "staging",
    "status": "ahead",
    "ahead_by": 2,
    "commits": [
        {
            "sha": "abc123456789",
            "html_url": f"https://github.com/{REPO}/commit/abc123456789",
            "subject": "Fix generation status (#475)",
        },
        {
            "sha": "def987654321",
            "html_url": f"https://github.com/{REPO}/commit/def987654321",
            "subject": "Harden scheduler guard (#441) (#445)",
        },
    ],
}


@pytest.mark.asyncio
async def test_ahead_repository_is_idempotent_across_repeated_runs(monkeypatch):
    open_pulls: list[dict] = []
    compare = AsyncMock(return_value=COMPARE_PAYLOAD)
    list_pulls = AsyncMock(
        side_effect=lambda *args, **kwargs: {"pull_requests": list(open_pulls)}
    )

    async def create_pull(**arguments):
        open_pulls.append({
            "number": 901,
            "base": {"ref": "main"},
            "head": {"ref": "staging"},
        })
        return json.dumps({
            "repo": REPO,
            "number": 901,
            "html_url": f"https://github.com/{REPO}/pull/901",
        })

    create = AsyncMock(side_effect=create_pull)
    monkeypatch.setattr(staging_promotion_pr, "async_compare_repo_branches", compare)
    monkeypatch.setattr(staging_promotion_pr, "async_list_repo_pull_requests", list_pulls)
    monkeypatch.setattr(
        staging_promotion_pr,
        "_handle_create_github_pull_request",
        create,
    )

    first = await staging_promotion_pr.reconcile_repository(REPO, token="app-token")
    second = await staging_promotion_pr.reconcile_repository(REPO, token="app-token")

    assert first["outcome"] == "created"
    assert second == {"repo": REPO, "outcome": "already_open", "number": 901}
    assert compare.await_count == 2
    assert list_pulls.await_count == 2
    create.assert_awaited_once()
    assert create.await_args.kwargs["title"] == PROMOTION_PULL_REQUEST_TITLE
    assert create.await_args.kwargs["draft"] is False
    body = create.await_args.kwargs["body"]
    assert "Fix generation status (#475)" in body
    assert "Harden scheduler guard (#441) (#445)" in body
    assert f"[#441](https://github.com/{REPO}/issues/441)" in body
    assert f"[#445](https://github.com/{REPO}/issues/445)" in body
    assert f"[#475](https://github.com/{REPO}/issues/475)" in body
    assert list_pulls.await_args.kwargs == {
        "token": "app-token",
        "state": "open",
        "base": "main",
        "head": "staging",
        "limit": 100,
    }


@pytest.mark.asyncio
async def test_identical_repository_settles_without_search_or_create(monkeypatch):
    compare = AsyncMock(return_value={**COMPARE_PAYLOAD, "status": "identical", "ahead_by": 0})
    list_pulls = AsyncMock()
    create = AsyncMock()
    monkeypatch.setattr(staging_promotion_pr, "async_compare_repo_branches", compare)
    monkeypatch.setattr(staging_promotion_pr, "async_list_repo_pull_requests", list_pulls)
    monkeypatch.setattr(
        staging_promotion_pr,
        "_handle_create_github_pull_request",
        create,
    )

    result = await staging_promotion_pr.reconcile_repository(REPO, token="app-token")

    assert result == {"repo": REPO, "outcome": "no_diff"}
    list_pulls.assert_not_awaited()
    create.assert_not_awaited()


@pytest.mark.asyncio
async def test_existing_open_promotion_pr_is_not_duplicated(monkeypatch):
    compare = AsyncMock(return_value=COMPARE_PAYLOAD)
    list_pulls = AsyncMock(return_value={
        "pull_requests": [
            {
                "number": 900,
                "base": {"ref": "main"},
                "head": {"ref": "staging"},
            }
        ]
    })
    create = AsyncMock()
    monkeypatch.setattr(staging_promotion_pr, "async_compare_repo_branches", compare)
    monkeypatch.setattr(staging_promotion_pr, "async_list_repo_pull_requests", list_pulls)
    monkeypatch.setattr(
        staging_promotion_pr,
        "_handle_create_github_pull_request",
        create,
    )

    result = await staging_promotion_pr.reconcile_repository(REPO, token="app-token")

    assert result == {"repo": REPO, "outcome": "already_open", "number": 900}
    create.assert_not_awaited()


@pytest.mark.asyncio
async def test_api_error_is_logged_and_other_repository_still_settles(monkeypatch, caplog):
    actor = staging_promotion_pr.PromotionActor(user_id="user-1", org_id="org-1")
    reconcile = AsyncMock(
        side_effect=[
            GitHubConnectorError(status_code=502, message="GitHub unavailable"),
            {"repo": "uwear-ai/uwearaiapp", "outcome": "no_diff"},
        ]
    )
    monkeypatch.setattr(
        staging_promotion_pr,
        "_promotion_actor",
        AsyncMock(return_value=actor),
    )
    monkeypatch.setattr(
        staging_promotion_pr,
        "_repo_token",
        AsyncMock(return_value="app-token"),
    )
    monkeypatch.setattr(staging_promotion_pr, "reconcile_repository", reconcile)

    result = await staging_promotion_pr.run_promotion_job()

    assert result["ok"] is False
    assert result["failures"] == 1
    assert reconcile.await_count == 2
    assert "Promotion PR reconciliation failed" in caplog.text
    assert "GitHub unavailable" in caplog.text


@pytest.mark.asyncio
async def test_repo_token_uses_only_the_project_bound_github_app(monkeypatch):
    actor = staging_promotion_pr.PromotionActor(user_id="user-1", org_id="org-1")
    resolve = AsyncMock(return_value={"GITHUB_TOKEN": "minted-app-token"})
    monkeypatch.setattr(
        staging_promotion_pr,
        "async_resolve_project_bound_env_tokens",
        resolve,
    )

    token = await staging_promotion_pr._repo_token(REPO, actor)

    assert token == "minted-app-token"
    resolve.assert_awaited_once_with(
        actor_user_id="user-1",
        org_id="org-1",
        project_slug=REPO,
        github_app_only=True,
    )


@pytest.mark.asyncio
async def test_failed_reconciliation_exits_nonzero_for_scheduler_failure_guard(
    monkeypatch,
):
    monkeypatch.setattr(
        staging_promotion_pr,
        "run_promotion_job",
        AsyncMock(return_value={
            "job": "uwear_staging_promotion_pr",
            "ok": False,
            "failures": 1,
            "results": [],
        }),
    )

    assert await staging_promotion_pr.async_main() == 1


@pytest.mark.asyncio
async def test_compare_connector_reads_ahead_count_and_commit_subjects():
    response = {
        "status": "ahead",
        "ahead_by": 1,
        "commits": [
            {
                "sha": "abc123",
                "html_url": f"https://github.com/{REPO}/commit/abc123",
                "commit": {"message": "Fix promotion flow (#475)\n\nLong explanation"},
            }
        ],
    }
    with patch(
        "brain.systems.cortex.project_context.github._async_request",
        new=AsyncMock(return_value=response),
    ) as request:
        result = await async_compare_repo_branches(
            REPO,
            "main",
            "staging",
            token="app-token",
        )

    assert result["ahead_by"] == 1
    assert result["commits"] == [
        {
            "sha": "abc123",
            "html_url": f"https://github.com/{REPO}/commit/abc123",
            "subject": "Fix promotion flow (#475)",
        }
    ]
    request.assert_awaited_once()
    assert request.await_args.args[1:] == (
        "GET",
        f"/repos/{REPO}/compare/main...staging",
    )
    assert request.await_args.kwargs["token"] == "app-token"


def test_promotion_run_mints_a_token_that_cannot_merge():
    assert DEFAULT_INSTALLATION_PERMISSIONS == {
        "issues": "write",
        "contents": "read",
        "pull_requests": "write",
        "checks": "read",
    }
    assert DEFAULT_INSTALLATION_PERMISSIONS["contents"] != "write"
