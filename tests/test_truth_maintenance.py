"""Tests for memory truth-maintenance helpers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from brain.app.api.schemas.memories import MemoryRead
from brain.systems.memory.truth_maintenance import (
    adjudicate_memory_pair,
    apply_truth_adjudication,
    build_demotion_truth_fields,
    build_policy_truth_fields,
    build_truth_state,
    can_promote_memory,
    filter_truth_safe_memories,
    normalize_memory_claim_metadata,
    quarantine_filter_enabled,
    record_contradiction,
    record_memory_review,
    resolve_contradiction,
    validate_truth_action_context,
)


def _sparse_memory():
    obj = MagicMock()
    obj.id = 7
    obj.content = "Sparse memory payload"
    obj.memory_type = "lesson"
    obj.salience = 4.0
    obj.emotion_valence = 0.1
    obj.emotion_arousal = 0.2
    obj.tags = ["memory"]
    obj.access_count = 3
    obj.last_accessed = datetime.now(timezone.utc) - timedelta(days=2)
    obj.created_at = datetime.now(timezone.utc) - timedelta(days=10)
    obj.scope = "personal"
    obj.visibility = "private"
    obj.user_id = "user-1"
    obj.org_id = None
    return obj


def test_memory_read_backfills_truth_defaults():
    memory = MemoryRead.model_validate(_sparse_memory())

    assert memory.truth_status == "unknown"
    assert memory.review_status == "unreviewed"
    assert memory.memory_tier == "episodic"
    assert memory.confidence == 0.5
    assert memory.freshness_score > 0


def test_normalize_memory_claim_metadata_backfills_legacy_claim_time():
    created_at = datetime(2026, 4, 24, 12, 0, tzinfo=timezone.utc)

    metadata = normalize_memory_claim_metadata(
        {
            "created_at": created_at,
            "source_type": "conversation",
            "source_session": "session-7",
            "freshness_score": 0.8,
        }
    )

    assert metadata["observed_at"] == created_at
    assert metadata["valid_from"] == created_at
    assert metadata["source_kind"] == "conversation"
    assert metadata["source_ref"] == "session-7"
    assert round(metadata["staleness_score"], 3) == 0.2


def test_normalize_memory_claim_metadata_preserves_repo_evidence_fields():
    metadata = normalize_memory_claim_metadata(
        {
            "observed_at": "2026-04-24T12:00:00Z",
            "source_kind": "git_commit",
            "source_ref": "commit:abc123",
            "source_digest": "sha256:context-pack",
            "subject_type": "repo_file",
            "subject_ref": "/worktree/app.py",
            "valid_until": "2026-04-25T12:00:00+00:00",
        }
    )

    assert metadata["source_kind"] == "git_commit"
    assert metadata["source_ref"] == "commit:abc123"
    assert metadata["source_digest"] == "sha256:context-pack"
    assert metadata["subject_type"] == "repo_file"
    assert metadata["subject_ref"] == "/worktree/app.py"
    assert metadata["valid_until"] == datetime(2026, 4, 25, 12, 0, tzinfo=timezone.utc)


def test_record_contradiction_writes_structured_payload():
    session = MagicMock()
    result = MagicMock()
    result.mappings.return_value.first.return_value = {
        "id": 42,
        "left_memory_id": 3,
        "right_memory_id": 9,
        "detected_by": "graph.detect_contradictions",
        "contradiction_type": "valence_conflict",
        "evidence": '{"note":"test"}',
        "severity": 0.8,
        "status": "open",
        "resolution_memory_id": None,
        "created_at": datetime.now(timezone.utc),
        "resolved_at": None,
        "resolved_by": None,
    }
    session.execute.return_value = result

    row = record_contradiction(
        session,
        left_memory_id=9,
        right_memory_id=3,
        contradiction_type="valence_conflict",
        detected_by="graph.detect_contradictions",
        evidence={"note": "test"},
        severity=0.8,
    )

    assert row["id"] == 42
    payload = session.execute.call_args.args[1]
    assert payload["left_memory_id"] == 3
    assert payload["right_memory_id"] == 9
    assert "note" in payload["evidence"]


def test_record_review_writes_structured_payload():
    session = MagicMock()
    result = MagicMock()
    result.mappings.return_value.first.return_value = {
        "id": 11,
        "memory_id": 5,
        "action": "promote",
        "from_tier": "episodic",
        "to_tier": "semantic",
        "reviewer_id": None,
        "rationale": "Consolidated repeated evidence",
        "evidence": '{"support_count": 3}',
        "created_at": datetime.now(timezone.utc),
    }
    session.execute.return_value = result

    row = record_memory_review(
        session,
        memory_id=5,
        action="promote",
        from_tier="episodic",
        to_tier="semantic",
        reviewer_id=None,
        rationale="Consolidated repeated evidence",
        evidence={"support_count": 3},
        confidence=0.82,
    )

    assert row["id"] == 11
    payload = session.execute.call_args.args[1]
    assert payload["memory_id"] == 5
    assert payload["action"] == "promote"
    assert "support_count" in payload["evidence"]
    assert "confidence" in payload["evidence"]


def test_conservative_truth_filter_suppresses_only_quarantined_or_expired(monkeypatch):
    monkeypatch.setenv("MEMORY_QUARANTINE_FILTER_ENABLED", "1")
    now = datetime.now(timezone.utc)
    memories = [
        {"id": 1, "truth_status": "reviewed", "review_status": "reviewed", "valid_until": None},
        {"id": 2, "truth_status": "quarantined", "review_status": "rejected", "valid_until": None},
        {"id": 3, "truth_status": "reviewed", "review_status": "reviewed", "valid_until": now - timedelta(days=1)},
    ]

    filtered = filter_truth_safe_memories(memories)

    assert [item["id"] for item in filtered] == [1]


def test_policy_promotion_requires_multi_source_or_review():
    blocked = can_promote_memory(
        from_tier="procedural",
        to_tier="policy",
        confidence=0.85,
        evidence={"support_count": 3},
        support_count=3,
        reviewed=False,
        policy_kind="runtime",
        policy_scope="checkout_flow",
    )
    allowed = can_promote_memory(
        from_tier="procedural",
        to_tier="policy",
        confidence=0.9,
        evidence={"support_count": 4},
        support_count=4,
        reviewed=False,
        adjudication={
            "relation": "supports",
            "action": "none",
            "confidence": 0.86,
            "severity": 0.0,
            "rationale": "No contradiction found",
            "evidence": ["supporting sources agree"],
        },
        policy_kind="runtime",
        policy_scope="checkout_flow",
    )
    reviewed = can_promote_memory(
        from_tier="procedural",
        to_tier="policy",
        confidence=0.88,
        evidence={"human_review": True},
        support_count=3,
        reviewed=True,
        policy_kind="runtime",
        policy_scope="checkout_flow",
    )

    assert blocked[0] is False
    assert allowed[0] is True
    assert reviewed[0] is True


def test_automated_promotion_requires_semantic_adjudication():
    blocked = can_promote_memory(
        from_tier="episodic",
        to_tier="semantic",
        confidence=0.9,
        evidence={"support_count": 3},
        support_count=3,
        reviewed=False,
    )

    assert blocked[0] is False
    assert "semantic adjudication" in blocked[1]


def test_policy_truth_fields_stay_tentative_without_support():
    fields = build_policy_truth_fields(
        source_kind="consolidation",
        source_ref="skill:checkout",
        confidence=0.9,
        evidence={"support_count": 1},
        support_count=1,
        policy_kind="runtime",
        policy_scope="checkout_flow",
    )

    assert fields["truth_status"] == "tentative"
    assert fields["review_status"] == "unreviewed"
    assert fields["policy_kind"] == "runtime"
    assert fields["policy_scope"] == "checkout_flow"


def test_demotion_fields_quarantine_memory():
    fields = build_demotion_truth_fields(
        reason="open contradiction",
        confidence=0.91,
        evidence={"contradiction_id": 42},
        reviewed_by="user-1",
    )

    assert fields["truth_status"] == "quarantined"
    assert fields["review_status"] == "rejected"
    assert fields["demoted_at"] is not None
    assert fields["valid_until"] is not None


def test_truth_state_marks_open_contradictions_as_unreviewed_active():
    state = build_truth_state(
        {
            "memory_tier": "policy",
            "truth_status": "reviewed",
            "review_status": "reviewed",
            "policy_kind": "runtime",
            "policy_scope": "checkout_flow",
            "open_contradiction_count": 1,
            "resolved_contradiction_count": 2,
        }
    )

    assert state["contradiction_status"] == "open"
    assert state["has_open_contradiction"] is True
    assert state["is_reviewed_active"] is False
    assert state["is_policy_effective"] is False


def test_resolve_contradiction_updates_resolution_payload():
    session = MagicMock()
    result = MagicMock()
    result.mappings.return_value.first.return_value = {
        "id": 42,
        "left_memory_id": 3,
        "right_memory_id": 9,
        "detected_by": "graph.detect_contradictions",
        "contradiction_type": "valence_conflict",
        "evidence": '{"note":"resolved"}',
        "severity": 0.8,
        "status": "resolved",
        "resolution_memory_id": 11,
        "created_at": datetime.now(timezone.utc),
        "resolved_at": datetime.now(timezone.utc),
        "resolved_by": "user-1",
    }
    session.execute.return_value = result

    row = resolve_contradiction(
        session,
        contradiction_id=42,
        resolution_memory_id=11,
        resolved_by="user-1",
        evidence={"note": "resolved"},
    )

    assert row["status"] == "resolved"
    payload = session.execute.call_args.args[1]
    assert payload["contradiction_id"] == 42
    assert payload["resolution_memory_id"] == 11


def test_truth_safe_recall_filter_is_enabled_by_default(monkeypatch):
    monkeypatch.delenv("MEMORY_QUARANTINE_FILTER_ENABLED", raising=False)

    assert quarantine_filter_enabled() is True


def test_truth_action_requires_evidence_and_confidence():
    try:
        validate_truth_action_context(action="review", evidence={}, confidence=None)
    except ValueError as exc:
        assert "confidence" in str(exc)
    else:
        raise AssertionError("expected missing confidence to fail")

    try:
        validate_truth_action_context(action="review", evidence={}, confidence=0.7)
    except ValueError as exc:
        assert "evidence" in str(exc)
    else:
        raise AssertionError("expected missing evidence to fail")


def test_correction_adjudication_supersedes_old_memory():
    decision = adjudicate_memory_pair(
        candidate_content="Actually use pnpm instead of npm for frontend installs.",
        existing_content="Use npm for frontend installs.",
        candidate_evidence={"quote": "Actually use pnpm instead of npm"},
        candidate_confidence=0.91,
    )

    assert decision["relation"] == "supersedes"
    assert decision["action"] == "supersede_existing"
    assert decision["is_high_confidence_conflict"] is True


def test_high_confidence_correction_quarantines_old_memory():
    session = MagicMock()
    record_result = MagicMock()
    record_result.mappings.return_value.first.return_value = {
        "id": 90,
        "left_memory_id": 1,
        "right_memory_id": 2,
        "detected_by": "truth_maintenance.adjudication",
        "contradiction_type": "semantic_supersession",
        "evidence": "{}",
        "severity": 0.91,
        "status": "open",
        "resolution_memory_id": None,
        "created_at": datetime.now(timezone.utc),
        "resolved_at": None,
        "resolved_by": None,
    }
    review_result = MagicMock()
    review_result.mappings.return_value.first.return_value = {
        "id": 91,
        "memory_id": 2,
        "action": "quarantine",
        "from_tier": "unknown",
        "to_tier": "unknown",
        "reviewer_id": "user-1",
        "rationale": "superseded",
        "evidence": "{}",
        "created_at": datetime.now(timezone.utc),
    }
    session.execute.side_effect = [
        record_result,
        MagicMock(),
        review_result,
        MagicMock(),
    ]

    with patch("brain.systems.memory.truth_maintenance._mark_memory_dependents_stale") as mock_stale:
        result = apply_truth_adjudication(
            session,
            candidate_memory_id=1,
            existing_memory_id=2,
            adjudication={
                "relation": "supersedes",
                "action": "supersede_existing",
                "confidence": 0.91,
                "severity": 0.9,
                "rationale": "superseded",
                "evidence": ["Actually use pnpm instead of npm"],
            },
            candidate_confidence=0.91,
            candidate_evidence={"quote": "Actually use pnpm instead of npm"},
            reviewer_id="user-1",
        )

    assert result["action_taken"] == "superseded_existing"
    mock_stale.assert_called_once_with(session, [2], "superseded")
    sql_texts = [call.args[0].text for call in session.execute.call_args_list if hasattr(call.args[0], "text")]
    assert any("superseded_by" in sql for sql in sql_texts)


def test_low_confidence_contradiction_records_without_hiding():
    session = MagicMock()
    record_result = MagicMock()
    record_result.mappings.return_value.first.return_value = {
        "id": 95,
        "left_memory_id": 1,
        "right_memory_id": 2,
        "detected_by": "truth_maintenance.adjudication",
        "contradiction_type": "semantic_conflict",
        "evidence": "{}",
        "severity": 0.55,
        "status": "needs_review",
        "resolution_memory_id": None,
        "created_at": datetime.now(timezone.utc),
        "resolved_at": None,
        "resolved_by": None,
    }
    session.execute.return_value = record_result

    result = apply_truth_adjudication(
        session,
        candidate_memory_id=1,
        existing_memory_id=2,
        adjudication={
            "relation": "contradicts",
            "action": "needs_review",
            "confidence": 0.55,
            "severity": 0.55,
            "rationale": "weak semantic conflict",
            "evidence": ["weak"],
        },
        candidate_confidence=0.55,
        candidate_evidence={"quote": "maybe not"},
    )

    assert result["action_taken"] == "recorded_for_review"
    assert session.execute.call_count == 1
    payload = session.execute.call_args.args[1]
    assert payload["status"] == "needs_review"
