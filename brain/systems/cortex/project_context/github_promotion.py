"""Policy and reconciliation service for GitHub staging promotion pull requests."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Literal

from brain.systems.cortex.project_context.github import (
    GitHubConnectorError,
    async_compare_repo_branches,
    async_create_repo_pull_request,
    async_list_repo_pull_requests,
)


_ISSUE_NUMBER_PATTERN = re.compile(r"(?<![A-Za-z0-9_])#([1-9][0-9]*)\b")


@dataclass(frozen=True)
class PromotionPullRequestTarget:
    """A requested promotion pull request target."""

    repo: str
    base: str
    head: str
    title: str
    draft: bool


class PromotionPullRequestPolicyViolation(ValueError):
    """The requested pull request falls outside the promotion policy."""


@dataclass(frozen=True)
class PromotionPullRequestPolicy:
    """Immutable allowlist for the scheduler's sole GitHub write."""

    repositories: frozenset[str]
    base: str
    head: str
    title: str
    require_non_draft: bool = True

    def target_for(self, repo: str) -> PromotionPullRequestTarget:
        target = PromotionPullRequestTarget(
            repo=repo,
            base=self.base,
            head=self.head,
            title=self.title,
            draft=False,
        )
        self.validate(target)
        return target

    def validate(self, target: PromotionPullRequestTarget) -> None:
        error = self.validation_error(target)
        if error:
            raise PromotionPullRequestPolicyViolation(error)

    def validation_error(self, target: PromotionPullRequestTarget) -> str | None:
        if target.repo not in self.repositories:
            return f"repository must be one of {sorted(self.repositories)}"
        if target.base != self.base:
            return f"base must be {self.base!r}"
        if target.head != self.head:
            return f"head must be {self.head!r}"
        if target.title != self.title:
            return f"title must be {self.title!r}"
        if self.require_non_draft and target.draft:
            return "draft must be false"
        return None


PROMOTION_PULL_REQUEST_POLICY = PromotionPullRequestPolicy(
    repositories=frozenset(
        {
            "uwear-ai/uwear-backend",
            "uwear-ai/uwearaiapp",
        }
    ),
    base="main",
    head="staging",
    title="Staging → main promotion",
)


PromotionPullRequestOutcome = Literal["no_diff", "already_open", "created"]
PromotionPullRequestSettlement = Literal[
    "comparison",
    "search",
    "create",
    "create_conflict",
]


@dataclass(frozen=True)
class PromotionPullRequestResult:
    """Typed result from reconciling one repository."""

    repo: str
    outcome: PromotionPullRequestOutcome
    settled_by: PromotionPullRequestSettlement
    number: int | None = None
    html_url: str | None = None
    state: str | None = None
    draft: bool = False
    conflict_message: str | None = None


class PromotionPullRequestOperationError(RuntimeError):
    """A recognized GitHub response that remains an operation failure."""

    def __init__(self, code: str, *, detail: str, status_code: int) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail
        self.status_code = status_code


def build_promotion_body(repo: str, commits: list[dict[str, Any]]) -> str:
    """Render compare commits and their issue references as Markdown."""

    lines = ["## Commits being promoted", ""]
    linked_issues: set[int] = set()
    for commit in commits:
        subject = str(commit.get("subject") or "(no commit subject)").strip()
        sha = str(commit.get("sha") or "").strip()
        url = str(commit.get("html_url") or "").strip()
        commit_label = sha[:7] if sha else "commit"
        commit_ref = f"[`{commit_label}`]({url})" if url else f"`{commit_label}`"
        issue_numbers = {
            int(match.group(1))
            for match in _ISSUE_NUMBER_PATTERN.finditer(subject)
        }
        linked_issues.update(issue_numbers)
        lines.append(f"- {commit_ref} {subject}")

    lines.extend(["", "## Linked issues", ""])
    if linked_issues:
        lines.extend(
            f"- [#{number}](https://github.com/{repo}/issues/{number})"
            for number in sorted(linked_issues)
        )
    else:
        lines.append("- None referenced in the commit subjects.")
    return "\n".join(lines)


async def async_reconcile_promotion_pull_request(
    target: PromotionPullRequestTarget,
    *,
    token: str,
    body: str | None = None,
) -> PromotionPullRequestResult:
    """Compare, search, and create a promotion pull request when needed."""

    PROMOTION_PULL_REQUEST_POLICY.validate(target)
    comparison = await async_compare_repo_branches(
        target.repo,
        target.base,
        target.head,
        token=token,
    )
    if comparison["ahead_by"] == 0:
        return PromotionPullRequestResult(
            repo=target.repo,
            outcome="no_diff",
            settled_by="comparison",
        )

    open_pulls = await async_list_repo_pull_requests(
        target.repo,
        token=token,
        state="open",
        base=target.base,
        head=target.head,
        limit=100,
    )
    matching_pull = next(
        (
            pull
            for pull in open_pulls.get("pull_requests", [])
            if (pull.get("base") or {}).get("ref") == target.base
            and (pull.get("head") or {}).get("ref") == target.head
        ),
        None,
    )
    if matching_pull is not None:
        return PromotionPullRequestResult(
            repo=target.repo,
            outcome="already_open",
            settled_by="search",
            number=_optional_int(matching_pull.get("number")),
        )

    pull_request_body = (
        body
        if body is not None
        else build_promotion_body(target.repo, comparison["commits"])
    )
    try:
        payload = await async_create_repo_pull_request(
            target.repo,
            base=target.base,
            head=target.head,
            title=target.title,
            body=pull_request_body,
            draft=target.draft,
            token=token,
        )
    except GitHubConnectorError as exc:
        lowered = exc.message.lower()
        if exc.status_code == 422 and "pull request already exists" in lowered:
            return PromotionPullRequestResult(
                repo=target.repo,
                outcome="already_open",
                settled_by="create_conflict",
                number=_existing_pull_request_number(exc.message),
                conflict_message=exc.message,
            )
        if exc.status_code == 422 and "no commits between" in lowered:
            raise PromotionPullRequestOperationError(
                "no_commits_between",
                detail=exc.message,
                status_code=exc.status_code,
            ) from exc
        raise

    pull_request = payload.get("pull_request")
    pull_request = pull_request if isinstance(pull_request, dict) else {}
    return PromotionPullRequestResult(
        repo=target.repo,
        outcome="created",
        settled_by="create",
        number=_optional_int(pull_request.get("number")),
        html_url=_optional_string(pull_request.get("html_url")),
        state=_optional_string(pull_request.get("state")),
        draft=bool(pull_request.get("draft")),
    )


def _existing_pull_request_number(message: str) -> int | None:
    for pattern in (r"/pull/(\d+)", r"pull request\s+#?(\d+)"):
        match = re.search(pattern, message, flags=re.IGNORECASE)
        if match:
            return int(match.group(1))
    return None


def _optional_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) else None
