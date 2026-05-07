"""Safe eval corpus examples derived from run trajectories.

This module is intentionally a pure builder. It does not run evals, call LLMs,
or wire itself into live run paths.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Literal


EVAL_EXAMPLE_SCHEMA_VERSION = 1
EVAL_CORPUS_SCHEMA_VERSION = 1
EVAL_CORPUS_BUILDER_VERSION = 1

EvalUseMode = Literal["internal", "hosted_eval", "external"]
VALID_EVAL_USE_MODES = {"internal", "hosted_eval", "external"}

SECRET_KEY_MARKERS = {
    "api_key",
    "authorization",
    "auth",
    "cookie",
    "credential",
    "password",
    "secret",
    "token",
}

TENANT_IDENTIFIER_KEYS = {
    "actor_id",
    "email",
    "org_id",
    "owner_id",
    "session_id",
    "source_session",
    "user_id",
}


@dataclass(frozen=True)
class EvalPrivacyPolicy:
    """Explicit export policy for one eval use mode.

    The hosted/external defaults deliberately exclude raw memory and context
    content. Callers that want to relax those defaults must pass a custom policy,
    making the privacy choice visible in the resulting example payload.
    """

    mode: EvalUseMode
    include_raw_io_text: bool
    include_raw_memory_content: bool
    include_context_pack_content: bool
    include_tenant_identifiers: bool
    include_source_row_ids: bool
    max_text_chars: int | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "include_raw_io_text": self.include_raw_io_text,
            "include_raw_memory_content": self.include_raw_memory_content,
            "include_context_pack_content": self.include_context_pack_content,
            "include_tenant_identifiers": self.include_tenant_identifiers,
            "include_source_row_ids": self.include_source_row_ids,
            "max_text_chars": self.max_text_chars,
            "raw_private_memory_content_default": self.include_raw_memory_content,
        }


def default_privacy_policy(mode: EvalUseMode) -> EvalPrivacyPolicy:
    """Return the default privacy policy for an eval export/use mode."""
    _validate_mode(mode)
    if mode == "internal":
        return EvalPrivacyPolicy(
            mode=mode,
            include_raw_io_text=True,
            include_raw_memory_content=True,
            include_context_pack_content=True,
            include_tenant_identifiers=True,
            include_source_row_ids=True,
            max_text_chars=None,
        )
    if mode == "hosted_eval":
        return EvalPrivacyPolicy(
            mode=mode,
            include_raw_io_text=True,
            include_raw_memory_content=False,
            include_context_pack_content=False,
            include_tenant_identifiers=False,
            include_source_row_ids=True,
            max_text_chars=8_000,
        )
    return EvalPrivacyPolicy(
        mode=mode,
        include_raw_io_text=False,
        include_raw_memory_content=False,
        include_context_pack_content=False,
        include_tenant_identifiers=False,
        include_source_row_ids=False,
        max_text_chars=2_000,
    )


def build_eval_example(
    source: Mapping[str, Any] | Any,
    *,
    mode: EvalUseMode = "hosted_eval",
    privacy_policy: EvalPrivacyPolicy | Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one stable, JSON-serializable eval example.

    ``source`` can be a full run trajectory, the compact payload returned by
    ``build_run_eval_case``, or a TrajectoryEvalCase-like row with a
    ``payload`` attribute.
    """
    policy = _coerce_policy(mode, privacy_policy)
    payload = _source_payload(source)
    source_kind = _source_kind(payload)

    source_meta = _source_metadata(payload, source_kind=source_kind, policy=policy)
    replay = _replay_payload(payload, source_kind=source_kind, policy=policy)
    scoring = _scoring_payload(payload, source_kind=source_kind, policy=policy)

    example = {
        "schema_version": EVAL_EXAMPLE_SCHEMA_VERSION,
        "builder": {
            "name": "brain.systems.learning.eval_corpus",
            "version": EVAL_CORPUS_BUILDER_VERSION,
        },
        "mode": policy.mode,
        "privacy_policy": policy.to_payload(),
        "source": source_meta,
        "replay": replay,
        "scoring": scoring,
        "metadata": {
            "source_kind": source_kind,
            "source_schema_version": payload.get("schema_version"),
            "source_redaction_mode": payload.get("redaction_mode"),
            "json_serializable": True,
        },
    }
    example["example_digest"] = stable_digest(example)
    example["example_id"] = f"eval_example_v1_{example['example_digest'][:24]}"
    return example


def build_eval_corpus(
    sources: Iterable[Mapping[str, Any] | Any],
    *,
    mode: EvalUseMode = "hosted_eval",
    privacy_policy: EvalPrivacyPolicy | Mapping[str, Any] | None = None,
    corpus_name: str | None = None,
) -> dict[str, Any]:
    """Build an idempotent eval corpus from trajectory or eval-case sources."""
    policy = _coerce_policy(mode, privacy_policy)
    examples_by_id: dict[str, dict[str, Any]] = {}
    source_count = 0
    for source in sources:
        source_count += 1
        example = build_eval_example(source, mode=policy.mode, privacy_policy=policy)
        examples_by_id[example["example_id"]] = example

    examples = [examples_by_id[key] for key in sorted(examples_by_id)]
    corpus = {
        "schema_version": EVAL_CORPUS_SCHEMA_VERSION,
        "builder": {
            "name": "brain.systems.learning.eval_corpus",
            "version": EVAL_CORPUS_BUILDER_VERSION,
        },
        "mode": policy.mode,
        "privacy_policy": policy.to_payload(),
        "name": corpus_name,
        "example_count": len(examples),
        "source_count": source_count,
        "deduped_count": source_count - len(examples),
        "examples": examples,
    }
    corpus["corpus_digest"] = stable_digest(corpus)
    corpus["corpus_id"] = f"eval_corpus_v1_{corpus['corpus_digest'][:24]}"
    return corpus


def eval_example_to_eval_case_values(
    example: Mapping[str, Any],
    *,
    user_id: str | None = None,
    org_id: str | None = None,
    visibility: str = "private",
    status: str = "active",
) -> dict[str, Any]:
    """Return kwargs suitable for TrajectoryEvalCaseRepository.upsert_eval_case."""
    source = _as_mapping(example.get("source"))
    scoring = _as_mapping(example.get("scoring"))
    quality = _as_mapping(scoring.get("quality"))
    return {
        "eval_digest": str(example["example_digest"]),
        "payload": _jsonable(example),
        "schema_version": int(example.get("schema_version") or EVAL_EXAMPLE_SCHEMA_VERSION),
        "redaction_mode": example.get("mode") or "hosted_eval",
        "status": status,
        "source_run_id": source.get("run_id"),
        "trace_id": source.get("trace_id"),
        "trajectory_digest": source.get("trajectory_digest"),
        "context_pack_digest": source.get("context_pack_digest"),
        "skill_effective_digest": source.get("skill_effective_digest"),
        "user_id": user_id,
        "org_id": org_id,
        "visibility": visibility,
        "quality": quality,
    }


def stable_digest(payload: Any, *, length: int = 64) -> str:
    """Return a deterministic SHA-256 digest over a JSON-normalized payload."""
    raw = json.dumps(_jsonable(payload), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:length]


def _validate_mode(mode: str) -> None:
    if mode not in VALID_EVAL_USE_MODES:
        raise ValueError("mode must be one of: internal, hosted_eval, external")


def _coerce_policy(
    mode: EvalUseMode,
    privacy_policy: EvalPrivacyPolicy | Mapping[str, Any] | None,
) -> EvalPrivacyPolicy:
    _validate_mode(mode)
    if privacy_policy is None:
        return default_privacy_policy(mode)
    if isinstance(privacy_policy, EvalPrivacyPolicy):
        policy = privacy_policy
    else:
        values = {**default_privacy_policy(mode).to_payload(), **dict(privacy_policy)}
        values.pop("raw_private_memory_content_default", None)
        policy = EvalPrivacyPolicy(**values)
    _validate_mode(policy.mode)
    if policy.mode != mode:
        raise ValueError("privacy_policy.mode must match mode")
    return policy


def _source_payload(source: Mapping[str, Any] | Any) -> dict[str, Any]:
    if isinstance(source, Mapping):
        return _jsonable(dict(source))

    payload = getattr(source, "payload", None)
    if isinstance(payload, Mapping):
        data = _jsonable(dict(payload))
    else:
        data = {}

    for attr, key in (
        ("eval_digest", "digest"),
        ("schema_version", "schema_version"),
        ("redaction_mode", "redaction_mode"),
        ("source_run_id", "run_id"),
        ("trace_id", "trace_id"),
        ("trajectory_digest", "trajectory_digest"),
        ("context_pack_digest", "context_digest"),
        ("skill_effective_digest", "skill_effective_digest"),
    ):
        value = getattr(source, attr, None)
        if value is not None:
            data.setdefault(key, _jsonable(value))

    quality = getattr(source, "quality", None)
    if isinstance(quality, Mapping):
        data.setdefault("quality", _jsonable(dict(quality)))
    if not data:
        raise TypeError("source must be a mapping or TrajectoryEvalCase-like row")
    return data


def _source_kind(payload: Mapping[str, Any]) -> str:
    if "input_envelope" in payload or "memory_writes" in payload or "quality_signals" in payload:
        return "run_trajectory"
    return "trajectory_eval_case"


def _source_metadata(
    payload: Mapping[str, Any],
    *,
    source_kind: str,
    policy: EvalPrivacyPolicy,
) -> dict[str, Any]:
    run_id = payload.get("run_id") if policy.include_source_row_ids else None
    trace_id = payload.get("trace_id") if policy.include_source_row_ids else None
    context_digest = _context_digest(payload, source_kind)
    return _redact_common(
        {
            "kind": source_kind,
            "run_id": run_id,
            "trace_id": trace_id,
            "trajectory_digest": payload.get("trajectory_digest") or payload.get("digest"),
            "eval_case_digest": payload.get("digest") if source_kind == "trajectory_eval_case" else None,
            "context_pack_digest": context_digest,
            "skill_effective_digest": payload.get("skill_effective_digest"),
        },
        policy=policy,
    )


def _replay_payload(
    payload: Mapping[str, Any],
    *,
    source_kind: str,
    policy: EvalPrivacyPolicy,
) -> dict[str, Any]:
    if source_kind == "run_trajectory":
        input_payload = _as_mapping(payload.get("input_envelope"))
        expected_output = _as_mapping(payload.get("final_output"))
        tool_calls = _as_list(payload.get("tool_calls"))
        verifier_summary = _as_mapping(payload.get("verifier_summary"))
    else:
        input_payload = _as_mapping(payload.get("input"))
        expected_output = _as_mapping(payload.get("expected_output"))
        tool_calls = _as_list(payload.get("tool_calls"))
        verifier_summary = _as_mapping(payload.get("verifier_summary"))

    return {
        "input": _redact_raw_io(input_payload, policy=policy, label="input"),
        "expected_output": _redact_raw_io(expected_output, policy=policy, label="expected_output"),
        "context": _context_payload(payload, source_kind=source_kind, policy=policy),
        "tool_calls": _redact_common(tool_calls, policy=policy),
        "verifier_summary": _redact_common(verifier_summary, policy=policy),
        "memory_writes": _memory_payload(payload, source_kind=source_kind, policy=policy),
    }


def _scoring_payload(
    payload: Mapping[str, Any],
    *,
    source_kind: str,
    policy: EvalPrivacyPolicy,
) -> dict[str, Any]:
    quality = _quality_payload(payload, source_kind=source_kind)
    learning_signals = _learning_signals_payload(payload, source_kind=source_kind)
    if not policy.include_raw_io_text:
        learning_signals = _strip_raw_feedback_text(learning_signals)
    outcome_label = _as_mapping(quality.get("outcome_label") or learning_signals.get("outcome_label"))
    return {
        "outcome_label": _redact_common(outcome_label, policy=policy),
        "quality": _redact_common(quality, policy=policy),
        "learning_signals": _redact_common(learning_signals, policy=policy),
        "score_targets": {
            "outcome_class": outcome_label.get("outcome_class"),
            "label_confidence": outcome_label.get("label_confidence"),
            "verifier_signal": outcome_label.get("verifier_signal"),
            "completion_state": outcome_label.get("completion_state"),
        },
    }


def _quality_payload(payload: Mapping[str, Any], *, source_kind: str) -> dict[str, Any]:
    if source_kind == "trajectory_eval_case":
        return dict(_as_mapping(payload.get("quality")))

    summary = _as_mapping(_as_mapping(payload.get("quality_signals")).get("summary"))
    return {
        "outcome_kind": summary.get("outcome_kind"),
        "settlement_state": summary.get("settlement_state"),
        "verifier_status": summary.get("verifier_status"),
        "tokens_total": summary.get("tokens_total"),
        "outcome_label": _as_mapping(payload.get("outcome_label")),
    }


def _learning_signals_payload(payload: Mapping[str, Any], *, source_kind: str) -> dict[str, Any]:
    if source_kind == "trajectory_eval_case":
        return dict(_as_mapping(payload.get("learning_signals")))
    return {
        "memory_write_count": len(_as_list(payload.get("memory_writes"))),
        "feedback": _as_mapping(payload.get("user_feedback")),
        "outcome_label": _as_mapping(payload.get("outcome_label")),
    }


def _strip_raw_feedback_text(learning_signals: Mapping[str, Any]) -> dict[str, Any]:
    cleaned = dict(learning_signals)
    feedback = _as_mapping(cleaned.get("feedback"))
    if feedback:
        cleaned["feedback"] = {
            "skill_feedback": feedback.get("skill_feedback"),
            "implicit_feedback_tags": list(feedback.get("implicit_feedback_tags") or []),
            "raw_text_redacted": any(
                feedback.get(key)
                for key in (
                    "skill_feedback_note",
                    "implicit_feedback_summary",
                    "followup_correction",
                )
            ),
        }
    return cleaned


def _context_payload(
    payload: Mapping[str, Any],
    *,
    source_kind: str,
    policy: EvalPrivacyPolicy,
) -> dict[str, Any]:
    context_digest = _context_digest(payload, source_kind)
    sections = _context_sections(payload, source_kind)
    result: dict[str, Any] = {
        "context_pack_digest": context_digest,
        "context_sections": sections,
        "context_section_count": len(sections),
        "context_pack_content_included": policy.include_context_pack_content,
    }
    if source_kind == "run_trajectory":
        context = _as_mapping(payload.get("context"))
        result["rendered_sections"] = _redact_common(
            context.get("rendered_sections") or [],
            policy=policy,
        )
        if policy.include_context_pack_content:
            result["context_pack"] = _redact_common(_as_mapping(payload.get("context_pack")), policy=policy)
    if not policy.include_context_pack_content:
        result["redaction"] = "context_pack_content_excluded"
    return result


def _context_digest(payload: Mapping[str, Any], source_kind: str) -> Any:
    if source_kind == "trajectory_eval_case":
        return payload.get("context_digest") or payload.get("context_pack_digest")
    context = _as_mapping(payload.get("context"))
    return context.get("context_pack_digest") or payload.get("context_pack_digest")


def _context_sections(payload: Mapping[str, Any], source_kind: str) -> list[Any]:
    if source_kind == "trajectory_eval_case":
        return list(payload.get("context_sections") or [])
    context = _as_mapping(payload.get("context"))
    sections = []
    for section in context.get("rendered_sections") or []:
        if isinstance(section, Mapping):
            sections.append(section.get("name"))
    return sections


def _memory_payload(
    payload: Mapping[str, Any],
    *,
    source_kind: str,
    policy: EvalPrivacyPolicy,
) -> dict[str, Any]:
    if source_kind == "trajectory_eval_case":
        signals = _as_mapping(payload.get("learning_signals"))
        return {
            "count": int(signals.get("memory_write_count") or 0),
            "items": [],
            "raw_content_included": False,
            "redaction": "source_eval_case_contains_no_raw_memory_writes",
        }

    rows = _as_list(payload.get("memory_writes"))
    items = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        item = dict(row)
        content = item.get("content")
        if not policy.include_raw_memory_content:
            item.pop("content", None)
            item["content_redacted"] = content is not None
            item["content_char_count"] = len(str(content)) if content is not None else 0
        items.append(_redact_common(item, policy=policy))

    return {
        "count": len(rows),
        "items": items,
        "raw_content_included": policy.include_raw_memory_content,
        "redaction": None if policy.include_raw_memory_content else "raw_memory_content_excluded",
    }


def _redact_raw_io(value: Any, *, policy: EvalPrivacyPolicy, label: str) -> Any:
    if policy.include_raw_io_text:
        redacted = _redact_common(value, policy=policy)
        return _truncate_text_values(redacted, policy=policy)
    return {
        "redacted": True,
        "redaction": f"{label}_raw_text_excluded",
        "payload_digest": stable_digest(value),
    }


def _redact_common(value: Any, *, policy: EvalPrivacyPolicy) -> Any:
    if isinstance(value, Mapping):
        redacted = {}
        for key, item in value.items():
            key_text = str(key)
            normalized = key_text.lower()
            if any(marker in normalized for marker in SECRET_KEY_MARKERS):
                redacted[key_text] = "[redacted]"
                continue
            if not policy.include_tenant_identifiers and normalized in TENANT_IDENTIFIER_KEYS:
                redacted[key_text] = "[redacted]"
                continue
            redacted[key_text] = _redact_common(item, policy=policy)
        return redacted
    if isinstance(value, list):
        return [_redact_common(item, policy=policy) for item in value]
    if isinstance(value, tuple):
        return [_redact_common(item, policy=policy) for item in value]
    return _jsonable(value)


def _truncate_text_values(value: Any, *, policy: EvalPrivacyPolicy) -> Any:
    if policy.max_text_chars is None:
        return value
    if isinstance(value, Mapping):
        return {key: _truncate_text_values(item, policy=policy) for key, item in value.items()}
    if isinstance(value, list):
        return [_truncate_text_values(item, policy=policy) for item in value]
    if isinstance(value, str) and len(value) > policy.max_text_chars:
        return value[: policy.max_text_chars] + "[truncated]"
    return value


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if value.__class__.__module__ == "unittest.mock":
        return None
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    return value


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _as_list(value: Any) -> list[Any]:
    return list(value or []) if isinstance(value, list) else []
