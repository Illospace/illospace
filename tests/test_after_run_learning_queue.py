from __future__ import annotations

from types import SimpleNamespace

from brain.systems.learning.budget import BudgetLane, LearningBudgetPolicy
from brain.systems.learning.queue import (
    AfterRunLearningJobStatus,
    AfterRunLearningJobType,
    AfterRunLearningQueueService,
    AfterRunLearningSource,
    AfterRunSkillReference,
    build_eval_case_from_trajectory,
)


class _SignalRepo:
    def __init__(self):
        self.rows = {}

    def record_signal(self, *, signal_digest: str, signal_type: str, **values):
        row = self.rows.get(signal_digest)
        if row is None:
            row = SimpleNamespace(id=len(self.rows) + 1, signal_digest=signal_digest)
            self.rows[signal_digest] = row
        row.signal_type = signal_type
        for key, value in values.items():
            setattr(row, key, value)
        return row


class _EvalCaseRepo:
    def __init__(self):
        self.rows = {}

    def upsert_eval_case(self, *, eval_digest: str, payload: dict, **values):
        row = self.rows.get(eval_digest)
        if row is None:
            row = SimpleNamespace(id=len(self.rows) + 1, eval_digest=eval_digest)
            self.rows[eval_digest] = row
        row.payload = payload
        for key, value in values.items():
            setattr(row, key, value)
        return row


class _SkillEvidenceRepo:
    def __init__(self):
        self.rows = {}

    def record_evidence_idempotent(self, *, run_id: int | None, skill_effective_digest: str, **values):
        key = (run_id, skill_effective_digest)
        row = self.rows.get(key)
        if row is None:
            row = SimpleNamespace(
                id=len(self.rows) + 1,
                run_id=run_id,
                skill_effective_digest=skill_effective_digest,
            )
            self.rows[key] = row
            for field, value in values.items():
                setattr(row, field, value)
        return row


def _trajectory() -> dict:
    return {
        "schema_version": 1,
        "run_id": 42,
        "trace_id": "run:42",
        "digest": "trajectory-digest-1",
        "input_envelope": {"event": "thread_reply", "message": "Ship PR-L04"},
        "context": {
            "context_pack_digest": "context-pack-1",
            "brain_context_loaded": True,
            "brain_recall_used": True,
            "brain_skills_used": True,
            "preloaded_memory_count": 3,
            "cognitive_misses": ["missing_recent_result"],
            "rendered_sections": [{"name": "thread_summary"}, {"name": "selected_skills"}],
        },
        "context_pack": {
            "digest": "context-pack-1",
            "sections": {
                "selected_skills": {
                    "content": {
                        "selected": {
                            "name": "develop",
                            "skill_record": {
                                "effective_digest": "sha256:effective",
                                "bundle_digest": "sha256:bundle",
                                "skill_version": "3",
                            },
                        }
                    }
                }
            },
        },
        "tool_calls": [{"tool_name": "exec_command"}],
        "action_manifests": [{"risk": "medium", "approval_required": False, "outcome_status": "completed"}],
        "verifier_summary": {"status": "satisfied"},
        "quality_signals": {
            "summary": {
                "outcome_kind": "success",
                "settlement_state": "settled_success",
                "verifier_status": "satisfied",
                "tokens_total": 1200,
                "estimated_cost": 0.012,
            },
        },
        "lease": {"total_duration_ms": 125_000},
        "final_output": {"status": "completed", "content": "Done"},
        "memory_writes": [{"id": 1}],
        "user_feedback": {"skill_feedback": "good"},
        "outcome_label": {
            "outcome_class": "good",
            "verifier_signal": "passed",
            "user_feedback_signal": "positive",
            "label_confidence": 0.84,
        },
    }


def _source(*, skill: AfterRunSkillReference | None = None) -> AfterRunLearningSource:
    trajectory = _trajectory()
    return AfterRunLearningSource(
        run_id=42,
        trace_id="run:42",
        user_id="user-1",
        org_id="org-1",
        trajectory=trajectory,
        eval_case=build_eval_case_from_trajectory(trajectory),
        skill=skill
        or AfterRunSkillReference(
            skill_name="develop",
            skill_effective_digest="sha256:effective",
            bundle_digest="sha256:bundle",
            bundle_version="3",
        ),
    )


def test_after_run_queue_records_all_allowed_jobs_idempotently():
    signals = _SignalRepo()
    eval_cases = _EvalCaseRepo()
    skill_evidence = _SkillEvidenceRepo()
    service = AfterRunLearningQueueService(policy=LearningBudgetPolicy())

    first = service.queue(
        _source(),
        learning_signals=signals,
        trajectory_eval_cases=eval_cases,
        skill_run_evidence=skill_evidence,
    )
    second = service.queue(
        _source(),
        learning_signals=signals,
        trajectory_eval_cases=eval_cases,
        skill_run_evidence=skill_evidence,
    )

    assert first.recorded_count == 3
    assert second.recorded_count == 3
    assert len(signals.rows) == 3
    assert len(eval_cases.rows) == 1
    assert len(skill_evidence.rows) == 1
    assert {row.status for row in signals.rows.values()} == {"recorded"}

    eval_row = next(iter(eval_cases.rows.values()))
    assert eval_row.source_run_id == 42
    assert eval_row.trajectory_digest == "trajectory-digest-1"
    assert eval_row.context_pack_digest == "context-pack-1"
    assert eval_row.quality["outcome_label"]["outcome_class"] == "good"

    skill_row = next(iter(skill_evidence.rows.values()))
    assert skill_row.skill_name == "develop"
    assert skill_row.outcome_label == "good"
    assert skill_row.verifier_status == "passed"
    assert skill_row.user_feedback == "positive"
    assert skill_row.token_bucket == "small"
    assert skill_row.runtime_bucket == "fast"


def test_after_run_queue_defers_budget_denied_work_without_target_writes():
    signals = _SignalRepo()
    eval_cases = _EvalCaseRepo()
    skill_evidence = _SkillEvidenceRepo()
    service = AfterRunLearningQueueService(
        policy=LearningBudgetPolicy(after_run_sample_rate=0.0)
    )

    result = service.queue(
        _source(),
        learning_signals=signals,
        trajectory_eval_cases=eval_cases,
        skill_run_evidence=skill_evidence,
    )

    assert result.recorded_count == 0
    assert result.deferred_count == 3
    assert len(signals.rows) == 3
    assert len(eval_cases.rows) == 0
    assert len(skill_evidence.rows) == 0
    assert {row.status for row in signals.rows.values()} == {"deferred"}


def test_after_run_queue_marks_tenant_budget_exhaustion_as_skipped():
    signals = _SignalRepo()
    policy = LearningBudgetPolicy(
        lane_token_limits={
            BudgetLane.HOT_PATH: 1_500,
            BudgetLane.AFTER_RUN: 20_000,
            BudgetLane.NIGHT: 100_000,
            BudgetLane.TENANT_DAILY: 0,
        }
    )

    result = AfterRunLearningQueueService(policy=policy).queue(
        _source(),
        learning_signals=signals,
        trajectory_eval_cases=_EvalCaseRepo(),
        skill_run_evidence=_SkillEvidenceRepo(),
    )

    assert result.skipped_count == 3
    assert {row.status for row in signals.rows.values()} == {"skipped"}


def test_after_run_queue_skips_skill_shell_without_effective_digest():
    signals = _SignalRepo()
    skill_evidence = _SkillEvidenceRepo()
    source = AfterRunLearningSource(
        run_id=42,
        trace_id="run:42",
        user_id="user-1",
        org_id="org-1",
        skill=AfterRunSkillReference(skill_name="develop"),
    )

    result = AfterRunLearningQueueService(policy=LearningBudgetPolicy()).queue(
        source,
        learning_signals=signals,
        skill_run_evidence=skill_evidence,
    )

    assert [job.job_type for job in result.jobs] == [AfterRunLearningJobType.SKILL_RUN_EVIDENCE]
    assert result.jobs[0].status == AfterRunLearningJobStatus.SKIPPED
    assert result.jobs[0].reason == "skill effective digest unavailable"
    assert len(signals.rows) == 1
    assert next(iter(signals.rows.values())).status == "skipped"
    assert len(skill_evidence.rows) == 0
