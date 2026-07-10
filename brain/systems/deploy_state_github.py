"""Best-effort GitHub reads for deploy-state ancestry checks."""

from __future__ import annotations

import logging

from brain.systems.cortex.project_context.github import (
    async_compare_commits,
    parse_github_repo_slug,
)


logger = logging.getLogger("illo.deploy_state.github")


async def is_ancestor_of(
    repo: str,
    sha: str,
    branch: str,
    *,
    token: str | None = None,
) -> bool | None:
    """Whether ``sha`` is an ancestor of ``branch``; ``None`` is indeterminate.

    GitHub's ``branch...sha`` status is ``behind`` when the SHA is behind (and
    therefore contained by) the branch.  Failures deliberately degrade open.
    """
    slug = parse_github_repo_slug(repo)
    clean_sha = str(sha or "").strip()
    clean_branch = str(branch or "").strip()
    if not slug or not clean_sha or not clean_branch:
        return None
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
        return None
    if status in {"identical", "behind"}:
        return True
    if status in {"ahead", "diverged"}:
        return False
    return None
