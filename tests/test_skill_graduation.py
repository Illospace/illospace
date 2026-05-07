"""Tests for agent-drafted skill graduation decisions."""

from __future__ import annotations

import pytest

from brain.systems.skills.graduation import (
    SkillGraduationAction,
    SkillGraduationEvidence,
    SkillGraduationPolicy,
    build_skill_graduation_update,
    evaluate_skill_graduation,
)


VALID_PROCEDURE = (
    "1. Inspect the repeated user request pattern.\n"
    "2. Apply the documented repo-specific workflow.\n"
    "3. Verify the result with focused tests and record the evidence."
)


def _skill(**overrides):
    base = {
        "name": "repo-pr-helper",
        "description": "Prepare repository pull requests with focused tests and review notes",
        "procedure": VALID_PROCEDURE,
        "source_kind": "agent_draft",
        "trust_level": "agent_draft",
        "provisional": True,
        "success_count": 5,
    }
    base.update(overrides)
    return base


def _quality(score=0.82, confidence=0.72, sample_confidence=0.62):
    return {
        "score": score,
        "confidence": confidence,
        "evidence": {
            "count": 12,
            "sample_size_confidence": sample_confidence,
        },
    }


def _evals(count=2):
    return [
        {
            "eval_digest": f"eval-{index}",
            "mode": "hosted_eval",
            "privacy_policy": {
                "include_raw_memory_content": False,
                "include_tenant_identifiers": False,
            },
        }
        for index in range(count)
    ]


def test_agent_draft_ready_for_private_promotion_after_evidence_gates():
    evidence = SkillGraduationEvidence.from_sources(
        _skill(),
        quality_score_payload=_quality(),
        eval_cases=_evals(),
        bundle_validation_passed=True,
    )

    decision = evaluate_skill_graduation(evidence)

    assert decision.action == SkillGraduationAction.READY_FOR_PRIVATE_PROMOTION
    assert decision.eligible is True
    assert decision.promotion_payload["trust_level"] == "private_local"

    update = build_skill_graduation_update(decision, approved_by="admin-1")
    assert update["source_kind"] == "private_local"
    assert update["provisional"] is False
    assert update["graduated_by"] == "admin-1"


def test_hosted_mode_requires_review_before_promotion():
    evidence = SkillGraduationEvidence.from_sources(
        _skill(),
        quality_score_payload=_quality(),
        eval_cases=_evals(),
        bundle_validation_passed=True,
        hosted_mode=True,
        tenant_review_approved=False,
    )

    decision = evaluate_skill_graduation(evidence)

    assert decision.action == SkillGraduationAction.NEEDS_REVIEW
    assert decision.required_review is True
    assert decision.eligible is False


def test_low_quality_or_missing_eval_keeps_skill_in_eval_state():
    evidence = SkillGraduationEvidence.from_sources(
        _skill(success_count=6),
        quality_score_payload=_quality(score=0.55, confidence=0.9),
        eval_cases=_evals(1),
        bundle_validation_passed=True,
    )

    decision = evaluate_skill_graduation(evidence)

    assert decision.action == SkillGraduationAction.NEEDS_EVAL
    assert "not enough redacted eval cases" in decision.blockers
    assert "quality score below graduation threshold" in decision.blockers


def test_permission_escalation_blocks_graduation():
    evidence = SkillGraduationEvidence.from_sources(
        _skill(),
        quality_score_payload=_quality(),
        eval_cases=_evals(),
        bundle_validation_passed=True,
        unresolved_permission_escalation=True,
    )

    decision = evaluate_skill_graduation(evidence)

    assert decision.eligible is False
    assert "unresolved permission escalation" in decision.blockers


def test_non_agent_draft_is_not_reclassified_by_graduation_gate():
    evidence = SkillGraduationEvidence.from_sources(
        _skill(source_kind="private_local", trust_level="private_local", provisional=False),
        quality_score_payload=_quality(),
        eval_cases=_evals(),
        bundle_validation_passed=True,
    )

    decision = evaluate_skill_graduation(evidence)

    assert decision.action == SkillGraduationAction.KEEP_DRAFT
    assert decision.eligible is False


def test_graduation_update_requires_ready_decision():
    decision = evaluate_skill_graduation(
        SkillGraduationEvidence.from_sources(_skill(success_count=0), quality_score_payload=_quality())
    )

    with pytest.raises(ValueError, match="ready promotion"):
        build_skill_graduation_update(decision)
