from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import subprocess


class FakeRunner:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[tuple[list[str], Path]] = []

    def __call__(self, command, *, cwd):
        self.calls.append((list(command), Path(cwd)))
        if not self.responses:
            raise AssertionError(f"unexpected command: {command}")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return SimpleNamespace(returncode=response[0], stdout=response[1], stderr=response[2])

    @property
    def commands(self):
        return [command for command, _cwd in self.calls]


def test_repo_draft_status_parses_changed_paths_and_unmerged_paths(tmp_path):
    from brain.systems.cortex.project_context.repo_publish import repo_draft_status

    runner = FakeRunner(
        [
            (
                0,
                " M README.md\n?? docs/new.md\nUU conflict.md\nR  old.md -> renamed.md\n",
                "",
            )
        ]
    )

    status = repo_draft_status(tmp_path, run_cmd=runner)

    assert status.changed_paths == ["README.md", "docs/new.md", "conflict.md", "renamed.md"]
    assert status.unmerged_paths == ["conflict.md"]
    assert status.has_changes is True
    assert status.has_conflicts is True
    assert status.ok is False
    assert runner.commands == [["git", "status", "--porcelain"]]


def test_repo_draft_status_scopes_mount_subpath_from_repo_root(tmp_path):
    from brain.systems.cortex.project_context.repo_publish import repo_draft_status

    mounted = tmp_path / "docs"
    mounted.mkdir()
    runner = FakeRunner(
        [
            (0, f"{tmp_path}\n", ""),
            (0, " M docs/README.md\nUU docs/conflict.md\n M outside.md\n", ""),
        ]
    )

    status = repo_draft_status(mounted, mount_subpath="docs", run_cmd=runner)

    assert status.changed_paths == ["README.md", "conflict.md"]
    assert status.unmerged_paths == ["conflict.md"]
    assert runner.commands == [
        ["git", "rev-parse", "--show-toplevel"],
        ["git", "status", "--porcelain", "--", "docs"],
    ]
    assert runner.calls[0][1] == mounted
    assert runner.calls[1][1] == tmp_path


def test_repo_draft_upstream_status_fetches_base_branch_and_detects_overlap(tmp_path):
    from brain.systems.cortex.project_context.repo_publish import repo_draft_upstream_status

    runner = FakeRunner(
        [
            (0, "", ""),
            (0, "base123\n", ""),
            (0, "README.md\ndocs/notes.md\n", ""),
        ]
    )

    status = repo_draft_upstream_status(
        tmp_path,
        changed_paths=["README.md", "app.py"],
        base_branch="main",
        run_cmd=runner,
    )

    assert status.ok is False
    assert status.status == "conflicted"
    assert status.fetch_status == "succeeded"
    assert status.base_commit == "base123"
    assert status.upstream_changed_paths == ["README.md", "docs/notes.md"]
    assert status.upstream_conflicted_paths == ["README.md"]
    assert status.errors == []
    assert runner.commands == [
        ["git", "fetch", "origin", "+refs/heads/main:refs/remotes/origin/main"],
        ["git", "rev-parse", "HEAD"],
        ["git", "diff", "--name-only", "base123..refs/remotes/origin/main", "--"],
    ]


def test_repo_draft_upstream_status_scopes_mount_subpath_diff(tmp_path):
    from brain.systems.cortex.project_context.repo_publish import repo_draft_upstream_status

    mounted = tmp_path / "docs"
    mounted.mkdir()
    runner = FakeRunner(
        [
            (0, f"{tmp_path}\n", ""),
            (0, "", ""),
            (0, "base123\n", ""),
            (0, "docs/README.md\ndocs/notes.md\noutside.md\n", ""),
        ]
    )

    status = repo_draft_upstream_status(
        mounted,
        changed_paths=["README.md", "app.py"],
        base_branch="main",
        mount_subpath="docs",
        run_cmd=runner,
    )

    assert status.status == "conflicted"
    assert status.upstream_changed_paths == ["README.md", "notes.md"]
    assert status.upstream_conflicted_paths == ["README.md"]
    assert runner.commands == [
        ["git", "rev-parse", "--show-toplevel"],
        ["git", "fetch", "origin", "+refs/heads/main:refs/remotes/origin/main"],
        ["git", "rev-parse", "HEAD"],
        ["git", "diff", "--name-only", "base123..refs/remotes/origin/main", "--", "docs"],
    ]
    assert [cwd for _command, cwd in runner.calls] == [mounted, tmp_path, tmp_path, tmp_path]


def test_publish_repo_draft_creates_branch_stages_commits_and_reads_commit_sha(tmp_path):
    from brain.systems.cortex.project_context.repo_publish import publish_repo_draft

    runner = FakeRunner(
        [
            (0, " M README.md\n?? docs/new.md\n D old.md\n", ""),
            (0, "", ""),
            (0, "", ""),
            (0, "[illo/publish abc123] Publish draft\n", ""),
            (0, "abc123def\n", ""),
        ]
    )

    result = publish_repo_draft(
        tmp_path,
        branch_name="illo/publish-project-draft",
        commit_message="Publish draft",
        check_upstream=False,
        run_cmd=runner,
    )

    assert result.ok is True
    assert result.branch == "illo/publish-project-draft"
    assert result.changed_paths == ["README.md", "docs/new.md", "old.md"]
    assert result.commit_sha == "abc123def"
    assert result.commit_status == "created"
    assert result.push_status == "not_requested"
    assert result.pr_status == "not_requested"
    assert runner.commands == [
        ["git", "status", "--porcelain"],
        ["git", "checkout", "-B", "illo/publish-project-draft"],
        ["git", "add", "-A"],
        ["git", "commit", "-m", "Publish draft"],
        ["git", "rev-parse", "HEAD"],
    ]


def test_publish_repo_draft_stages_only_selected_paths(tmp_path):
    from brain.systems.cortex.project_context.repo_publish import publish_repo_draft

    runner = FakeRunner(
        [
            (0, " M a.py\n M b.py\n", ""),
            (0, "", ""),
            (0, "", ""),
            (0, "[illo/publish abc123] Publish draft\n", ""),
            (0, "abc123def\n", ""),
        ]
    )

    result = publish_repo_draft(
        tmp_path,
        branch_name="illo/publish-project-draft",
        commit_message="Publish draft",
        selected_paths=["a.py"],
        check_upstream=False,
        run_cmd=runner,
    )

    assert result.ok is True
    assert result.changed_paths == ["a.py"]
    assert result.commit_sha == "abc123def"
    assert runner.commands == [
        ["git", "status", "--porcelain"],
        ["git", "checkout", "-B", "illo/publish-project-draft"],
        ["git", "add", "--", "a.py"],
        ["git", "commit", "-m", "Publish draft", "--", "a.py"],
        ["git", "rev-parse", "HEAD"],
    ]


def test_publish_repo_draft_interprets_selected_paths_inside_mount_subpath(tmp_path):
    from brain.systems.cortex.project_context.repo_publish import publish_repo_draft

    mounted = tmp_path / "docs"
    mounted.mkdir()
    runner = FakeRunner(
        [
            (0, f"{tmp_path}\n", ""),
            (0, f"{tmp_path}\n", ""),
            (0, " M docs/a.py\n M docs/b.py\n M outside.py\n", ""),
            (0, "", ""),
            (0, "", ""),
            (0, "[illo/publish abc123] Publish draft\n", ""),
            (0, "abc123def\n", ""),
        ]
    )

    result = publish_repo_draft(
        mounted,
        branch_name="illo/publish-project-draft",
        commit_message="Publish draft",
        selected_paths=["a.py"],
        check_upstream=False,
        mount_subpath="docs",
        run_cmd=runner,
    )

    assert result.ok is True
    assert result.changed_paths == ["a.py"]
    assert result.commit_sha == "abc123def"
    assert runner.commands == [
        ["git", "rev-parse", "--show-toplevel"],
        ["git", "rev-parse", "--show-toplevel"],
        ["git", "status", "--porcelain", "--", "docs"],
        ["git", "checkout", "-B", "illo/publish-project-draft"],
        ["git", "add", "--", "docs/a.py"],
        ["git", "commit", "-m", "Publish draft", "--", "docs/a.py"],
        ["git", "rev-parse", "HEAD"],
    ]
    assert [cwd for _command, cwd in runner.calls] == [
        mounted,
        mounted,
        tmp_path,
        tmp_path,
        tmp_path,
        tmp_path,
        tmp_path,
    ]


def test_publish_repo_draft_returns_error_when_selected_paths_match_no_changes(tmp_path):
    from brain.systems.cortex.project_context.repo_publish import publish_repo_draft

    runner = FakeRunner([(0, " M a.py\n M b.py\n", "")])

    result = publish_repo_draft(
        tmp_path,
        branch_name="illo/publish-project-draft",
        commit_message="Publish draft",
        selected_paths=["c.py"],
        check_upstream=False,
        run_cmd=runner,
    )

    assert result.ok is False
    assert result.commit_sha is None
    assert result.changed_paths == []
    assert result.commit_status == "skipped_no_matching_paths"
    assert result.errors == ["selected_paths do not match changed paths: c.py"]
    assert runner.commands == [["git", "status", "--porcelain"]]


def test_publish_repo_draft_checks_upstream_and_publishes_non_overlapping_changes(tmp_path):
    from brain.systems.cortex.project_context.repo_publish import publish_repo_draft

    runner = FakeRunner(
        [
            (0, " M app.py\n", ""),
            (0, "", ""),
            (0, "base123\n", ""),
            (0, "README.md\ndocs/notes.md\n", ""),
            (0, "", ""),
            (0, "", ""),
            (0, "[illo/publish abc123] Publish draft\n", ""),
            (0, "abc123def\n", ""),
        ]
    )

    result = publish_repo_draft(
        tmp_path,
        branch_name="illo/publish-project-draft",
        commit_message="Publish draft",
        run_cmd=runner,
    )

    assert result.ok is True
    assert result.commit_sha == "abc123def"
    assert result.upstream_status == "changed"
    assert result.upstream_changed_paths == ["README.md", "docs/notes.md"]
    assert result.upstream_conflicted_paths == []
    assert result.upstream_errors == []
    assert runner.commands == [
        ["git", "status", "--porcelain"],
        ["git", "fetch", "origin", "+refs/heads/main:refs/remotes/origin/main"],
        ["git", "rev-parse", "HEAD"],
        ["git", "diff", "--name-only", "base123..refs/remotes/origin/main", "--"],
        ["git", "checkout", "-B", "illo/publish-project-draft", "origin/main"],
        ["git", "add", "-A"],
        ["git", "commit", "-m", "Publish draft"],
        ["git", "rev-parse", "HEAD"],
    ]


def test_publish_repo_draft_blocks_upstream_overlap_before_mutating(tmp_path):
    from brain.systems.cortex.project_context.repo_publish import publish_repo_draft

    runner = FakeRunner(
        [
            (0, " M README.md\n M app.py\n", ""),
            (0, "", ""),
            (0, "base123\n", ""),
            (0, "README.md\ndocs/notes.md\n", ""),
        ]
    )

    result = publish_repo_draft(
        tmp_path,
        branch_name="illo/publish-project-draft",
        commit_message="Publish draft",
        run_cmd=runner,
    )

    assert result.ok is False
    assert result.commit_sha is None
    assert result.changed_paths == ["README.md", "app.py"]
    assert result.upstream_status == "conflicted"
    assert result.upstream_changed_paths == ["README.md", "docs/notes.md"]
    assert result.upstream_conflicted_paths == ["README.md"]
    assert result.errors == ["upstream changes overlap draft paths: README.md"]
    assert runner.commands == [
        ["git", "status", "--porcelain"],
        ["git", "fetch", "origin", "+refs/heads/main:refs/remotes/origin/main"],
        ["git", "rev-parse", "HEAD"],
        ["git", "diff", "--name-only", "base123..refs/remotes/origin/main", "--"],
    ]


def test_publish_repo_draft_refuses_unmerged_paths_before_mutating(tmp_path):
    from brain.systems.cortex.project_context.repo_publish import publish_repo_draft

    runner = FakeRunner([(0, "UU conflict.md\n M ok.md\n", "")])

    result = publish_repo_draft(
        tmp_path,
        branch_name="illo/publish-project-draft",
        commit_message="Publish draft",
        check_upstream=False,
        run_cmd=runner,
    )

    assert result.ok is False
    assert result.commit_sha is None
    assert result.changed_paths == ["conflict.md", "ok.md"]
    assert result.errors == ["repo draft has unmerged paths"]
    assert result.status is not None
    assert result.status.unmerged_paths == ["conflict.md"]
    assert runner.commands == [["git", "status", "--porcelain"]]


def test_publish_repo_draft_pushes_and_creates_pr_when_requested(tmp_path):
    from brain.systems.cortex.project_context.repo_publish import publish_repo_draft

    runner = FakeRunner(
        [
            (0, " M README.md\n", ""),
            (0, "", ""),
            (0, "", ""),
            (0, "[illo/publish abc123] Publish draft\n", ""),
            (0, "abc123def\n", ""),
            (0, "branch pushed\n", ""),
            (0, "https://github.test/org/repo/pull/1\n", ""),
        ]
    )

    result = publish_repo_draft(
        tmp_path,
        branch_name="illo/publish-project-draft",
        commit_message="Publish draft",
        create_pr=True,
        pr_title="Publish Project draft",
        pr_body="Generated from a Project repo draft workspace.",
        check_upstream=False,
        run_cmd=runner,
    )

    assert result.ok is True
    assert result.commit_sha == "abc123def"
    assert result.push_status == "succeeded"
    assert result.pr_status == "succeeded"
    assert result.pr_url == "https://github.test/org/repo/pull/1"
    assert runner.commands == [
        ["git", "status", "--porcelain"],
        ["git", "checkout", "-B", "illo/publish-project-draft"],
        ["git", "add", "-A"],
        ["git", "commit", "-m", "Publish draft"],
        ["git", "rev-parse", "HEAD"],
        ["git", "push", "-u", "origin", "illo/publish-project-draft"],
        [
            "gh",
            "pr",
            "create",
            "--head",
            "illo/publish-project-draft",
            "--title",
            "Publish Project draft",
            "--body",
            "Generated from a Project repo draft workspace.",
        ],
    ]


def test_publish_repo_draft_returns_optional_push_errors_without_throwing(tmp_path):
    from brain.systems.cortex.project_context.repo_publish import publish_repo_draft

    runner = FakeRunner(
        [
            (0, " M README.md\n", ""),
            (0, "", ""),
            (0, "", ""),
            (0, "[illo/publish abc123] Publish draft\n", ""),
            (0, "abc123def\n", ""),
            (1, "", "permission denied"),
        ]
    )

    result = publish_repo_draft(
        tmp_path,
        branch_name="illo/publish-project-draft",
        commit_message="Publish draft",
        push=True,
        check_upstream=False,
        run_cmd=runner,
    )

    assert result.commit_sha == "abc123def"
    assert result.push_status == "failed"
    assert result.push_error == "git push failed: permission denied"
    assert result.errors == ["git push failed: permission denied"]


def test_publish_repo_draft_refuses_protected_publish_branch(tmp_path):
    from brain.systems.cortex.project_context.repo_publish import publish_repo_draft

    runner = FakeRunner([])

    result = publish_repo_draft(
        tmp_path,
        branch_name="main",
        commit_message="Publish draft",
        run_cmd=runner,
    )

    assert result.ok is False
    assert result.errors == ["refusing to publish directly to protected branch: main"]
    assert runner.commands == []


def test_publish_repo_draft_with_mount_subpath_does_not_commit_sibling_changes(tmp_path):
    from brain.systems.cortex.project_context.repo_publish import publish_repo_draft

    repo = tmp_path / "repo"
    docs = repo / "docs"
    other = repo / "other"
    docs.mkdir(parents=True)
    other.mkdir()

    def git(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=repo,
            text=True,
            capture_output=True,
            check=True,
        )

    git("init", "-b", "main")
    git("config", "user.email", "tests@example.com")
    git("config", "user.name", "Project Tests")
    (docs / "guide.md").write_text("base docs\n", encoding="utf-8")
    (other / "notes.md").write_text("base other\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-m", "initial")

    (docs / "guide.md").write_text("draft docs\n", encoding="utf-8")
    (other / "notes.md").write_text("unrelated other\n", encoding="utf-8")
    git("add", "other/notes.md")

    result = publish_repo_draft(
        repo,
        branch_name="illo/project-docs",
        commit_message="Publish docs",
        check_upstream=False,
        mount_subpath="docs",
    )

    assert result.ok is True
    assert result.changed_paths == ["guide.md"]
    assert result.commit_sha
    committed_paths = git("show", "--name-only", "--format=", result.commit_sha).stdout.splitlines()
    assert committed_paths == ["docs/guide.md"]
    assert git("status", "--porcelain").stdout == "M  other/notes.md\n"
