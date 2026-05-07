from __future__ import annotations

from sqlalchemy import inspect

from brain.platform.db.models.learning import (
    LearningSignal,
    PolicyUpdateCandidate,
    TrajectoryEvalCase,
)


def test_learning_signal_model_columns_and_indexes():
    cols = {c.name for c in inspect(LearningSignal).columns}

    assert {
        "id",
        "signal_digest",
        "signal_type",
        "status",
        "review_status",
        "source_run_id",
        "trace_id",
        "trajectory_digest",
        "context_pack_digest",
        "skill_effective_digest",
        "org_id",
        "user_id",
        "visibility",
        "outcome_label",
        "label_confidence",
        "payload",
        "evidence",
        "applied_at",
        "rolled_back_at",
    }.issubset(cols)
    index_names = {idx.name for idx in LearningSignal.__table__.indexes}
    assert "ix_learning_signals_org_type_created" in index_names
    assert "ix_learning_signals_skill_digest" in index_names


def test_trajectory_eval_case_model_columns_and_indexes():
    cols = {c.name for c in inspect(TrajectoryEvalCase).columns}

    assert {
        "eval_digest",
        "schema_version",
        "redaction_mode",
        "status",
        "source_run_id",
        "trajectory_digest",
        "context_pack_digest",
        "skill_effective_digest",
        "payload",
        "quality",
    }.issubset(cols)
    index_names = {idx.name for idx in TrajectoryEvalCase.__table__.indexes}
    assert "ix_trajectory_eval_cases_org_status_created" in index_names


def test_policy_update_candidate_model_columns_and_indexes():
    cols = {c.name for c in inspect(PolicyUpdateCandidate).columns}

    assert {
        "candidate_digest",
        "candidate_type",
        "status",
        "review_status",
        "org_id",
        "user_id",
        "visibility",
        "source_signal_ids",
        "policy_payload",
        "evaluation_payload",
        "applied_at",
        "rolled_back_at",
    }.issubset(cols)
    index_names = {idx.name for idx in PolicyUpdateCandidate.__table__.indexes}
    assert "ix_policy_update_candidates_org_type_status" in index_names
