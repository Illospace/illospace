"""Small GitHub API client for Project Context connectors."""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

import httpx

from brain.platform.async_io import async_http_client, sync_http_client


GITHUB_API_BASE = "https://api.github.com"
GITHUB_REPO_PAGE_LIMIT = 10
GITHUB_SEARCH_LIMIT = 10


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
) -> Any:
    try:
        response = await client.request(method, f"{GITHUB_API_BASE}{path}", headers=_headers(token), params=params)
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
