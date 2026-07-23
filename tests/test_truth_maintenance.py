"""Tests for memory truth-maintenance helpers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from brain.app.api.schemas.memories import MemoryRead
from brain.systems.memory.truth_maintenance import (
    adjudicate_memory_pair,
    async_record_memory_review,
    build_demotion_truth_fields,
    build_policy_truth_fields,
    build_truth_state,
    can_promote_memory,
    filter_truth_safe_memories,
    normalize_memory_claim_metadata,
    quarantine_filter_enabled,
    validate_truth_action_context,
)
from brain.systems.reconstructive_memory.curation import link_memories, supersede_memory


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


async def test_async_record_review_writes_structured_payload():
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
    session.execute = AsyncMock(return_value=result)

    row = await async_record_memory_review(
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
    session.execute.assert_awaited_once()
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


async def test_supersede_memory_marks_old_memory_and_writes_graph_edge():
    session = MagicMock()
    session.scalar = AsyncMock(return_value=None)
    old_node = SimpleNamespace(id=2, visibility="private", content_kind="procedure", org_id=None)
    new_node = SimpleNamespace(id=1, visibility="private", content_kind="procedure", org_id=None)
    curation_source = SimpleNamespace(id=91)
    evidence_span = SimpleNamespace(id=92)

    node_repository = MagicMock()
    node_repository.mark_superseded = AsyncMock()
    assertion_repository = MagicMock()
    assertion_repository.mark_superseded_for_node = AsyncMock()
    edge_repository = MagicMock()
    edge_repository.upsert_edge = AsyncMock(
        return_value=SimpleNamespace(id=90, created_by="agent_curation")
    )

    with (
        patch(
            "brain.systems.reconstructive_memory.curation._visible_nodes_exact",
            new=AsyncMock(side_effect=[[old_node], [old_node, new_node]]),
        ),
        patch(
            "brain.systems.reconstructive_memory.curation._record_curation_source",
            new=AsyncMock(return_value=(curation_source, [evidence_span])),
        ),
        patch(
            "brain.systems.reconstructive_memory.curation.MemoryNodeRepository",
            return_value=node_repository,
        ),
        patch(
            "brain.systems.reconstructive_memory.curation.MemoryAssertionRepository",
            return_value=assertion_repository,
        ),
        patch(
            "brain.systems.reconstructive_memory.curation.MemoryEdgeRepository",
            return_value=edge_repository,
        ),
    ):
        result = await supersede_memory(
            session,
            old_node_id=2,
            new_node_id=1,
            reason="The new memory corrects the previous package-manager guidance.",
            user_id="user-1",
            org_id=None,
        )

    assert result["action"] == "supersede"
    assert result["old_node"] == 2
    assert result["new_node"] == 1
    node_repository.mark_superseded.assert_awaited_once_with(2)
    assertion_repository.mark_superseded_for_node.assert_awaited_once_with(2)
    edge_draft = edge_repository.upsert_edge.await_args.kwargs["draft"]
    assert edge_draft.source_node_id == 2
    assert edge_draft.target_node_id == 1
    assert edge_draft.edge_kind == "superseded_by"
    assert edge_draft.evidence_span_ids == (92,)


async def test_contradiction_edge_links_memories_without_hiding():
    session = MagicMock()
    left_node = SimpleNamespace(id=1, visibility="private", truth_status="unknown")
    right_node = SimpleNamespace(id=2, visibility="private", truth_status="unknown")
    curation_source = SimpleNamespace(id=96)
    evidence_span = SimpleNamespace(id=97)
    edge_repository = MagicMock()
    edge_repository.upsert_edge = AsyncMock(
        return_value=SimpleNamespace(id=95, created_by="agent_curation")
    )

    with (
        patch(
            "brain.systems.reconstructive_memory.curation._visible_nodes_exact",
            new=AsyncMock(return_value=[left_node, right_node]),
        ),
        patch(
            "brain.systems.reconstructive_memory.curation._record_curation_source",
            new=AsyncMock(return_value=(curation_source, [evidence_span])),
        ),
        patch(
            "brain.systems.reconstructive_memory.curation.MemoryEdgeRepository",
            return_value=edge_repository,
        ),
    ):
        result = await link_memories(
            session,
            source_node_id=1,
            target_node_id=2,
            relationship="contradicts",
            reason="The claims conflict and both remain visible pending review.",
            user_id="user-1",
            org_id=None,
        )

    assert result["action"] == "link"
    assert result["relationship"] == "contradicts"
    edge_draft = edge_repository.upsert_edge.await_args.kwargs["draft"]
    assert edge_draft.source_node_id == 1
    assert edge_draft.target_node_id == 2
    assert edge_draft.edge_kind == "contradicts"
    assert edge_draft.evidence_span_ids == (97,)
    assert left_node.truth_status == "unknown"
    assert right_node.truth_status == "unknown"
