"""GitHub source read handlers for runtime tools."""

from __future__ import annotations

import json
from typing import Any

from brain.systems.cortex.project_context.github import (
    GitHubConnectorError,
    async_get_repo_by_slug,
    async_list_repo_issues,
    async_list_repo_pull_requests,
    parse_github_repo_slug,
)
from brain.systems.runs.tool_catalog.handlers.common import _agent_context
from brain.systems.vault import async_get_secret


def _clean(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        value = [value]
    return [str(item).strip() for item in value if str(item or "").strip()]


def _limit(value: Any) -> int:
    try:
        return max(1, min(int(value or 30), 100))
    except (TypeError, ValueError):
        return 30


async def _github_token_from_secret(token_secret_key: str | None) -> str | None:
    key = _clean(token_secret_key)
    if not key:
        return None
    user_id = _clean(getattr(_agent_context, "user_id", None))
    org_id = _clean(getattr(_agent_context, "org_id", None))
    if not user_id:
        raise ValueError("token_secret_key requires a run user_id context")
    return await async_get_secret(
        key,
        actor_user_id=user_id,
        org_id=org_id,
        accessed_by="github_runtime_tool",
    )


async def _handle_read_github_source(
    action: str = "list_issues",
    repo: str | None = None,
    state: str = "open",
    labels: list[str] | str | None = None,
    assignee: str | None = None,
    creator: str | None = None,
    mentioned: str | None = None,
    since: str | None = None,
    include_pull_requests: bool = False,
    head: str | None = None,
    base: str | None = None,
    limit: int = 30,
    token_secret_key: str | None = None,
) -> str:
    """Read GitHub repo metadata, issues, or pull requests."""

    repo_slug = parse_github_repo_slug(repo or "")
    if not repo_slug:
        return json.dumps({"error": "read_github_source requires repo as owner/name or a GitHub URL"})
    token = await _github_token_from_secret(token_secret_key)
    clean_action = (action or "list_issues").strip().lower()
    try:
        if clean_action in {"repo", "get_repo"}:
            payload = {
                "repo": await async_get_repo_by_slug(repo_slug, token=token),
                "token_secret_key_used": bool(token_secret_key),
            }
        elif clean_action in {"list_issues", "issues"}:
            payload = await async_list_repo_issues(
                repo_slug,
                token=token,
                state=state,
                labels=_string_list(labels),
                assignee=_clean(assignee),
                creator=_clean(creator),
                mentioned=_clean(mentioned),
                since=_clean(since),
                include_pull_requests=bool(include_pull_requests),
                limit=_limit(limit),
            )
            payload["token_secret_key_used"] = bool(token_secret_key)
        elif clean_action in {"list_pull_requests", "pull_requests", "prs"}:
            payload = await async_list_repo_pull_requests(
                repo_slug,
                token=token,
                state=state,
                head=_clean(head),
                base=_clean(base),
                limit=_limit(limit),
            )
            payload["token_secret_key_used"] = bool(token_secret_key)
        else:
            return json.dumps({"error": "read_github_source action must be get_repo, list_issues, or list_pull_requests"})
    except GitHubConnectorError as exc:
        return json.dumps({"error": exc.message, "status_code": exc.status_code})
    return json.dumps(payload, default=str)


__all__ = ["_handle_read_github_source"]
