from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone


def _outcome_label(outcome_class: str = "good") -> dict:
    return {
        "schema_version": 1,
        "outcome_class": outcome_class,
        "completion_state": "completed",
        "verifier_signal": "passed",
        "user_feedback_signal": "positive",
        "followup_correction_signal": "none",
        "latency_signal": "normal",
        "cost_signal": "normal",
        "label_confidence": 0.91,
        "label_reasons": ["verified success"],
    }


def _trajectory() -> dict:
    label = _outcome_label()
    return {
        "schema_version": 1,
        "run_id": 42,
        "trace_id": "run:42",
        "digest": "trajectory-digest-full",
        "input_envelope": {
            "event": "thread_reply",
            "message": "PRIVATE USER TASK: ship the export pack",
            "user_id": "user-1",
            "metadata": {"api_key": "sk-secret-export", "org_id": "org-1"},
        },
        "context": {
            "context_pack_digest": "context-digest-full",
            "rendered_sections": [{"name": "retrieved_memory", "included": True}],
        },
        "context_pack": {
            "schema_version": 1,
            "digest": "context-digest-full",
            "sections": {
                "retrieved_memory": {
                    "title": "Retrieved Memory",
                    "content": {"memory": "PRIVATE MEMORY SHOULD NOT LEAVE"},
                }
            },
        },
        "tool_calls": [{"tool_name": "write_file", "target": {"token": "secret-token"}}],
        "verifier_summary": {"status": "satisfied"},
        "final_output": {"status": "completed", "output_artifact": "Final reply"},
        "memory_writes": [
            {
                "id": 9,
                "visibility": "private",
                "user_id": "user-1",
                "org_id": "org-1",
                "content": "OPERATOR PRIVATE MEMORY CONTENT",
            }
        ],
        "user_feedback": {
            "skill_feedback": "good",
            "skill_feedback_note": "PRIVATE FEEDBACK TEXT",
            "implicit_feedback_summary": "PRIVATE IMPLICIT FEEDBACK TEXT",
            "implicit_feedback_tags": ["positive"],
        },
        "quality_signals": {
            "summary": {
                "outcome_kind": "success",
                "settlement_state": "settled_success",
                "verifier_status": "satisfied",
                "tokens_total": 100,
            },
        },
        "outcome_label": label,
    }


def _quality_summary() -> dict:
    return {
        "schema_version": 1,
        "advisory_only": True,
        "score": 0.83,
        "confidence": 0.72,
        "rating": "strong",
        "skill": {"name": "exporter", "effective_digest": "sha256:skill"},
        "bundle": {
            "namespace": "local",
            "name": "exporter",
            "version": "1.0.0",
            "digest": "sha256:bundle",
        },
        "task_class": "learning_export",
        "trust_level": "private_local",
        "evidence": {"count": 12, "latest_observed_at": "2026-04-25T12:00:00+00:00"},
        "signals": {"verifier_pass_rate": {"score": 1.0, "sample_size": 12}},
        "reasons": ["verifier evidence is strong"],
        "user_id": "user-1",
        "org_id": "org-1",
        "notes": "PRIVATE NOTE SHOULD NOT LEAVE",
    }


@dataclass(frozen=True)
class BundleResult:
    run_id: int
    bundle_name: str
    status: str
    outcomes: list[dict]
    required_failures: list[str]


def _bundle_result() -> BundleResult:
    return BundleResult(
        run_id=42,
        bundle_name="run_completion",
        status="warning",
        outcomes=[
            {
                "verifier_type": "semantic_evidence_judge",
                "status": "warning",
                "severity": "warning",
                "failure_reason": "PRIVATE USER TASK was ambiguous",
                "evidence": {"token": "secret-token", "task_request": "PRIVATE USER TASK"},
            }
        ],
        required_failures=["PRIVATE FAILURE TEXT"],
    )


def _policy_benchmark() -> dict:
    return {
        "schema_version": 1,
        "benchmark_name": "scout replay",
        "policy_key": "scout_rule:docs:small",
        "promotion_type": "scout_rule",
        "status": "passed",
        "eligible": True,
        "metrics": {"support_count": 4, "mean_readiness": 0.88},
        "thresholds": {"min_support_count": 3},
        "sample_count": 4,
        "user_id": "user-1",
        "raw_user_message": "PRIVATE BENCHMARK PROMPT",
    }


def test_community_export_pack_is_deterministic_and_strictly_redacted():
    from brain.systems.learning.export import build_learning_export_pack, validate_community_eval_pack

    first = build_learning_export_pack(
        mode="community",
        eval_cases=[_trajectory()],
        skill_quality_summaries=[_quality_summary()],
        bundle_eval_results=[_bundle_result()],
        policy_benchmark_summaries=[_policy_benchmark()],
        metadata={"org_id": "org-1", "api_key": "sk-secret-export"},
    )
    second = build_learning_export_pack(
        mode="community",
        eval_cases=[_trajectory()],
        skill_quality_summaries=[_quality_summary()],
        bundle_eval_results=[_bundle_result()],
        policy_benchmark_summaries=[_policy_benchmark()],
        metadata={"org_id": "org-1", "api_key": "sk-secret-export"},
    )

    assert first == second
    assert first["pack_id"].startswith("learning_export_pack_v1_community_")
    assert first["summary"] == {
        "eval_case_count": 1,
        "skill_quality_summary_count": 1,
        "bundle_eval_result_count": 1,
        "policy_benchmark_summary_count": 1,
        "artifact_count": 4,
    }

    raw = json.dumps(first, sort_keys=True)
    assert "PRIVATE USER TASK" not in raw
    assert "OPERATOR PRIVATE MEMORY CONTENT" not in raw
    assert "PRIVATE MEMORY SHOULD NOT LEAVE" not in raw
    assert "PRIVATE FEEDBACK TEXT" not in raw
    assert "PRIVATE NOTE SHOULD NOT LEAVE" not in raw
    assert "PRIVATE BENCHMARK PROMPT" not in raw
    assert "secret-token" not in raw
    assert "sk-secret-export" not in raw
    assert "user-1" not in raw
    assert "org-1" not in raw
    assert "run:42" not in raw

    validation = validate_community_eval_pack(first)
    assert validation.valid is True
    assert validation.eval_case_count == 1


def test_import_community_eval_pack_returns_repository_ready_values():
    from brain.systems.learning.export import build_learning_export_pack, import_community_eval_pack

    pack = build_learning_export_pack(mode="community", eval_cases=[_trajectory()])
    imported = import_community_eval_pack(
        pack,
        user_id="self-hosted-user",
        org_id="self-hosted-org",
        visibility="org",
    )

    assert imported["schema_version"] == 1
    assert imported["import_mode"] == "community_self_hosted"
    assert imported["eval_case_count"] == 1
    values = imported["eval_cases"][0]
    assert values["redaction_mode"] == "community"
    assert values["source_run_id"] is None
    assert values["trace_id"] is None
    assert values["trajectory_digest"] == "trajectory-digest-full"
    assert values["user_id"] == "self-hosted-user"
    assert values["org_id"] == "self-hosted-org"
    assert values["visibility"] == "org"
    assert values["payload"]["replay"]["input"]["redacted"] is True


def test_community_validation_rejects_tampered_raw_private_payload():
    from brain.systems.learning.export import build_learning_export_pack, validate_community_eval_pack

    pack = build_learning_export_pack(mode="community", eval_cases=[_trajectory()])
    pack["artifacts"]["eval_cases"][0]["payload"]["replay"]["input"] = {
        "message": "PRIVATE USER TASK leaked"
    }

    validation = validate_community_eval_pack(pack)

    assert validation.valid is False
    assert any("pack_digest" in error for error in validation.errors)
    assert any("raw user-message" in error for error in validation.errors)


def test_hosted_internal_removes_raw_messages_but_keeps_source_row_refs():
    from brain.systems.learning.export import build_learning_export_pack, validate_learning_export_pack

    pack = build_learning_export_pack(mode="hosted_internal", eval_cases=[_trajectory()])
    raw = json.dumps(pack, sort_keys=True)

    assert "PRIVATE USER TASK" not in raw
    assert "OPERATOR PRIVATE MEMORY CONTENT" not in raw
    assert "user-1" not in raw
    assert "org-1" not in raw
    assert "run:42" in raw
    assert '"run_id": 42' in raw
    assert validate_learning_export_pack(pack).valid is True


def test_private_export_includes_private_eval_text_but_never_secret_values():
    from brain.systems.learning.export import build_learning_export_pack, validate_learning_export_pack

    pack = build_learning_export_pack(mode="private_export", eval_cases=[_trajectory()])
    raw = json.dumps(pack, sort_keys=True)

    assert "PRIVATE USER TASK: ship the export pack" in raw
    assert "OPERATOR PRIVATE MEMORY CONTENT" in raw
    assert "PRIVATE MEMORY SHOULD NOT LEAVE" in raw
    assert "user-1" in raw
    assert "org-1" in raw
    assert "secret-token" not in raw
    assert "sk-secret-export" not in raw
    assert validate_learning_export_pack(pack).valid is True


def test_skill_quality_object_payloads_are_supported():
    from brain.systems.skills.quality import score_skill_quality
    from brain.systems.learning.export import build_learning_export_pack

    evidence = [
        {
            "skill_name": "exporter",
            "skill_effective_digest": "sha256:skill",
            "bundle_namespace": "local",
            "bundle_name": "exporter",
            "bundle_version": "1.0.0",
            "bundle_digest": "sha256:bundle",
            "outcome_label": "success",
            "verifier_status": "passed",
            "user_feedback": "positive",
            "created_at": datetime(2026, 4, 25, 12, 0, tzinfo=timezone.utc),
        }
        for _ in range(3)
    ]
    score = score_skill_quality(evidence, trust_level="private_local")

    pack = build_learning_export_pack(mode="community", skill_quality_summaries=[score])

    artifact = pack["artifacts"]["skill_quality_summaries"][0]
    assert artifact["artifact_id"].startswith("learning_skill_quality_v1_")
    assert artifact["summary"]["skill"]["name"] == "exporter"
    assert artifact["summary"]["bundle"]["digest"] == "sha256:bundle"
