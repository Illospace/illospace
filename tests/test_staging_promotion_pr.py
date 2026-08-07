"""Tests for the scheduled staging-to-main promotion PR reconciler."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch
import uuid

from cryptography.fernet import Fernet
import pytest

from brain.jobs.pipelines import staging_promotion_pr
from brain.platform.db.models.environment import TargetRegistry  # noqa: F401
from brain.platform.db.models.org import Org, User
from brain.platform.db.models.run import AgentRun  # noqa: F401
from brain.platform.db.models.vault import (
    Secret,
    VaultAccessLog,
    VaultProjectBinding,
)
from brain.systems.cortex.project_context import github_promotion
from brain.systems.cortex.project_context.github import (
    GitHubConnectorError,
    async_compare_commits,
    async_compare_repo_branches,
)
from brain.systems.cortex.project_context.github_promotion import (
    PROMOTION_PULL_REQUEST_POLICY,
    PromotionPullRequestResult,
)


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


def _register_sqlite_functions(dbapi_conn, connection_record):
    dbapi_conn.create_function(
        "NOW",
        0,
        lambda: datetime.now(timezone.utc).isoformat(),
    )
    dbapi_conn.create_function("gen_random_uuid", 0, lambda: str(uuid.uuid4()))


@pytest.mark.asyncio
async def test_ahead_repository_is_idempotent_across_repeated_runs(monkeypatch):
    open_pulls: list[dict] = []
    compare = AsyncMock(return_value=COMPARE_PAYLOAD)
    list_pulls = AsyncMock(
        side_effect=lambda *args, **kwargs: {"pull_requests": list(open_pulls)}
    )

    async def create_pull(*args, **arguments):
        assert args == (REPO,)
        open_pulls.append({
            "number": 901,
            "base": {"ref": "main"},
            "head": {"ref": "staging"},
        })
        return {
            "repo": REPO,
            "pull_request": {
                "number": 901,
                "html_url": f"https://github.com/{REPO}/pull/901",
                "state": "open",
                "draft": False,
            },
        }

    create = AsyncMock(side_effect=create_pull)
    monkeypatch.setattr(github_promotion, "async_compare_repo_branches", compare)
    monkeypatch.setattr(github_promotion, "async_list_repo_pull_requests", list_pulls)
    monkeypatch.setattr(github_promotion, "async_create_repo_pull_request", create)

    target = PROMOTION_PULL_REQUEST_POLICY.target_for(REPO)
    first = await github_promotion.async_reconcile_promotion_pull_request(
        target,
        token="app-token",
    )
    second = await github_promotion.async_reconcile_promotion_pull_request(
        target,
        token="app-token",
    )

    assert first.outcome == "created"
    assert second == PromotionPullRequestResult(
        repo=REPO,
        outcome="already_open",
        settled_by="search",
        number=901,
    )
    assert compare.await_count == 2
    assert list_pulls.await_count == 2
    create.assert_awaited_once()
    assert create.await_args.kwargs["title"] == PROMOTION_PULL_REQUEST_POLICY.title
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
    monkeypatch.setattr(github_promotion, "async_compare_repo_branches", compare)
    monkeypatch.setattr(github_promotion, "async_list_repo_pull_requests", list_pulls)
    monkeypatch.setattr(github_promotion, "async_create_repo_pull_request", create)

    result = await github_promotion.async_reconcile_promotion_pull_request(
        PROMOTION_PULL_REQUEST_POLICY.target_for(REPO),
        token="app-token",
    )

    assert result == PromotionPullRequestResult(
        repo=REPO,
        outcome="no_diff",
        settled_by="comparison",
    )
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
    monkeypatch.setattr(github_promotion, "async_compare_repo_branches", compare)
    monkeypatch.setattr(github_promotion, "async_list_repo_pull_requests", list_pulls)
    monkeypatch.setattr(github_promotion, "async_create_repo_pull_request", create)

    result = await github_promotion.async_reconcile_promotion_pull_request(
        PROMOTION_PULL_REQUEST_POLICY.target_for(REPO),
        token="app-token",
    )

    assert result == PromotionPullRequestResult(
        repo=REPO,
        outcome="already_open",
        settled_by="search",
        number=900,
    )
    create.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_race_422_already_exists_settles_as_already_open(monkeypatch):
    compare = AsyncMock(return_value=COMPARE_PAYLOAD)
    list_pulls = AsyncMock(return_value={"pull_requests": []})
    create = AsyncMock(
        side_effect=GitHubConnectorError(
            status_code=422,
            message=(
                "Validation Failed: A pull request already exists for "
                "uwear-ai:staging: "
                f"https://github.com/{REPO}/pull/901"
            ),
        )
    )
    monkeypatch.setattr(github_promotion, "async_compare_repo_branches", compare)
    monkeypatch.setattr(github_promotion, "async_list_repo_pull_requests", list_pulls)
    monkeypatch.setattr(github_promotion, "async_create_repo_pull_request", create)

    result = await github_promotion.async_reconcile_promotion_pull_request(
        PROMOTION_PULL_REQUEST_POLICY.target_for(REPO),
        token="app-token",
    )

    assert result == PromotionPullRequestResult(
        repo=REPO,
        outcome="already_open",
        settled_by="create_conflict",
        number=901,
        conflict_message=create.side_effect.message,
    )


@pytest.mark.asyncio
async def test_concurrent_reconciliations_settle_one_create_and_one_422_conflict(
    monkeypatch,
):
    search_barrier = asyncio.Barrier(2)
    create_lock = asyncio.Lock()
    open_pull_numbers: list[int] = []
    search_count = 0
    compare = AsyncMock(return_value=COMPARE_PAYLOAD)

    async def list_pulls(*args, **kwargs):
        nonlocal search_count
        search_count += 1
        await search_barrier.wait()
        return {"pull_requests": []}

    async def create_pull(*args, **kwargs):
        assert search_count == 2
        async with create_lock:
            if not open_pull_numbers:
                open_pull_numbers.append(901)
                return {
                    "repo": REPO,
                    "pull_request": {
                        "number": 901,
                        "html_url": f"https://github.com/{REPO}/pull/901",
                        "state": "open",
                        "draft": False,
                    },
                }
            raise GitHubConnectorError(
                status_code=422,
                message=(
                    "Validation Failed: A pull request already exists for "
                    "uwear-ai:staging: "
                    f"https://github.com/{REPO}/pull/901"
                ),
            )

    list_open_pulls = AsyncMock(side_effect=list_pulls)
    create = AsyncMock(side_effect=create_pull)
    monkeypatch.setattr(github_promotion, "async_compare_repo_branches", compare)
    monkeypatch.setattr(
        github_promotion,
        "async_list_repo_pull_requests",
        list_open_pulls,
    )
    monkeypatch.setattr(github_promotion, "async_create_repo_pull_request", create)

    target = PROMOTION_PULL_REQUEST_POLICY.target_for(REPO)
    results = await asyncio.gather(
        github_promotion.async_reconcile_promotion_pull_request(
            target,
            token="app-token",
        ),
        github_promotion.async_reconcile_promotion_pull_request(
            target,
            token="app-token",
        ),
    )

    assert {(result.outcome, result.settled_by) for result in results} == {
        ("created", "create"),
        ("already_open", "create_conflict"),
    }
    assert open_pull_numbers == [901]
    assert compare.await_count == 2
    assert list_open_pulls.await_count == 2
    assert create.await_count == 2


@pytest.mark.asyncio
async def test_api_error_is_logged_and_other_repository_still_settles(monkeypatch, caplog):
    actor = staging_promotion_pr.PromotionActor(user_id="user-1", org_id="org-1")
    reconcile = AsyncMock(
        side_effect=[
            GitHubConnectorError(status_code=502, message="GitHub unavailable"),
            PromotionPullRequestResult(
                repo="uwear-ai/uwearaiapp",
                outcome="no_diff",
                settled_by="comparison",
            ),
        ]
    )
    monkeypatch.setattr(
        staging_promotion_pr,
        "_promotion_actor",
        AsyncMock(return_value=actor),
    )
    repo_token = AsyncMock(return_value="app-token")
    monkeypatch.setattr(staging_promotion_pr, "_repo_token", repo_token)
    monkeypatch.setattr(
        staging_promotion_pr,
        "async_reconcile_promotion_pull_request",
        reconcile,
    )

    result = await staging_promotion_pr.run_promotion_job()

    assert result["ok"] is False
    assert result["failures"] == 1
    assert repo_token.await_count == len(staging_promotion_pr.CONFIGURED_REPOS)
    assert reconcile.await_count == 2
    assert [call.args[0].repo for call in reconcile.await_args_list] == list(
        staging_promotion_pr.CONFIGURED_REPOS
    )
    assert all(
        call.kwargs == {"token": "app-token"}
        for call in reconcile.await_args_list
    )
    assert "Promotion PR reconciliation failed" in caplog.text
    assert "GitHub unavailable" in caplog.text


@pytest.mark.asyncio
async def test_missing_project_binding_is_an_explicit_configuration_skip(monkeypatch):
    reason = f"No GitHub App project binding is configured for {REPO}"
    monkeypatch.setattr(staging_promotion_pr, "CONFIGURED_REPOS", (REPO,))
    monkeypatch.setattr(
        staging_promotion_pr,
        "_promotion_actor",
        AsyncMock(side_effect=staging_promotion_pr.PromotionConfigurationError(reason)),
    )

    result = await staging_promotion_pr.run_promotion_job()

    assert result == {
        "job": "uwear_staging_promotion_pr",
        "ok": False,
        "failures": 1,
        "results": [
            {
                "repo": REPO,
                "outcome": "skipped",
                "skip_kind": "configuration",
                "reason": reason,
            }
        ],
    }


@pytest.mark.asyncio
async def test_multiple_repository_configuration_gaps_remain_classified(monkeypatch):
    repos = tuple(staging_promotion_pr.CONFIGURED_REPOS[:2])
    monkeypatch.setattr(staging_promotion_pr, "CONFIGURED_REPOS", repos)

    async def missing_binding(repo):
        raise staging_promotion_pr.PromotionConfigurationError(
            f"No GitHub App project binding is configured for {repo}"
        )

    monkeypatch.setattr(staging_promotion_pr, "_promotion_actor", missing_binding)

    result = await staging_promotion_pr.run_promotion_job()

    assert result["ok"] is False
    assert result["failures"] == 2
    assert [item["repo"] for item in result["results"]] == list(repos)
    assert all(
        item["outcome"] == "skipped"
        and item["skip_kind"] == "configuration"
        and item["repo"] in item["reason"]
        for item in result["results"]
    )


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


@pytest.mark.asyncio
async def test_async_compare_commits_preserves_legacy_non_object_diagnostic():
    with patch(
        "brain.systems.cortex.project_context.github._async_request",
        new=AsyncMock(return_value=[]),
    ):
        with pytest.raises(GitHubConnectorError) as exc_info:
            await async_compare_commits(
                REPO,
                "main",
                "staging",
                token="app-token",
            )

    assert exc_info.value.status_code == 502
    assert (
        exc_info.value.message
        == "GitHub compare response omitted a recognized status."
    )


@pytest.mark.asyncio
async def test_job_minted_token_requests_read_only_contents_permission(
    async_sqlite_session_factory,
    monkeypatch,
):
    """Prove the job's minted App token is read-only for repository contents."""

    from brain.systems.vault import _encrypt

    monkeypatch.setenv("VAULT_MASTER_KEY", Fernet.generate_key().decode())
    session = await async_sqlite_session_factory(
        [
            Org.__table__,
            User.__table__,
            Secret.__table__,
            VaultAccessLog.__table__,
            VaultProjectBinding.__table__,
        ],
        connect_listener=_register_sqlite_functions,
    )
    org_id = "bbbbbbbb-0000-4000-8000-000000000001"
    user_id = "aaaaaaaa-0000-4000-8000-000000000001"
    session.add(Org(id=org_id, name="Promotion Org", slug="promotion-org"))
    session.add(
        User(
            id=user_id,
            org_id=org_id,
            name="Promotion Actor",
            email="promotion@test.com",
        )
    )
    secret = Secret(
        key_name="GITHUB_APP__ILLO",
        encrypted_value=_encrypt("github-app-blob"),
        category="github_app",
        org_id=org_id,
        created_by_user_id=user_id,
        updated_by_user_id=user_id,
        agent_access_level="manual",
    )
    session.add(secret)
    await session.flush()
    session.add(
        VaultProjectBinding(
            secret_id=secret.id,
            org_id=org_id,
            created_by_user_id=user_id,
            project_slug=REPO,
            env_name="GITHUB_TOKEN",
            active=True,
        )
    )
    await session.commit()

    class TestUnitOfWork:
        def __init__(self):
            self.session = session

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            if exc_type:
                await self.session.rollback()
            else:
                await self.session.commit()
            return False

    mint = AsyncMock(return_value="minted-app-token")
    monkeypatch.setattr("brain.systems.vault.UnitOfWork", TestUnitOfWork)
    monkeypatch.setattr(
        "brain.systems.vault.github_app_mint.async_mint_installation_token",
        mint,
    )

    token = await staging_promotion_pr._repo_token(
        REPO,
        staging_promotion_pr.PromotionActor(user_id=user_id, org_id=org_id),
    )

    assert token == "minted-app-token"
    mint.assert_awaited_once()
    assert mint.await_args.kwargs["repositories"] == ["uwear-ai/uwear-backend"]
    assert mint.await_args.kwargs["permissions"]["contents"] == "read"
    assert mint.await_args.kwargs["permissions"]["contents"] != "write"


def _reconcile_pair(backend_outcome: str):
    """Reconcile results for (backend, app) in CONFIGURED_REPOS order."""
    return AsyncMock(
        side_effect=[
            PromotionPullRequestResult(
                repo=REPO,
                outcome=backend_outcome,
                settled_by="search" if backend_outcome == "already_open" else "comparison",
                number=900 if backend_outcome == "already_open" else None,
            ),
            PromotionPullRequestResult(
                repo="uwear-ai/uwearaiapp",
                outcome="no_diff",
                settled_by="comparison",
            ),
        ]
    )


def _patch_reconcile_plumbing(monkeypatch, reconcile):
    monkeypatch.setattr(
        staging_promotion_pr,
        "_promotion_actor",
        AsyncMock(
            return_value=staging_promotion_pr.PromotionActor(
                user_id="user-1", org_id="org-1"
            )
        ),
    )
    monkeypatch.setattr(
        staging_promotion_pr, "_repo_token", AsyncMock(return_value="app-token")
    )
    monkeypatch.setattr(
        staging_promotion_pr, "async_reconcile_promotion_pull_request", reconcile
    )


@pytest.mark.asyncio
async def test_existing_promotion_pr_does_not_wake_readiness_cycle(monkeypatch):
    _patch_reconcile_plumbing(monkeypatch, _reconcile_pair("already_open"))
    wake = AsyncMock(side_effect=AssertionError("hourly job must not wake agent cycle"))
    monkeypatch.setattr(
        staging_promotion_pr,
        "_async_wake_readiness_cycle",
        wake,
        raising=False,
    )

    result = await staging_promotion_pr.run_promotion_job()

    assert result["ok"] is True
    assert "readiness_wake" not in result
    wake.assert_not_awaited()
