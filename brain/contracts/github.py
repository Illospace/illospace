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


__all__ = ["GitHubConnectorError", "parse_github_repo_slug"]
