"""Cross-run degradation tracking and required-digest escalation."""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from hashlib import sha256
import re
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo


DEGRADATION_ESCALATION_THRESHOLD = 3
DEGRADATION_TRACKING_SCHEMA_VERSION = 1
REQUIRED_DIGEST_HOURS = (8, 13, 18)
REQUIRED_DIGEST_TIMEZONE = "America/Toronto"


def _aware_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    aware = _aware_utc(value)
    return aware.isoformat() if aware is not None else None


def _text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def _dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list | tuple) else []


def _cause_key(parts: Iterable[Any]) -> str:
    normalized = "|".join(_text(part).lower() for part in parts if _text(part))
    return f"degradation:{sha256(normalized.encode('utf-8')).hexdigest()[:16]}"


def _failure_cause(value: Mapping[str, Any]) -> dict[str, str]:
    failure = dict(value)
    tool = _text(failure.get("tool"))
    stage = _text(failure.get("stage"))
    repo = _text(failure.get("repo"))
    kind = _text(failure.get("kind"))
    error = _text(
        failure.get("error")
        or failure.get("reason")
        or failure.get("message")
        or "evidence reader failed"
    )
    scope_parts = [part for part in (tool, stage, repo) if part]
    summary = " / ".join(scope_parts)
    summary = f"{summary}: {error}" if summary else error
    return {
        "key": _cause_key((kind, tool, stage, repo, error)),
        "summary": summary,
    }


def degradation_causes(
    *,
    status: str,
    error: str | None,
    evidence_health: Mapping[str, Any] | None,
    reported_evidence_health: Mapping[str, Any] | None = None,
) -> list[dict[str, str]]:
    """Return stable, human-readable causes recorded by one scheduled run."""

    health = _dict(evidence_health)
    health_degraded = _text(health.get("status")).lower() == "degraded"
    reported_health = _dict(reported_evidence_health)
    reported_degraded = _text(reported_health.get("status")).lower() == "degraded"
    causes: list[dict[str, str]] = []
    for failure in _list(health.get("failures")):
        if isinstance(failure, Mapping):
            causes.append(_failure_cause(failure))

    for field in ("causes", "gaps"):
        for raw_cause in _list(health.get(field)):
            if isinstance(raw_cause, Mapping):
                causes.append(_failure_cause(raw_cause))
            elif _text(raw_cause):
                summary = _text(raw_cause)
                causes.append({"key": _cause_key((field, summary)), "summary": summary})

    status_degraded = _text(status).lower() == "degraded"
    if not causes and (health_degraded or reported_degraded or status_degraded):
        summary = (
            _text(reported_health.get("cause"))
            or _text(error)
            or _text(health.get("reason"))
        )
        if not summary:
            summary = "Evidence health degraded without a named cause."
        causes.append({"key": _cause_key(("run", summary)), "summary": summary})

    deduped: dict[str, dict[str, str]] = {}
    for cause in causes:
        deduped.setdefault(cause["key"], cause)
    return list(deduped.values())


def next_required_digest_at(scheduled_for: datetime) -> datetime:
    """Return the first required 08:00/13:00/18:00 ET digest after a run."""

    scheduled_utc = _aware_utc(scheduled_for)
    if scheduled_utc is None:
        raise ValueError("scheduled_for is required")
    zone = ZoneInfo(REQUIRED_DIGEST_TIMEZONE)
    local = scheduled_utc.astimezone(zone)
    for hour in REQUIRED_DIGEST_HOURS:
        candidate = datetime.combine(local.date(), time(hour=hour), tzinfo=zone)
        if candidate > local:
            return candidate.astimezone(timezone.utc)
    tomorrow = local.date() + timedelta(days=1)
    return datetime.combine(
        tomorrow,
        time(hour=REQUIRED_DIGEST_HOURS[0]),
        tzinfo=zone,
    ).astimezone(timezone.utc)


def empty_degradation_state() -> dict[str, Any]:
    return {
        "schema_version": DEGRADATION_TRACKING_SCHEMA_VERSION,
        "threshold": DEGRADATION_ESCALATION_THRESHOLD,
        "digest_cadence": {
            "timezone": REQUIRED_DIGEST_TIMEZONE,
            "local_hours": list(REQUIRED_DIGEST_HOURS),
        },
        "active_causes": [],
        "pending_escalations": [],
    }


def _persistent_state(value: Mapping[str, Any] | None) -> dict[str, Any]:
    state = empty_degradation_state()
    raw = _dict(value)
    state["active_causes"] = [
        dict(item) for item in _list(raw.get("active_causes")) if isinstance(item, Mapping)
    ]
    state["pending_escalations"] = [
        dict(item)
        for item in _list(raw.get("pending_escalations"))
        if isinstance(item, Mapping)
    ]
    return state


def degradation_tracking_for_run(
    state: Mapping[str, Any] | None,
    *,
    scheduled_for: datetime | None,
) -> dict[str, Any]:
    """Snapshot durable degradation state and mark escalations due in this run."""

    snapshot = _persistent_state(state)
    scheduled_utc = _aware_utc(scheduled_for)
    mandatory: list[dict[str, Any]] = []
    for escalation in snapshot["pending_escalations"]:
        target_text = _text(escalation.get("next_required_digest_at"))
        try:
            target = datetime.fromisoformat(target_text) if target_text else None
        except ValueError:
            target = None
        if scheduled_utc is not None and target is not None and scheduled_utc >= _aware_utc(target):
            mandatory.append(dict(escalation))
    snapshot["mandatory_in_current_digest"] = bool(mandatory)
    snapshot["mandatory_causes"] = mandatory
    return snapshot


def advance_degradation_state(
    run_tracking: Mapping[str, Any] | None,
    *,
    causes: Iterable[Mapping[str, Any]],
    scheduled_for: datetime,
    mandatory_digest_satisfied: bool,
) -> dict[str, Any]:
    """Advance counters after a run and retain escalations until a valid digest names them."""

    previous = _persistent_state(run_tracking)
    previous_active = {
        _text(item.get("key")): dict(item)
        for item in previous["active_causes"]
        if _text(item.get("key"))
    }
    pending = {
        _text(item.get("key")): dict(item)
        for item in previous["pending_escalations"]
        if _text(item.get("key"))
    }
    run_snapshot = _dict(run_tracking)
    if mandatory_digest_satisfied and run_snapshot.get("mandatory_in_current_digest"):
        for item in _list(run_snapshot.get("mandatory_causes")):
            if isinstance(item, Mapping):
                pending.pop(_text(item.get("key")), None)

    scheduled_iso = _iso(scheduled_for)
    active: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_cause in causes:
        cause = _dict(raw_cause)
        key = _text(cause.get("key"))
        summary = _text(cause.get("summary"))
        if not key or not summary or key in seen:
            continue
        seen.add(key)
        prior = previous_active.get(key, {})
        count = int(prior.get("consecutive_degraded_runs") or 0) + 1
        item = {
            "key": key,
            "summary": summary,
            "consecutive_degraded_runs": count,
            "first_seen_at": prior.get("first_seen_at") or scheduled_iso,
            "last_seen_at": scheduled_iso,
        }
        active.append(item)
        if count >= DEGRADATION_ESCALATION_THRESHOLD:
            escalation = pending.get(key)
            if escalation is None:
                escalation = {
                    **item,
                    "escalated_at": scheduled_iso,
                    "next_required_digest_at": _iso(next_required_digest_at(scheduled_for)),
                }
            else:
                escalation.update(
                    {
                        "summary": summary,
                        "consecutive_degraded_runs": count,
                        "last_seen_at": scheduled_iso,
                    }
                )
            pending[key] = escalation

    state = empty_degradation_state()
    state["active_causes"] = active
    state["pending_escalations"] = list(pending.values())
    return state


def mandatory_escalations(run_tracking: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    tracking = _dict(run_tracking)
    if not tracking.get("mandatory_in_current_digest"):
        return []
    return [
        dict(item)
        for item in _list(tracking.get("mandatory_causes"))
        if isinstance(item, Mapping)
    ]


__all__ = [
    "DEGRADATION_ESCALATION_THRESHOLD",
    "REQUIRED_DIGEST_HOURS",
    "REQUIRED_DIGEST_TIMEZONE",
    "advance_degradation_state",
    "degradation_causes",
    "degradation_tracking_for_run",
    "empty_degradation_state",
    "mandatory_escalations",
    "next_required_digest_at",
]
