"""GitHub source read handlers for runtime tools."""

from __future__ import annotations

import json
from typing import Any

from brain.systems.cortex.project_context.github import (
    GitHubConnectorError,
    async_create_repo_issue,
    async_get_repo_by_slug,
    async_list_repo_issues,
    async_list_repo_pull_requests,
    parse_github_repo_slug,
)
from brain.systems.runs.tool_catalog.handlers.common import _agent_context
from brain.systems.vault import (
    VAULT_AGENT_ACCESS_AVAILABLE,
    async_get_secret,
    async_list_secrets,
    async_resolve_project_bound_env_tokens,
    normalize_agent_access_level,
)


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


async def _github_token_candidates(
    *,
    repo_slug: str,
    token_secret_key: str | None,
    for_write: bool = False,
) -> list[dict[str, str | None]]:
    """Return safe token candidates for a GitHub repo read.

    A bad explicit key should not strand a read when the org has a project-bound
    or default GitHub token that can see the target repo.

    ``for_write`` narrows the vault-inventory fallback to the canonical
    ``GITHUB_TOKEN``/``GH_TOKEN`` default. A read may fall back to any available
    github-ish secret, but an irreversible public write must not silently author
    an issue under an arbitrary teammate's personal token — write identity is
    limited to an explicit key, a repo project-binding, or the designated
    default token.
    """

    user_id = _clean(getattr(_agent_context, "user_id", None))
    org_id = _clean(getattr(_agent_context, "org_id", None))
    if token_secret_key and not user_id:
        raise ValueError("token_secret_key requires a run user_id context")

    candidates: list[dict[str, str | None]] = []
    seen_keys: set[str] = set()
    seen_tokens: set[str] = set()

    async def add_secret_key(key_name: str | None, source: str) -> None:
        key = _clean(key_name)
        if not key or key in seen_keys:
            return
        seen_keys.add(key)
        token = await _github_token_from_secret(key)
        if not token or token in seen_tokens:
            return
        seen_tokens.add(token)
        candidates.append({"key_name": key, "token": token, "source": source})

    async def add_token(token: str | None, source: str) -> None:
        value = _clean(token)
        if not value or value in seen_tokens:
            return
        seen_tokens.add(value)
        candidates.append({"key_name": None, "token": value, "source": source})

    await add_secret_key(token_secret_key, "explicit")

    if user_id and org_id:
        try:
            bound_env = await async_resolve_project_bound_env_tokens(
                actor_user_id=user_id,
                org_id=org_id,
                project_slug=repo_slug,
            )
        except Exception:
            bound_env = {}
        for env_name in ("GITHUB_TOKEN", "GH_TOKEN"):
            await add_token(bound_env.get(env_name), f"project_binding:{env_name}")
        for env_name, token in sorted(bound_env.items()):
            await add_token(token, f"project_binding:{env_name}")

        try:
            secrets = await async_list_secrets(actor_user_id=user_id, org_id=org_id)
        except Exception:
            secrets = []

        def priority(secret: dict[str, Any]) -> tuple[int, str]:
            key = str(secret.get("key_name") or "")
            if key == "GITHUB_TOKEN":
                return (0, key)
            if str(secret.get("category") or "").lower() == "github":
                return (1, key)
            return (2, key)

        github_like = [
            secret
            for secret in secrets
            if normalize_agent_access_level(secret.get("agent_access_level"))
            == VAULT_AGENT_ACCESS_AVAILABLE
            and (
                str(secret.get("key_name") or "") in {"GITHUB_TOKEN", "GH_TOKEN"}
                if for_write
                else (
                    str(secret.get("key_name") or "") == "GITHUB_TOKEN"
                    or "github" in str(secret.get("key_name") or "").lower()
                    or str(secret.get("category") or "").lower() == "github"
                )
            )
        ]
        for secret in sorted(github_like, key=priority):
            await add_secret_key(str(secret.get("key_name") or ""), "vault_inventory")

    if not candidates:
        candidates.append({"key_name": None, "token": None, "source": "public"})
    return candidates


async def _read_with_token(
    *,
    action: str,
    repo_slug: str,
    state: str,
    labels: list[str] | str | None,
    assignee: str | None,
    creator: str | None,
    mentioned: str | None,
    since: str | None,
    include_pull_requests: bool,
    head: str | None,
    base: str | None,
    limit: int,
    token: str | None,
) -> dict[str, Any]:
    if action in {"repo", "get_repo"}:
        return {
            "repo": await async_get_repo_by_slug(repo_slug, token=token),
        }
    if action in {"list_issues", "issues"}:
        return await async_list_repo_issues(
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
    if action in {"list_pull_requests", "pull_requests", "prs"}:
        return await async_list_repo_pull_requests(
            repo_slug,
            token=token,
            state=state,
            head=_clean(head),
            base=_clean(base),
            limit=_limit(limit),
        )
    raise ValueError("read_github_source action must be get_repo, list_issues, or list_pull_requests")


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
    clean_action = (action or "list_issues").strip().lower()
    valid_actions = {
        "repo",
        "get_repo",
        "list_issues",
        "issues",
        "list_pull_requests",
        "pull_requests",
        "prs",
    }
    if clean_action not in valid_actions:
        return json.dumps({"error": "read_github_source action must be get_repo, list_issues, or list_pull_requests"})

    candidates = await _github_token_candidates(
        repo_slug=repo_slug,
        token_secret_key=token_secret_key,
    )
    last_error: GitHubConnectorError | None = None
    auth_statuses = {401, 403, 404}
    for index, candidate in enumerate(candidates):
        try:
            payload = await _read_with_token(
                action=clean_action,
                repo_slug=repo_slug,
                state=state,
                labels=labels,
                assignee=assignee,
                creator=creator,
                mentioned=mentioned,
                since=since,
                include_pull_requests=include_pull_requests,
                head=head,
                base=base,
                limit=limit,
                token=candidate["token"],
            )
        except GitHubConnectorError as exc:
            last_error = exc
            if exc.status_code in auth_statuses and index < len(candidates) - 1:
                continue
            return json.dumps({"error": exc.message, "status_code": exc.status_code})
        payload["token_secret_key_used"] = bool(candidate.get("key_name"))
        payload["token_source"] = candidate["source"]
        if last_error is not None:
            payload["fallback_from_status_code"] = last_error.status_code
        return json.dumps(payload, default=str)

    if last_error is not None:
        return json.dumps({"error": last_error.message, "status_code": last_error.status_code})
    return json.dumps({"error": "No GitHub token candidates were available"})


async def _handle_create_github_issue(
    repo: str | None = None,
    title: str | None = None,
    body: str | None = None,
    labels: list[str] | str | None = None,
    assignees: list[str] | str | None = None,
    token_secret_key: str | None = None,
) -> str:
    """Open a REAL GitHub issue in the target repository.

    Degrades gracefully: when no write-capable token can reach the repo (empty
    candidates, or a 401/403/404 from every candidate) it returns a JSON error
    carrying ``no_write_token: true`` so the triage flow can fall back to asking
    for clarification or recording an internal tracker record + handoff, instead
    of falsely claiming a GitHub issue was filed.
    """

    repo_slug = parse_github_repo_slug(repo or "")
    if not repo_slug:
        return json.dumps({"error": "create_github_issue requires repo as owner/name or a GitHub URL"})
    clean_title = _clean(title)
    if not clean_title:
        return json.dumps({"error": "create_github_issue requires a non-empty title"})

    candidates = await _github_token_candidates(
        repo_slug=repo_slug,
        token_secret_key=token_secret_key,
        for_write=True,
    )
    # A write must never fall back to the public/no-token candidate.
    write_candidates = [candidate for candidate in candidates if candidate.get("token")]
    if not write_candidates:
        return json.dumps({
            "error": (
                "No write-capable GitHub token could reach this repo. Ask for clarification "
                "(which repo, or a write-capable token) or fall back to an internal tracker "
                "record + handoff — do not claim a GitHub issue was filed."
            ),
            "status_code": 401,
            "no_write_token": True,
            "repo": repo_slug,
        })

    last_error: GitHubConnectorError | None = None
    auth_statuses = {401, 403, 404}
    for index, candidate in enumerate(write_candidates):
        try:
            payload = await async_create_repo_issue(
                repo_slug,
                title=clean_title,
                body=_clean(body),
                labels=_string_list(labels),
                assignees=_string_list(assignees),
                token=candidate["token"],
            )
        except GitHubConnectorError as exc:
            last_error = exc
            # Retry the next token only on auth/visibility failures. A 422 is a
            # hard validation error and a success ends the loop, so a filed issue
            # never retries under a second identity.
            if exc.status_code in auth_statuses and index < len(write_candidates) - 1:
                continue
            return json.dumps({
                "error": exc.message,
                "status_code": exc.status_code,
                "no_write_token": exc.status_code in auth_statuses,
                "repo": repo_slug,
                "token_key_name": candidate.get("key_name"),
            })
        payload["token_secret_key_used"] = bool(candidate.get("key_name"))
        payload["token_source"] = candidate["source"]
        # Surface the exact Vault key that authored the public issue so the audit
        # manifest and any human-facing note record the identity used.
        payload["token_key_name"] = candidate.get("key_name")
        if last_error is not None:
            payload["fallback_from_status_code"] = last_error.status_code
        return json.dumps(payload, default=str)

    if last_error is not None:
        return json.dumps({
            "error": last_error.message,
            "status_code": last_error.status_code,
            "no_write_token": last_error.status_code in auth_statuses,
            "repo": repo_slug,
        })
    return json.dumps({"error": "No GitHub token candidates were available", "no_write_token": True})


__all__ = ["_handle_read_github_source", "_handle_create_github_issue"]
