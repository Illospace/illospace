"""Advisory skill quality score tests."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from brain.systems.skills.quality import (
    SKILL_QUALITY_SCORE_SCHEMA_VERSION,
    SkillQualityScore,
    score_skill_quality,
    score_skill_quality_from_repository,
)


BASE_TIME = datetime(2026, 4, 25, 12, 0, tzinfo=timezone.utc)


@dataclass(slots=True)
class EvidenceRow:
    skill_name: str = "summarize"
    skill_effective_digest: str = "sha256:skill"
    bundle_namespace: str | None = "local"
    bundle_name: str | None = "summarize"
    bundle_version: str | None = "1.0.0"
    bundle_digest: str | None = "sha256:bundle"
    task_class: str | None = "summarization"
    outcome_label: str | None = "success"
    verifier_status: str | None = "passed"
    user_feedback: str | None = "positive"
    token_bucket: str | None = "small"
    total_tokens: int | None = 6_000
    cost_bucket: str | None = "small"
    cost_usd: float | None = 0.05
    runtime_bucket: str | None = "fast"
    runtime_ms: int | None = 60_000
    created_at: datetime = BASE_TIME


def test_skill_quality_score_payload_includes_required_signals():
    rows = []
    for index in range(30):
        rows.append(
            EvidenceRow(
                outcome_label="success" if index < 26 else "partial",
                verifier_status="passed" if index < 28 else "failed",
                user_feedback="positive" if index < 22 else None,
                created_at=BASE_TIME - timedelta(days=index % 5),
            )
        )

    score = score_skill_quality(
        rows,
        task_class="summarization",
        trust_level="illo_core",
        as_of=BASE_TIME,
    )
    payload = score.to_payload()

    assert isinstance(score, SkillQualityScore)
    assert payload["schema_version"] == SKILL_QUALITY_SCORE_SCHEMA_VERSION
    assert payload["advisory_only"] is True
    assert payload["score"] >= 0.80
    assert payload["confidence"] >= 0.90
    assert payload["rating"] == "strong"
    assert payload["skill"] == {
        "name": "summarize",
        "effective_digest": "sha256:skill",
    }
    assert payload["bundle"]["digest"] == "sha256:bundle"
    assert set(payload["signals"]) == {
        "cost_efficiency",
        "outcome_success_rate",
        "recency_reliability",
        "task_class_fit",
        "trust_level",
        "user_correction_feedback_rate",
        "verifier_pass_rate",
    }
    assert payload["signals"]["outcome_success_rate"]["value"] > 0.85
    assert payload["signals"]["verifier_pass_rate"]["value"] > 0.90
    assert payload["signals"]["task_class_fit"]["value"] == 1.0


def test_low_sample_failure_stays_close_to_neutral():
    rows = [
        EvidenceRow(
            outcome_label="failure",
            verifier_status="failed",
            user_feedback="correction",
            token_bucket="large",
            total_tokens=250_000,
            cost_bucket="very_high",
            cost_usd=15.0,
            runtime_bucket="very_slow",
            runtime_ms=45 * 60 * 1000,
        )
    ]

    score = score_skill_quality(
        rows,
        task_class="summarization",
        trust_level="agent_draft",
        as_of=BASE_TIME,
    )
    payload = score.to_payload()

    assert 0.45 <= payload["score"] <= 0.55
    assert payload["confidence"] < 0.20
    assert payload["rating"] == "learning"
    assert payload["evidence"]["sample_size_confidence"] < 0.20
    assert "low sample size keeps the advisory score close to neutral" in payload["reasons"]
    assert payload["signals"]["cost_efficiency"]["score"] <= 0.25
    assert payload["signals"]["trust_level"]["value"] == "agent_draft"


def test_feedback_and_task_class_fit_are_advisory_signals():
    rows = [
        EvidenceRow(task_class="deploy", user_feedback="positive"),
        EvidenceRow(task_class="deploy", user_feedback="positive"),
        EvidenceRow(task_class="deploy", user_feedback="negative"),
        EvidenceRow(task_class="deploy", user_feedback="correction"),
        EvidenceRow(task_class="research", user_feedback="positive"),
        EvidenceRow(task_class="research", user_feedback="positive"),
        EvidenceRow(task_class="research", user_feedback="redo"),
        EvidenceRow(task_class="research", user_feedback="positive"),
        EvidenceRow(task_class="debug", user_feedback=None),
        EvidenceRow(task_class="debug", user_feedback=None),
    ]

    score = score_skill_quality(
        rows,
        task_class="deploy",
        trust_level="public",
        as_of=BASE_TIME,
    )
    payload = score.to_payload()

    assert payload["signals"]["task_class_fit"]["value"] == 0.4
    assert payload["signals"]["task_class_fit"]["details"]["mode"] == "expected_task_class"
    assert payload["signals"]["user_correction_feedback_rate"]["value"] == 0.375
    assert payload["signals"]["user_correction_feedback_rate"]["details"]["feedback_rate"] == 0.8
    assert "task-class evidence is mixed for the requested slice" in payload["reasons"]
    assert "user feedback includes corrections or negative signals" in payload["reasons"]


def test_mapping_rows_and_stale_recency_are_supported():
    rows = [
        {
            "skill_name": "Research",
            "skill_effective_digest": "sha256:research",
            "bundle_namespace": "market",
            "bundle_name": "research",
            "task_class": "research",
            "outcome_label": "good",
            "verifier_status": "satisfied",
            "user_feedback": "accepted",
            "cost_bucket": "medium",
            "created_at": "2025-12-01T12:00:00+00:00",
        }
    ]

    score = score_skill_quality(rows, trust_level="marketplace", as_of=BASE_TIME)
    payload = score.to_payload()

    assert payload["skill"]["name"] == "Research"
    assert payload["trust_level"] == "marketplace"
    assert payload["signals"]["recency_reliability"]["details"]["age_days"] > 100
    assert "evidence is stale relative to the scoring window" in payload["reasons"]


def test_repository_adapter_reads_evidence_without_mutating_routing():
    rows = [EvidenceRow(skill_effective_digest="sha256:repo")]

    class FakeRepository:
        def __init__(self) -> None:
            self.calls = []

        def list_by_skill(self, **kwargs):
            self.calls.append(kwargs)
            return rows

    repository = FakeRepository()
    score = score_skill_quality_from_repository(
        repository,
        skill_effective_digest="sha256:repo",
        skill_name="summarize",
        limit=25,
        task_class="summarization",
        trust_level="private_local",
        as_of=BASE_TIME,
    )

    assert repository.calls == [
        {
            "skill_effective_digest": "sha256:repo",
            "skill_name": "summarize",
            "limit": 25,
        }
    ]
    assert score.skill_effective_digest == "sha256:repo"
    assert score.skill_name == "summarize"
    assert score.advisory_only is True
