"""Pure normalization of GitHub PR references used by deploy tracking."""

from __future__ import annotations

import re


_REPO_PR_RE = re.compile(
    r"\b(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#(?P<number>[1-9][0-9]*)\b"
)
_GITHUB_PR_URL_RE = re.compile(
    r"https?://github\.com/(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull/(?P<number>[1-9][0-9]*)\b",
    re.IGNORECASE,
)
_GITHUB_PR_KEY_RE = re.compile(
    r"\bgithub:(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+):pr:(?P<number>[1-9][0-9]*)\b",
    re.IGNORECASE,
)
_GITHUB_ISSUE_KEY_RE = re.compile(
    r"\bgithub:(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+):issue:[1-9][0-9]*\b",
    re.IGNORECASE,
)
_SHORT_PR_RE = re.compile(
    r"\b(?:pr|pull request)\s*#?(?P<number>[1-9][0-9]*)\b",
    re.IGNORECASE,
)
_BARE_PR_RE = re.compile(r"^(?P<number>[1-9][0-9]*)$")


def github_repo_from_issue_text(text: str) -> str | None:
    """Extract ``owner/repo`` from a tracker GitHub issue key."""
    match = _GITHUB_ISSUE_KEY_RE.search(str(text or "").strip())
    return match.group("repo") if match else None


def normalize_fix_pr_reference(
    text: str,
    *,
    default_repo: str | None,
) -> str | None:
    """Normalize a supported PR reference to ``owner/repo#N``."""
    url_match = _GITHUB_PR_URL_RE.search(text)
    if url_match:
        return f"{url_match.group('repo')}#{url_match.group('number')}"
    key_match = _GITHUB_PR_KEY_RE.search(text)
    if key_match:
        return f"{key_match.group('repo')}#{key_match.group('number')}"
    repo_match = _REPO_PR_RE.search(text)
    if repo_match:
        return f"{repo_match.group('repo')}#{repo_match.group('number')}"
    short_match = _SHORT_PR_RE.search(text)
    clean_repo = str(default_repo or "").strip()
    if short_match and clean_repo:
        return f"{clean_repo}#{short_match.group('number')}"
    bare_match = _BARE_PR_RE.fullmatch(str(text or "").strip())
    if bare_match and clean_repo:
        return f"{clean_repo}#{bare_match.group('number')}"
    return None
