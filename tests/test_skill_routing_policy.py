"""Tests for advisory skill quality routing."""

from __future__ import annotations

from brain.systems.routing.skills import (
    SkillRoutingQualityPolicy,
    apply_skill_quality_routing_to_plan,
    route_skills_with_quality,
)


def _quality(score: float, confidence: float = 0.8, sample_confidence: float = 0.8) -> dict:
    return {
        "schema_version": 1,
        "advisory_only": True,
        "score": score,
        "confidence": confidence,
        "rating": "good",
        "evidence": {
            "count": 40,
            "sample_size_confidence": sample_confidence,
        },
        "reasons": ["test quality"],
    }


def _recommendations() -> list[dict]:
    return [
        {
            "rank": 1,
            "name": "general",
            "match_score": 0.70,
            "effective_digest": "sha256:general",
            "trust_level": "private_local",
        },
        {
            "rank": 2,
            "name": "debug",
            "match_score": 0.68,
            "effective_digest": "sha256:debug",
            "trust_level": "private_local",
        },
    ]


def test_quality_routing_disabled_preserves_baseline_order_with_annotations():
    decision = route_skills_with_quality(
        _recommendations(),
        quality_by_digest={"sha256:debug": _quality(0.95)},
        policy=SkillRoutingQualityPolicy(active_enabled=False),
    )

    assert [item["name"] for item in decision.recommendations] == ["general", "debug"]
    assert decision.recommendations[1]["quality_routing"]["advisory_only"] is True
    assert "disabled" in decision.reasons[0]


def test_global_learning_kill_switch_disables_active_skill_quality_routing(monkeypatch):
    monkeypatch.setenv("SKILL_QUALITY_ROUTING_ENABLED", "1")
    monkeypatch.delenv("LEARNING_POLICY_SKILL_QUALITY_ROUTING_ENABLED", raising=False)
    monkeypatch.setenv("LEARNING_POLICY_SKILL_QUALITY_ROUTING_DISABLED", "1")

    decision = route_skills_with_quality(
        _recommendations(),
        quality_by_digest={
            "sha256:general": _quality(0.45, confidence=0.8, sample_confidence=0.8),
            "sha256:debug": _quality(0.98, confidence=0.9, sample_confidence=0.9),
        },
    )

    assert decision.active_enabled is False
    assert decision.policy["global_enabled"] is False
    assert [item["name"] for item in decision.recommendations] == ["general", "debug"]
    assert "learning policy kill switch" in decision.reasons[0]


def test_quality_routing_promotes_high_confidence_private_skill():
    decision = route_skills_with_quality(
        _recommendations(),
        quality_by_digest={
            "sha256:general": _quality(0.45, confidence=0.8, sample_confidence=0.8),
            "sha256:debug": _quality(0.98, confidence=0.9, sample_confidence=0.9),
        },
        policy=SkillRoutingQualityPolicy(active_enabled=True, min_quality_delta_to_rerank=0.02),
    )

    assert [item["name"] for item in decision.recommendations] == ["debug", "general"]
    assert decision.selected_skill["name"] == "debug"
    assert "changed recommendation order" in " ".join(decision.reasons)


def test_low_sample_quality_preserves_baseline_order():
    decision = route_skills_with_quality(
        _recommendations(),
        quality_by_digest={"sha256:debug": _quality(0.99, confidence=0.95, sample_confidence=0.05)},
        policy=SkillRoutingQualityPolicy(active_enabled=True),
    )

    assert [item["name"] for item in decision.recommendations] == ["general", "debug"]
    gate_reasons = decision.recommendations[1]["quality_routing"]["gate_reasons"]
    assert "sample size confidence below threshold" in gate_reasons


def test_public_skill_cannot_auto_override_trusted_private_baseline_by_default():
    recommendations = _recommendations()
    recommendations[1]["trust_level"] = "public"

    decision = route_skills_with_quality(
        recommendations,
        quality_by_digest={
            "sha256:general": _quality(0.50, confidence=0.8, sample_confidence=0.8),
            "sha256:debug": _quality(1.0, confidence=0.95, sample_confidence=0.95),
        },
        policy=SkillRoutingQualityPolicy(active_enabled=True, min_quality_delta_to_rerank=0.01),
    )

    assert [item["name"] for item in decision.recommendations] == ["general", "debug"]
    assert "untrusted or public skill remains gated" in decision.recommendations[1]["quality_routing"]["gate_reasons"]


def test_apply_skill_quality_routing_to_plan_keeps_shape_and_metadata():
    plan = {"strategy": "catalog", "recommended_skills": _recommendations()}

    updated = apply_skill_quality_routing_to_plan(
        plan,
        quality_by_name={"debug": _quality(0.95)},
        policy=SkillRoutingQualityPolicy(active_enabled=False),
    )

    assert updated["strategy"] == "catalog"
    assert updated["recommended_skills"][1]["quality"]["score"] == 0.95
    assert updated["skill_quality_routing"]["active_enabled"] is False
