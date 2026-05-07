from __future__ import annotations

from datetime import datetime, timezone

from brain.systems.learning.rollback import (
    apply_policy_candidate_application_rollback,
    apply_skill_graduation_rollback,
    build_memory_supersession_batch_rollback,
    build_policy_candidate_application_rollback,
    build_skill_graduation_rollback,
    build_skill_version_auto_update_rollback,
    evaluate_safety_monitors,
    triggered_safety_findings,
)


NOW = datetime(2026, 4, 25, 12, 0, tzinfo=timezone.utc)


def test_policy_candidate_rollback_plan_is_explicit_and_dry_runnable():
    plan = build_policy_candidate_application_rollback(
        {
            "id": 12,
            "candidate_digest": "cand-abc",
            "status": "applied",
            "review_status": "approved",
            "applied_at": "2026-04-25T10:00:00+00:00",
        },
        previous_active_candidate={
            "id": 10,
            "candidate_digest": "cand-prev",
            "status": "active",
            "applied_at": "2026-04-24T10:00:00+00:00",
        },
        reason="verifier failures spiked",
        requested_by="admin-1",
        created_at=NOW,
    )

    assert plan.target_type == "policy_candidate_application"
    assert plan.target_ref == "12"
    assert len(plan.operations) == 2
    first = plan.operations[0]
    assert first.action == "mark_rolled_back"
    assert first.target_type == "policy_update_candidate"
    assert first.set_fields == {
        "status": "rolled_back",
        "rolled_back_at": NOW.isoformat(),
    }
    assert first.to_payload()["destructive"] is False

    dry_run = apply_policy_candidate_application_rollback(plan)

    assert dry_run.dry_run is True
    assert dry_run.applied_count == 0
    assert [receipt["status"] for receipt in dry_run.receipts] == ["planned", "planned"]


def test_rollback_apply_uses_only_the_supplied_executor():
    plan = build_policy_candidate_application_rollback(
        {"candidate_digest": "cand-abc", "status": "applied"},
        reason="manual rollback",
        created_at=NOW,
    )
    seen = []

    result = apply_policy_candidate_application_rollback(
        plan,
        executor=lambda operation: seen.append(operation.to_payload()) or {"ok": True},
    )

    assert result.dry_run is False
    assert result.applied_count == 1
    assert seen[0]["target_ref"] == "cand-abc"
    assert result.receipts[0]["executor_result"] == {"ok": True}


def test_skill_version_and_graduation_rollbacks_restore_prior_fields():
    version_plan = build_skill_version_auto_update_rollback(
        {"id": 7, "name": "debugger", "version": 3, "bundle_version_id": 30},
        previous_version={
            "version": 2,
            "bundle_version_id": 20,
            "bundle_digest": "sha256:old",
            "effective_digest": "sha256:old",
        },
        applied_version={
            "version": 3,
            "bundle_version_id": 30,
            "bundle_digest": "sha256:new",
            "effective_digest": "sha256:new",
        },
        reason="auto-update regression",
        created_at=NOW,
    )

    assert version_plan.target_type == "skill_version_auto_update"
    assert version_plan.operations[0].restore_fields["version"] == 2
    assert version_plan.operations[0].expected_current_fields["bundle_digest"] == "sha256:new"
    assert version_plan.metadata["preserves_skill_version_rows"] is True

    graduation_plan = build_skill_graduation_rollback(
        {"id": 7, "name": "debugger"},
        previous_fields={
            "source_kind": "agent_draft",
            "trust_level": "agent_draft",
            "provisional": True,
            "review_status": "unreviewed",
        },
        graduation_update={
            "source_kind": "private_local",
            "trust_level": "private_local",
            "provisional": False,
        },
        reason="graduated skill caused fallbacks",
        created_at=NOW,
    )

    assert graduation_plan.target_type == "skill_graduation"
    assert graduation_plan.operations[0].restore_fields["trust_level"] == "agent_draft"
    result = apply_skill_graduation_rollback(graduation_plan)
    assert result.receipts[0]["status"] == "planned"


def test_memory_supersession_batch_rollback_uses_supplied_restore_metadata():
    plan = build_memory_supersession_batch_rollback(
        [
            {
                "action": "supersede_older",
                "rollback_metadata": {
                    "idempotency_key": "memory-action-1",
                    "affected_memory_ids": ["old"],
                    "restore_fields": {
                        "old": {
                            "truth_status": "reviewed",
                            "review_status": "reviewed",
                            "superseded_by": None,
                            "valid_until": None,
                        }
                    },
                    "preserves_memory_content": True,
                },
            }
        ],
        reason="correction was itself corrected",
        created_at=NOW,
    )

    assert plan.target_type == "memory_supersession_batch"
    assert plan.metadata["preserves_memory_content"] is True
    assert len(plan.operations) == 1
    operation = plan.operations[0]
    assert operation.target_type == "memory"
    assert operation.target_ref == "old"
    assert operation.restore_fields["truth_status"] == "reviewed"
    assert operation.metadata["preserves_memory_content"] is True


def test_safety_monitors_detect_learning_regressions():
    findings = evaluate_safety_monitors(
        {
            "verifier_failure_rate_baseline": 0.04,
            "verifier_failure_rate_current": 0.20,
            "user_corrections_baseline": 2,
            "user_interactions_baseline": 100,
            "user_corrections_current": 14,
            "user_interactions_current": 100,
            "fallback_rate_baseline": 0.05,
            "fallback_rate_current": 0.18,
            "budget_used_units": 125_000,
            "budget_limit_units": 100_000,
        }
    )

    triggered = {finding.kind: finding for finding in triggered_safety_findings(findings)}

    assert set(triggered) == {
        "verifier_failure_increase",
        "user_correction_rate_increase",
        "fallback_rate_increase",
        "budget_overrun",
    }
    assert triggered["budget_overrun"].severity == "critical"
