from __future__ import annotations

import json
from types import SimpleNamespace


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
        "label_confidence": 0.84,
        "label_reasons": ["verified success"],
    }


def _eval_case(suffix: str = "a") -> dict:
    label = _outcome_label()
    return {
        "schema_version": 1,
        "digest": f"eval-digest-{suffix}",
        "trajectory_digest": f"trajectory-digest-{suffix}",
        "run_id": 40 + len(suffix),
        "trace_id": f"run:{40 + len(suffix)}",
        "input": {
            "event": "thread_reply",
            "message": f"Ship the feature {suffix}",
            "user_id": "user-1",
            "metadata": {"api_key": "sk-secret", "org_id": "org-1"},
        },
        "context_digest": f"context-digest-{suffix}",
        "context_sections": ["thread_summary", "retrieved_memory"],
        "expected_output": {"status": "completed", "output_artifact": "Final reply"},
        "tool_calls": [{"tool_name": "write_file", "target": {"token": "secret-token"}}],
        "verifier_summary": {"status": "satisfied"},
        "quality": {
            "outcome_kind": "success",
            "settlement_state": "settled_success",
            "verifier_status": "satisfied",
            "tokens_total": 100,
            "outcome_label": label,
        },
        "learning_signals": {
            "memory_write_count": 1,
            "feedback": {"skill_feedback": "good"},
            "outcome_label": label,
        },
    }


def _trajectory() -> dict:
    label = _outcome_label()
    return {
        "schema_version": 1,
        "run_id": 42,
        "trace_id": "run:42",
        "redaction_mode": "internal",
        "digest": "trajectory-digest-full",
        "input_envelope": {
            "event": "thread_reply",
            "message": "Ship the trajectory exporter",
            "user_id": "user-1",
            "metadata": {"api_key": "sk-secret", "org_id": "org-1"},
        },
        "context": {
            "context_pack_digest": "context-digest-full",
            "rendered_sections": [
                {
                    "name": "retrieved_memory",
                    "title": "Retrieved Memory",
                    "source": "memory",
                    "included": True,
                }
            ],
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
                "tags": ["auto_encoded"],
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


def test_eval_example_from_eval_case_is_stable_and_serializable():
    from brain.systems.learning.eval_corpus import build_eval_example

    first = build_eval_example(_eval_case(), mode="hosted_eval")
    second = build_eval_example(_eval_case(), mode="hosted_eval")

    assert first == second
    assert first["schema_version"] == 1
    assert first["example_id"].startswith("eval_example_v1_")
    assert first["source"]["kind"] == "trajectory_eval_case"
    assert first["source"]["trajectory_digest"] == "trajectory-digest-a"
    assert first["replay"]["input"]["message"] == "Ship the feature a"
    assert first["replay"]["input"]["metadata"]["api_key"] == "[redacted]"
    assert first["replay"]["input"]["metadata"]["org_id"] == "[redacted]"
    assert first["scoring"]["score_targets"]["outcome_class"] == "good"
    assert first["scoring"]["score_targets"]["label_confidence"] == 0.84
    json.dumps(first, sort_keys=True)


def test_hosted_eval_mode_excludes_raw_memory_and_context_content_by_default():
    from brain.systems.learning.eval_corpus import build_eval_example

    example = build_eval_example(_trajectory(), mode="hosted_eval")
    raw = json.dumps(example, sort_keys=True)

    assert "OPERATOR PRIVATE MEMORY CONTENT" not in raw
    assert "PRIVATE MEMORY SHOULD NOT LEAVE" not in raw
    assert "secret-token" not in raw
    assert "sk-secret" not in raw
    assert example["privacy_policy"]["include_raw_memory_content"] is False
    assert example["privacy_policy"]["include_context_pack_content"] is False
    assert example["replay"]["input"]["message"] == "Ship the trajectory exporter"
    assert example["replay"]["input"]["user_id"] == "[redacted]"
    memory = example["replay"]["memory_writes"]
    assert memory["raw_content_included"] is False
    assert memory["items"][0]["content_redacted"] is True
    assert "content" not in memory["items"][0]
    assert memory["items"][0]["user_id"] == "[redacted]"


def test_external_mode_redacts_raw_io_and_source_row_ids():
    from brain.systems.learning.eval_corpus import build_eval_example

    example = build_eval_example(_trajectory(), mode="external")
    raw = json.dumps(example, sort_keys=True)

    assert "Ship the trajectory exporter" not in raw
    assert "OPERATOR PRIVATE MEMORY CONTENT" not in raw
    assert "PRIVATE FEEDBACK TEXT" not in raw
    assert "PRIVATE IMPLICIT FEEDBACK TEXT" not in raw
    assert example["source"]["run_id"] is None
    assert example["source"]["trace_id"] is None
    assert example["replay"]["input"]["redacted"] is True
    assert example["replay"]["expected_output"]["redacted"] is True
    assert example["replay"]["memory_writes"]["raw_content_included"] is False
    assert example["scoring"]["learning_signals"]["feedback"] == {
        "skill_feedback": "good",
        "implicit_feedback_tags": ["positive"],
        "raw_text_redacted": True,
    }


def test_internal_mode_can_preserve_raw_memory_content_for_local_use():
    from brain.systems.learning.eval_corpus import build_eval_example

    example = build_eval_example(_trajectory(), mode="internal")
    raw = json.dumps(example, sort_keys=True)

    assert "OPERATOR PRIVATE MEMORY CONTENT" in raw
    assert "PRIVATE MEMORY SHOULD NOT LEAVE" in raw
    assert example["replay"]["memory_writes"]["raw_content_included"] is True
    assert example["replay"]["memory_writes"]["items"][0]["user_id"] == "user-1"


def test_eval_corpus_is_order_stable_and_deduplicates_examples():
    from brain.systems.learning.eval_corpus import build_eval_corpus

    first = build_eval_corpus([_eval_case("b"), _eval_case("a"), _eval_case("a")], mode="hosted_eval")
    second = build_eval_corpus([_eval_case("a"), _eval_case("b"), _eval_case("a")], mode="hosted_eval")

    assert first == second
    assert first["example_count"] == 2
    assert first["source_count"] == 3
    assert first["deduped_count"] == 1
    assert [example["example_id"] for example in first["examples"]] == sorted(
        example["example_id"] for example in first["examples"]
    )


def test_eval_example_to_eval_case_values_are_repository_ready():
    from brain.systems.learning.eval_corpus import build_eval_example, eval_example_to_eval_case_values

    example = build_eval_example(_eval_case(), mode="hosted_eval")
    values = eval_example_to_eval_case_values(
        example,
        user_id="user-1",
        org_id="org-1",
        visibility="org",
    )

    assert values["eval_digest"] == example["example_digest"]
    assert values["payload"] == example
    assert values["schema_version"] == 1
    assert values["redaction_mode"] == "hosted_eval"
    assert values["source_run_id"] == 41
    assert values["trace_id"] == "run:41"
    assert values["trajectory_digest"] == "trajectory-digest-a"
    assert values["context_pack_digest"] == "context-digest-a"
    assert values["quality"] == example["scoring"]["quality"]
    assert values["user_id"] == "user-1"
    assert values["org_id"] == "org-1"
    assert values["visibility"] == "org"


def test_trajectory_eval_case_like_row_can_be_used_as_source():
    from brain.systems.learning.eval_corpus import build_eval_example

    row = SimpleNamespace(
        eval_digest="row-eval-digest",
        schema_version=1,
        redaction_mode="eval",
        source_run_id=99,
        trace_id="run:99",
        trajectory_digest="row-trajectory-digest",
        context_pack_digest="row-context-digest",
        skill_effective_digest="skill-digest",
        payload={"input": {"message": "Replay me"}, "quality": {"outcome_label": _outcome_label("weak")}},
        quality={"outcome_label": _outcome_label("weak")},
    )

    example = build_eval_example(row, mode="hosted_eval")

    assert example["source"]["eval_case_digest"] == "row-eval-digest"
    assert example["source"]["run_id"] == 99
    assert example["source"]["skill_effective_digest"] == "skill-digest"
    assert example["scoring"]["score_targets"]["outcome_class"] == "weak"
