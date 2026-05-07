"""Skill quality evidence model coverage."""

from sqlalchemy import inspect

from brain.platform.db.models.skill_quality import SkillRunEvidence


def test_skill_run_evidence_columns():
    cols = {c.name for c in inspect(SkillRunEvidence).columns}
    assert cols >= {
        "id",
        "skill_id",
        "skill_name",
        "skill_effective_digest",
        "bundle_namespace",
        "bundle_name",
        "bundle_version",
        "bundle_digest",
        "run_id",
        "trace_id",
        "task_class",
        "outcome_label",
        "verifier_status",
        "user_feedback",
        "token_bucket",
        "total_tokens",
        "cost_bucket",
        "cost_usd",
        "runtime_bucket",
        "runtime_ms",
        "tool_risk_class",
        "action_risk_class",
        "evidence_source",
        "notes",
        "org_id",
        "user_id",
        "created_at",
    }


def test_skill_run_evidence_indexes_and_uniqueness():
    indexes = {i.name for i in SkillRunEvidence.__table__.indexes}
    constraints = {c.name for c in SkillRunEvidence.__table__.constraints if c.name}

    assert "uq_skill_run_evidence_run_digest" in constraints
    assert "ix_skill_run_evidence_skill_digest" in indexes
    assert "ix_skill_run_evidence_skill_name" in indexes
    assert "ix_skill_run_evidence_bundle_identity" in indexes
    assert "ix_skill_run_evidence_org_user" in indexes
    assert "ix_skill_run_evidence_created_at" in indexes
