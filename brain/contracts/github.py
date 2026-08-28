"""Import-safe GitHub integration contracts shared across layers."""
from __future__ import annotations

from dataclasses import dataclass
import re


# The prefixes that mark a value as an explicit GitHub reference. Stated once: the
# same list decides whether the origin is trusted AND what gets stripped, so the two
# can never drift apart.
_GITHUB_PREFIX_RE = re.compile(
    r"^(?:git@github\.com:|https?://github\.com/|github://|github\.com/)",
    flags=re.IGNORECASE,
)

# Canonical GitHub artifact ref encodings. Keep these import-safe so tool
# handlers can emit refs and downstream readers can decode the same format.
_GITHUB_COMMENT_REF_INFIX = ":comment:"


def github_artifact_ref_id(repo_slug: str, number: int) -> str:
    return f"{repo_slug}#{number}"


def github_comment_ref_id(repo_slug: str, issue_number: int, comment_id: int) -> str:
    return f"{github_artifact_ref_id(repo_slug, issue_number)}{_GITHUB_COMMENT_REF_INFIX}{comment_id}"


def github_issue_ref(repo_slug: str, number: int) -> dict[str, str]:
    return {"kind": "github_issue", "id": github_artifact_ref_id(repo_slug, number)}


def github_pull_request_ref(repo_slug: str, number: int) -> dict[str, str]:
    return {"kind": "github_pull_request", "id": github_artifact_ref_id(repo_slug, number)}


def github_issue_comment_ref(repo_slug: str, issue_number: int, comment_id: int) -> dict[str, str]:
    return {
        "kind": "github_issue_comment",
        "id": github_comment_ref_id(repo_slug, issue_number, comment_id),
    }


@dataclass
class GitHubConnectorError(Exception):
    status_code: int
    message: str

    def __post_init__(self) -> None:
        super().__init__(self.message)


def parse_github_repo_slug(value: str) -> str | None:
    slug = (value or "").strip()
    if not slug:
        return None
    stripped = _GITHUB_PREFIX_RE.sub("", slug, count=1)
    has_github_prefix = stripped != slug
    slug = re.sub(r"[?#].*$", "", stripped)
    if not has_github_prefix:
        if slug.startswith("/") or len(slug.strip("/").split("/")) != 2:
            return None
    slug = slug.strip("/")
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


__all__ = [
    "GitHubConnectorError",
    "github_artifact_ref_id",
    "github_comment_ref_id",
    "github_issue_comment_ref",
    "github_issue_ref",
    "github_pull_request_ref",
    "parse_github_repo_slug",
]
