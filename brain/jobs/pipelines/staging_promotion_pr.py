"""Ensure each configured Uwear repository has a staging-to-main promotion PR."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import logging
from typing import Any

from sqlalchemy import select

from brain.platform.db.models.domain import Domain, DomainObjectType, DomainRecord
from brain.platform.db.models.org import User
from brain.platform.db.models.vault import Secret, VaultProjectBinding
from brain.platform.db.repositories.unit_of_work import UnitOfWork
from brain.systems.cortex.project_context.github import async_get_repo_branch_head
from brain.systems.cortex.project_context.github_promotion import (
    PROMOTION_PULL_REQUEST_POLICY,
    PromotionPullRequestResult,
    async_reconcile_promotion_pull_request,
)
from brain.systems.cycles.service import async_wake_cycle_now
from brain.systems.vault import async_resolve_project_bound_env_tokens


logger = logging.getLogger("illo.jobs.staging_promotion_pr")

CONFIGURED_REPOS = tuple(sorted(PROMOTION_PULL_REQUEST_POLICY.repositories))

# The repository whose promotion readiness has an agent-run analysis cycle.
# When this repo's (staging, main) pair moves past the cycle's last completed
# evaluation, the detector wakes that cycle instead of waiting for its
# scheduled backstop slot — small deltas per evaluation, no catch-up storms.
READINESS_WAKE_REPO = "uwear-ai/uwear-backend"
READINESS_CYCLE_NAME = "Uwear Backend Promotion Readiness"
READINESS_STATUS_DOMAIN_SLUG = "promotion-readiness"
READINESS_STATUS_OBJECT_KEY = "status"


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


async def _async_last_evaluated_pair(org_id: str) -> tuple[str | None, str | None]:
    """Read the readiness cycle's last evaluated (staging, main) SHA pair.

    The status record is runtime-authored by the cycle (its playbook pins the
    domain slug and object key), so every miss — no domain, no record, empty
    fields — reads as "no baseline" and the caller wakes the cycle to evaluate.
    Domain slugs are only unique per org, so the lookup is scoped to the org
    that owns the repo's GitHub App binding.
    """
    async with UnitOfWork() as uow:
        record = (
            await uow.session.scalars(
                select(DomainRecord)
                .join(Domain, Domain.id == DomainRecord.domain_id)
                .join(
                    DomainObjectType,
                    DomainObjectType.id == DomainRecord.object_type_id,
                )
                .where(
                    Domain.slug == READINESS_STATUS_DOMAIN_SLUG,
                    Domain.org_id == org_id,
                    Domain.archived_at.is_(None),
                    DomainObjectType.key == READINESS_STATUS_OBJECT_KEY,
                    DomainObjectType.archived_at.is_(None),
                    DomainRecord.org_id == org_id,
                    DomainRecord.archived_at.is_(None),
                )
                .order_by(DomainRecord.updated_at.desc())
                .limit(1)
            )
        ).first()
    if record is None or not isinstance(record.data, dict):
        return None, None
    staging = str(record.data.get("last_staging_sha") or "").strip() or None
    main = str(record.data.get("last_main_sha") or "").strip() or None
    return staging, main


# Wake dispositions that leave the scheduler run green. "not_found" and
# "ambiguous" are deliberately absent: a wake that can never fire again must
# fail the run loudly instead of presenting as healthy while doing no work.
READINESS_WAKE_OK_OUTCOMES = frozenset(
    {"pair_unchanged", "woken", "already_pending", "run_in_flight"}
)


async def _async_wake_readiness_cycle(token: str, org_id: str) -> dict[str, Any]:
    """Wake the readiness cycle when the promotable SHA pair has moved.

    Comparing against the cycle's own last completed evaluation (not this
    job's previous observation) makes the wake self-retrying: an evaluation
    that failed to complete leaves the baseline unchanged, so the next hourly
    tick wakes the cycle again until an evaluation lands.
    """
    staging_sha = await async_get_repo_branch_head(
        READINESS_WAKE_REPO, PROMOTION_PULL_REQUEST_POLICY.head, token=token
    )
    main_sha = await async_get_repo_branch_head(
        READINESS_WAKE_REPO, PROMOTION_PULL_REQUEST_POLICY.base, token=token
    )
    last_staging, last_main = await _async_last_evaluated_pair(org_id)
    if (staging_sha, main_sha) == (last_staging, last_main):
        return {
            "cycle": READINESS_CYCLE_NAME,
            "outcome": "pair_unchanged",
            "staging_sha": staging_sha,
            "main_sha": main_sha,
        }
    disposition = await async_wake_cycle_now(name=READINESS_CYCLE_NAME)
    logger.info(
        "Readiness wake for %s: %s (staging %s vs evaluated %s)",
        READINESS_WAKE_REPO,
        disposition,
        staging_sha[:7],
        (last_staging or "none")[:7],
    )
    return {
        "cycle": READINESS_CYCLE_NAME,
        "outcome": disposition,
        "staging_sha": staging_sha,
        "main_sha": main_sha,
    }


async def run_promotion_job() -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    failures = 0
    wake: dict[str, Any] | None = None
    for repo in CONFIGURED_REPOS:
        token: str | None = None
        actor: PromotionActor | None = None
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

        if (
            repo == READINESS_WAKE_REPO
            and token is not None
            and actor is not None
            and result["outcome"] in ("created", "already_open")
        ):
            try:
                wake = await _async_wake_readiness_cycle(token, actor.org_id)
                if wake["outcome"] not in READINESS_WAKE_OK_OUTCOMES:
                    failures += 1
                    logger.error(
                        "Readiness cycle wake settled dead for %s: %s",
                        repo,
                        wake["outcome"],
                    )
            except Exception as exc:  # noqa: BLE001 - a broken wake path must be visible, not silent
                failures += 1
                error = str(getattr(exc, "message", None) or exc)
                logger.error("Readiness cycle wake failed for %s: %s", repo, error)
                wake = {"cycle": READINESS_CYCLE_NAME, "outcome": "error", "error": error}
    payload: dict[str, Any] = {
        "job": "uwear_staging_promotion_pr",
        "ok": failures == 0,
        "failures": failures,
        "results": results,
    }
    if wake is not None:
        payload["readiness_wake"] = wake
    return payload


async def async_main() -> int:
    result = await run_promotion_job()
    print(json.dumps(result, sort_keys=True))
    return 0 if result["ok"] else 1


def main() -> int:
    return asyncio.run(async_main())


if __name__ == "__main__":
    raise SystemExit(main())
