"""Tests for cheap hot-path memory conflict scout."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from brain.systems.memory.conflict_scout import (
    has_blocking_context_conflict,
    scout_memory_conflicts,
)


def test_scout_flags_same_subject_different_digest_overlap():
    now = datetime(2026, 4, 25, 12, 0, tzinfo=timezone.utc)
    notices = scout_memory_conflicts(
        [
            {
                "id": 1,
                "content": "The repo uses Flask for the API.",
                "subject_type": "repo",
                "subject_ref": "illo-brain",
                "source_digest": "sha256:old",
                "valid_from": now - timedelta(days=30),
            },
            {
                "id": 2,
                "content": "The repo uses FastAPI for the API.",
                "subject_type": "repo",
                "subject_ref": "illo-brain",
                "source_digest": "sha256:new",
                "valid_from": now - timedelta(days=1),
            },
        ],
        now=now,
    )

    assert len(notices) == 1
    notice = notices[0]
    assert notice.conflict_ids == ("1", "2")
    assert notice.severity == "medium"
    assert notice.recommended_action == "include_both_with_warning"
    assert "same_subject_different_claim_digest" in notice.reasons


def test_scout_prefers_explicit_superseding_memory():
    notices = scout_memory_conflicts(
        [
            {"id": "old", "content": "Use pip.", "superseded_by": "new"},
            {"id": "new", "content": "Use uv."},
        ]
    )

    assert len(notices) == 1
    assert notices[0].severity == "high"
    assert notices[0].recommended_action == "include_one"
    assert notices[0].preferred_memory_id == "new"
    assert has_blocking_context_conflict(notices)


def test_scout_uses_correction_cue_for_same_subject():
    notices = scout_memory_conflicts(
        [
            {
                "id": "older",
                "content": "Use pip to manage Python dependencies.",
                "subject_type": "repo",
                "subject_ref": "illo-brain",
                "valid_from": "2026-03-01T00:00:00Z",
            },
            {
                "id": "newer",
                "content": "Actually use uv instead of pip.",
                "subject_type": "repo",
                "subject_ref": "illo-brain",
                "valid_from": "2026-04-01T00:00:00Z",
            },
        ]
    )

    assert len(notices) == 1
    assert notices[0].preferred_memory_id == "newer"
    assert "explicit_correction_cue" in notices[0].reasons


def test_scout_ignores_non_overlapping_validity_windows():
    notices = scout_memory_conflicts(
        [
            {
                "id": 1,
                "content": "Use Flask.",
                "subject_type": "repo",
                "subject_ref": "illo-brain",
                "source_digest": "sha256:old",
                "valid_from": "2026-01-01T00:00:00Z",
                "valid_until": "2026-02-01T00:00:00Z",
            },
            {
                "id": 2,
                "content": "Use FastAPI.",
                "subject_type": "repo",
                "subject_ref": "illo-brain",
                "source_digest": "sha256:new",
                "valid_from": "2026-03-01T00:00:00Z",
            },
        ]
    )

    assert notices == []


def test_scout_prefers_fresh_source_over_stale_source():
    notices = scout_memory_conflicts(
        [
            {
                "id": "a",
                "content": "The worker entrypoint is worker.py.",
                "subject_type": "repo_file",
                "subject_ref": "brain/worker.py",
                "source_digest": "sha256:a",
            },
            {
                "id": "b",
                "content": "The worker entrypoint is brain/kernel/runtime/worker.py.",
                "subject_type": "repo_file",
                "subject_ref": "brain/worker.py",
                "source_digest": "sha256:b",
            },
        ],
        freshness={"a": "stale", "b": "fresh"},
    )

    assert len(notices) == 1
    assert notices[0].preferred_memory_id == "b"
    assert "source_freshness_mismatch" in notices[0].reasons


def test_scout_accepts_objects_and_legacy_missing_metadata():
    memory = SimpleNamespace(
        id=7,
        content="Legacy memory without subject metadata",
        memory_tier="episodic",
    )

    assert scout_memory_conflicts([memory]) == []
