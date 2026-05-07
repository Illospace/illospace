"""SkillRunEvidenceRepository tests using in-memory SQLite."""
from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from brain.platform.db.models.skill_quality import SkillRunEvidence
from brain.platform.db.repositories.skill_quality import SkillRunEvidenceRepository


def _session():
    engine = create_engine("sqlite://", echo=False)
    SkillRunEvidence.__table__.create(engine, checkfirst=True)
    return Session(engine)


def test_record_evidence_is_idempotent_for_run_digest():
    session = _session()
    repo = SkillRunEvidenceRepository(session)

    first = repo.record_evidence_idempotent(
        skill_name="summarize",
        skill_effective_digest="sha256:effective",
        run_id=42,
        outcome_label="success",
        verifier_status="passed",
        token_bucket="small",
    )
    second = repo.record_evidence_idempotent(
        skill_name="summarize",
        skill_effective_digest="sha256:effective",
        run_id=42,
        outcome_label="failure",
        verifier_status="failed",
        token_bucket="large",
    )

    assert second.id == first.id
    assert session.query(SkillRunEvidence).count() == 1
    assert second.outcome_label == "success"
    session.close()


def test_record_evidence_allows_multiple_unknown_run_runs():
    session = _session()
    repo = SkillRunEvidenceRepository(session)

    repo.record_evidence_idempotent(
        skill_name="summarize",
        skill_effective_digest="sha256:effective",
        run_id=None,
    )
    repo.record_evidence_idempotent(
        skill_name="summarize",
        skill_effective_digest="sha256:effective",
        run_id=None,
    )

    assert session.query(SkillRunEvidence).count() == 2
    session.close()


def test_list_by_skill_filters_digest_and_name():
    session = _session()
    repo = SkillRunEvidenceRepository(session)

    repo.record_evidence_idempotent(
        skill_name="summarize",
        skill_effective_digest="sha256:a",
        run_id=1,
        bundle_namespace="local",
        bundle_name="summarize",
    )
    repo.record_evidence_idempotent(
        skill_name="summarize",
        skill_effective_digest="sha256:b",
        run_id=2,
    )
    repo.record_evidence_idempotent(
        skill_name="draft",
        skill_effective_digest="sha256:c",
        run_id=3,
    )

    by_digest = repo.list_by_skill(skill_effective_digest="sha256:a")
    by_name = repo.list_by_skill(skill_name="summarize")

    assert [row.skill_effective_digest for row in by_digest] == ["sha256:a"]
    assert {row.skill_effective_digest for row in by_name} == {"sha256:a", "sha256:b"}
    session.close()


def test_aggregate_counts_for_skill_slice():
    session = _session()
    repo = SkillRunEvidenceRepository(session)

    repo.record_evidence_idempotent(
        skill_name="summarize",
        skill_effective_digest="sha256:effective",
        run_id=1,
        outcome_label="success",
        verifier_status="passed",
        user_feedback="positive",
    )
    repo.record_evidence_idempotent(
        skill_name="summarize",
        skill_effective_digest="sha256:effective",
        run_id=2,
        outcome_label="success",
        verifier_status="passed",
        user_feedback="positive",
    )
    repo.record_evidence_idempotent(
        skill_name="summarize",
        skill_effective_digest="sha256:effective",
        run_id=3,
        outcome_label="partial",
        verifier_status="unknown",
        user_feedback=None,
    )
    repo.record_evidence_idempotent(
        skill_name="other",
        skill_effective_digest="sha256:other",
        run_id=4,
        outcome_label="failure",
    )

    counts = repo.aggregate_counts(skill_effective_digest="sha256:effective")

    assert counts == {
        "total": 3,
        "by_outcome_label": {"partial": 1, "success": 2},
        "by_verifier_status": {"passed": 2, "unknown": 1},
        "by_user_feedback": {"positive": 2, "unknown": 1},
    }
    session.close()
