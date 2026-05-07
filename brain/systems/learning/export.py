"""Exportable learning artifacts with explicit redaction modes.

This module is intentionally pure: it performs no persistence, no network I/O,
and no LLM calls. Callers pass already-built eval, skill-quality, bundle-result,
or benchmark summary payloads and receive deterministic, portable artifacts.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import re
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Literal

from brain.systems.learning.eval_corpus import EvalPrivacyPolicy, build_eval_example


LEARNING_EXPORT_PACK_SCHEMA_VERSION = 1
LEARNING_EXPORT_ARTIFACT_SCHEMA_VERSION = 1
LEARNING_EXPORT_BUILDER_VERSION = 1
COMMUNITY_EVAL_IMPORT_SCHEMA_VERSION = 1

ExportMode = Literal["community", "hosted_internal", "private_export"]
VALID_EXPORT_MODES = {"community", "hosted_internal", "private_export"}

ARTIFACT_EVAL_CASE = "eval_case"
ARTIFACT_SKILL_QUALITY_SUMMARY = "skill_quality_summary"
ARTIFACT_BUNDLE_EVAL_RESULT = "bundle_eval_result"
ARTIFACT_POLICY_BENCHMARK_SUMMARY = "policy_benchmark_summary"

ARTIFACT_GROUPS = {
    "eval_cases": ARTIFACT_EVAL_CASE,
    "skill_quality_summaries": ARTIFACT_SKILL_QUALITY_SUMMARY,
    "bundle_eval_results": ARTIFACT_BUNDLE_EVAL_RESULT,
    "policy_benchmark_summaries": ARTIFACT_POLICY_BENCHMARK_SUMMARY,
}

SECRET_KEY_MARKERS = {
    "api_key",
    "authorization",
    "auth_token",
    "cookie",
    "credential",
    "password",
    "private_key",
    "secret",
    "session_token",
    "token",
}

NON_SECRET_KEYS = {
    "include_secret_values",
}

TENANT_IDENTIFIER_KEYS = {
    "account_id",
    "actor_id",
    "email",
    "org_id",
    "organization_id",
    "owner_id",
    "tenant_id",
    "user_id",
}

SOURCE_ROW_IDENTIFIER_KEYS = {
    "run_id",
    "eval_case_id",
    "id",
    "source_run_id",
    "source_session",
    "trace_id",
}

RAW_USER_MESSAGE_KEYS = {
    "conversation",
    "input_text",
    "message",
    "messages",
    "prompt",
    "query",
    "raw_message",
    "raw_user_message",
    "request",
    "task_request",
    "transcript",
    "user_message",
}

RAW_MEMORY_KEYS = {
    "memories",
    "memory",
    "memory_content",
    "private_memory",
    "raw_memory",
    "retrieved_memory",
    "working_memory",
}

_EMAIL_RE = re.compile(r"\b[^@\s]+@[^@\s]+\.[^@\s]+\b")
_SECRET_VALUE_RE = re.compile(r"\b(?:sk|pk|ghp|github_pat|xox[baprs])-[-_A-Za-z0-9]{8,}\b")
_REDACTED = "[redacted]"


@dataclass(frozen=True)
class ExportPrivacyPolicy:
    """Redaction policy for one portable learning export mode."""

    mode: ExportMode
    include_raw_user_messages: bool
    include_raw_private_memories: bool
    include_context_pack_content: bool
    include_tenant_identifiers: bool
    include_source_row_ids: bool
    include_secret_values: bool = False
    max_text_chars: int | None = 4_000
    redaction_level: str = "strict"

    def to_payload(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "redaction_level": self.redaction_level,
            "include_raw_user_messages": self.include_raw_user_messages,
            "include_raw_private_memories": self.include_raw_private_memories,
            "include_context_pack_content": self.include_context_pack_content,
            "include_tenant_identifiers": self.include_tenant_identifiers,
            "include_source_row_ids": self.include_source_row_ids,
            "include_secret_values": self.include_secret_values,
            "max_text_chars": self.max_text_chars,
        }


@dataclass(frozen=True)
class ExportValidationResult:
    """Validation report for an export or community eval pack."""

    valid: bool
    mode: str | None
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    pack_id: str | None = None
    pack_digest: str | None = None
    artifact_count: int = 0
    eval_case_count: int = 0

    def to_payload(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "mode": self.mode,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "pack_id": self.pack_id,
            "pack_digest": self.pack_digest,
            "artifact_count": self.artifact_count,
            "eval_case_count": self.eval_case_count,
        }


def default_export_policy(mode: ExportMode) -> ExportPrivacyPolicy:
    """Return the default policy for an export mode."""
    _validate_export_mode(mode)
    if mode == "private_export":
        return ExportPrivacyPolicy(
            mode=mode,
            include_raw_user_messages=True,
            include_raw_private_memories=True,
            include_context_pack_content=True,
            include_tenant_identifiers=True,
            include_source_row_ids=True,
            include_secret_values=False,
            max_text_chars=None,
            redaction_level="private",
        )
    if mode == "hosted_internal":
        return ExportPrivacyPolicy(
            mode=mode,
            include_raw_user_messages=False,
            include_raw_private_memories=False,
            include_context_pack_content=False,
            include_tenant_identifiers=False,
            include_source_row_ids=True,
            include_secret_values=False,
            max_text_chars=4_000,
            redaction_level="hosted_internal",
        )
    return ExportPrivacyPolicy(
        mode=mode,
        include_raw_user_messages=False,
        include_raw_private_memories=False,
        include_context_pack_content=False,
        include_tenant_identifiers=False,
        include_source_row_ids=False,
        include_secret_values=False,
        max_text_chars=2_000,
        redaction_level="strict",
    )


def build_learning_export_pack(
    *,
    mode: ExportMode = "community",
    eval_corpus: Any | None = None,
    eval_cases: Iterable[Any] | None = None,
    skill_quality_summaries: Iterable[Any] | None = None,
    bundle_eval_results: Iterable[Any] | None = None,
    policy_benchmark_summaries: Iterable[Any] | None = None,
    name: str | None = None,
    metadata: Mapping[str, Any] | None = None,
    privacy_policy: ExportPrivacyPolicy | Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a deterministic portable learning artifact pack.

    ``eval_corpus`` may be an L05 corpus payload, one eval example, or an
    iterable of examples/sources. ``eval_cases`` accepts compact eval cases,
    trajectories, TrajectoryEvalCase-like rows, or already-built eval examples.
    """
    policy = _coerce_export_policy(mode, privacy_policy)
    eval_artifacts = [
        _eval_case_artifact(source, policy=policy)
        for source in _iter_eval_sources(eval_corpus=eval_corpus, eval_cases=eval_cases)
    ]
    skill_artifacts = [
        _skill_quality_artifact(summary, policy=policy)
        for summary in _iter_payloads(skill_quality_summaries)
    ]
    bundle_artifacts = [
        _bundle_eval_result_artifact(result, policy=policy)
        for result in _iter_payloads(bundle_eval_results)
    ]
    policy_artifacts = [
        _policy_benchmark_artifact(summary, policy=policy)
        for summary in _iter_payloads(policy_benchmark_summaries)
    ]

    artifacts = {
        "eval_cases": _dedupe_sorted(eval_artifacts),
        "skill_quality_summaries": _dedupe_sorted(skill_artifacts),
        "bundle_eval_results": _dedupe_sorted(bundle_artifacts),
        "policy_benchmark_summaries": _dedupe_sorted(policy_artifacts),
    }
    summary = {
        "eval_case_count": len(artifacts["eval_cases"]),
        "skill_quality_summary_count": len(artifacts["skill_quality_summaries"]),
        "bundle_eval_result_count": len(artifacts["bundle_eval_results"]),
        "policy_benchmark_summary_count": len(artifacts["policy_benchmark_summaries"]),
        "artifact_count": sum(len(items) for items in artifacts.values()),
    }
    pack = {
        "schema_version": LEARNING_EXPORT_PACK_SCHEMA_VERSION,
        "artifact_type": "illo.learning_export_pack",
        "builder": {
            "name": "brain.systems.learning.export",
            "version": LEARNING_EXPORT_BUILDER_VERSION,
        },
        "mode": policy.mode,
        "privacy_policy": policy.to_payload(),
        "name": _clean_text(name),
        "metadata": _redact_export_payload(dict(metadata or {}), policy=policy),
        "summary": summary,
        "artifacts": artifacts,
    }
    _stamp_digest(
        pack,
        digest_field="pack_digest",
        id_field="pack_id",
        prefix=f"learning_export_pack_v{LEARNING_EXPORT_PACK_SCHEMA_VERSION}_{policy.mode}",
    )
    return pack


def validate_learning_export_pack(
    pack: Mapping[str, Any],
    *,
    expected_mode: ExportMode | None = None,
) -> ExportValidationResult:
    """Validate schema, digests, artifact digests, and redaction invariants."""
    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(pack, Mapping):
        return ExportValidationResult(False, None, errors=("pack must be a mapping",))

    mode = _clean_text(pack.get("mode"))
    if mode not in VALID_EXPORT_MODES:
        errors.append("mode must be one of: community, hosted_internal, private_export")
        mode = None
    elif expected_mode and mode != expected_mode:
        errors.append(f"pack mode must be {expected_mode}")

    if pack.get("schema_version") != LEARNING_EXPORT_PACK_SCHEMA_VERSION:
        errors.append(f"schema_version must be {LEARNING_EXPORT_PACK_SCHEMA_VERSION}")
    if pack.get("artifact_type") != "illo.learning_export_pack":
        errors.append("artifact_type must be illo.learning_export_pack")

    if mode:
        try:
            policy = _coerce_export_policy(
                mode,
                pack.get("privacy_policy") if isinstance(pack.get("privacy_policy"), Mapping) else None,
            )
        except ValueError as exc:
            errors.append(str(exc))
            policy = default_export_policy(mode)  # continue scanning with fail-closed defaults
        if mode == "community":
            expected_policy = default_export_policy("community")
            if policy.to_payload() != expected_policy.to_payload():
                errors.append("community exports must use the strict default redaction policy")
    else:
        policy = default_export_policy("community")

    expected_digest = _digest_without_fields(pack, {"pack_digest", "pack_id"})
    if pack.get("pack_digest") != expected_digest:
        errors.append("pack_digest does not match pack contents")
    expected_pack_id = f"learning_export_pack_v{LEARNING_EXPORT_PACK_SCHEMA_VERSION}_{mode}_{expected_digest[:24]}" if mode else None
    if expected_pack_id and pack.get("pack_id") != expected_pack_id:
        errors.append("pack_id does not match pack_digest")

    artifacts = _artifact_groups(pack)
    artifact_count = 0
    eval_case_count = 0
    seen_ids: set[str] = set()
    for group_name, artifact_type in ARTIFACT_GROUPS.items():
        group = artifacts.get(group_name)
        if not isinstance(group, list):
            errors.append(f"artifacts.{group_name} must be a list")
            continue
        for index, artifact in enumerate(group):
            artifact_count += 1
            if artifact_type == ARTIFACT_EVAL_CASE:
                eval_case_count += 1
            if not isinstance(artifact, Mapping):
                errors.append(f"artifacts.{group_name}[{index}] must be a mapping")
                continue
            errors.extend(_validate_artifact(artifact, expected_type=artifact_type, path=f"artifacts.{group_name}[{index}]"))
            artifact_id = _clean_text(artifact.get("artifact_id"))
            if artifact_id:
                if artifact_id in seen_ids:
                    errors.append(f"duplicate artifact_id: {artifact_id}")
                seen_ids.add(artifact_id)

    if mode == "community":
        errors.extend(_scan_for_disallowed_private_data(pack, policy=policy, path=("pack",)))
    elif mode == "hosted_internal":
        errors.extend(_scan_for_disallowed_private_data(pack, policy=policy, path=("pack",)))
    else:
        errors.extend(_scan_for_secret_values(pack, path=("pack",)))

    return ExportValidationResult(
        valid=not errors,
        mode=mode,
        errors=tuple(dict.fromkeys(errors)),
        warnings=tuple(dict.fromkeys(warnings)),
        pack_id=_clean_text(pack.get("pack_id")),
        pack_digest=_clean_text(pack.get("pack_digest")),
        artifact_count=artifact_count,
        eval_case_count=eval_case_count,
    )


def validate_community_eval_pack(pack: Mapping[str, Any]) -> ExportValidationResult:
    """Validate that a pack is safe to import into self-hosted community evals."""
    result = validate_learning_export_pack(pack, expected_mode="community")
    errors = list(result.errors)
    if result.eval_case_count == 0:
        errors.append("community eval pack must include at least one eval case")
    return ExportValidationResult(
        valid=not errors,
        mode=result.mode,
        errors=tuple(dict.fromkeys(errors)),
        warnings=result.warnings,
        pack_id=result.pack_id,
        pack_digest=result.pack_digest,
        artifact_count=result.artifact_count,
        eval_case_count=result.eval_case_count,
    )


def import_community_eval_pack(
    pack: Mapping[str, Any],
    *,
    user_id: str | None = None,
    org_id: str | None = None,
    visibility: str = "private",
    require_valid: bool = True,
) -> dict[str, Any]:
    """Return repository-ready eval-case values for a validated community pack."""
    validation = validate_community_eval_pack(pack)
    if require_valid and not validation.valid:
        raise ValueError("; ".join(validation.errors))

    eval_cases = []
    for artifact in _artifact_groups(pack).get("eval_cases", []):
        if not isinstance(artifact, Mapping):
            continue
        payload = _as_mapping(artifact.get("payload"))
        source = _as_mapping(payload.get("source"))
        scoring = _as_mapping(payload.get("scoring"))
        eval_cases.append(
            {
                "eval_digest": payload.get("example_digest") or artifact.get("artifact_digest"),
                "payload": payload,
                "schema_version": int(payload.get("schema_version") or 1),
                "redaction_mode": "community",
                "status": "active",
                "source_run_id": None,
                "trace_id": None,
                "trajectory_digest": source.get("trajectory_digest"),
                "context_pack_digest": source.get("context_pack_digest"),
                "skill_effective_digest": source.get("skill_effective_digest"),
                "user_id": user_id,
                "org_id": org_id,
                "visibility": visibility,
                "quality": _as_mapping(scoring.get("quality")),
            }
        )

    return {
        "schema_version": COMMUNITY_EVAL_IMPORT_SCHEMA_VERSION,
        "import_mode": "community_self_hosted",
        "pack_id": pack.get("pack_id"),
        "pack_digest": pack.get("pack_digest"),
        "eval_case_count": len(eval_cases),
        "eval_cases": eval_cases,
        "validation": validation.to_payload(),
    }


def stable_digest(payload: Any, *, length: int = 64) -> str:
    """Return a deterministic SHA-256 digest over a JSON-normalized payload."""
    raw = json.dumps(_jsonable(payload), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:length]


def _eval_case_artifact(source: Any, *, policy: ExportPrivacyPolicy) -> dict[str, Any]:
    payload = _normalize_eval_example(source, policy=policy)
    summary = _eval_summary(payload)
    return _artifact(
        artifact_type=ARTIFACT_EVAL_CASE,
        payload=payload,
        summary=summary,
        prefix="learning_eval_case",
    )


def _normalize_eval_example(source: Any, *, policy: ExportPrivacyPolicy) -> dict[str, Any]:
    raw = _payload_from_object(source)
    if _looks_like_eval_example(raw):
        example = dict(raw)
        example["mode"] = _eval_mode_for_export(policy)
        example["privacy_policy"] = _eval_privacy_policy(policy).to_payload()
    else:
        eval_policy = _eval_privacy_policy(policy)
        example = build_eval_example(source, mode=eval_policy.mode, privacy_policy=eval_policy)

    example["export_mode"] = policy.mode
    redacted = _redact_export_payload(example, policy=policy)
    _stamp_digest(
        redacted,
        digest_field="example_digest",
        id_field="example_id",
        prefix=f"eval_example_v{redacted.get('schema_version') or 1}",
    )
    return redacted


def _skill_quality_artifact(summary: Any, *, policy: ExportPrivacyPolicy) -> dict[str, Any]:
    payload = _skill_quality_summary_payload(_payload_from_object(summary))
    payload = _redact_export_payload(payload, policy=policy)
    summary_payload = {
        "skill": _as_mapping(payload.get("skill")),
        "bundle": _as_mapping(payload.get("bundle")),
        "score": payload.get("score"),
        "confidence": payload.get("confidence"),
        "rating": payload.get("rating"),
        "evidence_count": _as_mapping(payload.get("evidence")).get("count"),
        "advisory_only": payload.get("advisory_only", True),
    }
    return _artifact(
        artifact_type=ARTIFACT_SKILL_QUALITY_SUMMARY,
        payload=payload,
        summary=summary_payload,
        prefix="learning_skill_quality",
    )


def _bundle_eval_result_artifact(result: Any, *, policy: ExportPrivacyPolicy) -> dict[str, Any]:
    payload = _payload_from_object(result)
    if not policy.include_raw_user_messages:
        payload = _bundle_eval_summary_payload(payload)
    payload = _redact_export_payload(payload, policy=policy)
    summary = {
        "bundle_name": payload.get("bundle_name"),
        "status": payload.get("status"),
        "run_id": payload.get("run_id"),
        "outcome_count": payload.get("outcome_count") or len(_as_list(payload.get("outcomes"))),
        "required_failure_count": payload.get("required_failure_count")
        if payload.get("required_failure_count") is not None
        else len(_as_list(payload.get("required_failures"))),
        "verifier_status_counts": payload.get("verifier_status_counts"),
    }
    return _artifact(
        artifact_type=ARTIFACT_BUNDLE_EVAL_RESULT,
        payload=payload,
        summary=summary,
        prefix="learning_bundle_eval",
    )


def _policy_benchmark_artifact(summary: Any, *, policy: ExportPrivacyPolicy) -> dict[str, Any]:
    payload = _payload_from_object(summary)
    if not policy.include_raw_user_messages:
        payload = _policy_benchmark_summary_payload(payload)
    payload = _redact_export_payload(payload, policy=policy)
    artifact_summary = {
        "benchmark_name": payload.get("benchmark_name") or payload.get("name"),
        "policy_key": payload.get("policy_key"),
        "promotion_type": payload.get("promotion_type"),
        "status": payload.get("status"),
        "eligible": payload.get("eligible"),
        "metrics": payload.get("metrics"),
        "sample_count": payload.get("sample_count"),
    }
    return _artifact(
        artifact_type=ARTIFACT_POLICY_BENCHMARK_SUMMARY,
        payload=payload,
        summary=artifact_summary,
        prefix="learning_policy_benchmark",
    )


def _artifact(
    *,
    artifact_type: str,
    payload: Mapping[str, Any],
    summary: Mapping[str, Any],
    prefix: str,
) -> dict[str, Any]:
    artifact = {
        "schema_version": LEARNING_EXPORT_ARTIFACT_SCHEMA_VERSION,
        "artifact_type": artifact_type,
        "payload": _jsonable(dict(payload)),
        "summary": _jsonable(dict(summary)),
    }
    _stamp_digest(
        artifact,
        digest_field="artifact_digest",
        id_field="artifact_id",
        prefix=f"{prefix}_v{LEARNING_EXPORT_ARTIFACT_SCHEMA_VERSION}",
    )
    return artifact


def _skill_quality_summary_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    known_keys = {
        "schema_version",
        "advisory_only",
        "score",
        "confidence",
        "rating",
        "skill",
        "bundle",
        "task_class",
        "trust_level",
        "evidence",
        "signals",
        "reasons",
    }
    if any(key in payload for key in ("score", "signals", "evidence", "skill", "bundle")):
        return {key: _jsonable(payload.get(key)) for key in known_keys if key in payload}
    return dict(payload)


def _bundle_eval_summary_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    outcomes = []
    for outcome in _as_list(payload.get("outcomes")):
        if not isinstance(outcome, Mapping):
            continue
        outcomes.append(
            {
                "verifier_type": outcome.get("verifier_type"),
                "status": outcome.get("status"),
                "severity": outcome.get("severity"),
            }
        )
    status_counts = Counter(_clean_text(outcome.get("status")) or "unknown" for outcome in outcomes)
    required_failures = _as_list(payload.get("required_failures"))
    return {
        "schema_version": payload.get("schema_version") or 1,
        "run_id": payload.get("run_id"),
        "trace_id": payload.get("trace_id"),
        "bundle_name": payload.get("bundle_name"),
        "status": payload.get("status"),
        "outcome_count": len(outcomes),
        "required_failure_count": len(required_failures),
        "verifier_status_counts": dict(sorted(status_counts.items())),
        "outcomes": outcomes,
    }


def _policy_benchmark_summary_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    allowed = {
        "schema_version",
        "benchmark_name",
        "name",
        "policy_key",
        "promotion_type",
        "status",
        "eligible",
        "reason",
        "thresholds",
        "metrics",
        "sample_count",
        "score",
        "scores",
        "task_family",
        "target_family",
        "created_at",
    }
    summary = {key: _jsonable(payload.get(key)) for key in allowed if key in payload}
    if "summary" in payload and isinstance(payload.get("summary"), Mapping):
        summary["summary"] = _policy_benchmark_summary_payload(_as_mapping(payload.get("summary")))
    return summary or dict(payload)


def _eval_summary(payload: Mapping[str, Any]) -> dict[str, Any]:
    source = _as_mapping(payload.get("source"))
    scoring = _as_mapping(payload.get("scoring"))
    targets = _as_mapping(scoring.get("score_targets"))
    return {
        "example_id": payload.get("example_id"),
        "example_digest": payload.get("example_digest"),
        "source_kind": source.get("kind"),
        "trajectory_digest": source.get("trajectory_digest"),
        "context_pack_digest": source.get("context_pack_digest"),
        "skill_effective_digest": source.get("skill_effective_digest"),
        "outcome_class": targets.get("outcome_class"),
        "verifier_signal": targets.get("verifier_signal"),
        "completion_state": targets.get("completion_state"),
    }


def _redact_export_payload(value: Any, *, policy: ExportPrivacyPolicy, path: tuple[str, ...] = ()) -> Any:
    if isinstance(value, Mapping):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            normalized = _normalize_key(key_text)
            item_path = (*path, normalized)
            if _is_secret_key(normalized) and not policy.include_secret_values:
                redacted[key_text] = _REDACTED
                continue
            if _is_tenant_key(normalized) and not policy.include_tenant_identifiers:
                redacted[key_text] = _REDACTED
                continue
            if _is_source_row_key(normalized) and not policy.include_source_row_ids:
                redacted[key_text] = None
                continue
            if _is_raw_message_field(normalized, item, item_path) and not policy.include_raw_user_messages:
                redacted[key_text] = _redacted_payload(item, reason="raw_user_message_excluded")
                continue
            if _is_memory_field(normalized, item, item_path) and not policy.include_raw_private_memories:
                redacted[key_text] = _redacted_payload(item, reason="raw_private_memory_excluded")
                continue
            redacted[key_text] = _redact_export_payload(item, policy=policy, path=item_path)
        return _truncate_text_values(redacted, policy=policy)
    if isinstance(value, list):
        return [_redact_export_payload(item, policy=policy, path=path) for item in value]
    if isinstance(value, tuple):
        return [_redact_export_payload(item, policy=policy, path=path) for item in value]
    if isinstance(value, str) and not policy.include_secret_values and _SECRET_VALUE_RE.search(value):
        return _REDACTED
    return _jsonable(value)


def _redacted_payload(value: Any, *, reason: str) -> dict[str, Any]:
    if _is_safe_redaction(value):
        return _jsonable(value)
    return {
        "redacted": True,
        "redaction": reason,
        "payload_digest": stable_digest(value),
    }


def _scan_for_disallowed_private_data(
    value: Any,
    *,
    policy: ExportPrivacyPolicy,
    path: tuple[str, ...],
) -> list[str]:
    errors = _scan_for_secret_values(value, path=path)
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = _normalize_key(key)
            item_path = (*path, normalized)
            if _is_secret_key(normalized) and not _is_safe_redaction(item):
                errors.append(f"{'.'.join(item_path)} contains an unredacted secret field")
            if _is_tenant_key(normalized) and not policy.include_tenant_identifiers and not _is_safe_redaction(item):
                errors.append(f"{'.'.join(item_path)} contains an unredacted tenant identifier")
            if _is_source_row_key(normalized) and not policy.include_source_row_ids and not _is_safe_redaction(item):
                errors.append(f"{'.'.join(item_path)} contains an unredacted source row identifier")
            if _is_raw_message_field(normalized, item, item_path) and not policy.include_raw_user_messages and not _is_safe_redaction(item):
                errors.append(f"{'.'.join(item_path)} contains unredacted raw user-message text")
            if _is_memory_field(normalized, item, item_path) and not policy.include_raw_private_memories and not _is_safe_redaction(item):
                errors.append(f"{'.'.join(item_path)} contains unredacted raw memory content")
            errors.extend(_scan_for_disallowed_private_data(item, policy=policy, path=item_path))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            errors.extend(_scan_for_disallowed_private_data(item, policy=policy, path=(*path, str(index))))
    elif isinstance(value, str) and not policy.include_tenant_identifiers and _EMAIL_RE.search(value):
        errors.append(f"{'.'.join(path)} contains an unredacted email address")
    return errors


def _scan_for_secret_values(value: Any, *, path: tuple[str, ...]) -> list[str]:
    errors: list[str] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = _normalize_key(key)
            item_path = (*path, normalized)
            if _is_secret_key(normalized) and not _is_safe_redaction(item):
                errors.append(f"{'.'.join(item_path)} contains an unredacted secret field")
            errors.extend(_scan_for_secret_values(item, path=item_path))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            errors.extend(_scan_for_secret_values(item, path=(*path, str(index))))
    elif isinstance(value, str) and _SECRET_VALUE_RE.search(value):
        errors.append(f"{'.'.join(path)} contains a secret-looking value")
    return errors


def _validate_artifact(artifact: Mapping[str, Any], *, expected_type: str, path: str) -> list[str]:
    errors: list[str] = []
    if artifact.get("schema_version") != LEARNING_EXPORT_ARTIFACT_SCHEMA_VERSION:
        errors.append(f"{path}.schema_version must be {LEARNING_EXPORT_ARTIFACT_SCHEMA_VERSION}")
    if artifact.get("artifact_type") != expected_type:
        errors.append(f"{path}.artifact_type must be {expected_type}")
    expected_digest = _digest_without_fields(artifact, {"artifact_digest", "artifact_id"})
    if artifact.get("artifact_digest") != expected_digest:
        errors.append(f"{path}.artifact_digest does not match artifact contents")
    expected_id = f"{_artifact_id_prefix(expected_type)}_v{LEARNING_EXPORT_ARTIFACT_SCHEMA_VERSION}_{expected_digest[:24]}"
    if artifact.get("artifact_id") != expected_id:
        errors.append(f"{path}.artifact_id does not match artifact_digest")
    if not isinstance(artifact.get("payload"), Mapping):
        errors.append(f"{path}.payload must be a mapping")
    return errors


def _artifact_id_prefix(artifact_type: str) -> str:
    return {
        ARTIFACT_EVAL_CASE: "learning_eval_case",
        ARTIFACT_SKILL_QUALITY_SUMMARY: "learning_skill_quality",
        ARTIFACT_BUNDLE_EVAL_RESULT: "learning_bundle_eval",
        ARTIFACT_POLICY_BENCHMARK_SUMMARY: "learning_policy_benchmark",
    }[artifact_type]


def _stamp_digest(
    payload: dict[str, Any],
    *,
    digest_field: str,
    id_field: str,
    prefix: str,
) -> None:
    payload.pop(digest_field, None)
    payload.pop(id_field, None)
    digest = _digest_without_fields(payload, {digest_field, id_field})
    payload[digest_field] = digest
    payload[id_field] = f"{prefix}_{digest[:24]}"


def _digest_without_fields(payload: Any, fields: set[str]) -> str:
    return stable_digest(_without_fields(payload, fields))


def _without_fields(value: Any, fields: set[str]) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _without_fields(item, fields)
            for key, item in value.items()
            if str(key) not in fields
        }
    if isinstance(value, list):
        return [_without_fields(item, fields) for item in value]
    if isinstance(value, tuple):
        return [_without_fields(item, fields) for item in value]
    return _jsonable(value)


def _iter_eval_sources(*, eval_corpus: Any | None, eval_cases: Iterable[Any] | None) -> list[Any]:
    sources: list[Any] = []
    if eval_corpus is not None:
        payload = _payload_from_object(eval_corpus)
        if isinstance(payload.get("examples"), list):
            sources.extend(payload["examples"])
        elif _looks_like_eval_example(payload) or payload:
            sources.append(eval_corpus)
        elif isinstance(eval_corpus, Iterable) and not isinstance(eval_corpus, (str, bytes, Mapping)):
            sources.extend(list(eval_corpus))
    sources.extend(_iter_payloads(eval_cases))
    return sources


def _iter_payloads(values: Iterable[Any] | None) -> list[Any]:
    if values is None:
        return []
    if isinstance(values, (str, bytes, Mapping)):
        return [values]
    return list(values)


def _dedupe_sorted(artifacts: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id = {artifact["artifact_id"]: artifact for artifact in artifacts}
    return [by_id[key] for key in sorted(by_id)]


def _artifact_groups(pack: Mapping[str, Any]) -> dict[str, Any]:
    artifacts = pack.get("artifacts")
    if isinstance(artifacts, Mapping):
        return {key: artifacts.get(key, []) for key in ARTIFACT_GROUPS}
    return {key: pack.get(key, []) for key in ARTIFACT_GROUPS}


def _payload_from_object(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return _jsonable(dict(value))
    to_payload = getattr(value, "to_payload", None)
    if callable(to_payload):
        payload = to_payload()
        return _jsonable(dict(payload)) if isinstance(payload, Mapping) else {}
    if dataclasses.is_dataclass(value):
        return _jsonable(dataclasses.asdict(value))
    payload = getattr(value, "payload", None)
    if isinstance(payload, Mapping):
        data = _jsonable(dict(payload))
        for attr in (
            "eval_digest",
            "schema_version",
            "redaction_mode",
            "source_run_id",
            "trace_id",
            "trajectory_digest",
            "context_pack_digest",
            "skill_effective_digest",
            "quality",
        ):
            attr_value = getattr(value, attr, None)
            if attr_value is not None:
                data.setdefault(attr, _jsonable(attr_value))
        return data
    if hasattr(value, "__dict__"):
        return _jsonable(
            {
                key: item
                for key, item in vars(value).items()
                if not key.startswith("_") and key != "metadata"
            }
        )
    return {}


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
    if dataclasses.is_dataclass(value):
        return _jsonable(dataclasses.asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    return value


def _coerce_export_policy(
    mode: ExportMode,
    privacy_policy: ExportPrivacyPolicy | Mapping[str, Any] | None,
) -> ExportPrivacyPolicy:
    _validate_export_mode(mode)
    if privacy_policy is None:
        return default_export_policy(mode)
    if isinstance(privacy_policy, ExportPrivacyPolicy):
        policy = privacy_policy
    else:
        values = {**default_export_policy(mode).to_payload(), **dict(privacy_policy)}
        policy = ExportPrivacyPolicy(**values)
    _validate_export_mode(policy.mode)
    if policy.mode != mode:
        raise ValueError("privacy_policy.mode must match mode")
    if policy.include_secret_values:
        raise ValueError("learning export artifacts never include secret values")
    return policy


def _eval_privacy_policy(policy: ExportPrivacyPolicy) -> EvalPrivacyPolicy:
    return EvalPrivacyPolicy(
        mode=_eval_mode_for_export(policy),
        include_raw_io_text=policy.include_raw_user_messages,
        include_raw_memory_content=policy.include_raw_private_memories,
        include_context_pack_content=policy.include_context_pack_content,
        include_tenant_identifiers=policy.include_tenant_identifiers,
        include_source_row_ids=policy.include_source_row_ids,
        max_text_chars=policy.max_text_chars,
    )


def _eval_mode_for_export(policy: ExportPrivacyPolicy) -> Literal["internal", "hosted_eval", "external"]:
    if policy.mode == "private_export":
        return "internal"
    return "external"


def _validate_export_mode(mode: str) -> None:
    if mode not in VALID_EXPORT_MODES:
        raise ValueError("mode must be one of: community, hosted_internal, private_export")


def _looks_like_eval_example(payload: Mapping[str, Any]) -> bool:
    return bool(
        payload.get("example_id")
        or payload.get("example_digest")
        or ("replay" in payload and "scoring" in payload)
    )


def _normalize_key(value: Any) -> str:
    return str(value or "").strip().lower().replace("-", "_")


def _clean_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _is_secret_key(key: str) -> bool:
    if key in NON_SECRET_KEYS:
        return False
    return any(marker in key for marker in SECRET_KEY_MARKERS)


def _is_tenant_key(key: str) -> bool:
    return key in TENANT_IDENTIFIER_KEYS


def _is_source_row_key(key: str) -> bool:
    return key in SOURCE_ROW_IDENTIFIER_KEYS


def _is_raw_message_field(key: str, value: Any, path: tuple[str, ...]) -> bool:
    if key in {"redaction", "redacted", "payload_digest", "example_digest", "artifact_digest", "pack_digest"}:
        return False
    if _is_safe_redaction(value):
        return False
    if key in RAW_USER_MESSAGE_KEYS:
        return isinstance(value, (str, list, tuple, Mapping))
    if key == "content" and any(part in {"input", "messages", "conversation"} for part in path):
        return isinstance(value, str)
    return False


def _is_memory_field(key: str, value: Any, path: tuple[str, ...]) -> bool:
    if _is_safe_redaction(value):
        return False
    if key in RAW_MEMORY_KEYS:
        return isinstance(value, (str, list, tuple, Mapping))
    if key == "content" and any("memory" in part for part in path):
        return isinstance(value, str)
    return False


def _is_safe_redaction(value: Any) -> bool:
    if value is None:
        return True
    if value == _REDACTED:
        return True
    if isinstance(value, Mapping):
        return bool(value.get("redacted")) or value.get("redaction") in {
            "raw_user_message_excluded",
            "raw_private_memory_excluded",
            "context_pack_content_excluded",
            "source_eval_case_contains_no_raw_memory_writes",
        }
    return False


def _truncate_text_values(value: Any, *, policy: ExportPrivacyPolicy) -> Any:
    if policy.max_text_chars is None:
        return value
    if isinstance(value, Mapping):
        return {key: _truncate_text_values(item, policy=policy) for key, item in value.items()}
    if isinstance(value, list):
        return [_truncate_text_values(item, policy=policy) for item in value]
    if isinstance(value, str) and len(value) > policy.max_text_chars:
        return value[: policy.max_text_chars] + "[truncated]"
    return value


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _as_list(value: Any) -> list[Any]:
    return list(value or []) if isinstance(value, list) else []


__all__ = [
    "COMMUNITY_EVAL_IMPORT_SCHEMA_VERSION",
    "LEARNING_EXPORT_ARTIFACT_SCHEMA_VERSION",
    "LEARNING_EXPORT_PACK_SCHEMA_VERSION",
    "ExportMode",
    "ExportPrivacyPolicy",
    "ExportValidationResult",
    "build_learning_export_pack",
    "default_export_policy",
    "import_community_eval_pack",
    "stable_digest",
    "validate_community_eval_pack",
    "validate_learning_export_pack",
]
