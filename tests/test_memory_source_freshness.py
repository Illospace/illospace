"""Tests for repo/project memory source freshness signals."""

from __future__ import annotations

import hashlib
import subprocess
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from brain.systems.memory.source_freshness import (
    annotate_source_freshness,
    evaluate_source_freshness,
    evaluate_source_freshness_batch,
)


NOW = datetime(2026, 4, 25, 12, 0, tzinfo=timezone.utc)


def _sha256(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


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


def test_unknown_without_source_metadata():
    result = evaluate_source_freshness({"id": 1}, now=NOW)

    assert result.status == "unknown"
    assert result.reasons == ("no_source_metadata",)
    assert result.checked_refs == ()


def test_expired_valid_until_marks_stale():
    result = evaluate_source_freshness(
        {
            "valid_until": NOW - timedelta(seconds=1),
            "staleness_score": 0.0,
        },
        now=NOW,
    )

    assert result.status == "stale"
    assert "valid_until_expired" in result.reasons
    assert "valid_until" in result.checked_refs


def test_memory_like_object_with_matching_subject_digest_is_fresh(tmp_path):
    source_file = tmp_path / "app.py"
    source_file.write_text("print('fresh')\n")
    digest = _sha256(source_file)
    memory = SimpleNamespace(
        id=7,
        subject_ref=str(source_file),
        source_digest=f"sha256:{digest}",
        observed_at=NOW.isoformat(),
        staleness_score=0.05,
    )

    result = evaluate_source_freshness(memory, now=NOW)

    assert result.status == "fresh"
    assert result.confidence >= 0.9
    assert "source_digest_matches_subject_path" in result.reasons
    assert result.metadata["id"] == 7


def test_subject_digest_mismatch_marks_stale(tmp_path):
    source_file = tmp_path / "app.py"
    source_file.write_text("new contents\n")
    old_digest = hashlib.sha256(b"old contents\n").hexdigest()

    result = evaluate_source_freshness(
        {
            "subject_ref": str(source_file),
            "source_digest": f"sha256:{old_digest}",
        },
        now=NOW,
    )

    assert result.status == "stale"
    assert "source_digest_mismatch" in result.reasons


def test_missing_subject_path_marks_stale(tmp_path):
    result = evaluate_source_freshness(
        {"subject_ref": str(tmp_path / "missing.py")},
        now=NOW,
    )

    assert result.status == "stale"
    assert "subject_path_missing" in result.reasons


def test_elevated_staleness_score_is_possibly_stale():
    result = evaluate_source_freshness({"staleness_score": 0.6}, now=NOW)

    assert result.status == "possibly_stale"
    assert "staleness_score_elevated" in result.reasons


def test_high_staleness_score_is_stale():
    result = evaluate_source_freshness({"staleness_score": 0.91}, now=NOW)

    assert result.status == "stale"
    assert "staleness_score_high" in result.reasons


def test_old_observation_is_possibly_stale_without_stronger_source_evidence():
    result = evaluate_source_freshness(
        {"observed_at": NOW - timedelta(days=45)},
        now=NOW,
    )

    assert result.status == "possibly_stale"
    assert "observed_at_older_than_fresh_window" in result.reasons


def test_reference_digest_map_can_mark_non_path_subject_fresh():
    digest = hashlib.sha256(b"context pack\n").hexdigest()

    result = evaluate_source_freshness(
        {
            "subject_ref": "repo:illo-brain",
            "source_ref": "pack:abc",
            "source_digest": f"sha256:{digest}",
        },
        reference_digests={"repo:illo-brain": digest},
        now=NOW,
    )

    assert result.status == "fresh"
    assert "source_digest_matches_reference" in result.reasons
    assert "subject_ref_not_a_local_path" in result.reasons


def test_commit_source_is_fresh_when_subject_path_unchanged(tmp_path):
    repo = _init_repo(tmp_path)
    source_file = repo / "app.py"
    source_file.write_text("version one\n")
    commit = _commit(repo, "add app")
    (repo / "README.md").write_text("docs changed\n")
    _commit(repo, "add docs")

    result = evaluate_source_freshness(
        {
            "source_kind": "git_commit",
            "source_ref": f"commit:{commit}",
            "subject_ref": "app.py",
        },
        repo_root=repo,
        now=NOW,
    )

    assert result.status == "fresh"
    assert "subject_path_unchanged_since_source_commit" in result.reasons


def test_commit_source_is_stale_when_subject_path_changed(tmp_path):
    repo = _init_repo(tmp_path)
    source_file = repo / "app.py"
    source_file.write_text("version one\n")
    commit = _commit(repo, "add app")
    source_file.write_text("version two\n")
    _commit(repo, "change app")

    result = evaluate_source_freshness(
        {
            "source_kind": "git_commit",
            "source_ref": f"commit:{commit}",
            "subject_ref": "app.py",
        },
        repo_root=repo,
        now=NOW,
    )

    assert result.status == "stale"
    assert "subject_path_changed_since_source_commit" in result.reasons


def test_commit_source_without_subject_path_is_possibly_stale_when_not_head(tmp_path):
    repo = _init_repo(tmp_path)
    (repo / "app.py").write_text("version one\n")
    commit = _commit(repo, "add app")
    (repo / "README.md").write_text("docs changed\n")
    _commit(repo, "add docs")

    result = evaluate_source_freshness(
        {
            "source_kind": "git_commit",
            "source_ref": commit,
        },
        repo_root=repo,
        now=NOW,
    )

    assert result.status == "possibly_stale"
    assert "source_commit_not_head" in result.reasons


def test_batch_evaluates_only_top_k(tmp_path):
    fresh_path = tmp_path / "fresh.py"
    fresh_path.write_text("fresh\n")
    digest = _sha256(fresh_path)
    candidates = [
        {"subject_ref": str(fresh_path), "source_digest": f"sha256:{digest}"},
        {"subject_ref": str(tmp_path / "missing.py")},
        {"valid_until": NOW - timedelta(days=1)},
    ]

    results = evaluate_source_freshness_batch(candidates, top_k=2, now=NOW)

    assert [result.status for result in results] == ["fresh", "stale"]
    assert len(results) == 2


def test_annotate_source_freshness_is_advisory_and_preserves_order(tmp_path):
    source_file = tmp_path / "app.py"
    source_file.write_text("fresh\n")
    digest = _sha256(source_file)
    candidates = [
        {"id": 1, "subject_ref": str(source_file), "source_digest": f"sha256:{digest}"},
        {"id": 2},
    ]

    annotated = annotate_source_freshness(candidates, top_k=1, now=NOW)

    assert [item["id"] for item in annotated] == [1, 2]
    assert annotated[0]["source_freshness"]["status"] == "fresh"
    assert "source_freshness" not in annotated[1]
