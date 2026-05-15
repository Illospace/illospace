"""SkillRunEvidenceRepository tests using in-memory SQLite."""
from __future__ import annotations

import pytest
from sqlalchemy import select

from brain.platform.db.models.skill_quality import SkillRunEvidence
from brain.platform.db.repositories.skill_quality import SkillRunEvidenceRepository


@pytest.fixture
async def session(async_sqlite_session_factory):
    return await async_sqlite_session_factory([SkillRunEvidence.__table__])


async def test_record_evidence_is_idempotent_for_run_digest(session):
    repo = SkillRunEvidenceRepository(session)

    first = await repo.a_record_evidence_idempotent(
        skill_name="summarize",
        skill_effective_digest="sha256:effective",
        run_id=42,
        outcome_label="success",
        verifier_status="passed",
        token_bucket="small",
    )
    second = await repo.a_record_evidence_idempotent(
        skill_name="summarize",
        skill_effective_digest="sha256:effective",
        run_id=42,
        outcome_label="failure",
        verifier_status="failed",
        token_bucket="large",
    )

    assert second.id == first.id
    rows = await session.scalars(select(SkillRunEvidence))
    assert len(rows.all()) == 1
    assert second.outcome_label == "success"


async def test_record_evidence_allows_multiple_unknown_run_runs(session):
    repo = SkillRunEvidenceRepository(session)

    await repo.a_record_evidence_idempotent(
        skill_name="summarize",
        skill_effective_digest="sha256:effective",
        run_id=None,
    )
    await repo.a_record_evidence_idempotent(
        skill_name="summarize",
        skill_effective_digest="sha256:effective",
        run_id=None,
    )

    rows = await session.scalars(select(SkillRunEvidence))
    assert len(rows.all()) == 2


async def test_list_by_skill_filters_digest_and_name(session):
    repo = SkillRunEvidenceRepository(session)

    await repo.a_record_evidence_idempotent(
        skill_name="summarize",
        skill_effective_digest="sha256:a",
        run_id=1,
        bundle_namespace="local",
        bundle_name="summarize",
    )
    await repo.a_record_evidence_idempotent(
        skill_name="summarize",
        skill_effective_digest="sha256:b",
        run_id=2,
    )
    await repo.a_record_evidence_idempotent(
        skill_name="draft",
        skill_effective_digest="sha256:c",
        run_id=3,
    )

    by_digest = await repo.a_list_by_skill(skill_effective_digest="sha256:a")
    by_name = await repo.a_list_by_skill(skill_name="summarize")

    assert [row.skill_effective_digest for row in by_digest] == ["sha256:a"]
    assert {row.skill_effective_digest for row in by_name} == {"sha256:a", "sha256:b"}


async def test_aggregate_counts_for_skill_slice(session):
    repo = SkillRunEvidenceRepository(session)

    await repo.a_record_evidence_idempotent(
        skill_name="summarize",
        skill_effective_digest="sha256:effective",
        run_id=1,
        outcome_label="success",
        verifier_status="passed",
        user_feedback="positive",
    )
    await repo.a_record_evidence_idempotent(
        skill_name="summarize",
        skill_effective_digest="sha256:effective",
        run_id=2,
        outcome_label="success",
        verifier_status="passed",
        user_feedback="positive",
    )
    await repo.a_record_evidence_idempotent(
        skill_name="summarize",
        skill_effective_digest="sha256:effective",
        run_id=3,
        outcome_label="partial",
        verifier_status="unknown",
        user_feedback=None,
    )
    await repo.a_record_evidence_idempotent(
        skill_name="other",
        skill_effective_digest="sha256:other",
        run_id=4,
        outcome_label="failure",
    )

    counts = await repo.a_aggregate_counts(skill_effective_digest="sha256:effective")

    assert counts == {
        "total": 3,
        "by_outcome_label": {"partial": 1, "success": 2},
        "by_verifier_status": {"passed": 2, "unknown": 1},
        "by_user_feedback": {"positive": 2, "unknown": 1},
    }
