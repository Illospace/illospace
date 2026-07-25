"""Ensure each configured Uwear repository has a staging-to-main promotion PR."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import logging
import re
from typing import Any

from sqlalchemy import select

from brain.platform.db.models.org import User
from brain.platform.db.models.vault import Secret, VaultProjectBinding
from brain.platform.db.repositories.unit_of_work import UnitOfWork
from brain.systems.cortex.project_context.github import (
    async_compare_repo_branches,
    async_list_repo_pull_requests,
)
from brain.systems.runs.execution_context import bind_agent_context
from brain.systems.runs.tool_catalog.handlers.github import (
    PROMOTION_PULL_REQUEST_BASE,
    PROMOTION_PULL_REQUEST_HEAD,
    PROMOTION_PULL_REQUEST_REPOS,
    PROMOTION_PULL_REQUEST_TITLE,
    _handle_create_github_pull_request,
)
from brain.systems.vault import async_resolve_project_bound_env_tokens


logger = logging.getLogger("illo.jobs.staging_promotion_pr")

CONFIGURED_REPOS = tuple(sorted(PROMOTION_PULL_REQUEST_REPOS))
ISSUE_NUMBER_PATTERN = re.compile(r"(?<![A-Za-z0-9_])#([1-9][0-9]*)\b")


@dataclass(frozen=True)
class PromotionActor:
    user_id: str
    org_id: str


class PromotionPullRequestError(RuntimeError):
    """A settled per-repository failure that should fail the scheduler run."""


def build_promotion_body(repo: str, commits: list[dict[str, Any]]) -> str:
    """Render compare commits and their issue references as Markdown."""

    lines = ["## Commits being promoted", ""]
    linked_issues: set[int] = set()
    for commit in commits:
        subject = str(commit.get("subject") or "(no commit subject)").strip()
        sha = str(commit.get("sha") or "").strip()
        url = str(commit.get("html_url") or "").strip()
        commit_label = sha[:7] if sha else "commit"
        commit_ref = f"[`{commit_label}`]({url})" if url else f"`{commit_label}`"
        issue_numbers = {
            int(match.group(1))
            for match in ISSUE_NUMBER_PATTERN.finditer(subject)
        }
        linked_issues.update(issue_numbers)
        lines.append(f"- {commit_ref} {subject}")

    lines.extend(["", "## Linked issues", ""])
    if linked_issues:
        lines.extend(
            f"- [#{number}](https://github.com/{repo}/issues/{number})"
            for number in sorted(linked_issues)
        )
    else:
        lines.append("- None referenced in the commit subjects.")
    return "\n".join(lines)


async def reconcile_repository(repo: str, *, token: str) -> dict[str, Any]:
    """Create the missing promotion PR for one repository, if staging is ahead."""

    comparison = await async_compare_repo_branches(
        repo,
        PROMOTION_PULL_REQUEST_BASE,
        PROMOTION_PULL_REQUEST_HEAD,
        token=token,
    )
    if comparison["ahead_by"] == 0:
        return {"repo": repo, "outcome": "no_diff"}

    open_pulls = await async_list_repo_pull_requests(
        repo,
        token=token,
        state="open",
        base=PROMOTION_PULL_REQUEST_BASE,
        head=PROMOTION_PULL_REQUEST_HEAD,
        limit=100,
    )
    matching_pull = next(
        (
            pull
            for pull in open_pulls.get("pull_requests", [])
            if (pull.get("base") or {}).get("ref") == PROMOTION_PULL_REQUEST_BASE
            and (pull.get("head") or {}).get("ref") == PROMOTION_PULL_REQUEST_HEAD
        ),
        None,
    )
    if matching_pull is not None:
        return {
            "repo": repo,
            "outcome": "already_open",
            "number": matching_pull.get("number"),
        }

    body = build_promotion_body(repo, comparison["commits"])
    raw_result = await _handle_create_github_pull_request(
        repo=repo,
        base=PROMOTION_PULL_REQUEST_BASE,
        head=PROMOTION_PULL_REQUEST_HEAD,
        title=PROMOTION_PULL_REQUEST_TITLE,
        body=body,
        draft=False,
    )
    try:
        result = json.loads(raw_result)
    except (TypeError, json.JSONDecodeError) as exc:
        raise PromotionPullRequestError(
            f"create_github_pull_request returned invalid JSON for {repo}"
        ) from exc
    if result.get("error") == "pull_request_exists":
        return {
            "repo": repo,
            "outcome": "already_open",
            "number": result.get("existing"),
        }
    if result.get("error"):
        raise PromotionPullRequestError(str(result["error"]))
    return {
        "repo": repo,
        "outcome": "created",
        "number": result.get("number"),
        "html_url": result.get("html_url"),
    }


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
            actor = await _promotion_actor(repo)
            token = await _repo_token(repo, actor)
            with bind_agent_context({
                "user_id": actor.user_id,
                "org_id": actor.org_id,
            }):
                result = await reconcile_repository(repo, token=token)
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
