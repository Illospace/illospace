"""Best-effort GitHub reads for deploy-state ancestry checks."""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass

from brain.contracts.github import parse_github_repo_slug
from brain.systems.cortex.project_context.github import async_compare_commits


logger = logging.getLogger("illo.deploy_state.github")


@dataclass(frozen=True, slots=True)
class AncestryObservation:
    """One GitHub compare result, including honest degradation metadata."""

    branch: str
    is_ancestor: bool | None
    status: str | None = None
    error_category: str | None = None
    status_code: int | None = None

    @property
    def failed(self) -> bool:
        return self.error_category is not None


def _exception_category(exc: Exception) -> tuple[str, int | None]:
    status_code = getattr(exc, "status_code", None)
    if isinstance(status_code, int):
        return f"github_http_{status_code}", status_code
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return "timeout", None
    name = re.sub(r"(?<!^)(?=[A-Z])", "_", type(exc).__name__).lower()
    return name or "compare_error", None


def ancestry_failure(
    branch: str,
    exc: Exception,
) -> AncestryObservation:
    """Normalize a compare-path exception into degradation metadata."""
    category, status_code = _exception_category(exc)
    return AncestryObservation(
        branch=branch,
        is_ancestor=None,
        error_category=category,
        status_code=status_code,
    )


async def observe_ancestry(
    repo: str,
    sha: str,
    branch: str,
    *,
    token: str | None = None,
) -> AncestryObservation:
    """Observe whether ``sha`` is an ancestor of ``branch``.

    GitHub's ``branch...sha`` status is ``behind`` when the SHA is behind (and
    therefore contained by) the branch. Failures deliberately degrade open,
    but retain a category so callers can make the degradation visible.
    """
    slug = parse_github_repo_slug(repo)
    clean_sha = str(sha or "").strip()
    clean_branch = str(branch or "").strip()
    if not slug or not clean_sha or not clean_branch:
        return AncestryObservation(
            branch=clean_branch,
            is_ancestor=None,
            error_category="invalid_reference",
        )
    try:
        status = await async_compare_commits(slug, clean_branch, clean_sha, token=token)
    except Exception as exc:
        logger.warning(
            "deploy ancestry check indeterminate for %s %s...%s: %s",
            slug,
            clean_branch,
            clean_sha,
            exc,
        )
        return ancestry_failure(clean_branch, exc)
    if status in {"identical", "behind"}:
        return AncestryObservation(
            branch=clean_branch,
            is_ancestor=True,
            status=status,
        )
    if status in {"ahead", "diverged"}:
        return AncestryObservation(
            branch=clean_branch,
            is_ancestor=False,
            status=status,
        )
    return AncestryObservation(
        branch=clean_branch,
        is_ancestor=None,
        status=str(status) if status is not None else None,
        error_category=(
            f"github_status_{status}"
            if status is not None
            else "github_status_unknown"
        ),
    )


async def is_ancestor_of(
    repo: str,
    sha: str,
    branch: str,
    *,
    token: str | None = None,
) -> bool | None:
    """Compatibility boolean façade over :func:`observe_ancestry`."""
    return (
        await observe_ancestry(
            repo,
            sha,
            branch,
            token=token,
        )
    ).is_ancestor
