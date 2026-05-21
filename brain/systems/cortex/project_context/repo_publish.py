"""Git publish adapter for Project repo draft workspaces."""
from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
import subprocess


DEFAULT_PUBLISH_BRANCH = "illo/project-draft-publish"
DEFAULT_BASE_BRANCH = "main"
PROTECTED_BRANCHES = {"main", "master"}


@dataclass(frozen=True)
class GitCommandResult:
    command: list[str]
    returncode: int
    stdout: str = ""
    stderr: str = ""


CommandRunner = Callable[..., Any]


@dataclass(frozen=True)
class RepoDraftStatus:
    repo_path: Path
    changed_paths: list[str] = field(default_factory=list)
    unmerged_paths: list[str] = field(default_factory=list)
    raw_status: str = ""
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors and not self.unmerged_paths

    @property
    def has_changes(self) -> bool:
        return bool(self.changed_paths)

    @property
    def has_conflicts(self) -> bool:
        return bool(self.unmerged_paths)


@dataclass(frozen=True)
class RepoDraftUpstreamStatus:
    repo_path: Path
    base_branch: str = DEFAULT_BASE_BRANCH
    status: str = "not_checked"
    upstream_changed_paths: list[str] = field(default_factory=list)
    upstream_conflicted_paths: list[str] = field(default_factory=list)
    base_commit: str | None = None
    fetch_status: str = "not_requested"
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors and not self.upstream_conflicted_paths

    @property
    def has_upstream_changes(self) -> bool:
        return bool(self.upstream_changed_paths)

    @property
    def has_conflicts(self) -> bool:
        return bool(self.upstream_conflicted_paths)


@dataclass(frozen=True)
class RepoDraftPublishResult:
    repo_path: Path
    branch: str
    changed_paths: list[str] = field(default_factory=list)
    commit_sha: str | None = None
    commit_status: str = "not_attempted"
    push_status: str = "not_requested"
    push_error: str | None = None
    pr_status: str = "not_requested"
    pr_url: str | None = None
    pr_error: str | None = None
    upstream_status: str = "not_checked"
    upstream_changed_paths: list[str] = field(default_factory=list)
    upstream_conflicted_paths: list[str] = field(default_factory=list)
    upstream_errors: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    status: RepoDraftStatus | None = None

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def committed(self) -> bool:
        return self.commit_sha is not None


def default_run_cmd(command: Sequence[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )


def _run_command(
    run_cmd: CommandRunner,
    command: Sequence[str],
    *,
    cwd: Path,
) -> GitCommandResult:
    args = list(command)
    try:
        result = run_cmd(args, cwd=cwd)
    except TypeError as exc:
        try:
            result = run_cmd(args)
        except Exception:
            return GitCommandResult(command=args, returncode=1, stderr=str(exc))
    except Exception as exc:
        return GitCommandResult(command=args, returncode=1, stderr=str(exc))
    return GitCommandResult(
        command=args,
        returncode=int(getattr(result, "returncode", 0)),
        stdout=str(getattr(result, "stdout", "") or ""),
        stderr=str(getattr(result, "stderr", "") or ""),
    )


def _command_error(action: str, result: GitCommandResult) -> str:
    detail = (result.stderr or result.stdout or f"exit code {result.returncode}").strip()
    return f"{action} failed: {detail}"


def _clean_branch_name(branch_name: str | None) -> str:
    branch = (branch_name or "").strip()
    return branch or DEFAULT_PUBLISH_BRANCH


def _clean_base_branch(base_branch: str | None) -> str:
    branch = (base_branch or "").strip() or DEFAULT_BASE_BRANCH
    for prefix in ("refs/remotes/origin/", "refs/heads/", "origin/"):
        if branch.startswith(prefix):
            return branch.removeprefix(prefix).strip() or DEFAULT_BASE_BRANCH
    return branch


def _is_protected_branch(branch_name: str) -> bool:
    branch = branch_name.removeprefix("refs/heads/").strip().lower()
    return branch in PROTECTED_BRANCHES or branch in {f"origin/{name}" for name in PROTECTED_BRANCHES}


def _status_path(payload: str) -> str:
    path = payload.strip()
    if " -> " in path:
        path = path.split(" -> ", 1)[1]
    return path.strip().strip('"')


def _unique(paths: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for path in paths:
        if not path or path in seen:
            continue
        seen.add(path)
        deduped.append(path)
    return deduped


def _normalise_repo_path(path: str) -> str:
    cleaned = path.strip().strip('"').replace("\\", "/")
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    return cleaned.rstrip("/")


def _parse_path_lines(output: str) -> list[str]:
    return _unique(
        [path for line in output.splitlines() if (path := _normalise_repo_path(line))]
    )


def _paths_overlap(left: str, right: str) -> bool:
    left_path = _normalise_repo_path(left)
    right_path = _normalise_repo_path(right)
    if not left_path or not right_path:
        return False
    return (
        left_path == right_path
        or left_path.startswith(f"{right_path}/")
        or right_path.startswith(f"{left_path}/")
    )


def _overlapping_paths(draft_paths: list[str], upstream_paths: list[str]) -> list[str]:
    return _unique(
        [
            draft_path
            for draft_path in draft_paths
            if any(_paths_overlap(draft_path, upstream_path) for upstream_path in upstream_paths)
        ]
    )


def _normalise_selected_paths(selected_paths: Sequence[str] | None) -> list[str]:
    if selected_paths is None:
        return []
    items = [selected_paths] if isinstance(selected_paths, str) else selected_paths
    return _unique(
        [
            path
            for path in (_normalise_repo_path(str(item)) for item in items)
            if path
        ]
    )


def _changed_paths_for_selection(
    changed_paths: list[str],
    selected_paths: list[str],
) -> list[str]:
    if not selected_paths:
        return list(changed_paths)
    return _unique(
        [
            changed_path
            for changed_path in changed_paths
            if any(
                _paths_overlap(changed_path, selected_path)
                for selected_path in selected_paths
            )
        ]
    )


def _add_paths_for_selection(
    changed_paths: list[str],
    selected_paths: list[str],
) -> list[str]:
    return _unique(
        [
            selected_path
            for selected_path in selected_paths
            if any(
                _paths_overlap(selected_path, changed_path)
                for changed_path in changed_paths
            )
        ]
    )


def _publish_upstream_fields(upstream: RepoDraftUpstreamStatus | None) -> dict[str, Any]:
    if upstream is None:
        return {}
    return {
        "upstream_status": upstream.status,
        "upstream_changed_paths": upstream.upstream_changed_paths,
        "upstream_conflicted_paths": upstream.upstream_conflicted_paths,
        "upstream_errors": upstream.errors,
    }


def _parse_status_paths(raw_status: str) -> tuple[list[str], list[str]]:
    changed_paths: list[str] = []
    unmerged_paths: list[str] = []

    for line in raw_status.splitlines():
        if not line:
            continue
        code = line[:2]
        if code == "!!":
            continue
        path = _status_path(line[3:] if len(line) > 3 else "")
        if not path:
            continue
        changed_paths.append(path)
        if code in {"DD", "AU", "UD", "UA", "DU", "AA", "UU"} or "U" in code:
            unmerged_paths.append(path)

    return _unique(changed_paths), _unique(unmerged_paths)


def repo_draft_status(
    repo_path: Path,
    *,
    run_cmd: CommandRunner = default_run_cmd,
) -> RepoDraftStatus:
    """Return porcelain git status for a local Project repo draft workspace."""

    repo_path = Path(repo_path)
    status = _run_command(run_cmd, ["git", "status", "--porcelain"], cwd=repo_path)
    if status.returncode != 0:
        return RepoDraftStatus(
            repo_path=repo_path,
            raw_status=status.stdout,
            errors=[_command_error("git status", status)],
        )

    changed_paths, unmerged_paths = _parse_status_paths(status.stdout)
    return RepoDraftStatus(
        repo_path=repo_path,
        changed_paths=changed_paths,
        unmerged_paths=unmerged_paths,
        raw_status=status.stdout,
    )


def repo_draft_upstream_status(
    repo_path: Path,
    *,
    changed_paths: Sequence[str] | None = None,
    base_branch: str | None = DEFAULT_BASE_BRANCH,
    fetch: bool = True,
    run_cmd: CommandRunner = default_run_cmd,
) -> RepoDraftUpstreamStatus:
    """Return upstream changes that may conflict with draft paths."""

    repo_path = Path(repo_path)
    branch = _clean_base_branch(base_branch)
    upstream_ref = f"refs/remotes/origin/{branch}"
    draft_paths = _unique(
        [path for path in (_normalise_repo_path(item) for item in changed_paths or []) if path]
    )
    fetch_status = "not_requested"

    if fetch:
        fetch_result = _run_command(
            run_cmd,
            ["git", "fetch", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}"],
            cwd=repo_path,
        )
        if fetch_result.returncode != 0:
            return RepoDraftUpstreamStatus(
                repo_path=repo_path,
                base_branch=branch,
                status="error",
                fetch_status="failed",
                errors=[_command_error("git fetch", fetch_result)],
            )
        fetch_status = "succeeded"

    rev_parse = _run_command(run_cmd, ["git", "rev-parse", "HEAD"], cwd=repo_path)
    if rev_parse.returncode != 0:
        return RepoDraftUpstreamStatus(
            repo_path=repo_path,
            base_branch=branch,
            status="error",
            fetch_status=fetch_status,
            errors=[_command_error("git rev-parse", rev_parse)],
        )

    base_commit = rev_parse.stdout.strip()
    if not base_commit:
        return RepoDraftUpstreamStatus(
            repo_path=repo_path,
            base_branch=branch,
            status="error",
            fetch_status=fetch_status,
            errors=["git rev-parse failed: no HEAD commit"],
        )

    diff = _run_command(
        run_cmd,
        ["git", "diff", "--name-only", f"{base_commit}..{upstream_ref}", "--"],
        cwd=repo_path,
    )
    if diff.returncode != 0:
        return RepoDraftUpstreamStatus(
            repo_path=repo_path,
            base_branch=branch,
            status="error",
            fetch_status=fetch_status,
            base_commit=base_commit,
            errors=[_command_error("git diff", diff)],
        )

    upstream_changed_paths = _parse_path_lines(diff.stdout)
    upstream_conflicted_paths = _overlapping_paths(draft_paths, upstream_changed_paths)
    upstream_status = (
        "conflicted"
        if upstream_conflicted_paths
        else "changed"
        if upstream_changed_paths
        else "clean"
    )
    return RepoDraftUpstreamStatus(
        repo_path=repo_path,
        base_branch=branch,
        status=upstream_status,
        upstream_changed_paths=upstream_changed_paths,
        upstream_conflicted_paths=upstream_conflicted_paths,
        base_commit=base_commit,
        fetch_status=fetch_status,
    )


def publish_repo_draft(
    repo_path: Path,
    *,
    branch_name: str | None = None,
    commit_message: str,
    push: bool = False,
    create_pr: bool = False,
    pr_title: str | None = None,
    pr_body: str | None = None,
    check_upstream: bool = True,
    base_branch: str | None = DEFAULT_BASE_BRANCH,
    selected_paths: Sequence[str] | None = None,
    run_cmd: CommandRunner = default_run_cmd,
) -> RepoDraftPublishResult:
    """Publish local repo draft changes onto a non-main branch."""

    repo_path = Path(repo_path)
    branch = _clean_branch_name(branch_name)
    message = (commit_message or "").strip()

    if _is_protected_branch(branch):
        return RepoDraftPublishResult(
            repo_path=repo_path,
            branch=branch,
            errors=[f"refusing to publish directly to protected branch: {branch}"],
        )
    if not message:
        return RepoDraftPublishResult(
            repo_path=repo_path,
            branch=branch,
            errors=["commit_message is required"],
        )

    status = repo_draft_status(repo_path, run_cmd=run_cmd)
    if status.errors:
        return RepoDraftPublishResult(
            repo_path=repo_path,
            branch=branch,
            changed_paths=status.changed_paths,
            errors=list(status.errors),
            status=status,
        )
    if status.unmerged_paths:
        return RepoDraftPublishResult(
            repo_path=repo_path,
            branch=branch,
            changed_paths=status.changed_paths,
            errors=["repo draft has unmerged paths"],
            status=status,
        )
    if not status.changed_paths:
        return RepoDraftPublishResult(
            repo_path=repo_path,
            branch=branch,
            changed_paths=[],
            commit_status="skipped_no_changes",
            push_status="skipped_no_commit" if push or create_pr else "not_requested",
            pr_status="skipped_no_commit" if create_pr else "not_requested",
            status=status,
        )

    publish_changed_paths = status.changed_paths
    add_command = ["git", "add", "-A"]
    if selected_paths is not None:
        selected_repo_paths = _normalise_selected_paths(selected_paths)
        publish_changed_paths = _changed_paths_for_selection(
            status.changed_paths,
            selected_repo_paths,
        )
        add_paths = _add_paths_for_selection(status.changed_paths, selected_repo_paths)
        if not selected_repo_paths:
            return RepoDraftPublishResult(
                repo_path=repo_path,
                branch=branch,
                changed_paths=[],
                commit_status="skipped_no_matching_paths",
                push_status=(
                    "skipped_no_commit" if push or create_pr else "not_requested"
                ),
                pr_status="skipped_no_commit" if create_pr else "not_requested",
                errors=["selected_paths did not include any usable paths"],
                status=status,
            )
        if not publish_changed_paths:
            return RepoDraftPublishResult(
                repo_path=repo_path,
                branch=branch,
                changed_paths=[],
                commit_status="skipped_no_matching_paths",
                push_status=(
                    "skipped_no_commit" if push or create_pr else "not_requested"
                ),
                pr_status="skipped_no_commit" if create_pr else "not_requested",
                errors=[
                    "selected_paths do not match changed paths: "
                    + ", ".join(selected_repo_paths)
                ],
                status=status,
            )
        add_command = ["git", "add", "--", *add_paths]

    upstream: RepoDraftUpstreamStatus | None = None
    if check_upstream:
        upstream = repo_draft_upstream_status(
            repo_path,
            changed_paths=publish_changed_paths,
            base_branch=base_branch,
            fetch=True,
            run_cmd=run_cmd,
        )
        if upstream.errors:
            return RepoDraftPublishResult(
                repo_path=repo_path,
                branch=branch,
                changed_paths=publish_changed_paths,
                errors=list(upstream.errors),
                status=status,
                **_publish_upstream_fields(upstream),
            )
        if upstream.upstream_conflicted_paths:
            conflicted = ", ".join(upstream.upstream_conflicted_paths)
            return RepoDraftPublishResult(
                repo_path=repo_path,
                branch=branch,
                changed_paths=publish_changed_paths,
                errors=[f"upstream changes overlap draft paths: {conflicted}"],
                status=status,
                **_publish_upstream_fields(upstream),
            )

    checkout = _run_command(run_cmd, ["git", "checkout", "-B", branch], cwd=repo_path)
    if checkout.returncode != 0:
        return RepoDraftPublishResult(
            repo_path=repo_path,
            branch=branch,
            changed_paths=publish_changed_paths,
            errors=[_command_error("git checkout", checkout)],
            status=status,
            **_publish_upstream_fields(upstream),
        )

    add = _run_command(run_cmd, add_command, cwd=repo_path)
    if add.returncode != 0:
        return RepoDraftPublishResult(
            repo_path=repo_path,
            branch=branch,
            changed_paths=publish_changed_paths,
            errors=[_command_error("git add", add)],
            status=status,
            **_publish_upstream_fields(upstream),
        )

    commit = _run_command(run_cmd, ["git", "commit", "-m", message], cwd=repo_path)
    if commit.returncode != 0:
        return RepoDraftPublishResult(
            repo_path=repo_path,
            branch=branch,
            changed_paths=publish_changed_paths,
            commit_status="failed",
            errors=[_command_error("git commit", commit)],
            status=status,
            **_publish_upstream_fields(upstream),
        )

    rev_parse = _run_command(run_cmd, ["git", "rev-parse", "HEAD"], cwd=repo_path)
    if rev_parse.returncode != 0:
        return RepoDraftPublishResult(
            repo_path=repo_path,
            branch=branch,
            changed_paths=publish_changed_paths,
            commit_status="created",
            errors=[_command_error("git rev-parse", rev_parse)],
            status=status,
            **_publish_upstream_fields(upstream),
        )

    commit_sha = rev_parse.stdout.strip() or None
    errors: list[str] = []
    push_status = "not_requested"
    push_error: str | None = None
    pr_status = "not_requested"
    pr_url: str | None = None
    pr_error: str | None = None

    if push or create_pr:
        push_result = _run_command(run_cmd, ["git", "push", "-u", "origin", branch], cwd=repo_path)
        if push_result.returncode == 0:
            push_status = "succeeded"
        else:
            push_status = "failed"
            push_error = _command_error("git push", push_result)
            errors.append(push_error)

    if create_pr:
        if push_status == "failed":
            pr_status = "skipped_push_failed"
            pr_error = "push failed; PR was not created"
            errors.append(pr_error)
        else:
            title = (pr_title or "").strip() or message.splitlines()[0]
            body = (pr_body or "").strip()
            pr_result = _run_command(
                run_cmd,
                ["gh", "pr", "create", "--head", branch, "--title", title, "--body", body],
                cwd=repo_path,
            )
            if pr_result.returncode == 0:
                pr_status = "succeeded"
                pr_url = pr_result.stdout.strip().splitlines()[-1].strip() or None
            else:
                pr_status = "failed"
                pr_error = _command_error("gh pr create", pr_result)
                errors.append(pr_error)

    return RepoDraftPublishResult(
        repo_path=repo_path,
        branch=branch,
        changed_paths=publish_changed_paths,
        commit_sha=commit_sha,
        commit_status="created",
        push_status=push_status,
        push_error=push_error,
        pr_status=pr_status,
        pr_url=pr_url,
        pr_error=pr_error,
        errors=errors,
        status=status,
        **_publish_upstream_fields(upstream),
    )
