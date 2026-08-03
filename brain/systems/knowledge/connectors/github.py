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
from brain.platform.db.models.vault import Secret, VaultProjectBinding
from brain.systems.cortex.project_context.github import (
    GitHubConnectorError,
    GithubIssueClosure,
    async_get_issue_closure_info,
    async_list_repo_issues,
    parse_github_repo_slug,
)
from brain.systems.production_gate_github import (
    GITHUB_READ_ACCESS_FORBIDDEN,
    GITHUB_READ_AUTH_FAILURE_REASONS,
    github_read_reason_code,
)
from brain.systems.knowledge.connectors.base import (
    EnumerationFailure,
    KnowledgeDraft,
    KnowledgeEnumeration,
)
from brain.systems.knowledge.service import RAW_TEXT_MAX_CHARS
from brain.systems.vault import async_resolve_org_project_bound_env_tokens
from brain.systems.vault.runtime_secrets import RuntimeSecretUnavailable


_ISSUE_REF_RE = re.compile(
    r"(?<![\w.-])(?:(?P<repo>[A-Za-z0-9-]+/[A-Za-z0-9._-]+))?#(?P<number>\d+)"
)
_READ_PERMISSIONS = {
    "contents": "read",
    "issues": "read",
    "pull_requests": "read",
}
_CURSOR_VERSION = 2

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
        raise GitHubConnectorError(
            status_code=401,
            message=(
                "No active GitHub App Vault project binding exists for the "
                "knowledge sync."
            ),
        )
    org_id = str(binding.org_id)
    try:
        env = await async_resolve_org_project_bound_env_tokens(
            org_id=org_id,
            accessed_by="knowledge_index_sync",
            project_slug=repo,
            github_app_only=True,
            github_app_permissions=_READ_PERMISSIONS,
        )
    except GitHubConnectorError as exc:
        if exc.status_code in {403, 404, 422}:
            raise GitHubConnectorError(
                status_code=403,
                message=(
                    f"The bound GitHub App could not mint a token for {repo}: {exc}"
                ),
            ) from exc
        raise
    except RuntimeSecretUnavailable as exc:
        raise GitHubConnectorError(
            status_code=401,
            message=f"The bound GitHub App credential could not mint a token: {exc}",
        ) from exc
    for key in ("GITHUB_TOKEN", "GH_TOKEN"):
        token = str(env.get(key) or "").strip()
        if token:
            return _GitHubAuthority(
                token=token,
                org_id=org_id,
                actor_user_id=None,
            )
    token = next(
        (str(value).strip() for value in env.values() if str(value).strip()),
        None,
    )
    if not token:
        raise GitHubConnectorError(
            status_code=401,
            message="The active GitHub App Vault binding did not resolve a token.",
        )
    return _GitHubAuthority(token=token, org_id=org_id, actor_user_id=None)


def _configuration_failure(
    repo: str,
    exc: GitHubConnectorError,
) -> EnumerationFailure | None:
    reason_code = github_read_reason_code(exc)
    if reason_code not in GITHUB_READ_AUTH_FAILURE_REASONS:
        return None
    if reason_code == GITHUB_READ_ACCESS_FORBIDDEN:
        remediation = (
            f"Grant the installed GitHub App access to {repo}, then reconnect or "
            "update its active Vault project binding for this exact repository slug."
        )
    else:
        remediation = (
            f"Install or reconnect the GitHub App for {repo}, then create an active "
            "VaultProjectBinding to a github_app secret for this exact repository slug."
        )
    return EnumerationFailure(
        scope=repo,
        reason_code=reason_code,
        configuration_fault=True,
        message=f"{exc.message.rstrip('.')}. Remediation: {remediation}",
    )


def _draft_for_issue(
    repo: str,
    issue: Mapping[str, Any],
    *,
    closure: GithubIssueClosure | None = None,
    closure_unavailable_reason: str | None = None,
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
    if closure_unavailable_reason:
        closure_extra["closure_enrichment"] = {
            "status": "unavailable",
            "reason": closure_unavailable_reason,
        }
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


async def _enumerate_repository(
    session: AsyncSession,
    repo: str,
    state: dict[str, Any],
    *,
    remaining: int,
) -> tuple[list[KnowledgeDraft], dict[str, Any]]:
    """Enumerate one repository without mutating another repository's state."""

    watermark_key = _timestamp_key(
        state.get("watermark"),
        state.get("watermark_id"),
    )
    high_key = _timestamp_key(
        state.get("high_watermark") or state.get("watermark"),
        state.get("high_watermark_id") or state.get("watermark_id"),
    )
    authority = await _github_authority(session, repo)
    token = authority.token
    if not token:
        raise GitHubConnectorError(
            status_code=401,
            message="The knowledge GitHub connector refused an anonymous request.",
        )
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
    issues = [
        item
        for item in payload.get("issues") or []
        if isinstance(item, Mapping)
    ]
    repo_drafts: list[KnowledgeDraft] = []
    for issue in issues:
        issue_key = _timestamp_key(issue.get("updated_at"), issue.get("id"))
        high_key = max(high_key, issue_key)
        if (
            not state.get("next_page")
            and state.get("watermark")
            and issue_key <= watermark_key
        ):
            continue
        kind = "pr" if issue.get("type") == "pull_request" else "issue"
        closure = None
        if str(issue.get("state") or "").lower() == "closed" and kind == "issue":
            try:
                closure = await async_get_issue_closure_info(
                    repo,
                    int(issue.get("number") or 0),
                    token=token,
                )
            except Exception as exc:
                logger.exception(
                    "GitHub closure enrichment unavailable for %s#%s: %s",
                    repo,
                    issue.get("number"),
                    exc,
                )
                raise
        repo_drafts.append(
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
        next_state = {
            **state,
            "next_page": next_page,
            "high_watermark": high_key[0].isoformat(),
            "high_watermark_id": high_key[1],
        }
        return repo_drafts, next_state

    next_state = {
        "watermark": high_key[0].isoformat(),
        "watermark_id": high_key[1],
    }
    return repo_drafts, next_state


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
    ) -> KnowledgeEnumeration:
        """Enumerate changes while preserving the version-2 cursor contract.

        ``version`` is 2 and stays 2. Settled ``repositories`` entries contain
        ``watermark`` and ``watermark_id``; mid-backfill entries contain
        ``next_page``, ``high_watermark``, and ``high_watermark_id``.
        ``active_repository`` is the wire name for the repository index where
        the next sweep begins, and the sweep walks circularly from that index.
        Rolling deploys are safe because old and new builds both read the
        integer as a starting index.
        """
        if int(cursor.get("version") or 0) != _CURSOR_VERSION:
            cursor = {}
        repositories = list(self.repositories or await _configured_repositories(session))
        if not repositories:
            return KnowledgeEnumeration(
                drafts=[],
                cursor={**dict(cursor), "version": _CURSOR_VERSION},
            )
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
        failures: list[EnumerationFailure] = []

        # ``active_repository`` is the first repository due in this sweep.
        # Modulo traversal lets existing version-2 cursors wrap immediately.
        repository_count = len(repositories)
        for offset in range(repository_count):
            repo_index = (active_index + offset) % repository_count
            repo = repositories[repo_index]
            state = dict(repo_states.get(repo) or {})
            remaining = self.max_items - len(drafts)
            if remaining <= 0:
                break
            # Reserve capacity for every repository that is still due in this
            # circular sweep. A long page chain can use its share, but cannot
            # prevent later repositories from checking their watermarks.
            repositories_left = repository_count - offset
            repository_limit = max(1, remaining // repositories_left)
            try:
                repo_drafts, next_state = await _enumerate_repository(
                    session,
                    repo,
                    state,
                    remaining=repository_limit,
                )
            except GitHubConnectorError as exc:
                failure = _configuration_failure(repo, exc)
                if failure is None:
                    failure = EnumerationFailure(scope=repo, message=exc.message)
                    logger.exception(
                        "GitHub knowledge enumeration skipped repository %s: %s",
                        repo,
                        failure.message,
                    )
                else:
                    logger.error(
                        "GitHub knowledge configuration fault for repository %s "
                        "(%s): %s",
                        repo,
                        failure.reason_code,
                        failure.message,
                    )
                failures.append(failure)
                continue
            except Exception as exc:
                message = str(exc).strip() or type(exc).__name__
                failures.append(EnumerationFailure(scope=repo, message=message))
                logger.exception(
                    "GitHub knowledge enumeration skipped repository %s: %s",
                    repo,
                    message,
                )
                continue

            drafts.extend(repo_drafts)
            repo_states[repo] = next_state
            if len(drafts) >= self.max_items:
                return KnowledgeEnumeration(
                    drafts=drafts,
                    cursor={
                        "version": _CURSOR_VERSION,
                        "active_repository": (repo_index + 1) % repository_count,
                        "repositories": repo_states,
                    },
                    failures=tuple(failures),
                )

        # Considering every repository completes this sweep even when one or more
        # were skipped, so the next invocation starts from the canonical index zero.
        return KnowledgeEnumeration(
            drafts=drafts,
            cursor={
                "version": _CURSOR_VERSION,
                "active_repository": 0,
                "repositories": repo_states,
            },
            failures=tuple(failures),
        )


__all__ = ["GitHubConnector"]
