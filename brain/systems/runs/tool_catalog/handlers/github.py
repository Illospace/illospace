"""GitHub source read handlers for runtime tools."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import re
from typing import Any

from brain.kernel.common.pagination import InvalidPageToken
from brain.systems.cortex.project_context.github import (
    GitHubConnectorError,
    async_add_repo_issue_comment,
    async_add_repo_sub_issue,
    async_create_repo_issue,
    async_create_repo_pull_request,
    async_grep_repo,
    async_get_repo_issue_parent,
    async_get_pull_request_deploy_info,
    async_get_pull_request,
    async_get_pull_request_checks,
    async_get_repo_counts,
    async_get_repo_by_slug,
    async_get_repo_file,
    async_list_repo_tree,
    async_list_repo_issues,
    async_list_repo_pull_requests,
    async_list_repo_sub_issues,
    async_remove_repo_sub_issue,
    async_update_repo_issue,
    parse_github_repo_slug,
)
from brain.systems.deploy_state import (
    DeployState,
    as_utc_datetime,
    classify_refire,
    derive_deploy_state,
)
from brain.systems.deploy_state_config import (
    deploy_feature_enabled,
    deploy_settle_window,
)
from brain.systems.deploy_state_github import is_ancestor_of
from brain.systems.runs.execution_context import get_or_create_agent_run_state
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


_SLACK_ORIGIN_REF_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_-])(slack:[A-Za-z0-9_-]+:[A-Za-z0-9_-]+:[0-9.]+)"
)


def _origin_ref_from_body(body: str | None) -> str | None:
    match = _SLACK_ORIGIN_REF_PATTERN.search(str(body or ""))
    return match.group(1) if match else None


def _origin_ref_from_context() -> str | None:
    containers: list[Any] = [
        getattr(_agent_context, "execution_metadata", None),
        getattr(_agent_context, "target_ref", None),
    ]
    run = getattr(_agent_context, "run", None)
    containers.extend(
        (
            getattr(run, "target_ref", None),
            getattr(run, "metadata_", None),
            getattr(run, "metadata", None),
        )
    )
    for container in containers:
        if not isinstance(container, dict):
            continue
        origin_ref = _clean(container.get("origin_ref"))
        if origin_ref:
            return origin_ref
    return None


def _current_tool_run_id() -> int | None:
    raw = getattr(_agent_context, "run_id", None)
    if raw in (None, ""):
        raw = getattr(getattr(_agent_context, "run", None), "id", None)
    try:
        return int(raw) if raw not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _body_with_origin_ref(body: str | None, origin_ref: str | None) -> str | None:
    cleaned = _clean(body)
    if not origin_ref or origin_ref in str(cleaned or ""):
        return cleaned
    suffix = f"Origin ref: `{origin_ref}`"
    return f"{cleaned}\n\n{suffix}" if cleaned else suffix


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


def _positive_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    if isinstance(value, bool) or number < 1:
        return None
    return number


def _is_known_legacy_token_key(value: Any) -> bool:
    key = str(value or "").strip().upper()
    return "__" in key and key.endswith("_LEGACY")


@dataclass
class _GitHubReadState:
    rejected: dict[tuple[str | None, str], GitHubConnectorError] = field(default_factory=dict)
    preferred: dict[str, str | None] = field(default_factory=dict)


def _github_read_state() -> _GitHubReadState:
    """Return context-local token history for the current agent run."""

    return get_or_create_agent_run_state("github_read", _GitHubReadState)


def _ordered_read_candidates(
    candidates: list[dict[str, str | None]],
    *,
    repo_slug: str,
    state: _GitHubReadState,
) -> list[dict[str, str | None]]:
    available = [
        candidate
        for candidate in candidates
        if (candidate.get("token"), repo_slug) not in state.rejected
    ]
    if repo_slug in state.preferred:
        preferred_token = state.preferred[repo_slug]
        available.sort(key=lambda candidate: candidate.get("token") != preferred_token)
    return available


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
    repo_slugs: list[str] | None = None,
    for_write: bool = False,
    org_id: str | None = None,
    user_id: str | None = None,
) -> list[dict[str, str | None]]:
    """Return safe token candidates for a GitHub repo operation.

    A bad explicit key should not strand a read when the org has a project-bound
    or default GitHub token that can see the target repo.

    ``for_write`` restricts automatic identity selection to GitHub App project
    bindings. Writes may also use an explicit ``token_secret_key``. Reads keep
    the broader project-binding and vault-inventory fallback behavior.

    ``repo_slugs`` lets a cross-repository operation mint one installation
    token down-scoped to every participating repository binding.

    ``org_id``/``user_id`` override the run context for BACKEND callers (e.g.
    dossier gathering) where no ``_agent_context`` exists; tool handlers omit
    them and inherit the run identity as before.
    """

    user_id = _clean(user_id) or _clean(getattr(_agent_context, "user_id", None))
    org_id = _clean(org_id) or _clean(getattr(_agent_context, "org_id", None))
    if token_secret_key and not user_id:
        raise ValueError("token_secret_key requires a run user_id context")

    candidates: list[dict[str, str | None]] = []
    seen_keys: set[str] = set()
    seen_tokens: set[str] = set()

    async def add_secret_key(key_name: str | None, source: str) -> None:
        key = _clean(key_name)
        if not key or key in seen_keys or _is_known_legacy_token_key(key):
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
                project_slugs=repo_slugs,
                github_app_only=for_write,
            )
        except Exception:
            bound_env = {}
        for env_name in ("GITHUB_TOKEN", "GH_TOKEN"):
            await add_token(bound_env.get(env_name), f"project_binding:{env_name}")

        sorted_secrets: list[dict[str, Any]] = []
        if not for_write:
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
                    str(secret.get("key_name") or "") == "GITHUB_TOKEN"
                    or "github" in str(secret.get("key_name") or "").lower()
                    or str(secret.get("category") or "").lower() == "github"
                )
            ]
            sorted_secrets = sorted(github_like, key=priority)
            for secret in sorted_secrets:
                if str(secret.get("key_name") or "") != "GITHUB_TOKEN":
                    continue
                await add_secret_key(str(secret.get("key_name") or ""), "vault_inventory")

        for env_name, token in sorted(bound_env.items()):
            if env_name in {"GITHUB_TOKEN", "GH_TOKEN"} or _is_known_legacy_token_key(env_name):
                continue
            await add_token(token, f"project_binding:{env_name}")

        if not for_write:
            for secret in sorted_secrets:
                if str(secret.get("key_name") or "") == "GITHUB_TOKEN":
                    continue
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
    pull_number: int | None,
    sha: str | None,
    cursor: str | None,
    ref: str | None,
    path: str | None,
    line_start: int,
    line_end: int | None,
    query: str | None,
    case_sensitive: bool,
) -> dict[str, Any]:
    if action in {"repo", "get_repo"}:
        repo = await async_get_repo_by_slug(repo_slug, token=token)
        if repo is None:
            raise GitHubConnectorError(status_code=404, message="Repository not found or not visible to this token.")
        return {"repo": repo}
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
            cursor=cursor,
        )
    if action in {"list_pull_requests", "pull_requests", "prs"}:
        return await async_list_repo_pull_requests(
            repo_slug,
            token=token,
            state=state,
            head=_clean(head),
            base=_clean(base),
            limit=_limit(limit),
            cursor=cursor,
        )
    if action in {"pull_request", "get_pull_request", "pr"}:
        return await async_get_pull_request(
            repo_slug,
            int(pull_number or 0),
            token=token,
        )
    if action in {"pull_request_checks", "pr_checks", "checks"}:
        return await async_get_pull_request_checks(
            repo_slug,
            str(sha or ""),
            token=token,
        )
    if action in {"counts", "get_counts", "exact_counts"}:
        return await async_get_repo_counts(
            repo_slug,
            token=token,
            state=state,
        )
    if action == "get_file":
        return await async_get_repo_file(
            repo_slug,
            str(path or ""),
            ref=str(ref or ""),
            token=token,
            line_start=line_start,
            line_end=line_end,
        )
    if action == "list_tree":
        return await async_list_repo_tree(
            repo_slug,
            ref=str(ref or ""),
            token=token,
            path=path,
            limit=_limit(limit),
            cursor=cursor,
        )
    if action == "grep":
        return await async_grep_repo(
            repo_slug,
            str(query or ""),
            ref=str(ref or ""),
            token=token,
            path=path,
            case_sensitive=bool(case_sensitive),
            limit=_limit(limit),
            cursor=cursor,
        )
    raise ValueError(
        "read_github_source action must be get_repo, list_issues, list_pull_requests, "
        "get_pull_request, pull_request_checks, get_counts, get_file, list_tree, or grep"
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
    pull_number: int | None = None,
    number: int | None = None,
    sha: str | None = None,
    limit: int = 30,
    token_secret_key: str | None = None,
    cursor: str | None = None,
    ref: str | None = None,
    path: str | None = None,
    line_start: int = 1,
    line_end: int | None = None,
    query: str | None = None,
    case_sensitive: bool = False,
) -> str:
    """Read GitHub metadata, work items, CI, or bounded source at a ref."""

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
        "pull_request",
        "get_pull_request",
        "pr",
        "pull_request_checks",
        "pr_checks",
        "checks",
        "counts",
        "get_counts",
        "exact_counts",
        "get_file",
        "list_tree",
        "grep",
    }
    if clean_action not in valid_actions:
        return json.dumps({
            "error": (
                "read_github_source action must be get_repo, list_issues, list_pull_requests, "
                "get_pull_request, pull_request_checks, get_counts, get_file, list_tree, or grep"
            )
        })
    if clean_action in {"pull_request", "get_pull_request", "pr"}:
        try:
            clean_pull_number = int(number or pull_number or 0)
        except (TypeError, ValueError):
            clean_pull_number = 0
        if clean_pull_number < 1:
            return json.dumps({"error": "get_pull_request requires a positive pull_number"})
    else:
        clean_pull_number = None
    clean_sha = _clean(sha)
    if clean_action in {"pull_request_checks", "pr_checks", "checks"} and not clean_sha:
        return json.dumps({"error": "pull_request_checks requires sha"})
    clean_ref = _clean(ref)
    clean_path = _clean(path)
    raw_query = str(query) if query is not None else None
    clean_query = raw_query if raw_query and raw_query.strip() else None
    if clean_action in {"get_file", "list_tree", "grep"} and not clean_ref:
        return json.dumps({"error": f"{clean_action} requires ref"})
    if clean_action == "grep" and not clean_query:
        return json.dumps({"error": "grep requires query"})
    if clean_action == "get_file":
        if not clean_path:
            return json.dumps({"error": "get_file requires path"})
        clean_line_start = _positive_int(line_start)
        if clean_line_start is None:
            return json.dumps({"error": "get_file line_start must be a positive integer"})
        clean_line_end = _positive_int(line_end) if line_end is not None else None
        if line_end is not None and clean_line_end is None:
            return json.dumps({"error": "get_file line_end must be a positive integer"})
        if clean_line_end is not None and clean_line_end < clean_line_start:
            return json.dumps({"error": "get_file line_end must be greater than or equal to line_start"})
    else:
        clean_line_start = 1
        clean_line_end = None

    candidates = await _github_token_candidates(
        repo_slug=repo_slug,
        token_secret_key=token_secret_key,
    )
    read_state = _github_read_state()
    read_candidates = _ordered_read_candidates(
        candidates,
        repo_slug=repo_slug,
        state=read_state,
    )
    if not read_candidates:
        cached_errors = [
            error
            for (token, rejected_repo), error in read_state.rejected.items()
            if rejected_repo == repo_slug and any(candidate.get("token") == token for candidate in candidates)
        ]
        if cached_errors:
            cached_error = cached_errors[-1]
            return json.dumps({
                "error": cached_error.message,
                "status_code": cached_error.status_code,
                "attempted_token_sources": [candidate["source"] for candidate in candidates],
            })
        return json.dumps({"error": "No GitHub token candidates were available"})
    last_error: GitHubConnectorError | None = None
    retry_statuses = {401, 403, 404}
    if clean_action in {"counts", "get_counts", "exact_counts"}:
        # The generated query is valid, so GitHub Search's 422 can mean that
        # this candidate cannot search the private repository.
        retry_statuses.add(422)
    # Source-path and ref 404s are ordinary content misses, not proof that the
    # token cannot see the repository. Caching them would poison later valid
    # reads for the same repository during this agent run.
    negative_cache_statuses = (
        {401}
        if clean_action in {"get_file", "list_tree", "grep"}
        else {401, 404}
    )
    attempted_token_sources: list[str] = []
    for index, candidate in enumerate(read_candidates):
        attempted_token_sources.append(candidate["source"])
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
                pull_number=clean_pull_number,
                sha=clean_sha,
                cursor=_clean(cursor),
                ref=clean_ref,
                path=clean_path,
                line_start=clean_line_start,
                line_end=clean_line_end,
                query=clean_query,
                case_sensitive=bool(case_sensitive),
            )
        except InvalidPageToken as exc:
            return json.dumps({"error": str(exc)})
        except GitHubConnectorError as exc:
            last_error = exc
            if exc.status_code in negative_cache_statuses:
                read_state.rejected[(candidate.get("token"), repo_slug)] = exc
            if exc.status_code in retry_statuses and index < len(read_candidates) - 1:
                continue
            return json.dumps({
                "error": exc.message,
                "status_code": exc.status_code,
                "attempted_token_sources": attempted_token_sources,
            })
        read_state.preferred[repo_slug] = candidate.get("token")
        payload["token_secret_key_used"] = bool(candidate.get("key_name"))
        payload["token_source"] = candidate["source"]
        if last_error is not None:
            payload["fallback_from_status_code"] = last_error.status_code
        return json.dumps(payload, default=str)

    if last_error is not None:
        return json.dumps({
            "error": last_error.message,
            "status_code": last_error.status_code,
            "attempted_token_sources": attempted_token_sources,
        })
    return json.dumps({"error": "No GitHub token candidates were available"})


async def _handle_create_github_issue(
    repo: str | None = None,
    title: str | None = None,
    body: str | None = None,
    labels: list[str] | str | None = None,
    assignees: list[str] | str | None = None,
    token_secret_key: str | None = None,
    origin_ref: str | None = None,
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
    resolved_origin_ref = (
        _clean(origin_ref)
        or _origin_ref_from_body(body)
        or _origin_ref_from_context()
    )
    issue_body = _body_with_origin_ref(body, resolved_origin_ref)

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
                f"No GitHub App identity is connected for {repo_slug}. Connect the GitHub App "
                "for this repo (or pass token_secret_key) so Illo can write as its own bot."
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
                body=issue_body,
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
        if resolved_origin_ref:
            from brain.systems.runs.slack_delivery import (
                OpenAskArtifact,
                deliver_open_ask_artifact_reply,
            )

            issue = payload.get("issue")
            issue = issue if isinstance(issue, dict) else {}
            issue_number = issue.get("number")
            issue_url = _clean(issue.get("html_url") or issue.get("url"))
            artifact_delivery = await deliver_open_ask_artifact_reply(
                origin_ref=resolved_origin_ref,
                artifact=OpenAskArtifact(
                    kind="GitHub issue",
                    reference=(
                        f"{repo_slug}#{issue_number}"
                        if issue_number is not None
                        else repo_slug
                    ),
                    title=_clean(issue.get("title")) or clean_title,
                    url=issue_url,
                ),
                answering_run_id=_current_tool_run_id(),
            )
            payload["origin_ref"] = resolved_origin_ref
            if artifact_delivery is not None:
                payload["origin_ask_delivery"] = artifact_delivery
                delivered = [
                    item
                    for item in artifact_delivery.get("origin_asks", [])
                    if isinstance(item, dict) and item.get("delivered")
                ]
                if delivered:
                    payload["origin_ask"] = {
                        "requester": delivered[0].get("requester"),
                        "request": delivered[0].get("ask"),
                        "mechanism": delivered[0].get("mechanism"),
                        "announcement": delivered[0].get("announcement"),
                    }
        return json.dumps(payload, default=str)

    if last_error is not None:
        return json.dumps({
            "error": last_error.message,
            "status_code": last_error.status_code,
            "no_write_token": last_error.status_code in auth_statuses,
            "repo": repo_slug,
        })
    return json.dumps({"error": "No GitHub token candidates were available", "no_write_token": True})


def _existing_pull_request_number(message: str) -> int | None:
    for pattern in (r"/pull/(\d+)", r"pull request\s+#?(\d+)"):
        match = re.search(pattern, message, flags=re.IGNORECASE)
        if match:
            return int(match.group(1))
    return None


async def _handle_create_github_pull_request(
    repo: str | None = None,
    base: str | None = None,
    head: str | None = None,
    title: str | None = None,
    body: str | None = None,
    draft: bool = False,
    token_secret_key: str | None = None,
) -> str:
    """Open a REAL GitHub pull request without ever merging it."""

    repo_slug = parse_github_repo_slug(repo or "")
    if not repo_slug:
        return json.dumps({
            "error": "create_github_pull_request requires repo as owner/name or a GitHub URL"
        })
    clean_base = _clean(base)
    if not clean_base:
        return json.dumps({"error": "create_github_pull_request requires a non-empty base"})
    clean_head = _clean(head)
    if not clean_head:
        return json.dumps({"error": "create_github_pull_request requires a non-empty head"})
    clean_title = _clean(title)
    if not clean_title:
        return json.dumps({"error": "create_github_pull_request requires a non-empty title"})
    clean_body = _clean(body)
    if not clean_body:
        return json.dumps({"error": "create_github_pull_request requires a non-empty body"})

    candidates = await _github_token_candidates(
        repo_slug=repo_slug,
        token_secret_key=token_secret_key,
        for_write=True,
    )
    write_candidates = [candidate for candidate in candidates if candidate.get("token")]
    if not write_candidates:
        return json.dumps({
            "error": (
                f"No GitHub App identity is connected for {repo_slug}. Connect the GitHub App "
                "for this repo (or pass token_secret_key) so Illo can open the pull request."
            ),
            "status_code": 401,
            "no_write_token": True,
            "repo": repo_slug,
        })

    last_error: GitHubConnectorError | None = None
    auth_statuses = {401, 403, 404}
    for index, candidate in enumerate(write_candidates):
        try:
            payload = await async_create_repo_pull_request(
                repo_slug,
                base=clean_base,
                head=clean_head,
                title=clean_title,
                body=clean_body,
                draft=bool(draft),
                token=candidate["token"],
            )
        except GitHubConnectorError as exc:
            last_error = exc
            if exc.status_code in auth_statuses and index < len(write_candidates) - 1:
                continue
            lowered = exc.message.lower()
            if exc.status_code == 422 and "no commits between" in lowered:
                return json.dumps({
                    "error": "no_commits_between",
                    "message": exc.message,
                    "status_code": 422,
                    "repo": repo_slug,
                    "base": clean_base,
                    "head": clean_head,
                })
            if exc.status_code == 422 and "pull request already exists" in lowered:
                return json.dumps({
                    "error": "pull_request_exists",
                    "existing": _existing_pull_request_number(exc.message),
                    "message": exc.message,
                    "status_code": 422,
                    "repo": repo_slug,
                    "base": clean_base,
                    "head": clean_head,
                })
            error_payload = {
                "error": exc.message,
                "status_code": exc.status_code,
                "no_write_token": exc.status_code in auth_statuses,
                "repo": repo_slug,
                "token_key_name": candidate.get("key_name"),
            }
            if exc.status_code == 403:
                error_payload["required_permission"] = "pull_requests:write"
            return json.dumps(error_payload)

        pull_request = payload.get("pull_request")
        pull_request = pull_request if isinstance(pull_request, dict) else {}
        result = {
            "repo": repo_slug,
            "number": pull_request.get("number"),
            "html_url": pull_request.get("html_url"),
            "state": pull_request.get("state"),
            "draft": bool(pull_request.get("draft")),
            "token_secret_key_used": bool(candidate.get("key_name")),
            "token_source": candidate["source"],
            "token_key_name": candidate.get("key_name"),
        }
        if last_error is not None:
            result["fallback_from_status_code"] = last_error.status_code
        return json.dumps(result, default=str)

    return json.dumps({"error": "No GitHub token candidates were available", "no_write_token": True})


async def _handle_add_github_issue_comment(
    repo: str | None = None,
    issue_number: int | None = None,
    body: str | None = None,
    token_secret_key: str | None = None,
) -> str:
    """Append one comment without changing issue fields."""

    repo_slug = parse_github_repo_slug(repo or "")
    if not repo_slug:
        return json.dumps({
            "error": "add_github_issue_comment requires repo as owner/name or a GitHub URL"
        })
    clean_issue_number = _positive_int(issue_number)
    if clean_issue_number is None:
        return json.dumps({"error": "add_github_issue_comment requires a positive issue_number"})
    raw_body = str(body) if body is not None else ""
    if not raw_body.strip():
        return json.dumps({
            "error": "add_github_issue_comment requires a non-empty body",
            "status_code": 422,
        })

    candidates = await _github_token_candidates(
        repo_slug=repo_slug,
        token_secret_key=token_secret_key,
        for_write=True,
    )
    write_candidates = [candidate for candidate in candidates if candidate.get("token")]
    if not write_candidates:
        return json.dumps({
            "error": (
                f"No GitHub App identity is connected for {repo_slug}. Connect the GitHub App "
                "for this repo (or pass token_secret_key) so Illo can comment on the issue."
            ),
            "status_code": 401,
            "no_write_token": True,
            "repo": repo_slug,
            "issue_number": clean_issue_number,
        })

    auth_statuses = {401, 403, 404}
    fallback_status_code: int | None = None
    for index, candidate in enumerate(write_candidates):
        try:
            payload = await async_add_repo_issue_comment(
                repo_slug,
                clean_issue_number,
                body=raw_body,
                token=str(candidate["token"]),
            )
        except GitHubConnectorError as exc:
            if exc.status_code in auth_statuses and index < len(write_candidates) - 1:
                fallback_status_code = exc.status_code
                continue
            return json.dumps({
                "error": exc.message,
                "status_code": exc.status_code,
                "no_write_token": exc.status_code in auth_statuses,
                "repo": repo_slug,
                "issue_number": clean_issue_number,
                "token_key_name": candidate.get("key_name"),
            })

        payload["token_secret_key_used"] = bool(candidate.get("key_name"))
        payload["token_source"] = candidate["source"]
        payload["token_key_name"] = candidate.get("key_name")
        if fallback_status_code is not None:
            payload["fallback_from_status_code"] = fallback_status_code
        return json.dumps(payload, default=str)

    return json.dumps({"error": "No GitHub token candidates were available", "no_write_token": True})


def _update_has_only_auth_failures(payload: dict[str, Any]) -> bool:
    if payload.get("applied"):
        return False
    failed = payload.get("failed")
    if not isinstance(failed, dict) or not failed:
        return False
    status_codes = {
        result.get("status_code")
        for result in failed.values()
        if isinstance(result, dict)
    }
    return bool(status_codes) and status_codes <= {401, 403, 404}


async def _handle_update_github_issue(
    repo: str | None = None,
    issue_number: int | None = None,
    assignees_add: list[str] | str | None = None,
    assignees_remove: list[str] | str | None = None,
    labels_add: list[str] | str | None = None,
    labels_remove: list[str] | str | None = None,
    labels_set: list[str] | str | None = None,
    state: str | None = None,
    title: str | None = None,
    body: str | None = None,
    token_secret_key: str | None = None,
) -> str:
    """Update a real GitHub issue using the issue-create App write lane."""

    repo_slug = parse_github_repo_slug(repo or "")
    if not repo_slug:
        return json.dumps({"error": "update_github_issue requires repo as owner/name or a GitHub URL"})
    clean_issue_number = _positive_int(issue_number)
    if clean_issue_number is None:
        return json.dumps({"error": "update_github_issue requires a positive issue_number"})

    clean_assignees_add = _string_list(assignees_add)
    clean_assignees_remove = _string_list(assignees_remove)
    clean_labels_add = _string_list(labels_add)
    clean_labels_remove = _string_list(labels_remove)
    clean_labels_set = _string_list(labels_set) if labels_set is not None else None
    clean_state = _clean(state)
    if clean_state is not None:
        clean_state = clean_state.lower()
    if clean_state not in {None, "open", "closed"}:
        return json.dumps({
            "error": "update_github_issue state must be open or closed",
            "status_code": 422,
        })
    clean_title = str(title).strip() if title is not None else None
    if title is not None and not clean_title:
        return json.dumps({
            "error": "update_github_issue title must be non-empty when provided",
            "status_code": 422,
        })
    clean_body = str(body) if body is not None else None
    if clean_labels_set is not None and (clean_labels_add or clean_labels_remove):
        return json.dumps({
            "error": "update_github_issue labels_set cannot be combined with labels_add or labels_remove",
            "status_code": 422,
        })
    if not any((
        clean_assignees_add,
        clean_assignees_remove,
        clean_labels_add,
        clean_labels_remove,
        clean_labels_set is not None,
        clean_state is not None,
        clean_title is not None,
        clean_body is not None,
    )):
        return json.dumps({
            "error": "update_github_issue requires at least one field to update",
            "status_code": 422,
        })

    candidates = await _github_token_candidates(
        repo_slug=repo_slug,
        token_secret_key=token_secret_key,
        for_write=True,
    )
    write_candidates = [candidate for candidate in candidates if candidate.get("token")]
    if not write_candidates:
        return json.dumps({
            "error": (
                f"No GitHub App identity is connected for {repo_slug}. Connect the GitHub App "
                "for this repo (or pass token_secret_key) so Illo can update the issue."
            ),
            "status_code": 401,
            "no_write_token": True,
            "repo": repo_slug,
            "issue_number": clean_issue_number,
        })

    last_error: GitHubConnectorError | None = None
    fallback_status_code: int | None = None
    auth_statuses = {401, 403, 404}
    for index, candidate in enumerate(write_candidates):
        try:
            payload = await async_update_repo_issue(
                repo_slug,
                clean_issue_number,
                assignees_add=clean_assignees_add,
                assignees_remove=clean_assignees_remove,
                labels_add=clean_labels_add,
                labels_remove=clean_labels_remove,
                labels_set=clean_labels_set,
                state=clean_state,
                title=clean_title,
                body=clean_body,
                token=str(candidate["token"]),
            )
        except GitHubConnectorError as exc:
            last_error = exc
            if exc.status_code in auth_statuses and index < len(write_candidates) - 1:
                continue
            return json.dumps({
                "error": exc.message,
                "status_code": exc.status_code,
                "no_write_token": exc.status_code in auth_statuses,
                "repo": repo_slug,
                "issue_number": clean_issue_number,
                "token_key_name": candidate.get("key_name"),
            })

        if _update_has_only_auth_failures(payload) and index < len(write_candidates) - 1:
            status_codes = [
                result.get("status_code")
                for result in payload.get("failed", {}).values()
                if isinstance(result, dict)
            ]
            fallback_status_code = next((code for code in status_codes if code is not None), None)
            continue

        payload["token_secret_key_used"] = bool(candidate.get("key_name"))
        payload["token_source"] = candidate["source"]
        payload["token_key_name"] = candidate.get("key_name")
        if fallback_status_code is not None:
            payload["fallback_from_status_code"] = fallback_status_code
        if _update_has_only_auth_failures(payload):
            payload["no_write_token"] = True
        return json.dumps(payload, default=str)

    if last_error is not None:
        return json.dumps({
            "error": last_error.message,
            "status_code": last_error.status_code,
            "no_write_token": last_error.status_code in auth_statuses,
            "repo": repo_slug,
            "issue_number": clean_issue_number,
        })
    return json.dumps({"error": "No GitHub token candidates were available", "no_write_token": True})


def _sub_issue_write_error(
    exc: GitHubConnectorError,
    *,
    operation: str,
    parent_repo: str,
    child_repo: str,
    token_key_name: str | None = None,
) -> dict[str, Any]:
    auth_statuses = {401, 403, 404}
    if exc.status_code == 403:
        error = (
            f"GitHub refused to {operation} the sub-issue with HTTP 403. Ensure the GitHub App is "
            f"installed on both {parent_repo} and {child_repo} with Issues: Read and write, then "
            "reapprove or reconnect the installation before retrying."
        )
    else:
        error = exc.message
    return {
        "error": error,
        "status_code": exc.status_code,
        "no_write_token": exc.status_code in auth_statuses,
        "missing_scope": exc.status_code == 403,
        "parent_repo": parent_repo,
        "child_repo": child_repo,
        "token_key_name": token_key_name,
    }


async def _handle_github_sub_issue_write(
    operation: str,
    *,
    parent_repo: str | None,
    parent_issue_number: int | None,
    child_repo: str | None,
    child_issue_number: int | None,
    token_secret_key: str | None,
) -> str:
    parent_slug = parse_github_repo_slug(parent_repo or "")
    if not parent_slug:
        return json.dumps({
            "error": f"{operation}_github_sub_issue requires parent_repo as owner/name or a GitHub URL"
        })
    child_slug = parse_github_repo_slug(child_repo or "")
    if not child_slug:
        return json.dumps({
            "error": f"{operation}_github_sub_issue requires child_repo as owner/name or a GitHub URL"
        })
    clean_parent_number = _positive_int(parent_issue_number)
    if clean_parent_number is None:
        return json.dumps({
            "error": f"{operation}_github_sub_issue requires a positive parent_issue_number"
        })
    clean_child_number = _positive_int(child_issue_number)
    if clean_child_number is None:
        return json.dumps({
            "error": f"{operation}_github_sub_issue requires a positive child_issue_number"
        })
    if parent_slug.split("/", 1)[0].lower() != child_slug.split("/", 1)[0].lower():
        return json.dumps({
            "error": "GitHub sub-issues must have the same repository owner as their parent issue",
            "status_code": 422,
            "parent_repo": parent_slug,
            "child_repo": child_slug,
        })

    repo_slugs = list(dict.fromkeys([parent_slug, child_slug]))
    candidates = await _github_token_candidates(
        repo_slug=parent_slug,
        repo_slugs=repo_slugs,
        token_secret_key=token_secret_key,
        for_write=True,
    )
    write_candidates = [candidate for candidate in candidates if candidate.get("token")]
    if not write_candidates:
        return json.dumps({
            "error": (
                "No GitHub App identity is connected for every repository in this sub-issue "
                f"relationship ({', '.join(repo_slugs)}). Connect the GitHub App to both repos "
                "(or pass token_secret_key) so Illo can update the native relationship."
            ),
            "status_code": 401,
            "no_write_token": True,
            "parent_repo": parent_slug,
            "child_repo": child_slug,
        })

    last_error: GitHubConnectorError | None = None
    auth_statuses = {401, 403, 404}
    for index, candidate in enumerate(write_candidates):
        try:
            if operation == "add":
                payload = await async_add_repo_sub_issue(
                    parent_slug,
                    clean_parent_number,
                    child_slug,
                    clean_child_number,
                    token=str(candidate["token"]),
                )
            else:
                payload = await async_remove_repo_sub_issue(
                    parent_slug,
                    clean_parent_number,
                    child_slug,
                    clean_child_number,
                    token=str(candidate["token"]),
                )
        except GitHubConnectorError as exc:
            last_error = exc
            if exc.status_code in auth_statuses and index < len(write_candidates) - 1:
                continue
            return json.dumps(_sub_issue_write_error(
                exc,
                operation=operation,
                parent_repo=parent_slug,
                child_repo=child_slug,
                token_key_name=candidate.get("key_name"),
            ))
        payload["token_secret_key_used"] = bool(candidate.get("key_name"))
        payload["token_source"] = candidate["source"]
        payload["token_key_name"] = candidate.get("key_name")
        if last_error is not None:
            payload["fallback_from_status_code"] = last_error.status_code
        return json.dumps(payload, default=str)

    if last_error is not None:
        return json.dumps(_sub_issue_write_error(
            last_error,
            operation=operation,
            parent_repo=parent_slug,
            child_repo=child_slug,
        ))
    return json.dumps({"error": "No GitHub token candidates were available", "no_write_token": True})


async def _handle_add_github_sub_issue(
    parent_repo: str | None = None,
    parent_issue_number: int | None = None,
    child_repo: str | None = None,
    child_issue_number: int | None = None,
    token_secret_key: str | None = None,
) -> str:
    """Idempotently add a native GitHub sub-issue using the App write lane."""

    return await _handle_github_sub_issue_write(
        "add",
        parent_repo=parent_repo,
        parent_issue_number=parent_issue_number,
        child_repo=child_repo,
        child_issue_number=child_issue_number,
        token_secret_key=token_secret_key,
    )


async def _handle_remove_github_sub_issue(
    parent_repo: str | None = None,
    parent_issue_number: int | None = None,
    child_repo: str | None = None,
    child_issue_number: int | None = None,
    token_secret_key: str | None = None,
) -> str:
    """Idempotently remove a native GitHub sub-issue using the App write lane."""

    return await _handle_github_sub_issue_write(
        "remove",
        parent_repo=parent_repo,
        parent_issue_number=parent_issue_number,
        child_repo=child_repo,
        child_issue_number=child_issue_number,
        token_secret_key=token_secret_key,
    )


async def _handle_list_github_sub_issues(
    action: str = "list",
    repo: str | None = None,
    issue_number: int | None = None,
    limit: int = 30,
    cursor: str | None = None,
    token_secret_key: str | None = None,
    counterpart_repo: str | None = None,
) -> str:
    """List a parent's native children or look up one issue's parent."""

    repo_slug = parse_github_repo_slug(repo or "")
    if not repo_slug:
        return json.dumps({"error": "list_github_sub_issues requires repo as owner/name or a GitHub URL"})
    clean_issue_number = _positive_int(issue_number)
    if clean_issue_number is None:
        return json.dumps({"error": "list_github_sub_issues requires a positive issue_number"})
    clean_action = str(action or "list").strip().lower()
    if clean_action not in {"list", "get_parent"}:
        return json.dumps({"error": "list_github_sub_issues action must be list or get_parent"})
    if clean_action == "get_parent" and _clean(cursor):
        return json.dumps({"error": "list_github_sub_issues cursor is only valid for action=list"})

    counterpart_slug = None
    if _clean(counterpart_repo):
        counterpart_slug = parse_github_repo_slug(counterpart_repo or "")
        if not counterpart_slug:
            return json.dumps({
                "error": "list_github_sub_issues counterpart_repo must be owner/name or a GitHub URL"
            })
        if counterpart_slug.split("/", 1)[0].lower() != repo_slug.split("/", 1)[0].lower():
            return json.dumps({
                "error": "GitHub sub-issue counterpart_repo must have the same repository owner",
                "status_code": 422,
            })
    repo_slugs = list(dict.fromkeys([repo_slug, counterpart_slug] if counterpart_slug else [repo_slug]))
    cross_repo_read = (
        counterpart_slug is not None
        and counterpart_slug.lower() != repo_slug.lower()
    )
    candidates = await _github_token_candidates(
        repo_slug=repo_slug,
        repo_slugs=repo_slugs if counterpart_slug else None,
        token_secret_key=token_secret_key,
    )
    read_state = _github_read_state()
    read_candidates = _ordered_read_candidates(candidates, repo_slug=repo_slug, state=read_state)
    if not read_candidates:
        cached_errors = [
            error
            for (token, rejected_repo), error in read_state.rejected.items()
            if rejected_repo == repo_slug
            and any(candidate.get("token") == token for candidate in candidates)
        ]
        if cached_errors:
            cached_error = cached_errors[-1]
            return json.dumps({
                "error": cached_error.message,
                "status_code": cached_error.status_code,
                "attempted_token_sources": [candidate["source"] for candidate in candidates],
            })
        return json.dumps({"error": "No GitHub token candidates were available"})
    attempted_token_sources: list[str] = []
    last_error: GitHubConnectorError | None = None
    unverified_payload: dict[str, Any] | None = None
    for index, candidate in enumerate(read_candidates):
        candidate_source = str(candidate["source"])
        attempted_token_sources.append(candidate_source)
        raw_response_auth_source = (
            candidate_source
            if cross_repo_read and candidate_source.startswith("project_binding:")
            else None
        )
        try:
            if clean_action == "list":
                list_kwargs: dict[str, Any] = {
                    "token": candidate.get("token"),
                    "limit": _limit(limit),
                    "cursor": _clean(cursor),
                }
                if raw_response_auth_source:
                    list_kwargs["raw_response_auth_source"] = raw_response_auth_source
                payload = await async_list_repo_sub_issues(
                    repo_slug,
                    clean_issue_number,
                    **list_kwargs,
                )
            else:
                parent_kwargs: dict[str, Any] = {"token": candidate.get("token")}
                if raw_response_auth_source:
                    parent_kwargs["raw_response_auth_source"] = raw_response_auth_source
                payload = await async_get_repo_issue_parent(
                    repo_slug,
                    clean_issue_number,
                    **parent_kwargs,
                )
        except InvalidPageToken as exc:
            return json.dumps({"error": str(exc)})
        except GitHubConnectorError as exc:
            last_error = exc
            if exc.status_code == 401 or (
                exc.status_code == 404 and clean_action != "get_parent"
            ):
                read_state.rejected[(candidate.get("token"), repo_slug)] = exc
            retry_statuses = {401, 403, 404, 502}
            if index < len(read_candidates) - 1 and (
                exc.status_code in retry_statuses or unverified_payload is not None
            ):
                continue
            if unverified_payload is not None:
                unverified_payload["attempted_token_sources"] = list(
                    attempted_token_sources
                )
                unverified_payload["last_candidate_error"] = {
                    "source": candidate["source"],
                    "status_code": exc.status_code,
                    "error": exc.message,
                }
                return json.dumps(unverified_payload, default=str)
            return json.dumps({
                "error": exc.message,
                "status_code": exc.status_code,
                "attempted_token_sources": attempted_token_sources,
            })
        if clean_action == "get_parent" and payload.get("verified") is False:
            payload["token_secret_key_used"] = bool(candidate.get("key_name"))
            payload["token_source"] = candidate["source"]
            payload["attempted_token_sources"] = list(attempted_token_sources)
            unverified_payload = payload
            if index < len(read_candidates) - 1:
                continue
            return json.dumps(unverified_payload, default=str)
        read_state.preferred[repo_slug] = candidate.get("token")
        payload["token_secret_key_used"] = bool(candidate.get("key_name"))
        payload["token_source"] = candidate["source"]
        if unverified_payload is not None:
            payload["attempted_token_sources"] = list(attempted_token_sources)
        if last_error is not None:
            payload["fallback_from_status_code"] = last_error.status_code
        return json.dumps(payload, default=str)

    if last_error is not None:
        return json.dumps({
            "error": last_error.message,
            "status_code": last_error.status_code,
            "attempted_token_sources": attempted_token_sources,
        })
    return json.dumps({"error": "No GitHub token candidates were available"})


async def _maybe_ensure_deploy_fields(repo_slug: str) -> None:
    """Lazily provision runtime fields when this env-gated feature is armed."""
    from brain.systems.deploy_tracker import ensure_deploy_state_fields

    if not deploy_feature_enabled(repo_slug):
        return
    org_id = _clean(getattr(_agent_context, "org_id", None))
    if not org_id:
        return
    import logging

    try:
        from brain.platform.db.repositories.unit_of_work import UnitOfWork

        async with UnitOfWork() as uow:
            summary = await ensure_deploy_state_fields(uow.session, org_id=org_id)
        if not summary.get("object_types"):
            # Zero matches is a schema/config mismatch (e.g. the tracker's
            # object key differs) — the failure mode a silent pass hides.
            logging.getLogger("illo.deploy_state").warning(
                "ensure_deploy_state_fields matched no ticket object types for org %s",
                org_id,
            )
    except Exception:
        # Schema bootstrap is best-effort for this read tool; GitHub facts remain
        # useful even if the domain database is temporarily unavailable.
        logging.getLogger("illo.deploy_state").warning(
            "deploy-state field bootstrap failed; continuing with GitHub facts only",
            exc_info=True,
        )
        return


def _parse_github_datetime(value: Any) -> datetime | None:
    if not isinstance(value, datetime | str):
        return None
    return as_utc_datetime(value)


def _indeterminate_deploy_result(repo_slug: str, *, error: GitHubConnectorError | None = None) -> str:
    action = classify_refire(
        deploy_state=None,
        ticket_status="Todo",
        deployed_at=None,
        now=datetime.now(timezone.utc),
        settle=deploy_settle_window(),
    )
    payload: dict[str, Any] = {
        "repo": repo_slug,
        "merged": None,
        "base_ref": None,
        "in_staging": None,
        "in_main": None,
        "deploy_state": None,
        "recommended_action": action.value,
        "indeterminate": True,
    }
    if error is not None:
        payload.update({"error": error.message, "status_code": error.status_code})
    return json.dumps(payload)


async def _handle_check_fix_deploy_state(
    repo: str | None = None,
    pr_number: int | None = None,
    sha: str | None = None,
    token_secret_key: str | None = None,
) -> str:
    """Return the ancestry-derived deploy state for a PR or commit SHA."""
    repo_slug = parse_github_repo_slug(repo or "")
    if not repo_slug:
        return json.dumps({"error": "check_fix_deploy_state requires repo as owner/name or a GitHub URL"})
    if not deploy_feature_enabled(repo_slug):
        return json.dumps({
            "error": "deploy-state checks are disabled for this repository",
            "repo": repo_slug,
            "disabled": True,
        })
    clean_sha = _clean(sha)
    try:
        clean_pr = int(pr_number) if pr_number is not None else None
    except (TypeError, ValueError):
        clean_pr = None
    if (clean_pr is None) == (clean_sha is None):
        return json.dumps({"error": "check_fix_deploy_state requires exactly one of pr_number or sha"})
    if clean_pr is not None and clean_pr < 1:
        return json.dumps({"error": "check_fix_deploy_state requires a positive pr_number"})

    await _maybe_ensure_deploy_fields(repo_slug)
    candidates = await _github_token_candidates(
        repo_slug=repo_slug,
        token_secret_key=token_secret_key,
    )
    read_state = _github_read_state()
    read_candidates = _ordered_read_candidates(candidates, repo_slug=repo_slug, state=read_state)
    last_error: GitHubConnectorError | None = None
    for index, candidate in enumerate(read_candidates):
        token = candidate.get("token")
        merged: bool | None = None
        base_ref: str | None = None
        merged_at: datetime | None = None
        candidate_sha = clean_sha
        if clean_pr is not None:
            try:
                info = await async_get_pull_request_deploy_info(
                    repo_slug,
                    clean_pr,
                    token=token,
                )
            except GitHubConnectorError as exc:
                last_error = exc
                if exc.status_code in {401, 404}:
                    read_state.rejected[(token, repo_slug)] = exc
                if exc.status_code in {401, 403, 404} and index < len(read_candidates) - 1:
                    continue
                return _indeterminate_deploy_result(repo_slug, error=exc)
            pr = info.get("pull_request") or {}
            merged = pr.get("merged") if isinstance(pr.get("merged"), bool) else None
            base_ref = _clean((pr.get("base") or {}).get("ref"))
            merged_at = _parse_github_datetime(pr.get("merged_at"))
            head_sha = _clean((pr.get("head") or {}).get("sha"))
            candidate_sha = (
                _clean(pr.get("merge_commit_sha")) or head_sha
                if merged is True
                else head_sha
            )
        if not candidate_sha:
            return _indeterminate_deploy_result(repo_slug)

        in_staging = await is_ancestor_of(repo_slug, candidate_sha, "staging", token=token)
        in_main = await is_ancestor_of(repo_slug, candidate_sha, "main", token=token)
        if (in_staging is None or in_main is None) and index < len(read_candidates) - 1:
            continue
        state = derive_deploy_state(
            merged=merged,
            base_ref=base_ref,
            in_staging=in_staging,
            in_main=in_main,
        )
        observed_at = datetime.now(timezone.utc)
        # A staging PR's merged_at is not the production deploy time, and a
        # raw SHA has no deploy timestamp. Treat a newly observed main ancestor
        # as inside settle rather than falsely escalating an unknown timeline.
        classified_deployed_at = None
        if state is DeployState.DEPLOYED:
            classified_deployed_at = (
                merged_at if str(base_ref or "").casefold() == "main" else observed_at
            )
        action = classify_refire(
            deploy_state=state,
            ticket_status="Todo",
            deployed_at=classified_deployed_at,
            now=observed_at,
            settle=deploy_settle_window(),
        )
        read_state.preferred[repo_slug] = token
        payload = {
            "repo": repo_slug,
            "merged": merged,
            "base_ref": base_ref,
            "in_staging": in_staging,
            "in_main": in_main,
            "deploy_state": state.value if state else None,
            "recommended_action": action.value,
            "indeterminate": in_staging is None or in_main is None,
            "token_secret_key_used": bool(candidate.get("key_name")),
            "token_source": candidate.get("source"),
        }
        if last_error is not None:
            payload["fallback_from_status_code"] = last_error.status_code
        return json.dumps(payload)
    return _indeterminate_deploy_result(repo_slug, error=last_error)


async def github_read_ref_for_backend(
    *,
    repo_slug: str,
    number: int,
    org_id: str,
    user_id: str | None = None,
) -> dict[str, Any] | None:
    """Read one issue/PR for BACKEND callers (e.g. dossier gathering).

    Reuses the handler-owned token candidates with EXPLICIT org context (no
    run ``_agent_context`` exists on backend paths) and the ordered-read
    preference. Returns a FLAT contract — ``{kind, title, body, state,
    body_total_chars, checks?}`` — or ``None`` when every candidate saw a
    genuine 404 on both the PR and exact-issue endpoints. Auth/permission
    errors fall through to the next candidate; other errors propagate.

    NOTE: candidate resolution may write vault access-audit rows — the one
    sanctioned write beneath the gather path's read-only stance (it belongs
    to the auth owner, not to gather; see the handoff-packets spec).
    """
    from brain.systems.cortex.project_context.github import (
        async_get_issue,
        async_get_pull_request,
    )

    candidates = await _github_token_candidates(
        repo_slug=repo_slug, token_secret_key=None, org_id=org_id, user_id=user_id
    )
    read_candidates = _ordered_read_candidates(
        candidates, repo_slug=repo_slug, state=_github_read_state()
    )
    if not read_candidates:
        raise GitHubConnectorError(status_code=401, message="No GitHub token candidates were available")

    clean_number = int(number)
    last_error: GitHubConnectorError | None = None
    saw_not_found = False
    for candidate in read_candidates:
        token = candidate.get("token")
        try:
            wrapper = await async_get_pull_request(repo_slug, clean_number, token=token)
            detail = dict(wrapper.get("pull_request") or {})
            return {
                "kind": "github_pr",
                "title": detail.get("title"),
                "body": detail.get("body"),
                "state": detail.get("state"),
                "body_total_chars": int(wrapper.get("body_total_chars") or 0),
                "checks": wrapper.get("checks"),
            }
        except GitHubConnectorError as exc:
            if exc.status_code != 404:
                last_error = exc
                if exc.status_code in {401, 403}:
                    continue
                raise
        # PR endpoint 404 → the ref may be a plain issue; exact read, same token.
        try:
            wrapper = await async_get_issue(repo_slug, clean_number, token=token)
            issue = dict(wrapper.get("issue") or {})
            return {
                "kind": "github_issue",
                "title": issue.get("title"),
                "body": issue.get("body"),
                "state": issue.get("state"),
                "body_total_chars": int(issue.get("body_total_chars") or 0),
            }
        except GitHubConnectorError as exc:
            if exc.status_code == 404:
                saw_not_found = True
                continue
            last_error = exc
            if exc.status_code in {401, 403}:
                continue
            raise
    if saw_not_found:
        return None
    if last_error is not None:
        raise last_error
    return None


__all__ = [
    "_handle_add_github_issue_comment",
    "_handle_add_github_sub_issue",
    "_handle_check_fix_deploy_state",
    "_handle_create_github_issue",
    "_handle_create_github_pull_request",
    "_handle_list_github_sub_issues",
    "_handle_read_github_source",
    "_handle_remove_github_sub_issue",
    "_handle_update_github_issue",
    "github_read_ref_for_backend",
]
