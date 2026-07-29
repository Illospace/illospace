"""Incremental GitHub issue and pull-request indexing."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import logging
import re
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from brain.kernel.config import (
    KNOWLEDGE_CONNECTOR_BATCH_SIZE,
    KNOWLEDGE_GITHUB_REPOSITORIES,
)
from brain.platform.db.models.idea import ProjectProfile
from brain.platform.db.models.org import User
from brain.platform.db.models.vault import Secret, VaultProjectBinding
from brain.systems.cortex.project_context.github import (
    GithubIssueClosure,
    async_get_issue_closure_info,
    async_list_repo_issues,
    parse_github_repo_slug,
)
from brain.systems.knowledge.connectors.base import KnowledgeDraft
from brain.systems.knowledge.service import RAW_TEXT_MAX_CHARS
from brain.systems.vault import async_resolve_project_bound_env_tokens


_ISSUE_REF_RE = re.compile(
    r"(?<![\w.-])(?:(?P<repo>[A-Za-z0-9-]+/[A-Za-z0-9._-]+))?#(?P<number>\d+)"
)
_READ_PERMISSIONS = {
    "contents": "read",
    "issues": "read",
    "pull_requests": "read",
}

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _GitHubAuthority:
    token: str | None
    org_id: str | None
    actor_user_id: str | None


def _parse_timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _timestamp_key(value: Any, row_id: Any = 0) -> tuple[datetime, int]:
    return (
        _parse_timestamp(value) or datetime.min.replace(tzinfo=timezone.utc),
        int(row_id or 0),
    )


def _linked_references(body: str, repo: str) -> list[str]:
    references: list[str] = []
    for match in _ISSUE_REF_RE.finditer(body):
        reference = f"{match.group('repo') or repo}#{match.group('number')}"
        if reference not in references:
            references.append(reference)
    return references


def _resource_repositories(context: Any) -> list[str]:
    if not isinstance(context, Mapping):
        return []
    candidates: list[Any] = [context.get("repo")]
    for resource in context.get("resources") or []:
        if not isinstance(resource, Mapping):
            continue
        candidates.extend(
            [
                resource.get("repo"),
                resource.get("repo_url"),
                resource.get("uri"),
                resource.get("url"),
            ]
        )
    repositories: list[str] = []
    for candidate in candidates:
        slug = parse_github_repo_slug(str(candidate or ""))
        if slug and slug not in repositories:
            repositories.append(slug)
    return repositories


async def _configured_repositories(session: AsyncSession) -> list[str]:
    profiles = list(
        (
            await session.scalars(
                select(ProjectProfile)
                .where(ProjectProfile.active.is_(True))
                .order_by(ProjectProfile.created_at.asc(), ProjectProfile.id.asc())
            )
        ).all()
    )
    repositories: list[str] = []
    for profile in profiles:
        for repo in _resource_repositories(profile.project_context):
            if repo not in repositories:
                repositories.append(repo)
    if repositories:
        return repositories
    for configured in KNOWLEDGE_GITHUB_REPOSITORIES:
        slug = parse_github_repo_slug(configured)
        if slug and slug not in repositories:
            repositories.append(slug)
    return repositories


async def _github_authority(session: AsyncSession, repo: str) -> _GitHubAuthority:
    binding = await session.scalar(
        select(VaultProjectBinding)
        .join(Secret, Secret.id == VaultProjectBinding.secret_id)
        .where(
            func.lower(VaultProjectBinding.project_slug) == repo.lower(),
            VaultProjectBinding.active.is_(True),
            Secret.category == "github_app",
        )
        .order_by(VaultProjectBinding.id.asc())
        .limit(1)
    )
    if binding is None:
        return _GitHubAuthority(token=None, org_id=None, actor_user_id=None)
    actor = None
    if binding.created_by_user_id:
        actor = await session.get(User, str(binding.created_by_user_id))
    if actor is None:
        actor = (
            await session.scalars(
                select(User)
                .where(User.org_id == str(binding.org_id))
                .order_by(User.created_at.asc(), User.id.asc())
                .limit(1)
            )
        ).first()
    if actor is None:
        return _GitHubAuthority(
            token=None,
            org_id=str(binding.org_id),
            actor_user_id=None,
        )
    env = await async_resolve_project_bound_env_tokens(
        actor_user_id=str(actor.id),
        org_id=str(actor.org_id),
        project_slug=repo,
        github_app_only=True,
        github_app_permissions=_READ_PERMISSIONS,
    )
    for key in ("GITHUB_TOKEN", "GH_TOKEN"):
        token = str(env.get(key) or "").strip()
        if token:
            return _GitHubAuthority(
                token=token,
                org_id=str(actor.org_id),
                actor_user_id=str(actor.id),
            )
    token = next(
        (str(value).strip() for value in env.values() if str(value).strip()),
        None,
    )
    return _GitHubAuthority(
        token=token,
        org_id=str(actor.org_id),
        actor_user_id=str(actor.id),
    )


def _draft_for_issue(
    repo: str,
    issue: Mapping[str, Any],
    *,
    closure: GithubIssueClosure | None = None,
    org_id: str | None = None,
    actor_user_id: str | None = None,
) -> KnowledgeDraft:
    labels = [
        str(label.get("name") or "").strip()
        for label in issue.get("labels") or []
        if isinstance(label, Mapping) and str(label.get("name") or "").strip()
    ]
    body = str(issue.get("body") or "").strip()
    state = str(issue.get("state") or "unknown")
    kind = "pr" if issue.get("type") == "pull_request" else "issue"
    number = int(issue.get("number") or 0)
    author = issue.get("user") if isinstance(issue.get("user"), Mapping) else {}
    summary_prefix = f"State: {state}."
    if labels:
        summary_prefix += f" Labels: {', '.join(labels)}."
    summary = f"{summary_prefix}\n{body}".strip()
    resolution = None
    closure_extra: dict[str, Any] = {}
    if state == "closed":
        if kind == "pr" and issue.get("merged_at"):
            resolution = f"Merged at {issue['merged_at']}"
        elif kind == "issue" and closure is not None:
            fixing_pull_requests = [
                {
                    "repo": fixing.repo,
                    "number": fixing.number,
                    "base_ref_name": fixing.base_ref_name,
                    "merge_commit_sha": fixing.merge_commit_sha,
                    "merged_at": (
                        fixing.merged_at.isoformat()
                        if fixing.merged_at is not None
                        else None
                    ),
                }
                for fixing in closure.fixing_pull_requests
            ]
            closure_extra = {
                "closed_by": closure.closed_by,
                "fixing_pull_requests": fixing_pull_requests,
            }
            if closure.fixing_pull_requests:
                fixing = closure.fixing_pull_requests[0]
                resolution = f"Resolved by merged PR {fixing.canonical_ref}"
                if fixing.merge_commit_sha:
                    resolution += f" (commit {fixing.merge_commit_sha})"
                if fixing.merged_at is not None:
                    resolution += f" at {fixing.merged_at.isoformat()}"
            elif closure.closed_at is not None:
                resolution = f"Closed at {closure.closed_at.isoformat()}"
            else:
                resolution = "Closed"
        elif issue.get("closed_at"):
            resolution = f"Closed at {issue['closed_at']}"
        else:
            resolution = "Closed"
    closure_refs = (
        [fixing.canonical_ref for fixing in closure.fixing_pull_requests]
        if closure is not None
        else []
    )
    return KnowledgeDraft(
        source="github",
        kind=kind,
        source_ref=f"github:{repo}#{number}",
        title=str(issue.get("title") or f"{repo}#{number}"),
        summary=summary,
        resolution=resolution,
        entities=[
            *labels,
            *_linked_references(body, repo),
            *closure_refs,
        ],
        raw_text=body,
        extra={
            "repo": repo,
            "number": number,
            "state": state,
            "author": author.get("login"),
            "labels": labels,
            "url": issue.get("html_url"),
            "body_truncated": bool(issue.get("body_truncated")),
            "body_total_chars": int(issue.get("body_total_chars") or len(body)),
            **closure_extra,
            **({"org_id": org_id} if org_id else {}),
            **({"actor_user_id": actor_user_id} if actor_user_id else {}),
        },
        source_created_at=_parse_timestamp(issue.get("created_at")),
        source_updated_at=_parse_timestamp(issue.get("updated_at")),
        distill=True,
    )


class GitHubConnector:
    source_key = "github"

    def __init__(
        self,
        *,
        repositories: list[str] | tuple[str, ...] | None = None,
        max_items: int = KNOWLEDGE_CONNECTOR_BATCH_SIZE,
    ):
        self.repositories = tuple(repositories) if repositories is not None else None
        self.max_items = max(1, int(max_items))

    async def enumerate_changed(
        self,
        session: AsyncSession,
        cursor: dict[str, Any],
    ) -> tuple[list[KnowledgeDraft], dict[str, Any]]:
        repositories = list(self.repositories or await _configured_repositories(session))
        if not repositories:
            return [], dict(cursor)
        repo_states = {
            str(key): dict(value)
            for key, value in (cursor.get("repositories") or {}).items()
            if isinstance(value, Mapping)
        }
        active_index = min(
            max(0, int(cursor.get("active_repository") or 0)),
            len(repositories) - 1,
        )
        drafts: list[KnowledgeDraft] = []

        for repo_index in range(active_index, len(repositories)):
            repo = repositories[repo_index]
            state = dict(repo_states.get(repo) or {})
            watermark_key = _timestamp_key(
                state.get("watermark"),
                state.get("watermark_id"),
            )
            high_key = _timestamp_key(
                state.get("high_watermark") or state.get("watermark"),
                state.get("high_watermark_id") or state.get("watermark_id"),
            )
            remaining = self.max_items - len(drafts)
            if remaining <= 0:
                break
            authority = await _github_authority(session, repo)
            token = authority.token
            payload = await async_list_repo_issues(
                repo,
                token=token,
                state="all",
                since=state.get("watermark"),
                include_pull_requests=True,
                limit=remaining,
                cursor=state.get("next_page"),
                # Project-context reads compact bodies to 1000 chars for LLM
                # budgets; an index wants the whole body and bounds it once,
                # in the pipeline, at RAW_TEXT_MAX_CHARS.
                body_limit=RAW_TEXT_MAX_CHARS,
            )
            issues = [item for item in payload.get("issues") or [] if isinstance(item, Mapping)]
            for issue in issues:
                issue_key = _timestamp_key(issue.get("updated_at"), issue.get("id"))
                high_key = max(high_key, issue_key)
                if not state.get("next_page") and state.get("watermark") and issue_key <= watermark_key:
                    continue
                kind = "pr" if issue.get("type") == "pull_request" else "issue"
                closure = None
                if (
                    str(issue.get("state") or "").lower() == "closed"
                    and kind == "issue"
                ):
                    try:
                        closure = await async_get_issue_closure_info(
                            repo,
                            int(issue.get("number") or 0),
                            token=token,
                        )
                    except Exception as exc:
                        logger.warning(
                            "GitHub closure enrichment unavailable for %s#%s: %s",
                            repo,
                            issue.get("number"),
                            exc,
                        )
                drafts.append(
                    _draft_for_issue(
                        repo,
                        issue,
                        closure=closure,
                        org_id=authority.org_id,
                        actor_user_id=authority.actor_user_id,
                    )
                )

            next_page = payload.get("next_page")
            if next_page:
                repo_states[repo] = {
                    **state,
                    "next_page": next_page,
                    "high_watermark": high_key[0].isoformat(),
                    "high_watermark_id": high_key[1],
                }
                return drafts, {
                    "active_repository": repo_index,
                    "repositories": repo_states,
                }

            repo_states[repo] = {
                "watermark": high_key[0].isoformat(),
                "watermark_id": high_key[1],
            }
            if len(drafts) >= self.max_items:
                return drafts, {
                    "active_repository": min(repo_index + 1, len(repositories) - 1),
                    "repositories": repo_states,
                }

        return drafts, {
            "active_repository": 0,
            "repositories": repo_states,
        }


__all__ = ["GitHubConnector"]
