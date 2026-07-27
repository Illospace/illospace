"""Tests for the scheduled staging-to-main promotion PR reconciler."""
from __future__ import annotations

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
    assert mint.await_args.kwargs["repositories"] == ["uwear-backend"]
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
async def test_moved_sha_pair_wakes_the_readiness_cycle(monkeypatch):
    _patch_reconcile_plumbing(monkeypatch, _reconcile_pair("already_open"))
    heads = AsyncMock(side_effect=["staging-new-sha", "main-new-sha"])
    monkeypatch.setattr(staging_promotion_pr, "async_get_repo_branch_head", heads)
    monkeypatch.setattr(
        staging_promotion_pr,
        "_async_last_evaluated_pair",
        AsyncMock(return_value=("staging-old-sha", "main-new-sha")),
    )
    wake = AsyncMock(return_value="woken")
    monkeypatch.setattr(staging_promotion_pr, "async_wake_cycle_now", wake)

    result = await staging_promotion_pr.run_promotion_job()

    assert result["ok"] is True
    assert result["readiness_wake"] == {
        "cycle": staging_promotion_pr.READINESS_CYCLE_NAME,
        "outcome": "woken",
        "staging_sha": "staging-new-sha",
        "main_sha": "main-new-sha",
    }
    wake.assert_awaited_once_with(name=staging_promotion_pr.READINESS_CYCLE_NAME)
    assert heads.await_args_list[0].args == (REPO, "staging")
    assert heads.await_args_list[1].args == (REPO, "main")


@pytest.mark.asyncio
async def test_unchanged_sha_pair_does_not_wake(monkeypatch):
    _patch_reconcile_plumbing(monkeypatch, _reconcile_pair("already_open"))
    monkeypatch.setattr(
        staging_promotion_pr,
        "async_get_repo_branch_head",
        AsyncMock(side_effect=["same-staging", "same-main"]),
    )
    monkeypatch.setattr(
        staging_promotion_pr,
        "_async_last_evaluated_pair",
        AsyncMock(return_value=("same-staging", "same-main")),
    )
    wake = AsyncMock()
    monkeypatch.setattr(staging_promotion_pr, "async_wake_cycle_now", wake)

    result = await staging_promotion_pr.run_promotion_job()

    assert result["ok"] is True
    assert result["readiness_wake"]["outcome"] == "pair_unchanged"
    wake.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_diff_backend_skips_the_wake_probe_entirely(monkeypatch):
    _patch_reconcile_plumbing(monkeypatch, _reconcile_pair("no_diff"))
    heads = AsyncMock()
    monkeypatch.setattr(staging_promotion_pr, "async_get_repo_branch_head", heads)
    wake = AsyncMock()
    monkeypatch.setattr(staging_promotion_pr, "async_wake_cycle_now", wake)

    result = await staging_promotion_pr.run_promotion_job()

    assert result["ok"] is True
    assert "readiness_wake" not in result
    heads.assert_not_awaited()
    wake.assert_not_awaited()


@pytest.mark.asyncio
async def test_wake_failure_fails_the_job_but_keeps_reconciliation_results(
    monkeypatch, caplog
):
    _patch_reconcile_plumbing(monkeypatch, _reconcile_pair("created"))
    monkeypatch.setattr(
        staging_promotion_pr,
        "_async_wake_readiness_cycle",
        AsyncMock(side_effect=RuntimeError("status record unreadable")),
    )

    result = await staging_promotion_pr.run_promotion_job()

    assert result["ok"] is False
    assert result["failures"] == 1
    assert [r["outcome"] for r in result["results"]] == ["created", "no_diff"]
    assert result["readiness_wake"]["outcome"] == "error"
    assert "Readiness cycle wake failed" in caplog.text


@pytest.mark.asyncio
async def test_last_evaluated_pair_reads_the_cycle_status_record(
    async_sqlite_session_factory,
    sqlite_postgres_ddl_patch,
    monkeypatch,
):
    from brain.platform.db.models.domain import (
        Domain,
        DomainObjectType,
        DomainRecord,
    )

    session = await async_sqlite_session_factory(
        [
            Org.__table__,
            Domain.__table__,
            DomainObjectType.__table__,
            DomainRecord.__table__,
        ],
        connect_listener=_register_sqlite_functions,
    )
    org_id = "bbbbbbbb-0000-4000-8000-000000000002"
    session.add(Org(id=org_id, name="Readiness Org", slug="readiness-org"))
    domain = Domain(org_id=org_id, slug="promotion-readiness", name="Promotion Readiness")
    session.add(domain)
    await session.flush()
    object_type = DomainObjectType(domain_id=domain.id, key="status", name="Status")
    session.add(object_type)
    await session.flush()
    session.add(
        DomainRecord(
            org_id=org_id,
            domain_id=domain.id,
            object_type_id=object_type.id,
            title="status",
            data={
                "last_staging_sha": "7bc4dd27c554",
                "last_main_sha": "762e2b48f7d9",
            },
        )
    )
    await session.commit()

    class TestUnitOfWork:
        def __init__(self):
            self.session = session

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(staging_promotion_pr, "UnitOfWork", TestUnitOfWork)

    pair = await staging_promotion_pr._async_last_evaluated_pair(org_id)

    assert pair == ("7bc4dd27c554", "762e2b48f7d9")

    # Another org's identically-slugged domain must not leak in.
    assert await staging_promotion_pr._async_last_evaluated_pair(
        "bbbbbbbb-0000-4000-8000-000000000099"
    ) == (None, None)

    # An archived object type reads as "no baseline", never as evidence.
    object_type.archived_at = datetime.now(timezone.utc)
    await session.commit()
    assert await staging_promotion_pr._async_last_evaluated_pair(org_id) == (None, None)


@pytest.mark.asyncio
async def test_dead_wake_disposition_fails_the_job(monkeypatch, caplog):
    """A wake that can never fire again (cycle gone) must not stay green."""
    _patch_reconcile_plumbing(monkeypatch, _reconcile_pair("already_open"))
    monkeypatch.setattr(
        staging_promotion_pr,
        "async_get_repo_branch_head",
        AsyncMock(side_effect=["staging-new", "main-new"]),
    )
    monkeypatch.setattr(
        staging_promotion_pr,
        "_async_last_evaluated_pair",
        AsyncMock(return_value=(None, None)),
    )
    monkeypatch.setattr(
        staging_promotion_pr, "async_wake_cycle_now", AsyncMock(return_value="not_found")
    )

    result = await staging_promotion_pr.run_promotion_job()

    assert result["ok"] is False
    assert result["failures"] == 1
    assert result["readiness_wake"]["outcome"] == "not_found"
    assert "settled dead" in caplog.text
