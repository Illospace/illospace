"""Skill quality evidence model contracts."""

from brain.platform.db.models.skill_quality import SkillRunEvidence


def test_skill_run_evidence_indexes_and_uniqueness():
    indexes = {i.name for i in SkillRunEvidence.__table__.indexes}
    constraints = {c.name for c in SkillRunEvidence.__table__.constraints if c.name}

    assert "uq_skill_run_evidence_run_digest" in constraints
    assert "ix_skill_run_evidence_skill_digest" in indexes
    assert "ix_skill_run_evidence_skill_name" in indexes
    assert "ix_skill_run_evidence_bundle_identity" in indexes
    assert "ix_skill_run_evidence_org_user" in indexes
    assert "ix_skill_run_evidence_created_at" in indexes
