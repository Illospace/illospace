"""Small GitHub API client for Project Context connectors."""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

import httpx

from brain.kernel.common.pagination import InvalidPageToken, decode_page_token, encode_page_token
from brain.platform.async_io import async_http_client, sync_http_client


GITHUB_API_BASE = "https://api.github.com"
GITHUB_REPO_PAGE_LIMIT = 10
GITHUB_SEARCH_LIMIT = 10
GITHUB_ITEM_LIMIT = 100


@dataclass
class GitHubConnectorError(Exception):
    status_code: int
    message: str


def parse_github_repo_slug(value: str) -> str | None:
    slug = (value or "").strip()
    if not slug:
        return None
    slug = re.sub(r"^git@github\.com:", "", slug, flags=re.IGNORECASE)
    slug = re.sub(r"^https?://github\.com/", "", slug, flags=re.IGNORECASE)
    slug = re.sub(r"^github://", "", slug, flags=re.IGNORECASE)
    slug = re.sub(r"^github\.com/", "", slug, flags=re.IGNORECASE)
    slug = re.sub(r"[?#].*$", "", slug).strip("/")
    parts = [part for part in slug.split("/") if part]
    if len(parts) < 2:
        return None
    owner = parts[0]
    repo = re.sub(r"\.git$", "", parts[1], flags=re.IGNORECASE)
    if not re.fullmatch(r"[A-Za-z0-9-]+", owner):
        return None
    if not re.fullmatch(r"[A-Za-z0-9._-]+", repo):
        return None
    return f"{owner}/{repo}"


def _headers(token: str | None = None) -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "illo-brain-project-context",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _github_error(response: httpx.Response) -> GitHubConnectorError:
    try:
        payload = response.json()
    except Exception:
        payload = {}
    raw_message = str(payload.get("message") or f"GitHub returned {response.status_code}")
    lowered = raw_message.lower()
    if response.status_code == 401 and "bad credentials" in lowered:
        message = "GitHub rejected this Vault token. Choose another token or save a fresh personal access token."
    elif response.status_code == 403:
        message = f"{raw_message}. Check that the token can list and read repositories."
    elif response.status_code == 404:
        message = "Repository not found or not visible to this token."
    else:
        message = raw_message
    return GitHubConnectorError(status_code=response.status_code, message=message)


def _request(client: httpx.Client, method: str, path: str, *, token: str | None = None, params: dict[str, Any] | None = None) -> Any:
    try:
        response = client.request(method, f"{GITHUB_API_BASE}{path}", headers=_headers(token), params=params)
    except httpx.HTTPError as exc:
        raise GitHubConnectorError(status_code=502, message="Could not reach GitHub.") from exc
    if not response.is_success:
        raise _github_error(response)
    return response.json()


async def _async_request(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    *,
    token: str | None = None,
    params: dict[str, Any] | None = None,
    json: Any = None,
) -> Any:
    try:
        response = await client.request(
            method,
            f"{GITHUB_API_BASE}{path}",
            headers=_headers(token),
            params=params,
            json=json,
        )
    except httpx.HTTPError as exc:
        raise GitHubConnectorError(status_code=502, message="Could not reach GitHub.") from exc
    if not response.is_success:
        raise _github_error(response)
    return response.json()


def _repo_payload(repo: dict[str, Any]) -> dict[str, Any]:
    permissions = repo.get("permissions") if isinstance(repo.get("permissions"), dict) else {}
    return {
        "full_name": repo.get("full_name") or "",
        "html_url": repo.get("html_url") or "",
        "description": repo.get("description"),
        "default_branch": repo.get("default_branch"),
        "language": repo.get("language"),
        "topics": repo.get("topics") if isinstance(repo.get("topics"), list) else [],
        "private": bool(repo.get("private")),
        "permissions": permissions,
    }


def _compact_body(value: Any, *, limit: int = 1000) -> str | None:
    text = " ".join(str(value or "").split())
    if not text:
        return None
    if len(text) <= limit:
        return text
    return text[: max(limit - 3, 0)].rstrip() + "..."


def _user_payload(user: Any) -> dict[str, Any] | None:
    if not isinstance(user, dict):
        return None
    login = user.get("login")
    if not login:
        return None
    return {
        "login": login,
        "id": user.get("id"),
        "html_url": user.get("html_url"),
    }


def _label_payloads(labels: Any) -> list[dict[str, Any]]:
    if not isinstance(labels, list):
        return []
    return [
        {
            "name": label.get("name"),
            "color": label.get("color"),
            "description": label.get("description"),
        }
        for label in labels
        if isinstance(label, dict) and label.get("name")
    ]


def _issue_payload(issue: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "pull_request" if issue.get("pull_request") else "issue",
        "id": issue.get("id"),
        "node_id": issue.get("node_id"),
        "number": issue.get("number"),
        "title": issue.get("title"),
        "state": issue.get("state"),
        "html_url": issue.get("html_url"),
        "user": _user_payload(issue.get("user")),
        "assignees": [
            user
            for user in (_user_payload(item) for item in issue.get("assignees") or [])
            if user is not None
        ],
        "labels": _label_payloads(issue.get("labels")),
        "comments": issue.get("comments"),
        "created_at": issue.get("created_at"),
        "updated_at": issue.get("updated_at"),
        "closed_at": issue.get("closed_at"),
        "body": _compact_body(issue.get("body")),
    }


def _pull_request_payload(pr: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "pull_request",
        "id": pr.get("id"),
        "node_id": pr.get("node_id"),
        "number": pr.get("number"),
        "title": pr.get("title"),
        "state": pr.get("state"),
        "draft": bool(pr.get("draft")),
        "html_url": pr.get("html_url"),
        "user": _user_payload(pr.get("user")),
        "assignees": [
            user
            for user in (_user_payload(item) for item in pr.get("assignees") or [])
            if user is not None
        ],
        "requested_reviewers": [
            user
            for user in (_user_payload(item) for item in pr.get("requested_reviewers") or [])
            if user is not None
        ],
        "labels": _label_payloads(pr.get("labels")),
        "head": {
            "ref": (pr.get("head") or {}).get("ref") if isinstance(pr.get("head"), dict) else None,
            "sha": (pr.get("head") or {}).get("sha") if isinstance(pr.get("head"), dict) else None,
        },
        "base": {
            "ref": (pr.get("base") or {}).get("ref") if isinstance(pr.get("base"), dict) else None,
            "sha": (pr.get("base") or {}).get("sha") if isinstance(pr.get("base"), dict) else None,
        },
        "created_at": pr.get("created_at"),
        "updated_at": pr.get("updated_at"),
        "closed_at": pr.get("closed_at"),
        "merged_at": pr.get("merged_at"),
        "body": _compact_body(pr.get("body")),
    }


def _pull_request_detail_payload(pr: dict[str, Any]) -> dict[str, Any]:
    payload = _pull_request_payload(pr)
    payload.update({
        "mergeable": pr.get("mergeable"),
        "mergeable_state": pr.get("mergeable_state"),
        "merged": bool(pr.get("merged")),
        "merge_commit_sha": pr.get("merge_commit_sha"),
        "commits": pr.get("commits"),
        "changed_files": pr.get("changed_files"),
        "additions": pr.get("additions"),
        "deletions": pr.get("deletions"),
    })
    return payload


def _check_runs_payload(data: Any) -> dict[str, Any]:
    raw_runs = data.get("check_runs") if isinstance(data, dict) else []
    runs = [run for run in raw_runs if isinstance(run, dict)] if isinstance(raw_runs, list) else []
    check_runs = [
        {
            "name": run.get("name"),
            "status": run.get("status"),
            "conclusion": run.get("conclusion"),
            "details_url": run.get("details_url"),
            "started_at": run.get("started_at"),
            "completed_at": run.get("completed_at"),
        }
        for run in runs
    ]
    failure_conclusions = {
        "action_required",
        "cancelled",
        "failure",
        "stale",
        "startup_failure",
        "timed_out",
    }
    if not check_runs:
        status = "none"
    elif any(run.get("status") != "completed" for run in check_runs):
        status = "pending"
    elif any(run.get("conclusion") in failure_conclusions for run in check_runs):
        status = "failure"
    elif all(run.get("conclusion") in {"success", "neutral", "skipped"} for run in check_runs):
        status = "success"
    else:
        status = "unknown"
    total_count = data.get("total_count") if isinstance(data, dict) else None
    success_conclusions = {"success", "neutral", "skipped"}
    success = sum(
        run.get("status") == "completed" and run.get("conclusion") in success_conclusions
        for run in check_runs
    )
    failure = sum(
        run.get("status") == "completed" and run.get("conclusion") in failure_conclusions
        for run in check_runs
    )
    pending = len(check_runs) - success - failure
    return {
        "status": status,
        "total_count": int(total_count) if isinstance(total_count, int) else len(check_runs),
        "total": len(check_runs),
        "success": success,
        "failure": failure,
        "pending": pending,
        "check_runs": check_runs,
    }


def _merge_repos(*groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    merged: list[dict[str, Any]] = []
    for group in groups:
        for repo in group:
            payload = _repo_payload(repo)
            name = payload.get("full_name")
            if not name or name in seen:
                continue
            seen.add(name)
            merged.append(payload)
    return merged


def connect_with_token(token: str) -> dict[str, Any]:
    with sync_http_client(timeout=httpx.Timeout(12.0, connect=5.0)) as client:
        user = _request(client, "GET", "/user", token=token)
        repos: list[dict[str, Any]] = []
        for page in range(1, GITHUB_REPO_PAGE_LIMIT + 1):
            data = _request(
                client,
                "GET",
                "/user/repos",
                token=token,
                params={
                    "per_page": 100,
                    "page": page,
                    "visibility": "all",
                    "sort": "updated",
                    "affiliation": "owner,collaborator,organization_member",
                },
            )
            page_repos = data if isinstance(data, list) else []
            repos.extend(page_repos)
            if len(page_repos) < 100:
                break
        return {
            "login": user.get("login") if isinstance(user, dict) else None,
            "repos": _merge_repos(repos),
        }


async def async_connect_with_token(token: str) -> dict[str, Any]:
    async with async_http_client(timeout=httpx.Timeout(12.0, connect=5.0)) as client:
        user = await _async_request(client, "GET", "/user", token=token)
        repos: list[dict[str, Any]] = []
        for page in range(1, GITHUB_REPO_PAGE_LIMIT + 1):
            data = await _async_request(
                client,
                "GET",
                "/user/repos",
                token=token,
                params={
                    "per_page": 100,
                    "page": page,
                    "visibility": "all",
                    "sort": "updated",
                    "affiliation": "owner,collaborator,organization_member",
                },
            )
            page_repos = data if isinstance(data, list) else []
            repos.extend(page_repos)
            if len(page_repos) < 100:
                break
        return {
            "login": user.get("login") if isinstance(user, dict) else None,
            "repos": _merge_repos(repos),
        }


def get_repo_by_slug(slug: str, *, token: str | None = None) -> dict[str, Any] | None:
    owner, repo = slug.split("/", 1)
    with sync_http_client(timeout=httpx.Timeout(12.0, connect=5.0)) as client:
        try:
            payload = _request(
                client,
                "GET",
                f"/repos/{owner}/{repo}",
                token=token,
            )
        except GitHubConnectorError as exc:
            if exc.status_code == 404:
                return None
            raise
    return _repo_payload(payload)


async def async_get_repo_by_slug(slug: str, *, token: str | None = None) -> dict[str, Any] | None:
    owner, repo = slug.split("/", 1)
    async with async_http_client(timeout=httpx.Timeout(12.0, connect=5.0)) as client:
        try:
            payload = await _async_request(
                client,
                "GET",
                f"/repos/{owner}/{repo}",
                token=token,
            )
        except GitHubConnectorError as exc:
            if exc.status_code == 404:
                return None
            raise
    return _repo_payload(payload)


async def async_list_repo_issues(
    slug: str,
    *,
    token: str | None = None,
    state: str = "open",
    labels: list[str] | None = None,
    assignee: str | None = None,
    creator: str | None = None,
    mentioned: str | None = None,
    since: str | None = None,
    include_pull_requests: bool = False,
    limit: int = 30,
    cursor: str | None = None,
) -> dict[str, Any]:
    owner, repo = slug.split("/", 1)
    max_items = max(1, min(int(limit or 30), GITHUB_ITEM_LIMIT))
    page_kind = f"github_issues:{slug}"
    position = decode_page_token(cursor, kind=page_kind)
    try:
        page = int(position.get("page", 1))
        page_index = int(position.get("index", 0))
    except (TypeError, ValueError) as exc:
        raise InvalidPageToken("Invalid pagination cursor") from exc
    if page < 1 or page_index < 0:
        raise InvalidPageToken("Invalid pagination cursor")
    params: dict[str, Any] = {
        "state": state or "open",
        "per_page": GITHUB_ITEM_LIMIT,
        "page": page,
        "sort": "updated",
        "direction": "desc",
    }
    if labels:
        params["labels"] = ",".join([str(label).strip() for label in labels if str(label).strip()])
    if assignee:
        params["assignee"] = assignee
    if creator:
        params["creator"] = creator
    if mentioned:
        params["mentioned"] = mentioned
    if since:
        params["since"] = since
    async with async_http_client(timeout=httpx.Timeout(12.0, connect=5.0)) as client:
        data = await _async_request(
            client,
            "GET",
            f"/repos/{owner}/{repo}/issues",
            token=token,
            params=params,
        )
    raw_items = data if isinstance(data, list) else []
    items = raw_items
    if not include_pull_requests:
        items = [item for item in items if not (isinstance(item, dict) and item.get("pull_request"))]
    selected = items[page_index : page_index + max_items]
    next_position = None
    if page_index + max_items < len(items):
        next_position = {"page": page, "index": page_index + max_items}
    elif len(raw_items) == GITHUB_ITEM_LIMIT:
        next_position = {"page": page + 1, "index": 0}
    return {
        "repo": slug,
        "state": params["state"],
        "issues": [_issue_payload(item) for item in selected if isinstance(item, dict)],
        "included_pull_requests": include_pull_requests,
        "truncated": next_position is not None,
        "next_page": encode_page_token(page_kind, next_position) if next_position else None,
        "evidence_health": {
            "status": "ok",
            "completeness": "more_available" if next_position else "complete",
        },
    }


async def async_list_repo_pull_requests(
    slug: str,
    *,
    token: str | None = None,
    state: str = "open",
    head: str | None = None,
    base: str | None = None,
    limit: int = 30,
    cursor: str | None = None,
) -> dict[str, Any]:
    owner, repo = slug.split("/", 1)
    max_items = max(1, min(int(limit or 30), GITHUB_ITEM_LIMIT))
    page_kind = f"github_pull_requests:{slug}"
    position = decode_page_token(cursor, kind=page_kind)
    try:
        page = int(position.get("page", 1))
        page_index = int(position.get("index", 0))
    except (TypeError, ValueError) as exc:
        raise InvalidPageToken("Invalid pagination cursor") from exc
    if page < 1 or page_index < 0:
        raise InvalidPageToken("Invalid pagination cursor")
    params: dict[str, Any] = {
        "state": state or "open",
        "per_page": GITHUB_ITEM_LIMIT,
        "page": page,
        "sort": "updated",
        "direction": "desc",
    }
    if head:
        params["head"] = head
    if base:
        params["base"] = base
    async with async_http_client(timeout=httpx.Timeout(12.0, connect=5.0)) as client:
        data = await _async_request(
            client,
            "GET",
            f"/repos/{owner}/{repo}/pulls",
            token=token,
            params=params,
        )
    items = data if isinstance(data, list) else []
    selected = items[page_index : page_index + max_items]
    next_position = None
    if page_index + max_items < len(items):
        next_position = {"page": page, "index": page_index + max_items}
    elif len(items) == GITHUB_ITEM_LIMIT:
        next_position = {"page": page + 1, "index": 0}
    return {
        "repo": slug,
        "state": params["state"],
        "pull_requests": [_pull_request_payload(item) for item in selected if isinstance(item, dict)],
        "truncated": next_position is not None,
        "next_page": encode_page_token(page_kind, next_position) if next_position else None,
        "evidence_health": {
            "status": "ok",
            "completeness": "more_available" if next_position else "complete",
        },
    }


async def async_get_pull_request(
    slug: str,
    pull_number: int,
    *,
    token: str | None = None,
) -> dict[str, Any]:
    """Read one pull request and its head commit's CI state with one identity."""

    owner, repo = slug.split("/", 1)
    async with async_http_client(timeout=httpx.Timeout(12.0, connect=5.0)) as client:
        pr = await _async_request(
            client,
            "GET",
            f"/repos/{owner}/{repo}/pulls/{pull_number}",
            token=token,
        )
        head = pr.get("head") if isinstance(pr, dict) else None
        head_sha = head.get("sha") if isinstance(head, dict) else None
        if not head_sha:
            raise GitHubConnectorError(status_code=502, message="GitHub pull request response omitted the head SHA.")
        ci = await _async_get_pull_request_checks(client, slug, head_sha, token=token)
    return {
        "repo": slug,
        "pull_request": _pull_request_detail_payload(pr if isinstance(pr, dict) else {}),
        "checks": ci["checks"],
        "combined_status": ci["combined_status"],
        # True normalized body length BEFORE _compact_body's 1000-char cap, so
        # downstream honesty accounting (handoff packets) can report what
        # compaction removed.
        "body_total_chars": len(" ".join(str((pr or {}).get("body") or "").split()))
        if isinstance(pr, dict)
        else 0,
    }


async def _async_get_pull_request_checks(
    client: httpx.AsyncClient,
    slug: str,
    sha: str,
    *,
    token: str | None,
) -> dict[str, Any]:
    owner, repo = slug.split("/", 1)
    checks = await _async_request(
        client,
        "GET",
        f"/repos/{owner}/{repo}/commits/{sha}/check-runs",
        token=token,
        params={"per_page": GITHUB_ITEM_LIMIT, "page": 1},
    )
    total_checks = checks.get("total_count") if isinstance(checks, dict) else None
    check_runs = checks.get("check_runs") if isinstance(checks, dict) else None
    if isinstance(total_checks, int) and isinstance(check_runs, list):
        page_count = (total_checks + GITHUB_ITEM_LIMIT - 1) // GITHUB_ITEM_LIMIT
        for page in range(2, page_count + 1):
            page_data = await _async_request(
                client,
                "GET",
                f"/repos/{owner}/{repo}/commits/{sha}/check-runs",
                token=token,
                params={"per_page": GITHUB_ITEM_LIMIT, "page": page},
            )
            page_runs = page_data.get("check_runs") if isinstance(page_data, dict) else None
            if isinstance(page_runs, list):
                check_runs.extend(page_runs)
    combined = await _async_request(
        client,
        "GET",
        f"/repos/{owner}/{repo}/commits/{sha}/status",
        token=token,
    )
    return {
        "repo": slug,
        "sha": sha,
        "checks": _check_runs_payload(checks),
        "combined_status": combined.get("state") if isinstance(combined, dict) else None,
    }


async def async_get_pull_request_checks(
    slug: str,
    sha: str,
    *,
    token: str | None = None,
) -> dict[str, Any]:
    """Read check runs and the combined commit status for a PR head SHA."""

    async with async_http_client(timeout=httpx.Timeout(12.0, connect=5.0)) as client:
        return await _async_get_pull_request_checks(client, slug, sha, token=token)


async def async_get_issue(
    slug: str,
    issue_number: int,
    *,
    token: str | None = None,
) -> dict[str, Any]:
    """Read ONE issue by exact number — no listing window, no recency limit."""
    owner, repo = slug.split("/", 1)
    async with async_http_client(timeout=httpx.Timeout(12.0, connect=5.0)) as client:
        issue = await _async_request(
            client,
            "GET",
            f"/repos/{owner}/{repo}/issues/{issue_number}",
            token=token,
        )
    payload = _issue_payload(issue if isinstance(issue, dict) else {})
    payload["body_total_chars"] = (
        len(" ".join(str((issue or {}).get("body") or "").split())) if isinstance(issue, dict) else 0
    )
    return {"repo": slug, "issue": payload}


async def async_get_pull_request_deploy_info(
    slug: str,
    pull_number: int,
    *,
    token: str | None = None,
) -> dict[str, Any]:
    """Read only the PR facts needed for deploy-state ancestry checks."""
    owner, repo = slug.split("/", 1)
    async with async_http_client(timeout=httpx.Timeout(12.0, connect=5.0)) as client:
        pr = await _async_request(
            client,
            "GET",
            f"/repos/{owner}/{repo}/pulls/{pull_number}",
            token=token,
        )
    detail = _pull_request_detail_payload(pr if isinstance(pr, dict) else {})
    return {
        "repo": slug,
        "pull_request": detail,
    }


async def async_compare_commits(
    slug: str,
    base: str,
    head: str,
    *,
    token: str | None = None,
) -> str:
    """Return GitHub's compare status for ``base...head``."""
    owner, repo = slug.split("/", 1)
    async with async_http_client(timeout=httpx.Timeout(12.0, connect=5.0)) as client:
        payload = await _async_request(
            client,
            "GET",
            f"/repos/{owner}/{repo}/compare/{base}...{head}",
            token=token,
        )
    status = payload.get("status") if isinstance(payload, dict) else None
    if status not in {"identical", "behind", "ahead", "diverged"}:
        raise GitHubConnectorError(
            status_code=502,
            message="GitHub compare response omitted a recognized status.",
        )
    return str(status)


async def async_get_repo_counts(
    slug: str,
    *,
    token: str | None = None,
    state: str = "open",
) -> dict[str, Any]:
    """Return exact issue and pull-request counts from authenticated GitHub Search."""

    clean_state = (state or "open").strip().lower()
    if clean_state not in {"open", "closed", "all"}:
        raise GitHubConnectorError(status_code=422, message="Count state must be open, closed, or all.")
    state_qualifier = "" if clean_state == "all" else f" is:{clean_state}"
    counts: dict[str, int] = {}
    async with async_http_client(timeout=httpx.Timeout(12.0, connect=5.0)) as client:
        for result_key, kind in (("issues", "issue"), ("pull_requests", "pr")):
            data = await _async_request(
                client,
                "GET",
                "/search/issues",
                token=token,
                params={
                    "q": f"repo:{slug} is:{kind}{state_qualifier}",
                    "per_page": 1,
                },
            )
            raw_count = data.get("total_count") if isinstance(data, dict) else None
            if not isinstance(raw_count, int):
                raise GitHubConnectorError(
                    status_code=502,
                    message="GitHub search response omitted total_count.",
                )
            counts[result_key] = raw_count
    return {
        "repo": slug,
        "state": clean_state,
        "counts": counts,
        "total_count": sum(counts.values()),
    }


async def async_create_repo_issue(
    slug: str,
    *,
    title: str,
    body: str | None = None,
    labels: list[str] | None = None,
    assignees: list[str] | None = None,
    token: str | None = None,
) -> dict[str, Any]:
    """Open a real GitHub issue via POST /repos/{owner}/{repo}/issues.

    Raises GitHubConnectorError on any non-success (401/403/404 auth/visibility,
    422 bad label/assignee, 502 unreachable). The caller decides how to degrade.
    """

    owner, repo = slug.split("/", 1)
    clean_title = (title or "").strip()
    if not clean_title:
        raise GitHubConnectorError(status_code=422, message="Issue title is required.")
    payload: dict[str, Any] = {"title": clean_title}
    clean_body = (body or "").strip()
    if clean_body:
        payload["body"] = clean_body
    clean_labels = [str(label).strip() for label in (labels or []) if str(label).strip()]
    if clean_labels:
        payload["labels"] = clean_labels
    clean_assignees = [str(assignee).strip() for assignee in (assignees or []) if str(assignee).strip()]
    if clean_assignees:
        payload["assignees"] = clean_assignees
    async with async_http_client(timeout=httpx.Timeout(12.0, connect=5.0)) as client:
        created = await _async_request(
            client,
            "POST",
            f"/repos/{owner}/{repo}/issues",
            token=token,
            json=payload,
        )
    return {
        "repo": slug,
        "issue": _issue_payload(created if isinstance(created, dict) else {}),
    }


def search_repos(query: str, *, token: str | None = None) -> dict[str, Any]:
    trimmed = (query or "").strip()
    if not trimmed:
        raise GitHubConnectorError(status_code=422, message="Search query is required.")

    slug = parse_github_repo_slug(trimmed)
    if slug:
        token_candidates = [token, None] if token else [None]
        first_error: GitHubConnectorError | None = None
        for token_candidate in token_candidates:
            try:
                repo = get_repo_by_slug(slug, token=token_candidate)
            except GitHubConnectorError as exc:
                first_error = first_error or exc
                continue
            if repo:
                return {"repos": [repo], "matched_exact": True}
        if first_error and first_error.status_code != 404:
            raise first_error
        return {"repos": [], "matched_exact": True}

    with sync_http_client(timeout=httpx.Timeout(12.0, connect=5.0)) as client:
        groups: list[list[dict[str, Any]]] = []
        errors: list[GitHubConnectorError] = []
        for token_candidate in ([token, None] if token else [None]):
            try:
                data = _request(
                    client,
                    "GET",
                    "/search/repositories",
                    token=token_candidate,
                    params={"q": trimmed, "per_page": GITHUB_SEARCH_LIMIT},
                )
                items = data.get("items") if isinstance(data, dict) else []
                groups.append(items if isinstance(items, list) else [])
            except GitHubConnectorError as exc:
                errors.append(exc)
        repos = _merge_repos(*groups)
        if not repos and errors:
            raise errors[0]
        return {"repos": repos, "matched_exact": False}


async def async_search_repos(query: str, *, token: str | None = None) -> dict[str, Any]:
    trimmed = (query or "").strip()
    if not trimmed:
        raise GitHubConnectorError(status_code=422, message="Search query is required.")

    slug = parse_github_repo_slug(trimmed)
    if slug:
        token_candidates = [token, None] if token else [None]
        first_error: GitHubConnectorError | None = None
        for token_candidate in token_candidates:
            try:
                repo = await async_get_repo_by_slug(slug, token=token_candidate)
            except GitHubConnectorError as exc:
                first_error = first_error or exc
                continue
            if repo:
                return {"repos": [repo], "matched_exact": True}
        if first_error and first_error.status_code != 404:
            raise first_error
        return {"repos": [], "matched_exact": True}

    async with async_http_client(timeout=httpx.Timeout(12.0, connect=5.0)) as client:
        groups: list[list[dict[str, Any]]] = []
        errors: list[GitHubConnectorError] = []
        for token_candidate in ([token, None] if token else [None]):
            try:
                data = await _async_request(
                    client,
                    "GET",
                    "/search/repositories",
                    token=token_candidate,
                    params={"q": trimmed, "per_page": GITHUB_SEARCH_LIMIT},
                )
                items = data.get("items") if isinstance(data, dict) else []
                groups.append(items if isinstance(items, list) else [])
            except GitHubConnectorError as exc:
                errors.append(exc)
        repos = _merge_repos(*groups)
        if not repos and errors:
            raise errors[0]
        return {"repos": repos, "matched_exact": False}
