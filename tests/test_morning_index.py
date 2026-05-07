from __future__ import annotations

import json
from datetime import datetime, timezone

from brain.systems.learning.morning_index import (
    build_morning_index,
    build_morning_source_manifest,
    is_morning_index_current,
    morning_index_invalidated,
)

NOW = datetime(2026, 4, 25, 12, 0, tzinfo=timezone.utc)


def _repo_summary(source_digest: str = "sha256:repo-a") -> dict:
    return {
        "schema_version": "repo-summary/v1",
        "summary_id": "summary-current",
        "summary_kind": "architecture",
        "status": "current",
        "lifecycle_status": "current",
        "generated_at": NOW.isoformat(),
        "source_digest": source_digest,
        "summary_identity": {
            "scope_digest": "sha256:scope-a",
            "identity_digest": "sha256:identity-a",
            "repo_root": "/repo/illo-brain",
            "branch": "codex/test",
            "commit_sha": "abc123",
            "source_digest": source_digest,
            "summary_kind": "architecture",
        },
        "hot_path": {
            "repo_name": "illo-brain",
            "repo_root": "/repo/illo-brain",
            "branch": "codex/test",
            "commit_sha": "abc123",
            "summary_kind": "architecture",
            "path_globs": ["brain/**/*.py"],
            "source_digest": source_digest,
            "source_digest_complete": True,
            "file_count": 12,
            "byte_count": 3456,
            "sample_paths": ["brain/systems/learning/morning_index.py"],
            "top_directories": [{"name": "brain", "files": 12}],
            "extensions": [{"extension": ".py", "files": 12}],
            "largest_files": [{"path": "brain/systems/learning/morning_index.py", "size_bytes": 1200}],
        },
        "content": "This long generated prose should stay off the hot path.",
        "summary_prose": "Also do not include this text.",
    }


def test_morning_index_builds_compact_scope_hints_without_long_prose():
    index = build_morning_index(
        scope_id="repo:illo-brain",
        scope_type="repo",
        repo_summaries=[
            _repo_summary(),
            {
                **_repo_summary("sha256:old"),
                "summary_id": "summary-historical",
                "status": "historical",
                "lifecycle_status": "historical",
            },
        ],
        stale_memories=[
            {
                "id": "mem-old",
                "content": "A stale memory body must not leak.",
                "source_digest": "sha256:memory-old",
                "freshness_status": "stale",
                "freshness_confidence": 0.91,
                "confidence": 0.2,
            },
            {
                "id": "mem-fresh",
                "content": "Fresh memory body",
                "source_digest": "sha256:memory-fresh",
                "freshness_status": "fresh",
            },
        ],
        skill_pins={
            "implement": [
                {
                    "rank": 1,
                    "name": "repo-edit",
                    "effective_digest": "sha256:skill-edit",
                    "pin_strength": 0.9,
                    "trust_level": "private_local",
                    "pin_reasons": ["best recent implementation fit"],
                    "procedure": "Long skill body should not appear.",
                }
            ],
        },
        review_items=[
            {
                "id": "review-1",
                "risk_level": "high",
                "status": "needs_review",
                "severity": "required",
                "source": "verifier",
                "source_digest": "sha256:review-1",
                "target": {"type": "run", "id": "42"},
                "failure_reason": "semantic verifier unavailable for high-risk run",
                "evidence": "Long evidence prose should not appear.",
            },
            {
                "id": "review-2",
                "risk_level": "high",
                "status": "approved",
                "source_digest": "sha256:review-2",
            },
        ],
        generated_at=NOW,
    )

    payload = index.to_payload()
    scope = payload["scopes"][0]

    assert payload["schema_version"] == "morning-index/v1"
    assert payload["generated_at"] == NOW.isoformat()
    assert payload["stats"] == {
        "scope_count": 1,
        "repo_summary_count": 1,
        "known_stale_memory_count": 1,
        "skill_pin_count": 1,
        "unresolved_high_risk_review_count": 1,
        "source_digest_count": 4,
    }
    assert scope["known_stale_memory_ids"] == ["mem-old"]
    assert scope["repo_summaries"][0]["hot_path"]["file_count"] == 12
    assert scope["preferred_skill_pins"]["implement"][0]["name"] == "repo-edit"
    assert scope["context_policy"]["thresholds"]["suppress_confidence_floor"] == 0.78
    assert scope["skill_routing_policy"]["policy_version"] == "skill-quality-routing-v1"
    assert scope["unresolved_high_risk_review_items"][0]["review_id"] == "review-1"

    encoded = json.dumps(payload, sort_keys=True)
    assert "This long generated prose" not in encoded
    assert "A stale memory body" not in encoded
    assert "Long skill body" not in encoded
    assert "Long evidence prose" not in encoded


def test_morning_index_fingerprint_changes_when_source_digest_changes():
    original = build_morning_index(
        scope_id="repo:illo-brain",
        repo_summaries=[_repo_summary("sha256:repo-a")],
    )
    updated = build_morning_index(
        scope_id="repo:illo-brain",
        repo_summaries=[_repo_summary("sha256:repo-b")],
    )

    assert original.source_fingerprint != updated.source_fingerprint
    assert is_morning_index_current(original, source_fingerprint=original.source_fingerprint)
    assert morning_index_invalidated(original, source_fingerprint=updated.source_fingerprint)


def test_morning_index_fingerprint_changes_when_policy_version_changes():
    index = build_morning_index(
        scope_id="repo:illo-brain",
        repo_summaries=[_repo_summary()],
        context_policy={"policy_version": "active-context-policy-v1", "thresholds": {}},
        skill_routing_policy={"policy_version": "skill-quality-routing-v1"},
    )
    changed_manifest = build_morning_source_manifest(
        index.scopes,
        context_policy={"policy_version": "active-context-policy-v2", "thresholds": {}},
        skill_routing_policy={"policy_version": "skill-quality-routing-v1"},
    )

    assert morning_index_invalidated(index, source_manifest=changed_manifest)


def test_morning_index_groups_and_limits_skill_pins_by_task_class():
    index = build_morning_index(
        scope_id="repo:illo-brain",
        skill_pins={
            "review": [
                {"rank": 3, "name": "third", "effective_digest": "sha256:third", "score": 0.9},
                {"rank": 1, "name": "first", "effective_digest": "sha256:first", "score": 0.6},
                {"rank": 2, "name": "second", "effective_digest": "sha256:second", "score": 0.7},
                {"rank": 4, "name": "fourth", "effective_digest": "sha256:fourth", "score": 1.0},
            ],
            "debug": [
                {"rank": 1, "name": "investigate", "effective_digest": "sha256:investigate"},
            ],
        },
    )

    pins = index.to_payload()["scopes"][0]["preferred_skill_pins"]
    assert [pin["name"] for pin in pins["review"]] == ["first", "second", "third"]
    assert pins["investigate"][0]["task_class"] == "investigate"
