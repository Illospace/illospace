"""Deterministic provider-alert classification loaded from durable config.

This module deliberately has no dependency on reconstructed/consolidated memory.
The Slack posting path loads the checked-in map for every alert attempt.  The
upstream coordinator that composes summaries should apply this same map too;
the outbound gate remains authoritative when generated text is misclassified.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import re
from typing import Any, Mapping

from brain.kernel import config


PROVIDER_ALERT_POLICY_ENV = "ILLO_PROVIDER_ALERT_POLICY_PATH"
DEFAULT_PROVIDER_ALERT_POLICY_PATH = (
    Path(config.BRAIN_DIR) / "deploy" / "compose" / "provider-alert-severity.json"
)
_ALERT_HEADER = re.compile(
    r"(?i)(?P<prefix>\bALERT\s*[—–-]\s*)"
    r"(?:CRITICAL|HIGH|MEDIUM|LOW|CONTENT[ _-]?POLICY)"
)
_ALERT_MARKER = re.compile(r"(?i)\bALERT\s*[—–-]")
_PROVIDER_MARKER = re.compile(
    r"(?i)\b(?:provider|model (?:call|generation)|seedream|fal|vertex|oom)\b"
    r"|Runtime\.OutOfMemory"
)
_TYPED_REASON_AFTER = re.compile(
    r"(?i)\b(?:typed\s+)?(?:error[ _-]?reason|reason[ _-]?code|reason|type)"
    r"\s*[:=]\s*[\"']?([a-z][a-z0-9_. -]{1,40})"
)
_TYPED_REASON_BEFORE = re.compile(
    r"(?i)\b([a-z][a-z0-9_.-]{1,30})\s+(?:typed\s+)?(?:reason|rejection)\b"
)
_COUNT = re.compile(
    r"(?i)\b(?:occurrences?|count|failures?|rejections?|total)\s*[:=]?\s*(\d+)\b"
)
_VOLATILE_SIGNATURE_PARTS = (
    re.compile(r"\b\d{4}-\d{2}-\d{2}[T ][0-9:.+-]+Z?\b", re.IGNORECASE),
    re.compile(r"\b(?:occurrences?|count|total)\s*[:=]?\s*\d+\b", re.IGNORECASE),
    re.compile(r"\bstill ongoing,?\s*\+\d+\s+since last\b", re.IGNORECASE),
)


class ProviderAlertPolicyError(RuntimeError):
    """The durable alert policy could not be loaded or validated."""


@dataclass(frozen=True)
class ProviderAlertEvidence:
    providers: tuple[str, ...]
    status_code: int | None
    error_type: str | None
    typed_reason: str | None
    occurrence_count: int | None


@dataclass(frozen=True)
class ProviderAlertDecision:
    original_body: str
    body: str
    classification: str
    severity: str
    rule_id: str
    signature: str
    throttle_minutes: int
    policy_source: str
    evidence: ProviderAlertEvidence
    escalation_reason: str | None = None


def _token(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").casefold()).strip("_")


def _policy_path(path: str | Path | None = None) -> Path:
    configured = path or os.getenv(PROVIDER_ALERT_POLICY_ENV)
    return Path(configured) if configured else DEFAULT_PROVIDER_ALERT_POLICY_PATH


def load_provider_alert_policy(path: str | Path | None = None) -> dict[str, Any]:
    """Read and validate the source-controlled severity map without caching it."""

    source = _policy_path(path)
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except Exception as exc:  # config failures must never silently emit false HIGHs
        raise ProviderAlertPolicyError(
            f"provider alert policy unavailable at {source}: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise ProviderAlertPolicyError("provider alert policy must be a JSON object")
    rules = payload.get("rules")
    if payload.get("version") != 1 or not isinstance(rules, list) or not rules:
        raise ProviderAlertPolicyError("provider alert policy requires version=1 and rules")
    if _token(payload.get("default_severity")) not in {"high", "medium", "low"}:
        raise ProviderAlertPolicyError("provider alert policy has invalid default_severity")
    for rule in rules:
        if not isinstance(rule, dict) or not str(rule.get("id") or "").strip():
            raise ProviderAlertPolicyError("every provider alert rule requires an id")
        if _token(rule.get("severity")) not in {"high", "medium", "low"}:
            raise ProviderAlertPolicyError(
                f"provider alert rule {rule['id']} has invalid severity"
            )
        if not _token(rule.get("classification")):
            raise ProviderAlertPolicyError(
                f"provider alert rule {rule['id']} requires a classification"
            )
        try:
            [int(value) for value in rule.get("status_codes") or []]
        except (TypeError, ValueError) as exc:
            raise ProviderAlertPolicyError(
                f"provider alert rule {rule['id']} has an invalid status code"
            ) from exc
    try:
        throttle_minutes = int(payload.get("throttle_minutes"))
    except (TypeError, ValueError) as exc:
        raise ProviderAlertPolicyError("provider alert throttle_minutes must be an integer") from exc
    if throttle_minutes <= 0:
        raise ProviderAlertPolicyError("provider alert throttle_minutes must be positive")
    try:
        abnormal_volume_threshold = int(payload.get("abnormal_volume_threshold") or 20)
    except (TypeError, ValueError) as exc:
        raise ProviderAlertPolicyError(
            "provider alert abnormal_volume_threshold must be an integer"
        ) from exc
    if abnormal_volume_threshold <= 0:
        raise ProviderAlertPolicyError(
            "provider alert abnormal_volume_threshold must be positive"
        )
    signals = payload.get("escalation_signals") or {}
    if not isinstance(signals, dict):
        raise ProviderAlertPolicyError("provider alert escalation_signals must be an object")
    try:
        for patterns in signals.values():
            if not isinstance(patterns, list):
                raise TypeError("each escalation signal must contain a list of patterns")
            for pattern in patterns:
                re.compile(str(pattern))
    except (TypeError, re.error) as exc:
        raise ProviderAlertPolicyError(
            f"provider alert escalation_signals contain an invalid pattern: {exc}"
        ) from exc
    payload["_source"] = str(source.resolve())
    return payload


def _configured_values(policy: Mapping[str, Any], key: str) -> set[str]:
    return {
        _token(value)
        for rule in policy.get("rules") or []
        if isinstance(rule, Mapping)
        for value in rule.get(key) or []
        if _token(value)
    }


def _typed_reason(body: str, configured_reasons: set[str]) -> str | None:
    candidates: list[str] = []
    for pattern in (_TYPED_REASON_AFTER, _TYPED_REASON_BEFORE):
        candidates.extend(match.group(1) for match in pattern.finditer(body))
    normalized_body = _token(body)
    for candidate in candidates:
        normalized = _token(candidate)
        for configured in sorted(configured_reasons, key=len, reverse=True):
            if normalized == configured or normalized.startswith(f"{configured}_"):
                return configured
    # Explicit "typed nsfw" is common in compact provider diagnostics.
    for configured in sorted(configured_reasons, key=len, reverse=True):
        if f"typed_{configured}" in normalized_body:
            return configured
    return None


def _provider_alert_evidence(body: str, policy: Mapping[str, Any]) -> ProviderAlertEvidence:
    normalized_body = _token(body)
    providers = tuple(
        sorted(
            provider
            for provider in _configured_values(policy, "providers_any")
            if re.search(rf"(?<![a-z0-9]){re.escape(provider)}(?![a-z0-9])", normalized_body)
        )
    )
    configured_status_codes = {
        int(value)
        for rule in policy.get("rules") or []
        if isinstance(rule, Mapping)
        for value in rule.get("status_codes") or []
    }
    status_pattern = re.compile(
        r"(?i)\b(?:HTTP(?:\s+status)?\s*[:=]?\s*)?("
        + "|".join(str(value) for value in sorted(configured_status_codes))
        + r")\b"
    )
    status_match = status_pattern.search(body) if configured_status_codes else None
    configured_error_types = _configured_values(policy, "error_types")
    error_type = next(
        (
            error
            for error in sorted(configured_error_types, key=len, reverse=True)
            if re.search(
                rf"(?<![a-z0-9]){re.escape(error)}(?![a-z0-9])",
                normalized_body,
            )
        ),
        None,
    )
    count_match = _COUNT.search(body)
    return ProviderAlertEvidence(
        providers=providers,
        status_code=int(status_match.group(1)) if status_match else None,
        error_type=error_type,
        typed_reason=_typed_reason(
            body,
            _configured_values(policy, "typed_reasons"),
        ),
        occurrence_count=int(count_match.group(1)) if count_match else None,
    )


def _rule_matches(rule: Mapping[str, Any], evidence: ProviderAlertEvidence) -> bool:
    providers = {_token(value) for value in rule.get("providers_any") or []}
    if providers and not providers.intersection(evidence.providers):
        return False
    status_codes = {int(value) for value in rule.get("status_codes") or []}
    if status_codes and evidence.status_code not in status_codes:
        return False
    error_types = {_token(value) for value in rule.get("error_types") or []}
    if error_types and evidence.error_type not in error_types:
        return False
    reasons = {_token(value) for value in rule.get("typed_reasons") or []}
    if reasons and evidence.typed_reason not in reasons:
        return False
    if rule.get("typed_reason_absent") is True and evidence.typed_reason is not None:
        return False
    return bool(providers or status_codes or error_types or reasons)


def _escalation_reason(
    body: str,
    rule: Mapping[str, Any],
    policy: Mapping[str, Any],
    evidence: ProviderAlertEvidence,
) -> str | None:
    allowed = {_token(value) for value in rule.get("escalate_on") or []}
    configured = policy.get("escalation_signals") or {}
    if isinstance(configured, Mapping):
        for signal in allowed:
            patterns = configured.get(signal) or []
            if any(re.search(str(pattern), body, re.IGNORECASE) for pattern in patterns):
                return signal
    if "abnormal_volume" in allowed:
        threshold = int(policy.get("abnormal_volume_threshold") or 20)
        if evidence.occurrence_count is not None and evidence.occurrence_count >= threshold:
            return "abnormal_volume"
    return None


def _signature(body: str, evidence: ProviderAlertEvidence, classification: str) -> str:
    canonical_body = _ALERT_HEADER.sub("ALERT-SEVERITY", body)
    for pattern in _VOLATILE_SIGNATURE_PARTS:
        canonical_body = pattern.sub("<volatile>", canonical_body)
    canonical_body = " ".join(canonical_body.casefold().split())
    typed = {
        "providers": evidence.providers,
        "status_code": evidence.status_code,
        "error_type": evidence.error_type,
        "typed_reason": evidence.typed_reason,
        "classification": classification,
        "body": canonical_body,
    }
    return sha256(json.dumps(typed, sort_keys=True).encode("utf-8")).hexdigest()


def _render_body(body: str, severity: str, classification: str) -> str:
    label = f"{severity.upper()} · {classification.replace('_', '-')}"
    if _ALERT_HEADER.search(body):
        return _ALERT_HEADER.sub(lambda match: f"{match.group('prefix')}{label}", body, count=1)
    return body


def classify_provider_alert_body(
    body: str,
    *,
    policy_path: str | Path | None = None,
) -> ProviderAlertDecision | None:
    """Classify and rewrite an alert body, or return ``None`` for ordinary text."""

    original_body = str(body or "")
    if not _ALERT_MARKER.search(original_body) or not _PROVIDER_MARKER.search(original_body):
        return None
    policy = load_provider_alert_policy(policy_path)
    evidence = _provider_alert_evidence(original_body, policy)
    matched = next(
        (
            rule
            for rule in policy["rules"]
            if isinstance(rule, Mapping) and _rule_matches(rule, evidence)
        ),
        None,
    )
    if matched is None:
        severity = _token(policy["default_severity"])
        classification = "provider_failure"
        rule_id = "default_provider_failure"
        escalation_reason = None
    else:
        severity = _token(matched.get("severity"))
        classification = _token(matched.get("classification")) or "provider_failure"
        rule_id = str(matched.get("id") or "unnamed_rule")
        escalation_reason = _token(matched.get("escalation_reason")) or None
        detected_escalation = _escalation_reason(
            original_body,
            matched,
            policy,
            evidence,
        )
        if detected_escalation:
            severity = "high"
            escalation_reason = detected_escalation
    body_after_gate = _render_body(original_body, severity, classification)
    return ProviderAlertDecision(
        original_body=original_body,
        body=body_after_gate,
        classification=classification,
        severity=severity,
        rule_id=rule_id,
        signature=_signature(original_body, evidence, classification),
        throttle_minutes=int(policy["throttle_minutes"]),
        policy_source=str(policy["_source"]),
        evidence=evidence,
        escalation_reason=escalation_reason,
    )


__all__ = [
    "DEFAULT_PROVIDER_ALERT_POLICY_PATH",
    "PROVIDER_ALERT_POLICY_ENV",
    "ProviderAlertDecision",
    "ProviderAlertEvidence",
    "ProviderAlertPolicyError",
    "classify_provider_alert_body",
    "load_provider_alert_policy",
]
