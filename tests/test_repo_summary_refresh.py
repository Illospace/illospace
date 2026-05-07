"""Tests for deterministic repo-summary refresh payloads."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone

from brain.systems.memory.repo_summary import refresh_repo_summaries
from brain.jobs.pipelines.nightly_repo_refresh import run_nightly_repo_refresh


NOW = datetime(2026, 4, 25, 12, 0, tzinfo=timezone.utc)


def _git(repo, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return result.stdout.strip()


def _init_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    return repo


def _commit(repo, message: str) -> str:
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _write(repo, rel_path: str, contents: str) -> None:
    path = repo / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents)


def test_refresh_creates_current_summary_identity_and_pending_prose_action(tmp_path):
    repo = _init_repo(tmp_path)
    _write(repo, "brain/systems/memory/app.py", "def app():\n    return 'fresh'\n")
    commit = _commit(repo, "add app")

    result = refresh_repo_summaries(
        repo_root=repo,
        path_globs=["brain/systems/memory/*.py"],
        include_prose=True,
        now=NOW,
    )

    summary = result["current_summaries"][0]
    assert summary["action"] == "created"
    assert summary["repo_root"] == repo.resolve().as_posix()
    assert summary["commit_sha"] == commit
    assert summary["path_globs"] == ["brain/systems/memory/*.py"]
    assert summary["source_digest"].startswith("sha256:")
    assert summary["summary_identity"]["repo_root"] == repo.resolve().as_posix()
    assert summary["summary_identity"]["branch"] == _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    assert summary["summary_identity"]["commit_sha"] == commit
    assert summary["summary_identity"]["path_globs"] == ["brain/systems/memory/*.py"]
    assert summary["summary_identity"]["source_digest"] == summary["source_digest"]
    assert summary["architecture"]["file_count"] == 1
    assert summary["architecture"]["sample_paths"] == ["brain/systems/memory/app.py"]
    assert summary["requires_model"] is False
    assert summary["prose_status"] == "pending_model"
    assert any(action["reason"] == "architecture_prose_requires_model" for action in result["actions"])


def test_refresh_reuses_unchanged_previous_summary(tmp_path):
    repo = _init_repo(tmp_path)
    _write(repo, "brain/systems/memory/app.py", "VALUE = 1\n")
    _commit(repo, "add app")
    first = refresh_repo_summaries(
        repo_root=repo,
        path_globs=["brain/systems/memory/*.py"],
        now=NOW,
    )["current_summaries"][0]

    result = refresh_repo_summaries(
        repo_root=repo,
        path_globs=["brain/systems/memory/*.py"],
        previous_summaries=[first],
        now=NOW,
    )

    summary = result["current_summaries"][0]
    assert summary["action"] == "unchanged"
    assert summary["refresh_reason"] == "source_digest_matches"
    assert summary["summary_id"] == first["summary_id"]
    assert result["historical_summaries"] == []
    assert result["stats"]["unchanged"] == 1
    assert summary["source_freshness"]["status"] == "fresh"


def test_changed_scoped_file_refreshes_and_marks_previous_historical(tmp_path):
    repo = _init_repo(tmp_path)
    _write(repo, "brain/systems/memory/app.py", "VALUE = 1\n")
    _commit(repo, "add app")
    first = refresh_repo_summaries(
        repo_root=repo,
        path_globs=["brain/systems/memory/*.py"],
        now=NOW,
    )["current_summaries"][0]

    _write(repo, "brain/systems/memory/app.py", "VALUE = 2\n")
    _commit(repo, "change app")
    result = refresh_repo_summaries(
        repo_root=repo,
        path_globs=["brain/systems/memory/*.py"],
        previous_summaries=[first],
        now=NOW,
    )

    summary = result["current_summaries"][0]
    historical = result["historical_summaries"][0]
    assert summary["action"] == "refreshed"
    assert summary["refresh_reason"] == "source_digest_changed"
    assert summary["source_digest"] != first["source_digest"]
    assert historical["summary_id"] == first["summary_id"]
    assert historical["source_digest"] == first["source_digest"]
    assert historical["lifecycle_status"] == "historical"
    assert historical["superseded_by"] == summary["summary_id"]
    assert historical["source_freshness"]["status"] == "stale"
    assert result["stats"]["refreshed"] == 1


def test_unrelated_commit_refreshes_identity_without_regenerating_summary(tmp_path):
    repo = _init_repo(tmp_path)
    _write(repo, "brain/systems/memory/app.py", "VALUE = 1\n")
    _commit(repo, "add app")
    first = refresh_repo_summaries(
        repo_root=repo,
        path_globs=["brain/systems/memory/*.py"],
        now=NOW,
    )["current_summaries"][0]

    _write(repo, "README.md", "# Docs only\n")
    _commit(repo, "change docs")
    result = refresh_repo_summaries(
        repo_root=repo,
        path_globs=["brain/systems/memory/*.py"],
        previous_summaries=[first],
        now=NOW,
    )

    summary = result["current_summaries"][0]
    assert summary["source_digest"] == first["source_digest"]
    assert summary["commit_sha"] != first["commit_sha"]
    assert summary["action"] == "metadata_refreshed"
    assert summary["refresh_reason"] == "commit_identity_changed"
    assert summary["summary_reused_from"] == first["summary_id"]
    assert result["refreshed_summaries"] == []
    assert result["historical_summaries"][0]["lifecycle_status"] == "historical"
    assert result["stats"]["metadata_refreshed"] == 1


def test_nightly_repo_refresh_shell_writes_json_payload(tmp_path):
    repo = _init_repo(tmp_path)
    _write(repo, "brain/jobs/pipelines/app.py", "def run():\n    return True\n")
    _commit(repo, "add pipeline")
    output_path = tmp_path / "repo-summary.json"

    result = run_nightly_repo_refresh(
        repo_root=repo,
        path_globs=["brain/jobs/pipelines/*.py"],
        output_json=output_path,
    )

    stored = json.loads(output_path.read_text())
    assert stored["schema_version"] == result["schema_version"]
    assert stored["current_summaries"][0]["path_globs"] == ["brain/jobs/pipelines/*.py"]
    assert stored["current_summaries"][0]["architecture"]["sample_paths"] == [
        "brain/jobs/pipelines/app.py"
    ]
