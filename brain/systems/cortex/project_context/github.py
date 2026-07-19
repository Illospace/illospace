"""Small GitHub API client for Project Context connectors."""
from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
import hashlib
import json
import logging
import re
from typing import Any, Callable
from urllib.parse import quote

import httpx

from brain.kernel.common.pagination import InvalidPageToken, decode_page_token, encode_page_token
from brain.platform.async_io import async_http_client, sync_http_client


logger = logging.getLogger(__name__)


GITHUB_API_BASE = "https://api.github.com"
GITHUB_REPO_PAGE_LIMIT = 10
GITHUB_SEARCH_LIMIT = 10
GITHUB_ITEM_LIMIT = 100
GITHUB_SOURCE_FILE_MAX_BYTES = 1_000_000
GITHUB_SOURCE_FILE_MAX_LINES = 200
GITHUB_SOURCE_OUTPUT_CHARS = 12_000
GITHUB_SOURCE_TOOL_OUTPUT_CHARS = 18_000
GITHUB_SOURCE_HANDLER_RESERVE_CHARS = 1_000
GITHUB_SOURCE_BUDGET_SAFETY_CHARS = 512
GITHUB_GREP_MAX_FILES = 25
GITHUB_GREP_MAX_BYTES = 512_000
GITHUB_GREP_MAX_FILE_BYTES = 128_000
GITHUB_GREP_MAX_MATCHES = 50
GITHUB_GREP_MATCH_TEXT_CHARS = 300
GITHUB_SOURCE_REF_MAX_CHARS = 512
GITHUB_SOURCE_PATH_MAX_CHARS = 4096
GITHUB_GREP_QUERY_MAX_CHARS = 500

GITHUB_ISSUE_PARENT_QUERY = """
query GetIssueParent($issueId: ID!) {
  node(id: $issueId) {
    ... on Issue {
      parent {
        number
        repository {
          nameWithOwner
        }
      }
    }
  }
}
""".strip()


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


def _source_path(value: str) -> str:
    path = str(value or "").strip().strip("/")
    if (
        not path
        or len(path) > GITHUB_SOURCE_PATH_MAX_CHARS
        or any(part in {"", ".", ".."} for part in path.split("/"))
    ):
        raise GitHubConnectorError(
            status_code=422,
            message="GitHub source path must be a repository-relative file path.",
        )
    return path


def _source_prefix(value: str | None) -> str | None:
    prefix = str(value or "").strip().strip("/")
    if not prefix:
        return None
    if len(prefix) > GITHUB_SOURCE_PATH_MAX_CHARS or any(
        part in {"", ".", ".."} for part in prefix.split("/")
    ):
        raise GitHubConnectorError(
            status_code=422,
            message="GitHub source prefix must be a repository-relative directory path.",
        )
    return prefix


def _truncate_for_json_string_budget(value: str, max_chars: int) -> tuple[str, bool]:
    """Fit text to its JSON-escaped size so tool-level budgets are predictable."""

    budget = max(0, int(max_chars))

    def escaped_size(candidate: str) -> int:
        return max(0, len(json.dumps(candidate)) - 2)

    if escaped_size(value) <= budget:
        return value, False
    low = 0
    high = len(value)
    while low < high:
        middle = (low + high + 1) // 2
        if escaped_size(value[:middle]) <= budget:
            low = middle
        else:
            high = middle - 1
    return value[:low], True


def _bounded_json_items(items: list[dict[str, Any]], *, limit: int, budget_chars: int) -> list[dict[str, Any]]:
    """Take an item page that remains below a reserved model-visible JSON budget."""

    selected: list[dict[str, Any]] = []
    used = 2  # JSON list brackets.
    for item in items[: max(0, int(limit))]:
        item_size = len(json.dumps(item, default=str))
        separator_size = 2 if selected else 0
        if used + separator_size + item_size > budget_chars:
            break
        selected.append(item)
        used += separator_size + item_size
    return selected


def _source_variable_output_budget(payload_without_items: dict[str, Any]) -> int:
    """Reserve room for handler provenance while budgeting model-visible evidence."""

    fixed_chars = len(json.dumps(payload_without_items, default=str))
    return max(
        0,
        min(
            GITHUB_SOURCE_OUTPUT_CHARS,
            GITHUB_SOURCE_TOOL_OUTPUT_CHARS
            - GITHUB_SOURCE_HANDLER_RESERVE_CHARS
            - GITHUB_SOURCE_BUDGET_SAFETY_CHARS
            - fixed_chars,
        ),
    )


def _bounded_source_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Fail with valid compact JSON instead of letting runtime truncate the result."""

    connector_budget = GITHUB_SOURCE_TOOL_OUTPUT_CHARS - GITHUB_SOURCE_HANDLER_RESERVE_CHARS
    if len(json.dumps(payload, default=str)) > connector_budget:
        raise GitHubConnectorError(
            status_code=413,
            message="GitHub source evidence metadata exceeds the tool output budget.",
        )
    return payload


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
    response_observer: Callable[[httpx.Response, Any], None] | None = None,
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
    payload = response.json()
    if response_observer is not None:
        response_observer(response, payload)
    return payload


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


def _issue_comment_payload(comment: dict[str, Any]) -> dict[str, Any]:
    raw_body = str(comment.get("body") or "")
    bounded_body, body_truncated = _truncate_for_json_string_budget(raw_body, 3_997)
    if body_truncated:
        bounded_body = bounded_body.rstrip() + "..."
    return {
        "id": comment.get("id"),
        "node_id": comment.get("node_id"),
        "html_url": comment.get("html_url"),
        "body": bounded_body,
        "body_total_chars": len(raw_body),
        "body_truncated": body_truncated,
        "user": _user_payload(comment.get("user")),
        "created_at": comment.get("created_at"),
        "updated_at": comment.get("updated_at"),
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


async def _async_resolve_repo_ref(
    client: httpx.AsyncClient,
    slug: str,
    ref: str,
    *,
    token: str | None,
) -> dict[str, str]:
    """Resolve a caller ref once so every source read names its commit snapshot."""

    owner, repo = slug.split("/", 1)
    clean_ref = str(ref or "").strip()
    if not clean_ref:
        raise GitHubConnectorError(status_code=422, message="GitHub source reads require an explicit ref.")
    if len(clean_ref) > GITHUB_SOURCE_REF_MAX_CHARS:
        raise GitHubConnectorError(status_code=422, message="GitHub source ref is too long.")
    payload = await _async_request(
        client,
        "GET",
        f"/repos/{owner}/{repo}/commits/{quote(clean_ref, safe='')}",
        token=token,
    )
    commit_sha = payload.get("sha") if isinstance(payload, dict) else None
    commit = payload.get("commit") if isinstance(payload, dict) else None
    tree = commit.get("tree") if isinstance(commit, dict) else None
    tree_sha = tree.get("sha") if isinstance(tree, dict) else None
    if not isinstance(commit_sha, str) or not commit_sha.strip():
        raise GitHubConnectorError(status_code=502, message="GitHub commit response omitted the resolved SHA.")
    return {
        "requested_ref": clean_ref,
        "resolved_ref": commit_sha.strip(),
        "tree_sha": str(tree_sha or "").strip(),
    }


async def async_get_repo_file(
    slug: str,
    path: str,
    *,
    ref: str,
    token: str | None = None,
    line_start: int = 1,
    line_end: int | None = None,
) -> dict[str, Any]:
    """Read one bounded, line-numbered text window at an explicitly resolved ref."""

    owner, repo = slug.split("/", 1)
    clean_path = _source_path(path)
    start = max(1, int(line_start or 1))
    requested_end = int(line_end) if line_end is not None else None
    if requested_end is not None and requested_end < start:
        raise GitHubConnectorError(status_code=422, message="line_end must be greater than or equal to line_start.")

    async with async_http_client(timeout=httpx.Timeout(12.0, connect=5.0)) as client:
        resolved = await _async_resolve_repo_ref(client, slug, ref, token=token)
        try:
            payload = await _async_request(
                client,
                "GET",
                f"/repos/{owner}/{repo}/contents/{quote(clean_path, safe='/')}",
                token=token,
                params={"ref": resolved["resolved_ref"]},
            )
        except GitHubConnectorError as exc:
            if exc.status_code == 404:
                raise GitHubConnectorError(
                    status_code=404,
                    message=(
                        f"GitHub source path {clean_path!r} was not found at resolved ref "
                        f"{resolved['resolved_ref']}."
                    ),
                ) from exc
            raise

    if not isinstance(payload, dict) or payload.get("type") != "file":
        raise GitHubConnectorError(status_code=422, message="GitHub source path does not identify a file.")
    size = payload.get("size")
    if isinstance(size, int) and size > GITHUB_SOURCE_FILE_MAX_BYTES:
        raise GitHubConnectorError(
            status_code=413,
            message=f"GitHub source file exceeds the {GITHUB_SOURCE_FILE_MAX_BYTES}-byte read limit.",
        )
    if payload.get("encoding") != "base64" or not isinstance(payload.get("content"), str):
        raise GitHubConnectorError(status_code=415, message="GitHub source file is not available as base64 text.")
    try:
        raw = base64.b64decode(payload["content"], validate=False)
        text = raw.decode("utf-8")
    except (binascii.Error, UnicodeDecodeError) as exc:
        raise GitHubConnectorError(status_code=415, message="GitHub source file is not UTF-8 text.") from exc
    if len(raw) > GITHUB_SOURCE_FILE_MAX_BYTES:
        raise GitHubConnectorError(
            status_code=413,
            message=f"GitHub source file exceeds the {GITHUB_SOURCE_FILE_MAX_BYTES}-byte read limit.",
        )

    lines = text.splitlines()
    total_lines = len(lines)
    if start > total_lines and (total_lines > 0 or start > 1):
        raise GitHubConnectorError(
            status_code=416,
            message=f"get_file line_start {start} exceeds the file's {total_lines} lines.",
        )
    target_end = min(total_lines, requested_end or total_lines, start + GITHUB_SOURCE_FILE_MAX_LINES - 1)
    probe_end = target_end if target_end >= start else None
    probe_citation = None
    if probe_end is not None:
        probe_citation = (
            f"{clean_path}:{start}" if probe_end == start else f"{clean_path}:{start}-{probe_end}"
        )
    output_probe = {
        "repo": slug,
        **{key: value for key, value in resolved.items() if key != "tree_sha"},
        "path": clean_path,
        "blob_sha": payload.get("sha"),
        "size": len(raw),
        "total_lines": total_lines,
        "line_start": start,
        "line_end": probe_end,
        "content": "",
        "citation_range": probe_citation,
        "truncated": True,
        "next_line": start,
        "line_truncated": True,
        "evidence_health": {"status": "warning", "completeness": "line_truncated"},
    }
    content_budget = _source_variable_output_budget(output_probe)
    if content_budget <= 0 and target_end >= start:
        raise GitHubConnectorError(
            status_code=413,
            message="GitHub source evidence metadata leaves no room within the tool output budget.",
        )
    numbered: list[str] = []
    char_count = 0
    line_truncated = False
    for line_number in range(start, target_end + 1):
        rendered = f"{line_number}: {lines[line_number - 1]}"
        separator_cost = 2 if numbered else 0
        remaining = content_budget - char_count - separator_cost
        if remaining <= 0:
            break
        shown, was_truncated = _truncate_for_json_string_budget(rendered, remaining)
        if shown:
            numbered.append(shown)
            char_count += separator_cost + max(0, len(json.dumps(shown)) - 2)
        if was_truncated:
            line_truncated = bool(shown)
            break

    returned_end = start + len(numbered) - 1 if numbered else None
    capped_end = min(total_lines, requested_end or total_lines)
    has_unreturned_lines = (
        (returned_end is None and capped_end >= start)
        or (returned_end is not None and returned_end < capped_end)
    )
    truncated = line_truncated or has_unreturned_lines
    citation_range = None
    if numbered:
        citation_range = (
            f"{clean_path}:{start}" if returned_end == start else f"{clean_path}:{start}-{returned_end}"
        )
    result = {
        "repo": slug,
        **resolved,
        "path": clean_path,
        "blob_sha": payload.get("sha"),
        "size": len(raw),
        "total_lines": total_lines,
        "line_start": start,
        "line_end": returned_end,
        "content": "\n".join(numbered),
        "citation_range": citation_range,
        "truncated": truncated,
        "next_line": (
            (returned_end + 1 if returned_end is not None else start)
            if truncated and (returned_end is None or returned_end < total_lines)
            else None
        ),
        "line_truncated": line_truncated,
        "evidence_health": {
            "status": "warning" if line_truncated else "ok",
            "completeness": (
                "line_truncated" if line_truncated else "more_available" if truncated else "complete"
            ),
        },
    }
    result.pop("tree_sha", None)
    return _bounded_source_payload(result)


async def async_list_repo_tree(
    slug: str,
    *,
    ref: str,
    token: str | None = None,
    path: str | None = None,
    limit: int = 30,
    cursor: str | None = None,
) -> dict[str, Any]:
    """List a bounded page of one repository tree at a resolved commit."""

    owner, repo = slug.split("/", 1)
    prefix = _source_prefix(path)
    max_items = max(1, min(int(limit or 30), GITHUB_ITEM_LIMIT))
    async with async_http_client(timeout=httpx.Timeout(12.0, connect=5.0)) as client:
        resolved = await _async_resolve_repo_ref(client, slug, ref, token=token)
        if not resolved["tree_sha"]:
            raise GitHubConnectorError(status_code=502, message="GitHub commit response omitted the tree SHA.")
        prefix_fingerprint = hashlib.sha256(str(prefix or "").encode("utf-8")).hexdigest()[:20]
        page_kind = f"github_tree:{slug}:{resolved['resolved_ref']}:{prefix_fingerprint}"
        position = decode_page_token(cursor, kind=page_kind)
        try:
            offset = int(position.get("offset", 0))
        except (TypeError, ValueError, OverflowError) as exc:
            raise InvalidPageToken("Invalid pagination cursor") from exc
        if offset < 0:
            raise InvalidPageToken("Invalid pagination cursor")
        payload = await _async_request(
            client,
            "GET",
            f"/repos/{owner}/{repo}/git/trees/{resolved['tree_sha']}",
            token=token,
            params={"recursive": "1"},
        )

    raw_tree = payload.get("tree") if isinstance(payload, dict) else None
    if not isinstance(raw_tree, list):
        raise GitHubConnectorError(status_code=502, message="GitHub tree response omitted tree entries.")
    source_truncated = bool(payload.get("truncated")) if isinstance(payload, dict) else False
    entries = [
        {
            "path": str(item.get("path")),
            "type": item.get("type"),
            "mode": item.get("mode"),
            "sha": item.get("sha"),
            "size": item.get("size"),
        }
        for item in raw_tree
        if isinstance(item, dict)
        and item.get("path")
        and (
            prefix is None
            or str(item.get("path")) == prefix
            or str(item.get("path")).startswith(f"{prefix}/")
        )
    ]
    entries.sort(key=lambda item: (str(item["path"]), str(item.get("type") or "")))
    if prefix is not None and not entries and not source_truncated:
        raise GitHubConnectorError(
            status_code=404,
            message=f"GitHub source prefix {prefix!r} was not found at the resolved ref.",
        )
    if offset > len(entries):
        raise InvalidPageToken("Invalid pagination cursor")
    probe_next_page = (
        encode_page_token(page_kind, {"offset": min(len(entries), offset + 1)})
        if offset < len(entries)
        else None
    )
    output_probe = {
        "repo": slug,
        "requested_ref": resolved["requested_ref"],
        "resolved_ref": resolved["resolved_ref"],
        "path": prefix,
        "tree_sha": resolved["tree_sha"],
        "entries": [],
        "returned": 0,
        "truncated": True,
        "source_truncated": source_truncated,
        "next_page": probe_next_page,
        "evidence_health": {"status": "warning", "completeness": "source_truncated"},
    }
    selected = _bounded_json_items(
        entries[offset:],
        limit=max_items,
        budget_chars=_source_variable_output_budget(output_probe),
    )
    if offset < len(entries) and not selected:
        raise GitHubConnectorError(
            status_code=413,
            message="A GitHub tree entry cannot fit within the tool output budget.",
        )
    next_offset = offset + len(selected)
    has_more = next_offset < len(entries)
    next_page = encode_page_token(page_kind, {"offset": next_offset}) if has_more else None
    if source_truncated:
        evidence_health = {"status": "warning", "completeness": "source_truncated"}
    elif has_more:
        evidence_health = {"status": "ok", "completeness": "more_available"}
    else:
        evidence_health = {"status": "ok", "completeness": "complete"}
    result = {
        "repo": slug,
        "requested_ref": resolved["requested_ref"],
        "resolved_ref": resolved["resolved_ref"],
        "path": prefix,
        "tree_sha": resolved["tree_sha"],
        "entries": selected,
        "returned": len(selected),
        "truncated": has_more or source_truncated,
        "source_truncated": source_truncated,
        "next_page": next_page,
        "evidence_health": evidence_health,
    }
    return _bounded_source_payload(result)


async def async_grep_repo(
    slug: str,
    query: str,
    *,
    ref: str,
    token: str | None = None,
    path: str | None = None,
    case_sensitive: bool = False,
    limit: int = 30,
    cursor: str | None = None,
) -> dict[str, Any]:
    """Search UTF-8 blobs with bounded work and return exact path:line citations."""

    owner, repo = slug.split("/", 1)
    literal = str(query or "")
    if not literal.strip():
        raise GitHubConnectorError(status_code=422, message="GitHub source grep requires a non-empty query.")
    if len(literal) > GITHUB_GREP_QUERY_MAX_CHARS:
        raise GitHubConnectorError(status_code=422, message="GitHub source grep query is too long.")
    if "\n" in literal or "\r" in literal:
        raise GitHubConnectorError(status_code=422, message="GitHub source grep query must be a single line.")
    prefix = _source_prefix(path)
    max_matches = max(1, min(int(limit or 30), GITHUB_GREP_MAX_MATCHES))
    fingerprint = hashlib.sha256(
        f"{literal}\0{prefix or ''}\0{bool(case_sensitive)}".encode("utf-8")
    ).hexdigest()[:20]

    async with async_http_client(timeout=httpx.Timeout(12.0, connect=5.0)) as client:
        resolved = await _async_resolve_repo_ref(client, slug, ref, token=token)
        if not resolved["tree_sha"]:
            raise GitHubConnectorError(status_code=502, message="GitHub commit response omitted the tree SHA.")
        page_kind = f"github_grep:{slug}:{resolved['resolved_ref']}:{fingerprint}"
        position = decode_page_token(cursor, kind=page_kind)
        try:
            file_index = int(position.get("file_index", 0))
            first_line = int(position.get("line", 1))
            prior_skipped_binary = int(position.get("skipped_binary_files", 0))
            prior_skipped_large = int(position.get("skipped_large_files", 0))
        except (TypeError, ValueError, OverflowError) as exc:
            raise InvalidPageToken("Invalid pagination cursor") from exc
        if file_index < 0 or first_line < 1 or prior_skipped_binary < 0 or prior_skipped_large < 0:
            raise InvalidPageToken("Invalid pagination cursor")

        tree_payload = await _async_request(
            client,
            "GET",
            f"/repos/{owner}/{repo}/git/trees/{resolved['tree_sha']}",
            token=token,
            params={"recursive": "1"},
        )
        raw_tree = tree_payload.get("tree") if isinstance(tree_payload, dict) else None
        if not isinstance(raw_tree, list):
            raise GitHubConnectorError(status_code=502, message="GitHub tree response omitted tree entries.")
        source_truncated = bool(tree_payload.get("truncated")) if isinstance(tree_payload, dict) else False
        prefix_found = any(
            isinstance(item, dict)
            and item.get("path")
            and (
                str(item.get("path")) == prefix
                or str(item.get("path")).startswith(f"{prefix}/")
            )
            for item in raw_tree
        ) if prefix is not None else True
        if prefix is not None and not prefix_found and not source_truncated:
            raise GitHubConnectorError(
                status_code=404,
                message=f"GitHub source prefix {prefix!r} was not found at the resolved ref.",
            )
        blobs = [
            item
            for item in raw_tree
            if isinstance(item, dict)
            and item.get("type") == "blob"
            and item.get("path")
            and item.get("sha")
            and (
                prefix is None
                or str(item.get("path")) == prefix
                or str(item.get("path")).startswith(f"{prefix}/")
            )
        ]
        blobs.sort(key=lambda item: str(item.get("path")))
        if file_index > len(blobs):
            raise InvalidPageToken("Invalid pagination cursor")
        if prior_skipped_binary > len(blobs) or prior_skipped_large > len(blobs):
            raise InvalidPageToken("Invalid pagination cursor")

        probe_next_page = encode_page_token(page_kind, {
            "file_index": min(file_index, len(blobs)),
            "line": GITHUB_SOURCE_FILE_MAX_BYTES + 1,
            "skipped_binary_files": max(prior_skipped_binary, len(blobs)),
            "skipped_large_files": max(prior_skipped_large, len(blobs)),
        })
        output_probe = {
            "repo": slug,
            "requested_ref": resolved["requested_ref"],
            "resolved_ref": resolved["resolved_ref"],
            "path": prefix,
            "query": literal,
            "case_sensitive": bool(case_sensitive),
            "matches": [],
            "returned": max_matches,
            "examined_files": GITHUB_GREP_MAX_FILES,
            "scanned_files": GITHUB_GREP_MAX_FILES,
            "scanned_bytes": GITHUB_GREP_MAX_BYTES,
            "skipped_binary_files": max(prior_skipped_binary, len(blobs)),
            "skipped_large_files": max(prior_skipped_large, len(blobs)),
            "scan_budget": {
                "max_files": GITHUB_GREP_MAX_FILES,
                "max_bytes": GITHUB_GREP_MAX_BYTES,
                "max_file_bytes": GITHUB_GREP_MAX_FILE_BYTES,
                "max_matches": max_matches,
                "max_match_output_chars": GITHUB_SOURCE_OUTPUT_CHARS,
            },
            "truncated": True,
            "search_incomplete": True,
            "source_truncated": source_truncated,
            "next_page": probe_next_page,
            "evidence_health": {"status": "warning", "completeness": "source_truncated"},
        }
        match_output_budget = _source_variable_output_budget(output_probe)

        matches: list[dict[str, Any]] = []
        match_json_chars = 2
        examined_files = 0
        scanned_files = 0
        scanned_bytes = 0
        skipped_binary_files = prior_skipped_binary
        skipped_large_files = prior_skipped_large
        next_position: dict[str, int] | None = None
        index = file_index
        while index < len(blobs):
            if examined_files >= GITHUB_GREP_MAX_FILES:
                next_position = {"file_index": index, "line": 1}
                break
            blob = blobs[index]
            examined_files += 1
            declared_size = blob.get("size")
            if isinstance(declared_size, int) and declared_size > GITHUB_GREP_MAX_FILE_BYTES:
                skipped_large_files += 1
                index += 1
                first_line = 1
                continue
            if (
                isinstance(declared_size, int)
                and scanned_files > 0
                and scanned_bytes + declared_size > GITHUB_GREP_MAX_BYTES
            ):
                next_position = {"file_index": index, "line": 1}
                break

            blob_payload = await _async_request(
                client,
                "GET",
                f"/repos/{owner}/{repo}/git/blobs/{quote(str(blob['sha']), safe='')}",
                token=token,
            )
            scanned_files += 1
            if (
                not isinstance(blob_payload, dict)
                or blob_payload.get("encoding") != "base64"
                or not isinstance(blob_payload.get("content"), str)
            ):
                skipped_binary_files += 1
                index += 1
                first_line = 1
                continue
            try:
                raw = base64.b64decode(blob_payload["content"], validate=False)
            except binascii.Error:
                skipped_binary_files += 1
                index += 1
                first_line = 1
                continue
            if len(raw) > GITHUB_GREP_MAX_FILE_BYTES:
                skipped_large_files += 1
                index += 1
                first_line = 1
                continue
            if scanned_bytes > 0 and scanned_bytes + len(raw) > GITHUB_GREP_MAX_BYTES:
                next_position = {"file_index": index, "line": 1}
                break
            scanned_bytes += len(raw)
            if b"\x00" in raw:
                skipped_binary_files += 1
                index += 1
                first_line = 1
                continue
            try:
                source = raw.decode("utf-8")
            except UnicodeDecodeError:
                skipped_binary_files += 1
                index += 1
                first_line = 1
                continue

            lines = source.splitlines()
            needle = literal if case_sensitive else literal.casefold()
            for line_number in range(first_line, len(lines) + 1):
                line = lines[line_number - 1]
                haystack = line if case_sensitive else line.casefold()
                if needle not in haystack:
                    continue
                match_index = max(0, min(haystack.find(needle), len(line)))
                snippet_budget = max(
                    GITHUB_GREP_MATCH_TEXT_CHARS,
                    min(len(literal), GITHUB_GREP_QUERY_MAX_CHARS),
                )
                context_chars = max(0, snippet_budget - min(len(literal), snippet_budget))
                snippet_start = max(0, match_index - context_chars // 2)
                snippet_end = min(len(line), snippet_start + snippet_budget)
                if snippet_end - snippet_start < snippet_budget:
                    snippet_start = max(0, snippet_end - snippet_budget)
                shown = line[snippet_start:snippet_end]
                match = {
                    "path": str(blob["path"]),
                    "line": line_number,
                    "column": match_index + 1,
                    "text": shown,
                    "citation": f"{blob['path']}:{line_number}",
                    "text_truncated": snippet_start > 0 or snippet_end < len(line),
                    "prefix_truncated": snippet_start > 0,
                    "suffix_truncated": snippet_end < len(line),
                }
                match_size = len(json.dumps(match, default=str))
                separator_size = 2 if matches else 0
                if match_json_chars + separator_size + match_size > match_output_budget:
                    if not matches:
                        raise GitHubConnectorError(
                            status_code=413,
                            message="A GitHub grep match cannot fit within the tool output budget.",
                        )
                    next_position = {"file_index": index, "line": line_number}
                    break
                matches.append(match)
                match_json_chars += separator_size + match_size
                if len(matches) >= max_matches:
                    if line_number < len(lines):
                        next_position = {"file_index": index, "line": line_number + 1}
                    elif index + 1 < len(blobs):
                        next_position = {"file_index": index + 1, "line": 1}
                    break
            if next_position is not None:
                break
            index += 1
            first_line = 1
            if scanned_bytes >= GITHUB_GREP_MAX_BYTES and index < len(blobs):
                next_position = {"file_index": index, "line": 1}
                break

    files_skipped = skipped_binary_files > 0 or skipped_large_files > 0
    if next_position is not None:
        next_position.update({
            "skipped_binary_files": skipped_binary_files,
            "skipped_large_files": skipped_large_files,
        })
    next_page = encode_page_token(page_kind, next_position) if next_position else None
    if source_truncated:
        evidence_health = {"status": "warning", "completeness": "source_truncated"}
    elif files_skipped:
        evidence_health = {"status": "warning", "completeness": "files_skipped"}
    elif next_page:
        evidence_health = {"status": "ok", "completeness": "more_available"}
    else:
        evidence_health = {"status": "ok", "completeness": "complete"}
    result = {
        "repo": slug,
        "requested_ref": resolved["requested_ref"],
        "resolved_ref": resolved["resolved_ref"],
        "path": prefix,
        "query": literal,
        "case_sensitive": bool(case_sensitive),
        "matches": matches,
        "returned": len(matches),
        "examined_files": examined_files,
        "scanned_files": scanned_files,
        "scanned_bytes": scanned_bytes,
        "skipped_binary_files": skipped_binary_files,
        "skipped_large_files": skipped_large_files,
        "scan_budget": {
            "max_files": GITHUB_GREP_MAX_FILES,
            "max_bytes": GITHUB_GREP_MAX_BYTES,
            "max_file_bytes": GITHUB_GREP_MAX_FILE_BYTES,
            "max_matches": max_matches,
            "max_match_output_chars": GITHUB_SOURCE_OUTPUT_CHARS,
        },
        "truncated": next_page is not None or source_truncated or files_skipped,
        "search_incomplete": source_truncated or files_skipped,
        "source_truncated": source_truncated,
        "next_page": next_page,
        "evidence_health": evidence_health,
    }
    return _bounded_source_payload(result)


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
    checks_payload = _check_runs_payload(checks)
    # Modern CI (GitHub Actions, gitleaks, etc.) surfaces through check runs. The
    # legacy statuses API needs statuses:read, which illo-bot deliberately omits
    # to avoid a GitHub App re-approval.
    return {
        "repo": slug,
        "sha": sha,
        "checks": checks_payload,
        "combined_status": checks_payload["status"],
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


def _issue_reference(slug: str, issue_number: int) -> dict[str, Any]:
    return {
        "repo": slug,
        "number": issue_number,
        "html_url": f"https://github.com/{slug}/issues/{issue_number}",
    }


def _numeric_issue_id(issue: Any, *, slug: str, issue_number: int) -> int:
    issue_id = issue.get("id") if isinstance(issue, dict) else None
    if not isinstance(issue_id, int) or isinstance(issue_id, bool) or issue_id < 1:
        raise GitHubConnectorError(
            status_code=502,
            message=f"GitHub issue response for {slug}#{issue_number} omitted its numeric id.",
        )
    return issue_id


def _issue_node_id(issue: Any, *, slug: str, issue_number: int) -> str:
    node_id = issue.get("node_id") if isinstance(issue, dict) else None
    if not isinstance(node_id, str) or not node_id.strip():
        raise GitHubConnectorError(
            status_code=502,
            message=f"GitHub issue response for {slug}#{issue_number} omitted its node id.",
        )
    return node_id.strip()


def _graphql_parent_reference(data: Any) -> tuple[str, int] | None:
    errors = data.get("errors") if isinstance(data, dict) else None
    if isinstance(errors, list) and errors:
        messages = [
            str(error.get("message") or "Unknown GraphQL error")
            for error in errors
            if isinstance(error, dict)
        ]
        raise GitHubConnectorError(
            status_code=502,
            message=f"GitHub GraphQL parent lookup failed: {'; '.join(messages) or 'unknown error'}",
        )

    payload = data.get("data") if isinstance(data, dict) else None
    node = payload.get("node") if isinstance(payload, dict) else None
    if not isinstance(node, dict):
        raise GitHubConnectorError(
            status_code=502,
            message="GitHub GraphQL parent lookup did not resolve the child issue node.",
        )
    parent = node.get("parent")
    if parent is None:
        return None
    repository = parent.get("repository") if isinstance(parent, dict) else None
    parent_slug = parse_github_repo_slug(
        str(repository.get("nameWithOwner") or "") if isinstance(repository, dict) else ""
    )
    parent_number = parent.get("number") if isinstance(parent, dict) else None
    if (
        parent_slug is None
        or not isinstance(parent_number, int)
        or isinstance(parent_number, bool)
        or parent_number < 1
    ):
        raise GitHubConnectorError(
            status_code=502,
            message="GitHub GraphQL parent lookup returned an incomplete parent reference.",
        )
    return parent_slug, parent_number


async def _async_list_parent_sub_issues(
    client: httpx.AsyncClient,
    slug: str,
    issue_number: int,
    *,
    token: str | None,
    page: int = 1,
    raw_response_auth_source: str | None = None,
) -> list[Any]:
    """Read the authoritative parent-side sub-issue collection."""

    owner, repo = slug.split("/", 1)

    request_path = f"/repos/{owner}/{repo}/issues/{issue_number}/sub_issues"

    def observe_response(response: httpx.Response, data: Any) -> None:
        if raw_response_auth_source:
            logger.info(
                "github_sub_issue_raw_response %s",
                json.dumps({
                    "auth_source": raw_response_auth_source,
                    "body_base64": base64.b64encode(response.content).decode("ascii"),
                    "path": request_path,
                    "status_code": response.status_code,
                }, separators=(",", ":"), sort_keys=True),
            )
        items = data if isinstance(data, list) else []
        logger.info(
            "github_sub_issue_read_response %s",
            json.dumps({
                "authenticated": bool(token),
                "body_bytes": len(response.content),
                "body_sha256": hashlib.sha256(response.content).hexdigest(),
                "item_count": len(items),
                "item_id_count": sum(
                    1
                    for item in items
                    if isinstance(item, dict) and isinstance(item.get("id"), int)
                ),
                "item_node_id_count": sum(
                    1
                    for item in items
                    if isinstance(item, dict)
                    and isinstance(item.get("node_id"), str)
                    and bool(item["node_id"].strip())
                ),
                "items": [
                    _sub_issue_identity_summary(item)
                    for item in items
                    if isinstance(item, dict)
                ],
                "json_shape": (
                    "array"
                    if isinstance(data, list)
                    else "object"
                    if isinstance(data, dict)
                    else type(data).__name__
                ),
                "status_code": response.status_code,
            }, separators=(",", ":"), sort_keys=True),
        )

    data = await _async_request(
        client,
        "GET",
        request_path,
        token=token,
        params={"per_page": GITHUB_ITEM_LIMIT, "page": page},
        response_observer=observe_response,
    )
    if not isinstance(data, list):
        raise GitHubConnectorError(
            status_code=502,
            message="GitHub parent sub-issue response was not an array.",
        )
    return data


def _contains_sub_issue(items: list[Any], child_id: int) -> bool:
    return any(isinstance(item, dict) and item.get("id") == child_id for item in items)


def _sub_issue_repository_slug(issue: dict[str, Any]) -> str | None:
    for key in ("repository_url", "html_url", "url"):
        value = str(issue.get(key) or "").strip()
        if not value:
            continue
        api_slug = re.sub(
            r"^https?://api\.github\.com/repos/",
            "",
            value,
            flags=re.IGNORECASE,
        )
        parsed = parse_github_repo_slug(api_slug)
        if parsed:
            return parsed
    return None


def _sub_issue_identity_summary(issue: dict[str, Any]) -> dict[str, Any]:
    issue_id = issue.get("id")
    node_id = issue.get("node_id")
    number = issue.get("number")
    repo_slug = _sub_issue_repository_slug(issue)
    safe_id = issue_id if isinstance(issue_id, int) and not isinstance(issue_id, bool) else None
    safe_node_id = node_id.strip() if isinstance(node_id, str) and node_id.strip() else None
    safe_number = number if isinstance(number, int) and not isinstance(number, bool) else None
    return {
        "api_url": (
            f"https://api.github.com/repos/{repo_slug}/issues/{safe_number}"
            if repo_slug and safe_number is not None
            else None
        ),
        "html_url": (
            f"https://github.com/{repo_slug}/issues/{safe_number}"
            if repo_slug and safe_number is not None
            else None
        ),
        "id": safe_id,
        "node_id": safe_node_id,
        "number": safe_number,
        "repository_slug": repo_slug,
    }


def _sub_issue_payload(issue: dict[str, Any]) -> dict[str, Any]:
    payload = _issue_payload(issue)
    payload["repo"] = _sub_issue_repository_slug(issue)
    return payload


async def async_list_repo_sub_issues(
    slug: str,
    issue_number: int,
    *,
    token: str | None = None,
    limit: int = 30,
    cursor: str | None = None,
    raw_response_auth_source: str | None = None,
) -> dict[str, Any]:
    """List one bounded page of a parent issue's native GitHub sub-issues."""

    max_items = max(1, min(int(limit or 30), GITHUB_ITEM_LIMIT))
    page_kind = f"github_sub_issues:{slug}#{issue_number}"
    position = decode_page_token(cursor, kind=page_kind)
    try:
        page = int(position.get("page", 1))
        page_index = int(position.get("index", 0))
    except (TypeError, ValueError) as exc:
        raise InvalidPageToken("Invalid pagination cursor") from exc
    if page < 1 or page_index < 0:
        raise InvalidPageToken("Invalid pagination cursor")

    async with async_http_client(timeout=httpx.Timeout(12.0, connect=5.0)) as client:
        items = await _async_list_parent_sub_issues(
            client,
            slug,
            issue_number,
            token=token,
            page=page,
            raw_response_auth_source=raw_response_auth_source,
        )
    selected = items[page_index : page_index + max_items]
    next_position = None
    if page_index + max_items < len(items):
        next_position = {"page": page, "index": page_index + max_items}
    elif len(items) == GITHUB_ITEM_LIMIT:
        next_position = {"page": page + 1, "index": 0}
    return {
        "repo": slug,
        "parent": _issue_reference(slug, issue_number),
        "sub_issues": [_sub_issue_payload(item) for item in selected if isinstance(item, dict)],
        "truncated": next_position is not None,
        "next_page": encode_page_token(page_kind, next_position) if next_position else None,
        "evidence_health": {
            "status": "ok",
            "completeness": "more_available" if next_position else "complete",
        },
    }


async def async_get_repo_issue_parent(
    slug: str,
    issue_number: int,
    *,
    token: str | None = None,
    raw_response_auth_source: str | None = None,
) -> dict[str, Any]:
    """Resolve an issue's native parent by global node id, across repositories."""

    owner, repo = slug.split("/", 1)
    async with async_http_client(timeout=httpx.Timeout(12.0, connect=5.0)) as client:
        issue = await _async_request(
            client,
            "GET",
            f"/repos/{owner}/{repo}/issues/{issue_number}",
            token=token,
        )
        issue_data = issue if isinstance(issue, dict) else {}
        parent_slug: str | None = None
        parent: Any = None
        parent_payload: dict[str, Any] | None = None
        candidate_parent: dict[str, Any] | None = None
        verified: bool | None = None
        verification_source: str | None = None
        if token:
            parent_lookup = await _async_request(
                client,
                "POST",
                "/graphql",
                token=token,
                json={
                    "query": GITHUB_ISSUE_PARENT_QUERY,
                    "variables": {
                        "issueId": _issue_node_id(issue_data, slug=slug, issue_number=issue_number),
                    },
                },
            )
            parent_reference = _graphql_parent_reference(parent_lookup)
            if parent_reference is not None:
                parent_slug, parent_issue_number = parent_reference
                candidate_parent = _issue_reference(parent_slug, parent_issue_number)
                parent_items = await _async_list_parent_sub_issues(
                    client,
                    parent_slug,
                    parent_issue_number,
                    token=token,
                    raw_response_auth_source=raw_response_auth_source,
                )
                verified = _contains_sub_issue(
                    parent_items,
                    _numeric_issue_id(issue_data, slug=slug, issue_number=issue_number),
                )
                verification_source = "parent_sub_issues"
                if verified:
                    parent_payload = candidate_parent
            else:
                verified = False
                verification_source = "graphql_parent"
        else:
            try:
                parent = await _async_request(
                    client,
                    "GET",
                    f"/repos/{owner}/{repo}/issues/{issue_number}/parent",
                    token=None,
                )
            except GitHubConnectorError as exc:
                if exc.status_code != 404:
                    raise
            if isinstance(parent, dict):
                parent_slug = parse_github_repo_slug(str(parent.get("html_url") or ""))
                parent_payload = _issue_payload(parent)
                parent_payload["repo"] = parent_slug
    return {
        "repo": slug,
        "issue": _issue_payload(issue_data),
        "parent": parent_payload,
        **({"candidate_parent": candidate_parent} if candidate_parent is not None else {}),
        **({"verified": verified} if verified is not None else {}),
        **({"verification_source": verification_source} if verification_source is not None else {}),
    }


async def async_add_repo_sub_issue(
    parent_slug: str,
    parent_issue_number: int,
    child_slug: str,
    child_issue_number: int,
    *,
    token: str,
) -> dict[str, Any]:
    """Idempotently link a child issue after resolving number to numeric id."""

    parent_owner, parent_repo = parent_slug.split("/", 1)
    child_owner, child_repo = child_slug.split("/", 1)
    async with async_http_client(timeout=httpx.Timeout(12.0, connect=5.0)) as client:
        child = await _async_request(
            client,
            "GET",
            f"/repos/{child_owner}/{child_repo}/issues/{child_issue_number}",
            token=token,
        )
        child_id = _numeric_issue_id(child, slug=child_slug, issue_number=child_issue_number)
        current_items = await _async_list_parent_sub_issues(
            client,
            parent_slug,
            parent_issue_number,
            token=token,
        )
        if _contains_sub_issue(current_items, child_id):
            return {
                "action": "already_linked",
                "changed": False,
                "already_linked": True,
                "verified": True,
                "verification_source": "parent_sub_issues",
                "parent": _issue_reference(parent_slug, parent_issue_number),
                "child": {"repo": child_slug, **_issue_payload(child)},
            }
        linked = await _async_request(
            client,
            "POST",
            f"/repos/{parent_owner}/{parent_repo}/issues/{parent_issue_number}/sub_issues",
            token=token,
            json={"sub_issue_id": child_id},
        )
        read_back = await _async_list_parent_sub_issues(
            client,
            parent_slug,
            parent_issue_number,
            token=token,
        )
        verified = _contains_sub_issue(read_back, child_id)
    return {
        "action": "linked",
        "changed": True,
        "already_linked": False,
        "verified": verified,
        "verification_source": "parent_sub_issues",
        "parent": _issue_reference(parent_slug, parent_issue_number),
        "child": {"repo": child_slug, **_issue_payload(linked if isinstance(linked, dict) else child)},
    }


async def async_remove_repo_sub_issue(
    parent_slug: str,
    parent_issue_number: int,
    child_slug: str,
    child_issue_number: int,
    *,
    token: str,
) -> dict[str, Any]:
    """Idempotently remove a child after resolving its number to numeric id."""

    parent_owner, parent_repo = parent_slug.split("/", 1)
    child_owner, child_repo = child_slug.split("/", 1)
    async with async_http_client(timeout=httpx.Timeout(12.0, connect=5.0)) as client:
        child = await _async_request(
            client,
            "GET",
            f"/repos/{child_owner}/{child_repo}/issues/{child_issue_number}",
            token=token,
        )
        child_id = _numeric_issue_id(child, slug=child_slug, issue_number=child_issue_number)
        current_items = await _async_list_parent_sub_issues(
            client,
            parent_slug,
            parent_issue_number,
            token=token,
        )
        if not _contains_sub_issue(current_items, child_id):
            return {
                "action": "already_unlinked",
                "changed": False,
                "already_unlinked": True,
                "parent": _issue_reference(parent_slug, parent_issue_number),
                "child": {"repo": child_slug, **_issue_payload(child)},
            }
        removed = await _async_request(
            client,
            "DELETE",
            f"/repos/{parent_owner}/{parent_repo}/issues/{parent_issue_number}/sub_issue",
            token=token,
            json={"sub_issue_id": child_id},
        )
    return {
        "action": "unlinked",
        "changed": True,
        "already_unlinked": False,
        "parent": _issue_reference(parent_slug, parent_issue_number),
        "child": {"repo": child_slug, **_issue_payload(removed if isinstance(removed, dict) else child)},
    }


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


async def async_add_repo_issue_comment(
    slug: str,
    issue_number: int,
    *,
    body: str,
    token: str | None = None,
) -> dict[str, Any]:
    """Append one comment via the dedicated GitHub issue-comments endpoint."""

    if isinstance(issue_number, bool) or not isinstance(issue_number, int) or issue_number < 1:
        raise GitHubConnectorError(status_code=422, message="Issue number must be positive.")
    raw_body = str(body) if body is not None else ""
    if not raw_body.strip():
        raise GitHubConnectorError(status_code=422, message="Issue comment body is required.")

    owner, repo = slug.split("/", 1)
    async with async_http_client(timeout=httpx.Timeout(12.0, connect=5.0)) as client:
        created = await _async_request(
            client,
            "POST",
            f"/repos/{owner}/{repo}/issues/{issue_number}/comments",
            token=token,
            json={"body": raw_body},
        )
    return {
        "repo": slug,
        "issue_number": issue_number,
        "comment": _issue_comment_payload(created if isinstance(created, dict) else {}),
    }


def _issue_update_field_result(
    *,
    status: str,
    requested: Any,
    applied: Any,
    failed: Any | None = None,
    error: GitHubConnectorError | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": status,
        "requested": requested,
        "applied": applied,
    }
    if failed is not None:
        result["failed"] = failed
    if error is not None:
        result["status_code"] = error.status_code
        result["error"] = error.message
    return result


async def async_update_repo_issue(
    slug: str,
    issue_number: int,
    *,
    assignees_add: list[str] | None = None,
    assignees_remove: list[str] | None = None,
    labels_add: list[str] | None = None,
    labels_remove: list[str] | None = None,
    labels_set: list[str] | None = None,
    state: str | None = None,
    title: str | None = None,
    body: str | None = None,
    token: str | None = None,
) -> dict[str, Any]:
    """Update one issue field at a time, then read the issue back.

    GitHub validates assignees and labels independently. Keeping each requested
    field in its own REST call lets a valid ownership transfer survive an
    invalid label while giving the caller an honest per-field receipt.
    """

    if isinstance(issue_number, bool) or not isinstance(issue_number, int) or issue_number < 1:
        raise GitHubConnectorError(status_code=422, message="Issue number must be positive.")

    def clean_list(values: list[str] | None) -> list[str]:
        return [str(value).strip() for value in (values or []) if str(value).strip()]

    clean_assignees_add = clean_list(assignees_add)
    clean_assignees_remove = clean_list(assignees_remove)
    clean_labels_add = clean_list(labels_add)
    clean_labels_remove = clean_list(labels_remove)
    clean_labels_set = clean_list(labels_set) if labels_set is not None else None
    clean_state = str(state or "").strip().lower() or None
    if clean_state not in {None, "open", "closed"}:
        raise GitHubConnectorError(status_code=422, message="Issue state must be open or closed.")
    clean_title = str(title).strip() if title is not None else None
    if title is not None and not clean_title:
        raise GitHubConnectorError(status_code=422, message="Issue title cannot be empty.")
    clean_body = str(body) if body is not None else None
    if clean_labels_set is not None and (clean_labels_add or clean_labels_remove):
        raise GitHubConnectorError(
            status_code=422,
            message="labels_set cannot be combined with labels_add or labels_remove.",
        )

    requested_updates = any((
        clean_assignees_add,
        clean_assignees_remove,
        clean_labels_add,
        clean_labels_remove,
        clean_labels_set is not None,
        clean_state is not None,
        clean_title is not None,
        clean_body is not None,
    ))
    if not requested_updates:
        raise GitHubConnectorError(status_code=422, message="At least one issue update field is required.")

    owner, repo = slug.split("/", 1)
    issue_path = f"/repos/{owner}/{repo}/issues/{issue_number}"
    fields: dict[str, dict[str, Any]] = {}
    applied: dict[str, Any] = {}
    failed: dict[str, dict[str, Any]] = {}

    async with async_http_client(timeout=httpx.Timeout(12.0, connect=5.0)) as client:
        async def apply_field(
            key: str,
            requested: Any,
            *,
            method: str,
            path: str,
            payload: dict[str, Any] | None = None,
        ) -> None:
            try:
                await _async_request(
                    client,
                    method,
                    path,
                    token=token,
                    json=payload,
                )
            except GitHubConnectorError as exc:
                field_result = _issue_update_field_result(
                    status="failed",
                    requested=requested,
                    applied=[] if isinstance(requested, list) else None,
                    failed=requested,
                    error=exc,
                )
                fields[key] = field_result
                failed[key] = field_result
                return
            fields[key] = _issue_update_field_result(
                status="applied",
                requested=requested,
                applied=requested,
            )
            applied[key] = requested

        if clean_assignees_add:
            await apply_field(
                "assignees_add",
                clean_assignees_add,
                method="POST",
                path=f"{issue_path}/assignees",
                payload={"assignees": clean_assignees_add},
            )
        if clean_assignees_remove:
            await apply_field(
                "assignees_remove",
                clean_assignees_remove,
                method="DELETE",
                path=f"{issue_path}/assignees",
                payload={"assignees": clean_assignees_remove},
            )
        if clean_labels_set is not None:
            await apply_field(
                "labels_set",
                clean_labels_set,
                method="PUT",
                path=f"{issue_path}/labels",
                payload={"labels": clean_labels_set},
            )
        if clean_labels_add:
            await apply_field(
                "labels_add",
                clean_labels_add,
                method="POST",
                path=f"{issue_path}/labels",
                payload={"labels": clean_labels_add},
            )
        if clean_labels_remove:
            removed: list[str] = []
            removal_errors: list[tuple[str, GitHubConnectorError]] = []
            for label in clean_labels_remove:
                try:
                    await _async_request(
                        client,
                        "DELETE",
                        f"{issue_path}/labels/{quote(label, safe='')}",
                        token=token,
                    )
                except GitHubConnectorError as exc:
                    removal_errors.append((label, exc))
                else:
                    removed.append(label)
            if removal_errors:
                first_error = removal_errors[0][1]
                failed_labels = [label for label, _error in removal_errors]
                field_result = _issue_update_field_result(
                    status="partial" if removed else "failed",
                    requested=clean_labels_remove,
                    applied=removed,
                    failed=failed_labels,
                    error=first_error,
                )
                if len(removal_errors) > 1:
                    field_result["errors"] = [
                        {
                            "label": label,
                            "status_code": error.status_code,
                            "error": error.message,
                        }
                        for label, error in removal_errors
                    ]
                fields["labels_remove"] = field_result
                failed["labels_remove"] = field_result
                if removed:
                    applied["labels_remove"] = removed
            else:
                fields["labels_remove"] = _issue_update_field_result(
                    status="applied",
                    requested=clean_labels_remove,
                    applied=removed,
                )
                applied["labels_remove"] = removed
        if clean_state is not None:
            await apply_field(
                "state",
                clean_state,
                method="PATCH",
                path=issue_path,
                payload={"state": clean_state},
            )
        if clean_title is not None:
            await apply_field(
                "title",
                clean_title,
                method="PATCH",
                path=issue_path,
                payload={"title": clean_title},
            )
        if clean_body is not None:
            await apply_field(
                "body",
                clean_body,
                method="PATCH",
                path=issue_path,
                payload={"body": clean_body},
            )

        issue: dict[str, Any] | None = None
        try:
            read_back = await _async_request(
                client,
                "GET",
                issue_path,
                token=token,
            )
        except GitHubConnectorError as exc:
            field_result = _issue_update_field_result(
                status="failed",
                requested=True,
                applied=False,
                failed=True,
                error=exc,
            )
            fields["read_back"] = field_result
            failed["read_back"] = field_result
        else:
            raw_issue = read_back if isinstance(read_back, dict) else {}
            assignee_logins = {
                str(item.get("login") or "").casefold()
                for item in raw_issue.get("assignees") or []
                if isinstance(item, dict) and item.get("login")
            }
            label_names = {
                str(item.get("name") if isinstance(item, dict) else item).casefold()
                for item in raw_issue.get("labels") or []
                if (isinstance(item, str) and item) or (isinstance(item, dict) and item.get("name"))
            }

            def read_back_confirms(key: str, value: Any) -> bool:
                if key == "assignees_add":
                    return "assignees" in raw_issue and {
                        str(item).casefold() for item in value
                    } <= assignee_logins
                if key == "assignees_remove":
                    return "assignees" in raw_issue and not (
                        {str(item).casefold() for item in value} & assignee_logins
                    )
                if key == "labels_add":
                    return "labels" in raw_issue and {
                        str(item).casefold() for item in value
                    } <= label_names
                if key == "labels_remove":
                    return "labels" in raw_issue and not (
                        {str(item).casefold() for item in value} & label_names
                    )
                if key == "labels_set":
                    return "labels" in raw_issue and {
                        str(item).casefold() for item in value
                    } == label_names
                if key == "state":
                    return str(raw_issue.get("state") or "").casefold() == str(value).casefold()
                if key == "title":
                    return raw_issue.get("title") == value
                if key == "body":
                    return raw_issue.get("body") == value
                return False

            for key, value in list(applied.items()):
                if read_back_confirms(key, value):
                    fields[key]["verified"] = True
                    continue
                verification_error = GitHubConnectorError(
                    status_code=502,
                    message=f"GitHub read-back did not confirm {key}.",
                )
                field_result = _issue_update_field_result(
                    status="failed",
                    requested=fields[key]["requested"],
                    applied=[] if isinstance(value, list) else None,
                    failed=fields[key]["requested"],
                    error=verification_error,
                )
                field_result["verified"] = False
                fields[key] = field_result
                failed[key] = field_result
                del applied[key]

            issue = _issue_payload(raw_issue)

    is_partial = bool(applied) and bool(failed)
    return {
        "repo": slug,
        "issue_number": issue_number,
        "ok": not failed,
        "partial": is_partial,
        "status": "partial" if is_partial else ("failed" if failed else "applied"),
        "fields": fields,
        "applied": applied,
        "failed": failed,
        "issue": issue,
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
