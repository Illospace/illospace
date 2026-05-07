from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock


def _repo_session(existing=None):
    session = MagicMock()
    scalars = MagicMock()
    scalars.first.return_value = existing
    scalars.all.return_value = [existing] if existing is not None else []
    session.scalars.return_value = scalars
    return session


def test_learning_signal_repository_records_new_signal():
    from brain.platform.db.repositories.learning import LearningSignalRepository

    session = _repo_session()
    repo = LearningSignalRepository(session)

    row = repo.record_signal(
        signal_digest="sig-1",
        signal_type="outcome_label",
        source_run_id=42,
        trajectory_digest="traj-1",
        outcome_label="good",
        label_confidence=0.84,
        payload={"outcome_class": "good"},
        evidence={"reason": "verified"},
        org_id="org-1",
    )

    assert row.signal_digest == "sig-1"
    assert row.signal_type == "outcome_label"
    assert row.payload == {"outcome_class": "good"}
    session.add.assert_called_once_with(row)
    session.flush.assert_called_once()


def test_learning_signal_repository_updates_existing_digest():
    from brain.platform.db.repositories.learning import LearningSignalRepository

    existing = SimpleNamespace(signal_digest="sig-1", signal_type="old", payload={})
    session = _repo_session(existing)
    repo = LearningSignalRepository(session)

    row = repo.record_signal(
        signal_digest="sig-1",
        signal_type="outcome_label",
        payload={"outcome_class": "weak"},
    )

    assert row is existing
    assert existing.signal_type == "outcome_label"
    assert existing.payload == {"outcome_class": "weak"}
    session.add.assert_not_called()
    session.flush.assert_called_once()


def test_trajectory_eval_case_repository_upserts_payload():
    from brain.platform.db.repositories.learning import TrajectoryEvalCaseRepository

    session = _repo_session()
    repo = TrajectoryEvalCaseRepository(session)

    row = repo.upsert_eval_case(
        eval_digest="eval-1",
        payload={"input": "redacted"},
        schema_version=1,
        redaction_mode="eval",
        trajectory_digest="traj-1",
        quality={"outcome_label": {"outcome_class": "good"}},
    )

    assert row.eval_digest == "eval-1"
    assert row.payload == {"input": "redacted"}
    assert row.quality == {"outcome_label": {"outcome_class": "good"}}
    session.add.assert_called_once_with(row)
    session.flush.assert_called_once()


def test_policy_update_candidate_repository_upserts_candidate():
    from brain.platform.db.repositories.learning import PolicyUpdateCandidateRepository

    existing = SimpleNamespace(
        candidate_digest="cand-1",
        candidate_type="context_policy",
        policy_payload={},
        evaluation_payload={},
    )
    session = _repo_session(existing)
    repo = PolicyUpdateCandidateRepository(session)

    row = repo.upsert_candidate(
        candidate_digest="cand-1",
        candidate_type="context_policy",
        policy_payload={"version": "candidate-v1"},
        evaluation_payload={"passed": True},
    )

    assert row is existing
    assert existing.policy_payload == {"version": "candidate-v1"}
    assert existing.evaluation_payload == {"passed": True}
    session.add.assert_not_called()
    session.flush.assert_called_once()
