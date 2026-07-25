"""Ensure each configured Uwear repository has a staging-to-main promotion PR."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import logging
from typing import Any

from sqlalchemy import select

from brain.platform.db.models.org import User
from brain.platform.db.models.vault import Secret, VaultProjectBinding
from brain.platform.db.repositories.unit_of_work import UnitOfWork
from brain.systems.cortex.project_context.github_promotion import (
    PROMOTION_PULL_REQUEST_POLICY,
    PromotionPullRequestResult,
    async_reconcile_promotion_pull_request,
)
from brain.systems.vault import async_resolve_project_bound_env_tokens


logger = logging.getLogger("illo.jobs.staging_promotion_pr")

CONFIGURED_REPOS = tuple(sorted(PROMOTION_PULL_REQUEST_POLICY.repositories))


@dataclass(frozen=True)
class PromotionActor:
    user_id: str
    org_id: str


class PromotionPullRequestError(RuntimeError):
    """A settled per-repository failure that should fail the scheduler run."""


def _job_result(result: PromotionPullRequestResult) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "repo": result.repo,
        "outcome": result.outcome,
    }
    if result.number is not None:
        payload["number"] = result.number
    if result.html_url is not None:
        payload["html_url"] = result.html_url
    return payload


async def _promotion_actor(repo: str) -> PromotionActor:
    async with UnitOfWork() as uow:
        binding = await uow.session.scalar(
            select(VaultProjectBinding)
            .join(Secret, Secret.id == VaultProjectBinding.secret_id)
            .where(
                VaultProjectBinding.project_slug == repo,
                VaultProjectBinding.active.is_(True),
                Secret.category == "github_app",
            )
            .order_by(VaultProjectBinding.id.asc())
            .limit(1)
        )
        if binding is None:
            raise PromotionPullRequestError(
                f"No GitHub App project binding is configured for {repo}"
            )

        actor = None
        if binding.created_by_user_id:
            actor = await uow.session.get(User, str(binding.created_by_user_id))
        if actor is None:
            actor = (
                await uow.session.scalars(
                    select(User)
                    .where(User.org_id == str(binding.org_id))
                    .order_by(User.created_at.asc(), User.id.asc())
                    .limit(1)
                )
            ).first()
        if actor is None:
            raise PromotionPullRequestError(
                f"No Illospace user is available for {repo}'s GitHub App binding"
            )
        return PromotionActor(user_id=str(actor.id), org_id=str(actor.org_id))


async def _repo_token(repo: str, actor: PromotionActor) -> str:
    env = await async_resolve_project_bound_env_tokens(
        actor_user_id=actor.user_id,
        org_id=actor.org_id,
        project_slug=repo,
        github_app_only=True,
    )
    token = str(env.get("GITHUB_TOKEN") or env.get("GH_TOKEN") or "").strip()
    if not token:
        raise PromotionPullRequestError(
            f"No project-bound GitHub App token is available for {repo}"
        )
    return token


async def run_promotion_job() -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    failures = 0
    for repo in CONFIGURED_REPOS:
        try:
            target = PROMOTION_PULL_REQUEST_POLICY.target_for(repo)
            actor = await _promotion_actor(repo)
            token = await _repo_token(repo, actor)
            promotion = await async_reconcile_promotion_pull_request(
                target,
                token=token,
            )
            result = _job_result(promotion)
        except Exception as exc:  # noqa: BLE001 - each repository must settle independently
            failures += 1
            error = str(getattr(exc, "message", None) or exc)
            logger.error("Promotion PR reconciliation failed for %s: %s", repo, error)
            result = {"repo": repo, "outcome": "error", "error": error}
        results.append(result)
    return {
        "job": "uwear_staging_promotion_pr",
        "ok": failures == 0,
        "failures": failures,
        "results": results,
    }


async def async_main() -> int:
    result = await run_promotion_job()
    print(json.dumps(result, sort_keys=True))
    return 0 if result["ok"] else 1


def main() -> int:
    return asyncio.run(async_main())


if __name__ == "__main__":
    raise SystemExit(main())
