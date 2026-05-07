"""Shadow-safe routing marketplace for model selection."""
from __future__ import annotations

import logging
import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from hashlib import sha1
from statistics import median
from typing import Any

from sqlalchemy import select, text

from brain.kernel.common.env import env_flag as _shared_env_flag
from brain.kernel.common.env import env_float as _shared_env_float
from brain.kernel.common.env import env_int as _shared_env_int

from brain.platform.db.models.agent import AgentApiCall
from brain.platform.db.models.routing import ProviderHealthSnapshot, RoutingDecision, RoutingExperiment
from brain.platform.db.repositories.unit_of_work import UnitOfWork
from brain.platform.providers.model_policy import (
    DEFAULT_MODEL_TIER,
    DEFAULT_PROVIDER_MODEL_MAPS,
    SkillRoutingProfile,
    calculate_model_cost,
    get_provider_model_map,
    infer_provider_from_model,
    normalize_model_name,
    normalize_model_tier,
    normalize_runtime_provider,
    resolve_default_provider,
    resolve_provider_selection,
    resolve_skill_routing_profile,
    resolve_skill_runtime,
)

logger = logging.getLogger("routing.marketplace")


def _env_flag(name: str, default: bool) -> bool:
    return _shared_env_flag(name, default=default, false_values={"0", "false", "no", "off", ""})


def _env_int(name: str, default: int) -> int:
    return _shared_env_int(name, default)


def _env_float(name: str, default: float) -> float:
    return _shared_env_float(name, default)


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    if isinstance(value, tuple):
        return [_jsonable(v) for v in value]
    return value


def _model_identity(model: str | None, provider_hint: str | None = None) -> tuple[str | None, str | None]:
    if not model:
        return provider_hint, None
    value = model.strip()
    if "/" in value:
        provider, bare = value.split("/", 1)
        return provider or provider_hint, bare or None
    provider = infer_provider_from_model(value, default=provider_hint)
    return provider, value


def prefixed_runtime_model(provider: str, model_name: str) -> str:
    """Return the runtime model string expected by agent invocation code."""
    provider = normalize_runtime_provider(provider)
    if model_name.startswith(("anthropic/", "openai/")):
        return model_name
    if model_name.startswith("anthropic:"):
        return f"anthropic/{model_name[len('anthropic:'):]}"
    if model_name.startswith("openai:"):
        return f"openai/{model_name[len('openai:'):]}"
    return f"{provider}/{model_name}"


def _percentile(values: list[int], percentile: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * percentile
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    weight = rank - low
    return int(round(ordered[low] * (1 - weight) + ordered[high] * weight))


def _window_range(hours: int) -> tuple[datetime, datetime]:
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=hours)
    return start, end


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _weighted_average(values: list[tuple[float, float]]) -> float | None:
    weighted_total = 0.0
    total_weight = 0.0
    for value, weight in values:
        if value is None or weight is None:
            continue
        if weight <= 0:
            continue
        weighted_total += float(value) * float(weight)
        total_weight += float(weight)
    if not total_weight:
        return None
    return round(weighted_total / total_weight)


def _model_rank(model: str | None) -> int:
    normalized = normalize_model_name(model)
    if normalized == "local":
        return 0
    for model_map in DEFAULT_PROVIDER_MODEL_MAPS.values():
        for tier, model_name in model_map.items():
            if normalized.endswith(f"/{model_name}") or normalized == model_name:
                return {"local": 0, "low": 1, "medium": 2, "high": 3}.get(tier, 1)
    if "nano" in normalized:
        return 0
    if "mini" in normalized:
        return 1
    if normalized.endswith("/gpt-5.4") or normalized.endswith("/gpt-4o"):
        return 2
    if "pro" in normalized:
        return 3
    return 1


def _complexity_rank(contract_complexity: str | None, risk_class: str | None) -> int:
    base = {
        "simple": 0,
        "skill_scoped": 1,
        "forced_skill": 1,
        "non_trivial": 1,
        "medium": 1,
        "high": 2,
        "critical": 3,
    }.get((contract_complexity or "").strip().lower(), 1)
    if (risk_class or "").strip().lower() in {"high", "critical", "safety", "sensitive"}:
        base += 1
    return min(base, 3)


def _budget_score(candidate_cost: float, budget: dict[str, Any] | None) -> tuple[float, dict[str, Any]]:
    budget = budget or {}
    max_cost = budget.get("max_cost")
    target_cost = budget.get("target_cost")
    if max_cost is not None:
        try:
            max_cost_value = float(max_cost)
            if max_cost_value <= 0:
                return 0.0, {"max_cost": max_cost_value, "status": "invalid"}
            ratio = candidate_cost / max_cost_value
            if ratio <= 1:
                score = max(0.2, 1.0 - max(0.0, ratio - 0.7))
                return round(score, 4), {"max_cost": max_cost_value, "ratio": round(ratio, 4), "status": "within_budget"}
            return 0.2, {"max_cost": max_cost_value, "ratio": round(ratio, 4), "status": "over_budget"}
        except Exception:
            return 1.0, {"status": "unparseable"}
    if target_cost is not None:
        try:
            target_cost_value = float(target_cost)
            if target_cost_value <= 0:
                return 1.0, {"target_cost": target_cost_value, "status": "invalid"}
            ratio = candidate_cost / target_cost_value
            score = max(0.25, min(1.0, 1.0 - max(0.0, ratio - 1.0)))
            return round(score, 4), {"target_cost": target_cost_value, "ratio": round(ratio, 4), "status": "targeted"}
        except Exception:
            return 1.0, {"status": "unparseable"}
    return 1.0, {"status": "unbounded"}


def _health_confidence(snapshot: dict[str, Any] | None, *, min_samples: int) -> dict[str, Any]:
    snapshot = snapshot or {}
    sample_count = int(snapshot.get("sample_count") or 0)
    window_end = snapshot.get("window_end")
    age_hours = None
    if isinstance(window_end, datetime):
        age_hours = round((datetime.now(timezone.utc) - window_end).total_seconds() / 3600.0, 4)
    evidence_strength = "strong" if sample_count >= max(min_samples, 1) * 2 else "moderate" if sample_count >= min_samples else "sparse"
    if age_hours is not None and age_hours > 0:
        if age_hours >= 48:
            evidence_strength = "stale"
        elif age_hours >= 24 and evidence_strength != "strong":
            evidence_strength = "aging"
    return {
        "sample_count": sample_count,
        "age_hours": age_hours,
        "strength": evidence_strength,
        "source": snapshot.get("source"),
    }


def _load_provider_health_rollup(
    session,
    provider: str,
    *,
    lookback_hours: int,
) -> dict[str, Any] | None:
    window_start, window_end = _window_range(lookback_hours)
    try:
        rows = session.execute(
            select(ProviderHealthSnapshot)
            .where(
                ProviderHealthSnapshot.provider == provider,
                ProviderHealthSnapshot.window_end >= window_start,
            )
            .order_by(ProviderHealthSnapshot.window_end.desc(), ProviderHealthSnapshot.id.desc())
        ).scalars().all()
    except Exception:
        return None

    if not rows:
        return None

    weighted_rows = [(float(row.sample_count or 0), row) for row in rows if int(row.sample_count or 0) > 0]
    if not weighted_rows:
        weighted_rows = [(1.0, row) for row in rows]

    window_start = min((row.window_start for row in rows if row.window_start), default=window_start)
    window_end = max((row.window_end for row in rows if row.window_end), default=window_end)
    sample_count = sum(int(row.sample_count or 0) for row in rows)
    if sample_count <= 0:
        sample_count = len(rows)

    def _aggregate(attr: str) -> int | None:
        value = _weighted_average([
            (getattr(row, attr), weight)
            for weight, row in weighted_rows
            if getattr(row, attr) is not None
        ])
        return int(value) if value is not None else None

    def _rate(attr: str) -> float | None:
        value = _weighted_average([
            (_safe_float(getattr(row, attr), 0.0), weight)
            for weight, row in weighted_rows
            if getattr(row, attr) is not None
        ])
        return round(float(value), 4) if value is not None else None

    return {
        "provider": provider,
        "model": "__provider_rollup__",
        "window_start": window_start,
        "window_end": window_end,
        "p50_latency_ms": _aggregate("p50_latency_ms"),
        "p95_latency_ms": _aggregate("p95_latency_ms"),
        "error_rate": _rate("error_rate"),
        "auth_fail_rate": _rate("auth_fail_rate"),
        "rate_limit_rate": _rate("rate_limit_rate"),
        "sample_count": sample_count,
        "source": "provider_rollup",
    }


def _load_health_context(
    session,
    provider: str,
    model: str,
    *,
    lookback_hours: int,
) -> dict[str, Any]:
    model_snapshot = _load_latest_health_snapshot(session, provider, model)
    provider_snapshot = _load_provider_health_rollup(session, provider, lookback_hours=lookback_hours)
    active_snapshot = model_snapshot or provider_snapshot
    if not active_snapshot and provider_snapshot:
        active_snapshot = provider_snapshot

    return {
        "model_snapshot": model_snapshot,
        "provider_snapshot": provider_snapshot,
        "snapshot": active_snapshot,
        "model_confidence": _health_confidence(model_snapshot, min_samples=1),
        "provider_confidence": _health_confidence(provider_snapshot, min_samples=1),
    }


@dataclass(frozen=True)
class RoutingCandidate:
    provider: str
    model: str
    reasoning_effort: str | None = None
    eligible: bool = True
    exclusion_reason: str | None = None
    score: float = 0.0
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RoutingDecisionResult:
    run_id: int | None
    task_family: str
    lane: str
    decision_mode: str
    selected_provider: str
    selected_model: str
    selected_reasoning_effort: str | None
    legacy_provider: str
    legacy_model: str
    legacy_reasoning_effort: str | None
    inputs: dict[str, Any]
    candidate_scores: list[dict[str, Any]]
    constraints: dict[str, Any]
    experiment_id: int | None = None
    applied: bool = False
    fallback_used: bool = False
    post_run_outcome: dict[str, Any] | None = None
    decision_id: int | None = None


def get_routing_marketplace_flags() -> dict[str, Any]:
    """Return the current routing rollout flags."""
    active = _env_flag("CORTEX_ROUTING_MARKETPLACE_ACTIVE", False)
    shadow = _env_flag("CORTEX_ROUTING_MARKETPLACE_SHADOW", True if not active else False)
    force_legacy = _env_flag("CORTEX_ROUTING_FORCE_LEGACY", False)
    return {
        "shadow": shadow or not active,
        "active": active,
        "force_legacy": force_legacy,
        "allow_provider_switch": _env_flag("CORTEX_ROUTING_ALLOW_PROVIDER_SWITCH", False),
        "allow_model_switch_within_provider": _env_flag("CORTEX_ROUTING_ALLOW_MODEL_SWITCH_WITHIN_PROVIDER", False),
        "require_min_samples": _env_int("CORTEX_ROUTING_REQUIRE_MIN_SAMPLES", 5),
        "lookback_hours": _env_int("CORTEX_ROUTING_HEALTH_LOOKBACK_HOURS", 24),
        "stale_after_hours": _env_int("CORTEX_ROUTING_STALE_AFTER_HOURS", 24),
        "include_warm_state": _env_flag("CORTEX_ROUTING_INCLUDE_WARM_STATE", False),
        "canary_percent": _env_float("CORTEX_ROUTING_CANARY_PERCENT", 10.0),
        "require_eval_pass": _env_flag("CORTEX_ROUTING_REQUIRE_EVAL_PASS", True),
        "min_eval_score": _env_float("CORTEX_ROUTING_MIN_EVAL_SCORE", 0.85),
        "min_verifier_pass_rate": _env_float("CORTEX_ROUTING_MIN_VERIFIER_PASS_RATE", 0.85),
        "max_cost_ratio": _env_float("CORTEX_ROUTING_MAX_COST_RATIO", 0.0),
        "max_p95_latency_ms": _env_int("CORTEX_ROUTING_MAX_P95_LATENCY_MS", 0),
        "rollback_window_decisions": _env_int("CORTEX_ROUTING_ROLLBACK_WINDOW_DECISIONS", 10),
        "rollback_min_failed_decisions": _env_int("CORTEX_ROUTING_ROLLBACK_MIN_FAILED_DECISIONS", 3),
    }


def _task_family_match(value: str | None, task_family: str) -> bool:
    if not value:
        return True
    value = value.strip()
    if not value:
        return True
    return task_family == value or task_family.startswith(value)


def _resolve_active_experiment(session, task_family: str) -> RoutingExperiment | None:
    try:
        rows = session.scalars(
            select(RoutingExperiment)
            .where(RoutingExperiment.status == "active")
            .order_by(RoutingExperiment.started_at.desc().nullslast(), RoutingExperiment.id.desc())
        ).all()
    except Exception:
        return None
    for row in rows:
        filters = row.task_family_filter or []
        if not filters:
            return row
        for candidate in filters:
            if isinstance(candidate, str) and _task_family_match(candidate, task_family):
                return row
    return None


def _parse_allocation_policy(policy: str | None, flags: dict[str, Any]) -> dict[str, Any]:
    """Normalize experiment allocation policy into explicit canary gates.

    ``routing_experiments.allocation_policy`` is intentionally text so ops can
    ship a staged rollout without a migration for every new gate. Supported
    examples:

    - ``{"canary_percent": 25, "min_eval_score": 0.9}``
    - ``canary:10``
    - ``active`` / ``shadow``
    """
    parsed: dict[str, Any] = {
        "canary_percent": float(flags.get("canary_percent", 10.0) or 0.0),
        "require_eval_pass": bool(flags.get("require_eval_pass", True)),
        "min_eval_score": float(flags.get("min_eval_score", 0.85) or 0.0),
        "min_verifier_pass_rate": float(flags.get("min_verifier_pass_rate", 0.85) or 0.0),
        "max_cost_ratio": float(flags.get("max_cost_ratio", 0.0) or 0.0),
        "max_p95_latency_ms": int(flags.get("max_p95_latency_ms", 0) or 0),
        "rollback_window_decisions": int(flags.get("rollback_window_decisions", 10) or 10),
        "rollback_min_failed_decisions": int(flags.get("rollback_min_failed_decisions", 3) or 3),
    }
    text_policy = (policy or "").strip()
    if not text_policy:
        return parsed

    loaded: dict[str, Any] | None = None
    if text_policy.startswith("{"):
        try:
            raw = json.loads(text_policy)
            if isinstance(raw, dict):
                loaded = raw
        except Exception:
            loaded = None
    elif text_policy.lower().startswith("canary:"):
        loaded = {"canary_percent": text_policy.split(":", 1)[1].strip()}
    elif text_policy.lower() in {"active", "all", "full"}:
        loaded = {"canary_percent": 100}
    elif text_policy.lower() in {"shadow", "off", "none"}:
        loaded = {"canary_percent": 0}

    if loaded:
        for key in parsed:
            if key in loaded:
                parsed[key] = loaded[key]

    parsed["canary_percent"] = max(0.0, min(100.0, _safe_float(parsed.get("canary_percent"), 0.0)))
    parsed["min_eval_score"] = max(0.0, min(1.0, _safe_float(parsed.get("min_eval_score"), 0.0)))
    parsed["min_verifier_pass_rate"] = max(0.0, min(1.0, _safe_float(parsed.get("min_verifier_pass_rate"), 0.0)))
    parsed["max_cost_ratio"] = max(0.0, _safe_float(parsed.get("max_cost_ratio"), 0.0))
    parsed["max_p95_latency_ms"] = max(0, int(_safe_float(parsed.get("max_p95_latency_ms"), 0.0)))
    parsed["rollback_window_decisions"] = max(1, int(_safe_float(parsed.get("rollback_window_decisions"), 10.0)))
    parsed["rollback_min_failed_decisions"] = max(1, int(_safe_float(parsed.get("rollback_min_failed_decisions"), 3.0)))
    parsed["require_eval_pass"] = bool(parsed.get("require_eval_pass"))
    return parsed


def _canary_bucket(
    *,
    org_id: str | None,
    user_id: str | None,
    task_family: str,
    lane: str,
    experiment_name: str | None,
    run_id: int | None,
) -> int:
    scope_key = "|".join([
        str(org_id or "no-org"),
        str(user_id or "no-user"),
        str(task_family or "general"),
        str(lane or "coordinator"),
        str(experiment_name or "default"),
        str(run_id or ""),
    ])
    digest = sha1(scope_key.encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % 100


def _canary_allocation(
    *,
    policy: dict[str, Any],
    org_id: str | None,
    user_id: str | None,
    task_family: str,
    lane: str,
    experiment_name: str | None,
    run_id: int | None,
) -> dict[str, Any]:
    org_id = _optional_text(org_id)
    user_id = _optional_text(user_id)
    task_family = _optional_text(task_family) or "general"
    lane = _optional_text(lane) or "coordinator"
    experiment_name = _optional_text(experiment_name)
    percent = max(0.0, min(100.0, _safe_float(policy.get("canary_percent"), 0.0)))
    bucket = _canary_bucket(
        org_id=org_id,
        user_id=user_id,
        task_family=task_family,
        lane=lane,
        experiment_name=experiment_name,
        run_id=run_id,
    )
    return {
        "percent": percent,
        "bucket": bucket,
        "allocated": bucket < percent,
        "scope": {
            "org_id": org_id,
            "user_id": user_id,
            "task_family": task_family,
            "lane": lane,
            "experiment_name": experiment_name,
        },
    }


def _eval_gate(genome_signals: dict[str, Any] | None, policy: dict[str, Any]) -> dict[str, Any]:
    signals = genome_signals or {}
    require_eval = bool(policy.get("require_eval_pass", True))
    min_score = _safe_float(policy.get("min_eval_score"), 0.85)
    passed = signals.get("routing_eval_passed")
    if passed is None:
        passed = signals.get("eval_passed")
    score = signals.get("routing_eval_score")
    if score is None:
        score = signals.get("eval_score")
    numeric_score = _safe_float(score, -1.0) if score is not None else None

    ok = True
    reason = None
    if require_eval:
        if passed is not True:
            ok = False
            reason = "eval_gate_failed"
        elif numeric_score is not None and numeric_score < min_score:
            ok = False
            reason = "eval_score_below_threshold"

    return {
        "ok": ok,
        "reason": reason,
        "required": require_eval,
        "passed": passed,
        "score": numeric_score,
        "min_score": min_score,
    }


def _canary_gate_status(
    *,
    selected_candidate: dict[str, Any],
    legacy_candidate: dict[str, Any],
    allocation: dict[str, Any],
    eval_gate: dict[str, Any],
    policy: dict[str, Any],
) -> dict[str, Any]:
    """Return whether a non-legacy candidate may be applied actively."""
    if selected_candidate.get("is_legacy"):
        return {"ok": True, "reason": None, "checks": {"legacy": True}}

    checks: dict[str, Any] = {
        "allocation": allocation,
        "eval": eval_gate,
    }
    if not allocation.get("allocated"):
        return {"ok": False, "reason": "canary_not_allocated", "checks": checks}
    if not eval_gate.get("ok"):
        return {"ok": False, "reason": eval_gate.get("reason") or "eval_gate_failed", "checks": checks}

    evidence = selected_candidate.get("evidence", {}) if isinstance(selected_candidate, dict) else {}
    verifier = evidence.get("verifier") if isinstance(evidence.get("verifier"), dict) else {}
    verifier_success_rate = verifier.get("success_rate")
    min_verifier_pass_rate = _safe_float(policy.get("min_verifier_pass_rate"), 0.85)
    checks["verifier"] = {
        "success_rate": verifier_success_rate,
        "min_success_rate": min_verifier_pass_rate,
    }
    if verifier_success_rate is None or _safe_float(verifier_success_rate, 0.0) < min_verifier_pass_rate:
        return {"ok": False, "reason": "verifier_gate_failed", "checks": checks}

    candidate_cost = _safe_float(evidence.get("cost_estimate"), 0.0)
    legacy_evidence = legacy_candidate.get("evidence", {}) if isinstance(legacy_candidate, dict) else {}
    legacy_cost = _safe_float(legacy_evidence.get("cost_estimate"), candidate_cost or 0.0)
    max_cost_ratio = _safe_float(policy.get("max_cost_ratio"), 0.0)
    cost_ratio = round(candidate_cost / legacy_cost, 4) if legacy_cost > 0 else None
    checks["cost"] = {
        "candidate_cost": candidate_cost,
        "legacy_cost": legacy_cost,
        "ratio": cost_ratio,
        "max_ratio": max_cost_ratio or None,
    }
    if max_cost_ratio and cost_ratio is not None and cost_ratio > max_cost_ratio:
        return {"ok": False, "reason": "cost_gate_failed", "checks": checks}

    health = evidence.get("health") if isinstance(evidence.get("health"), dict) else {}
    if not health:
        health = evidence.get("provider_health") if isinstance(evidence.get("provider_health"), dict) else {}
    p95_latency_ms = health.get("p95_latency_ms")
    max_p95_latency_ms = int(policy.get("max_p95_latency_ms") or 0)
    checks["latency"] = {
        "p95_latency_ms": p95_latency_ms,
        "max_p95_latency_ms": max_p95_latency_ms or None,
    }
    if max_p95_latency_ms and p95_latency_ms is not None and int(p95_latency_ms) > max_p95_latency_ms:
        return {"ok": False, "reason": "latency_gate_failed", "checks": checks}

    return {"ok": True, "reason": None, "checks": checks}


def _post_run_outcome_failed(outcome: dict[str, Any], policy: dict[str, Any]) -> bool:
    if outcome.get("eval_passed") is False or outcome.get("verifier_passed") is False:
        return True
    score = outcome.get("eval_score")
    if score is not None and _safe_float(score, 1.0) < _safe_float(policy.get("min_eval_score"), 0.85):
        return True
    verifier_rate = outcome.get("verifier_pass_rate")
    if verifier_rate is not None and _safe_float(verifier_rate, 1.0) < _safe_float(policy.get("min_verifier_pass_rate"), 0.85):
        return True
    return bool(outcome.get("degraded"))


def _maybe_roll_back_experiment(session, experiment: RoutingExperiment | None, policy: dict[str, Any]) -> str | None:
    if not experiment or experiment.status != "active":
        return None
    window_size = int(policy.get("rollback_window_decisions") or 10)
    min_failed = int(policy.get("rollback_min_failed_decisions") or 3)
    try:
        rows = session.scalars(
            select(RoutingDecision)
            .where(
                RoutingDecision.experiment_id == experiment.id,
                RoutingDecision.applied.is_(True),
            )
            .order_by(RoutingDecision.created_at.desc(), RoutingDecision.id.desc())
            .limit(window_size)
        ).all()
    except Exception:
        return None
    failed = 0
    observed = 0
    for row in rows:
        outcome = row.post_run_outcome if isinstance(row.post_run_outcome, dict) else None
        if not outcome:
            continue
        observed += 1
        if _post_run_outcome_failed(outcome, policy):
            failed += 1
    if observed >= min_failed and failed >= min_failed:
        experiment.status = "rolled_back"
        experiment.ended_at = datetime.now(timezone.utc)
        return f"experiment_rolled_back_after_{failed}_failed_outcomes"
    return None


def _provider_health_from_rows(
    api_rows: list[dict[str, Any]],
    summary_rows: list[dict[str, Any]],
    *,
    window_start: datetime,
    window_end: datetime,
    source: str,
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], dict[str, Any]] = defaultdict(lambda: {
        "latencies": [],
        "errors": 0,
        "auth_fails": 0,
        "rate_limits": 0,
        "samples": 0,
    })

    for row in api_rows:
        provider, model = _model_identity(row.get("model"))
        if not provider or not model:
            continue
        bucket = grouped[(provider, model)]
        bucket["samples"] += 1
        latency = row.get("latency_ms")
        if latency is not None:
            bucket["latencies"].append(int(latency))
        error_text = f"{row.get('error') or ''} {row.get('status') or ''}".lower()
        if row.get("error"):
            bucket["errors"] += 1
        if any(token in error_text for token in ("auth", "unauthor", "forbidden", "permission", "401")):
            bucket["auth_fails"] += 1
        if any(token in error_text for token in ("rate limit", "429", "quota")):
            bucket["rate_limits"] += 1

    # Verifier and settlement rows can seed additional snapshots if the API call
    # stream is sparse for a given model.
    for row in summary_rows:
        provider = (row.get("provider_used") or "").strip().lower()
        model = (row.get("model_used") or "").strip()
        if not provider or not model:
            continue
        provider, model = _model_identity(model, provider_hint=provider)
        if not provider or not model:
            continue
        bucket = grouped[(provider, model)]
        bucket["samples"] += 1
        if (row.get("verifier_status") or "").strip().lower() in {"failed", "regressed", "blocked"}:
            bucket["errors"] += 1
        if (row.get("settlement_state") or "").strip().lower() in {"settled_failure", "cancelled", "canceled", "superseded"}:
            bucket["errors"] += 1

    snapshots: list[dict[str, Any]] = []
    for (provider, model), bucket in grouped.items():
        samples = bucket["samples"]
        if not samples:
            continue
        latencies = bucket["latencies"]
        snapshots.append({
            "provider": provider,
            "model": model,
            "window_start": window_start,
            "window_end": window_end,
            "p50_latency_ms": int(median(latencies)) if latencies else None,
            "p95_latency_ms": _percentile(latencies, 0.95),
            "error_rate": round(bucket["errors"] / samples, 4),
            "auth_fail_rate": round(bucket["auth_fails"] / samples, 4),
            "rate_limit_rate": round(bucket["rate_limits"] / samples, 4),
            "sample_count": samples,
            "source": source,
        })
    return snapshots


def refresh_provider_health_snapshots(
    *,
    lookback_hours: int | None = None,
) -> list[dict[str, Any]]:
    """Aggregate recent run health into persistable snapshots."""
    flags = get_routing_marketplace_flags()
    lookback_hours = lookback_hours or flags["lookback_hours"]
    window_start, window_end = _window_range(lookback_hours)

    try:
        with UnitOfWork() as uow:
            api_rows = uow.session.execute(
                text(
                    """
                    SELECT model, latency_ms, error, status, created_at
                    FROM agent_api_calls
                    WHERE created_at >= :window_start
                    """
                ),
                {"window_start": window_start},
            ).mappings().all()
            summary_rows = uow.session.execute(
                text(
                    """
                    SELECT provider_used, model_used, verifier_status, settlement_state, created_at
                    FROM run_run_summaries
                    WHERE created_at >= :window_start
                    """
                ),
                {"window_start": window_start},
            ).mappings().all()

            snapshots = _provider_health_from_rows(
                list(api_rows),
                list(summary_rows),
                window_start=window_start,
                window_end=window_end,
                source="agent_api_calls+run_run_summaries",
            )

            for payload in snapshots:
                existing = uow.session.execute(
                    select(ProviderHealthSnapshot).where(
                        ProviderHealthSnapshot.provider == payload["provider"],
                        ProviderHealthSnapshot.model == payload["model"],
                        ProviderHealthSnapshot.window_start == payload["window_start"],
                        ProviderHealthSnapshot.window_end == payload["window_end"],
                        ProviderHealthSnapshot.source == payload["source"],
                    )
                ).scalar_one_or_none()
                if existing:
                    existing.p50_latency_ms = payload["p50_latency_ms"]
                    existing.p95_latency_ms = payload["p95_latency_ms"]
                    existing.error_rate = payload["error_rate"]
                    existing.auth_fail_rate = payload["auth_fail_rate"]
                    existing.rate_limit_rate = payload["rate_limit_rate"]
                    existing.sample_count = payload["sample_count"]
                else:
                    uow.session.add(ProviderHealthSnapshot(**payload))

            return snapshots
    except Exception as exc:
        logger.debug("refresh_provider_health_snapshots failed: %s", exc)
        return []


def _load_latest_health_snapshot(session, provider: str, model: str) -> dict[str, Any] | None:
    try:
        row = session.execute(
            select(ProviderHealthSnapshot)
            .where(
                ProviderHealthSnapshot.provider == provider,
                ProviderHealthSnapshot.model == model,
            )
            .order_by(ProviderHealthSnapshot.window_end.desc(), ProviderHealthSnapshot.id.desc())
        ).scalars().first()
    except Exception:
        return None
    if not row:
        return None
    return {
        "provider": row.provider,
        "model": row.model,
        "window_start": row.window_start,
        "window_end": row.window_end,
        "p50_latency_ms": row.p50_latency_ms,
        "p95_latency_ms": row.p95_latency_ms,
        "error_rate": row.error_rate,
        "auth_fail_rate": row.auth_fail_rate,
        "rate_limit_rate": row.rate_limit_rate,
        "sample_count": row.sample_count,
        "source": row.source,
    }


def _load_verifier_evidence(
    session,
    provider: str,
    model: str,
    *,
    lookback_hours: int,
) -> dict[str, Any]:
    window_start, _ = _window_range(lookback_hours)
    try:
        rows = session.execute(
            text(
                """
                SELECT verifier_status
                FROM run_run_summaries
                WHERE created_at >= :window_start
                  AND provider_used = :provider
                  AND model_used = :model
                """
            ),
            {"window_start": window_start, "provider": provider, "model": model},
        ).mappings().all()
    except Exception:
        rows = []

    sample_count = 0
    success_count = 0
    for row in rows:
        sample_count += 1
        if (row.get("verifier_status") or "").strip().lower() in {"passed", "success", "clean", "ok", "resolved"}:
            success_count += 1
    success_rate = round(success_count / sample_count, 4) if sample_count else None
    return {
        "sample_count": sample_count,
        "success_rate": success_rate,
    }


def _score_candidate(
    *,
    candidate_provider: str,
    candidate_model: str,
    legacy_provider: str,
    legacy_model: str,
    snapshot: dict[str, Any] | None,
    provider_snapshot: dict[str, Any] | None,
    verifier: dict[str, Any] | None,
    budget: dict[str, Any] | None,
    contract_complexity: str | None,
    risk_class: str | None,
    task_family: str,
    lane: str,
) -> tuple[float, dict[str, Any]]:
    snapshot = snapshot or {}
    provider_snapshot = provider_snapshot or {}
    verifier = verifier or {}

    verifier_score = verifier.get("success_rate")
    if verifier_score is None:
        verifier_score = 0.5

    health_source = snapshot if snapshot else provider_snapshot
    error_rate = _safe_float(health_source.get("error_rate"))
    auth_fail_rate = _safe_float(health_source.get("auth_fail_rate"))
    rate_limit_rate = _safe_float(health_source.get("rate_limit_rate"))
    freshness = 1.0
    if health_source.get("window_end"):
        age_hours = (datetime.now(timezone.utc) - health_source["window_end"]).total_seconds() / 3600.0
        freshness = max(0.0, 1.0 - min(1.0, age_hours / 72.0))
    health_score = max(0.0, 1.0 - min(1.0, error_rate + auth_fail_rate * 1.5 + rate_limit_rate))
    health_score = round((health_score * 0.8) + (freshness * 0.2), 4)

    p95_latency = health_source.get("p95_latency_ms")
    latency_score = 1.0
    if isinstance(p95_latency, (int, float)) and p95_latency >= 0:
        latency_score = max(0.0, 1.0 - min(1.0, float(p95_latency) / 5000.0))

    candidate_cost = calculate_model_cost(candidate_model, 1_000, 1_000)
    legacy_cost = calculate_model_cost(legacy_model, 1_000, 1_000)
    cost_floor = max(candidate_cost, legacy_cost, 0.001)
    cost_score = max(0.0, 1.0 - min(1.0, candidate_cost / (cost_floor * 1.5)))
    budget_score, budget_context = _budget_score(candidate_cost, budget)

    candidate_rank = _model_rank(candidate_model)
    required_rank = _complexity_rank(contract_complexity, risk_class)
    complexity_delta = candidate_rank - required_rank
    if complexity_delta >= 1:
        complexity_score = 1.0
    elif complexity_delta == 0:
        complexity_score = 0.9
    elif complexity_delta == -1:
        complexity_score = 0.65
    else:
        complexity_score = 0.4

    risk_score = 1.0
    if (risk_class or "").strip().lower() in {"high", "critical", "safety", "sensitive"}:
        risk_score = 1.0 if candidate_rank >= max(required_rank, 2) else 0.55
    elif (risk_class or "").strip().lower() in {"low", "low_cost", "background"}:
        risk_score = 1.0 if candidate_cost <= legacy_cost else 0.85

    lane_bonus = 1.0
    if lane == "worker":
        lane_bonus = 0.98
    elif lane == "coordinator":
        lane_bonus = 1.0

    verifier_score = round(float(verifier_score), 4)

    score = round(
        (0.38 * verifier_score) +
        (0.18 * health_score) +
        (0.1 * latency_score) +
        (0.08 * cost_score) +
        (0.1 * complexity_score) +
        (0.08 * risk_score) +
        (0.08 * budget_score) +
        (0.02 * lane_bonus),
        4,
    )
    evidence = {
        "health": _jsonable(snapshot),
        "provider_health": _jsonable(provider_snapshot),
        "verifier": _jsonable(verifier),
        "cost_estimate": round(candidate_cost, 6),
        "legacy_cost_estimate": round(legacy_cost, 6),
        "components": {
            "verifier": verifier_score,
            "health": round(float(health_score), 4),
            "latency": round(float(latency_score), 4),
            "cost": round(float(cost_score), 4),
            "budget": round(float(budget_score), 4),
            "complexity": round(float(complexity_score), 4),
            "risk": round(float(risk_score), 4),
            "lane": round(float(lane_bonus), 4),
        },
        "dimensions": {
            "candidate_rank": candidate_rank,
            "required_rank": required_rank,
            "contract_complexity": contract_complexity,
            "risk_class": risk_class,
            "task_family": task_family,
            "lane": lane,
            "candidate_provider": candidate_provider,
            "legacy_provider": legacy_provider,
        },
        "budget": budget_context,
        "health_source": "model" if snapshot else "provider_rollup" if provider_snapshot else "none",
    }
    return score, evidence


def _candidate_pool(
    *,
    task_family: str,
    lane: str,
    skill_name: str | None,
    user_id: str | None,
    org_id: str | None,
    preferred_provider: str | None,
) -> tuple[list[RoutingCandidate], dict[str, Any], SkillRoutingProfile, str, str, str]:
    provider_resolution = resolve_provider_selection(
        user_id=user_id,
        org_id=org_id,
        preferred_provider=preferred_provider,
    )
    skill_profile = resolve_skill_routing_profile(
        skill_name or "",
        user_id=user_id,
        org_id=org_id,
        preferred_provider=preferred_provider,
    ) if skill_name else SkillRoutingProfile(
        skill_name=skill_name or "",
        reasoning_effort=None,
    )
    legacy_runtime = resolve_skill_runtime(
        skill_name or "coordinate",
        user_id=user_id,
        org_id=org_id,
        preferred_provider=preferred_provider,
    ) if skill_name else None

    legacy_provider = normalize_runtime_provider(legacy_runtime.provider if legacy_runtime else provider_resolution.provider)
    legacy_model = (
        f"{legacy_runtime.provider}/{legacy_runtime.model_name}"
        if legacy_runtime
        else f"{provider_resolution.provider}/{get_provider_model_map(provider_resolution.provider, user_id=user_id, org_id=org_id).get(DEFAULT_MODEL_TIER)}"
    )
    legacy_reasoning_effort = legacy_runtime.reasoning_effort if legacy_runtime else "medium"

    explicit_provider = provider_resolution.provider if provider_resolution.explicit else None
    hard_provider = explicit_provider or None
    hard_model = None

    if hard_provider:
        provider_names = [normalize_runtime_provider(hard_provider)]
    else:
        provider_names = [normalize_runtime_provider(provider_resolution.provider)]

    candidates: list[RoutingCandidate] = []
    candidate_constraints = {
        "provider_pin": {
            "provider": hard_provider,
            "explicit": bool(explicit_provider),
            "source": provider_resolution.source,
        },
        "skill_pin": {
            "provider": None,
            "model_name": None,
            "reasoning_effort": skill_profile.reasoning_effort,
        },
        "provider_resolution": {
            "provider": provider_resolution.provider,
            "source": provider_resolution.source,
            "explicit": provider_resolution.explicit,
        },
    }

    if hard_model and hard_provider:
        candidates.append(RoutingCandidate(
            provider=hard_provider,
            model=hard_model,
            reasoning_effort=skill_profile.reasoning_effort or legacy_reasoning_effort,
            eligible=True,
            evidence={"constraint": "skill_model_pin"},
        ))
    else:
        for provider in provider_names:
            model_map = get_provider_model_map(provider, user_id=user_id, org_id=org_id)
            model_tier = normalize_model_tier(
                skill_profile.model_tier or (legacy_runtime.model_tier if legacy_runtime else None)
            ) or DEFAULT_MODEL_TIER
            model_name = model_map.get(model_tier, model_map.get(DEFAULT_MODEL_TIER))
            if not model_name:
                continue
            candidates.append(RoutingCandidate(
                provider=provider,
                model=model_name,
                reasoning_effort=skill_profile.reasoning_effort or legacy_reasoning_effort,
                eligible=True,
                evidence={"model_tier": model_tier},
            ))

        # Always retain the legacy route as a shadow baseline for comparison.
        if all(candidate.provider != legacy_provider or candidate.model != _model_identity(legacy_model, legacy_provider)[1] for candidate in candidates):
            legacy_model_provider, legacy_model_name = _model_identity(legacy_model, legacy_provider)
            if legacy_model_provider and legacy_model_name:
                candidates.append(RoutingCandidate(
                    provider=legacy_model_provider,
                    model=legacy_model_name,
                    reasoning_effort=legacy_reasoning_effort,
                    eligible=True,
                    evidence={"baseline": True},
                ))

    return candidates, candidate_constraints, skill_profile, legacy_provider, legacy_model, legacy_reasoning_effort


def _apply_hard_constraints(
    *,
    candidates: list[RoutingCandidate],
    provider_resolution,
    skill_profile: SkillRoutingProfile,
    user_id: str | None,
    org_id: str | None,
) -> list[RoutingCandidate]:
    from brain.systems.services.runtime_introspection import get_provider_auth_status

    filtered: list[RoutingCandidate] = []
    for candidate in candidates:
        exclusion_reason = None
        if provider_resolution.explicit and candidate.provider != provider_resolution.provider:
            exclusion_reason = "user_org_provider_pin"

        auth_status = None
        if not exclusion_reason:
            try:
                auth_status = get_provider_auth_status(user_id=user_id, org_id=org_id, provider=candidate.provider)
                if not auth_status.get("authenticated", False):
                    exclusion_reason = "provider_auth_unavailable"
            except Exception:
                auth_status = None

        if exclusion_reason:
            filtered.append(RoutingCandidate(
                provider=candidate.provider,
                model=candidate.model,
                reasoning_effort=candidate.reasoning_effort,
                eligible=False,
                exclusion_reason=exclusion_reason,
                evidence={**candidate.evidence, "auth_status": _jsonable(auth_status) if auth_status else None},
            ))
            continue

        filtered.append(RoutingCandidate(
            provider=candidate.provider,
            model=candidate.model,
            reasoning_effort=candidate.reasoning_effort,
            eligible=True,
            evidence={**candidate.evidence, "auth_status": _jsonable(auth_status) if auth_status else None},
        ))
    return filtered


def _maybe_refresh_health_snapshots(flags: dict[str, Any]) -> None:
    if flags.get("force_legacy"):
        return
    refresh_provider_health_snapshots(lookback_hours=flags["lookback_hours"])


def apply_marketplace_route(
    *,
    task_family: str,
    lane: str,
    skill_name: str | None,
    user_id: str | None,
    org_id: str | None,
    run_id: int | None,
    legacy_provider: str,
    legacy_model: str,
    legacy_thinking: str,
    contract_complexity: str | None = None,
    risk_class: str | None = None,
    budget: dict | None = None,
    genome_signals: dict | None = None,
    logger_name: str = "routing.marketplace",
) -> tuple[str, str, RoutingDecisionResult]:
    """Resolve marketplace routing and return the runtime model/thinking pair.

    Coordinator and worker lanes share this helper so active canaries, fallback
    reasons, and routing trace metadata are applied consistently.
    """
    decision = resolve_marketplace_routing(
        task_family=task_family,
        lane=lane,
        skill_name=skill_name,
        user_id=user_id,
        org_id=org_id,
        run_id=run_id,
        legacy_provider=legacy_provider,
        legacy_model=legacy_model,
        legacy_reasoning_effort=legacy_thinking,
        contract_complexity=contract_complexity,
        risk_class=risk_class,
        budget=budget,
        genome_signals=genome_signals,
    )
    route_summary = {}
    if isinstance(getattr(decision, "inputs", None), dict):
        route_summary = decision.inputs.get("route_summary") or {}
    fallback_reason = route_summary.get("fallback_reason") or (
        "legacy_fallback" if getattr(decision, "fallback_used", False) else None
    )
    logging.getLogger(logger_name).info(
        "Marketplace routing task=%s lane=%s selected=%s/%s mode=%s fallback=%s reason=%s",
        task_family,
        lane,
        getattr(decision, "selected_provider", None),
        getattr(decision, "selected_model", None),
        getattr(decision, "decision_mode", None),
        getattr(decision, "fallback_used", None),
        fallback_reason or "none",
    )
    if decision.decision_mode == "active":
        selected_model = prefixed_runtime_model(decision.selected_provider, decision.selected_model)
        selected_thinking = decision.selected_reasoning_effort or legacy_thinking
        return selected_model, selected_thinking, decision
    return legacy_model, legacy_thinking, decision


def resolve_marketplace_routing(
    *,
    task_family: str,
    lane: str,
    skill_name: str | None = None,
    user_id: str | None = None,
    org_id: str | None = None,
    run_id: int | None = None,
    legacy_provider: str | None = None,
    legacy_model: str | None = None,
    legacy_reasoning_effort: str | None = None,
    contract_complexity: str | None = None,
    risk_class: str | None = None,
    budget: dict[str, Any] | None = None,
    genome_signals: dict[str, Any] | None = None,
    experiment_name: str | None = None,
) -> RoutingDecisionResult:
    """Score routing candidates while staying reversible by default."""
    flags = get_routing_marketplace_flags()
    user_id = _optional_text(user_id)
    org_id = _optional_text(org_id)
    task_family = (task_family or skill_name or "general").strip() or "general"
    lane = (lane or "coordinator").strip() or "coordinator"
    legacy_provider = normalize_runtime_provider(legacy_provider or resolve_default_provider(user_id=user_id, org_id=org_id))
    if legacy_model:
        legacy_model = legacy_model.strip()
    else:
        legacy_model = f"{legacy_provider}/{get_provider_model_map(legacy_provider, user_id=user_id, org_id=org_id).get(DEFAULT_MODEL_TIER)}"
    legacy_reasoning_effort = legacy_reasoning_effort or "medium"

    try:
        with UnitOfWork() as uow:
            experiment = _resolve_active_experiment(uow.session, task_family)
            if experiment and not experiment_name:
                experiment_name = experiment.name
            canary_policy = _parse_allocation_policy(
                experiment.allocation_policy if experiment else None,
                flags,
            )
            rollback_reason = _maybe_roll_back_experiment(uow.session, experiment, canary_policy)
            if rollback_reason:
                logger.warning(
                    "Routing experiment %s rolled back: %s",
                    getattr(experiment, "name", None),
                    rollback_reason,
                )

            _maybe_refresh_health_snapshots(flags)
            candidates, constraints, skill_profile, legacy_provider, legacy_model, legacy_reasoning_effort = _candidate_pool(
                task_family=task_family,
                lane=lane,
                skill_name=skill_name,
                user_id=user_id,
                org_id=org_id,
                preferred_provider=legacy_provider,
            )
            provider_resolution = resolve_provider_selection(
                user_id=user_id,
                org_id=org_id,
                preferred_provider=legacy_provider,
            )
            constrained_candidates = _apply_hard_constraints(
                candidates=candidates,
                provider_resolution=provider_resolution,
                skill_profile=skill_profile,
                user_id=user_id,
                org_id=org_id,
            )

            window_hours = flags["lookback_hours"]
            stale_after = timedelta(hours=flags["stale_after_hours"])
            min_samples = flags["require_min_samples"]
            legacy_model_name = legacy_model.split("/", 1)[-1]
            canary_allocation = _canary_allocation(
                policy=canary_policy,
                org_id=org_id,
                user_id=user_id,
                task_family=task_family,
                lane=lane,
                experiment_name=experiment_name,
                run_id=run_id,
            )
            eval_gate = _eval_gate(genome_signals, canary_policy)
            scored_candidates: list[dict[str, Any]] = []
            health_cache: dict[tuple[str, str], dict[str, Any]] = {}
            verifier_cache: dict[tuple[str, str], dict[str, Any]] = {}

            def _candidate_context(provider: str, model: str) -> tuple[dict[str, Any], dict[str, Any]]:
                key = (provider, model)
                if key not in health_cache:
                    health_cache[key] = _load_health_context(
                        uow.session,
                        provider,
                        model,
                        lookback_hours=window_hours,
                    )
                if key not in verifier_cache:
                    verifier_cache[key] = _load_verifier_evidence(
                        uow.session,
                        provider,
                        model,
                        lookback_hours=window_hours,
                    )
                return health_cache[key], verifier_cache[key]

            legacy_health_context, legacy_verifier = _candidate_context(legacy_provider, legacy_model_name)
            legacy_score, legacy_evidence = _score_candidate(
                candidate_provider=legacy_provider,
                candidate_model=legacy_model_name,
                legacy_provider=legacy_provider,
                legacy_model=legacy_model,
                snapshot=legacy_health_context["snapshot"],
                provider_snapshot=legacy_health_context["provider_snapshot"],
                verifier=legacy_verifier,
                budget=budget,
                contract_complexity=contract_complexity,
                risk_class=risk_class,
                task_family=task_family,
                lane=lane,
            )

            for candidate in constrained_candidates:
                health_context, verifier = _candidate_context(candidate.provider, candidate.model)
                model_snapshot = health_context["model_snapshot"]
                provider_snapshot = health_context["provider_snapshot"]
                snapshot = health_context["snapshot"]
                model_confidence = health_context["model_confidence"]
                provider_confidence = health_context["provider_confidence"]
                snapshot_source = model_snapshot or provider_snapshot or {}
                snapshot_age = None
                if snapshot_source.get("window_end"):
                    snapshot_age = datetime.now(timezone.utc) - snapshot_source["window_end"]
                model_samples = int(model_confidence.get("sample_count") or 0)
                verifier_samples = int(verifier.get("sample_count") or 0)
                provider_samples = int(provider_confidence.get("sample_count") or 0)
                evidence_sparse = model_samples < min_samples or (verifier_samples + provider_samples) < min_samples
                evidence_stale = bool(snapshot_age and snapshot_age > stale_after)
                score, evidence = _score_candidate(
                    candidate_provider=candidate.provider,
                    candidate_model=candidate.model,
                    legacy_provider=legacy_provider,
                    legacy_model=legacy_model,
                    snapshot=snapshot,
                    provider_snapshot=provider_snapshot,
                    verifier=verifier,
                    budget=budget,
                    contract_complexity=contract_complexity,
                    risk_class=risk_class,
                    task_family=task_family,
                    lane=lane,
                )
                comparison = {
                    "score_delta": round(score - legacy_score, 4),
                    "candidate_vs_legacy_cost_delta": round(
                        (evidence.get("cost_estimate") or 0.0) - (legacy_evidence.get("cost_estimate") or 0.0),
                        6,
                    ),
                    "verifier_delta": round(
                        _safe_float((verifier or {}).get("success_rate"), 0.5) - _safe_float((legacy_verifier or {}).get("success_rate"), 0.5),
                        4,
                    ),
                    "health_delta": round(
                        _safe_float((snapshot or provider_snapshot or {}).get("error_rate"), 0.0)
                        - _safe_float((legacy_health_context["snapshot"] or legacy_health_context["provider_snapshot"] or {}).get("error_rate"), 0.0),
                        4,
                    ),
                    "legacy_score": round(legacy_score, 4),
                }
                eligible = bool(candidate.eligible and not (evidence_sparse or evidence_stale))
                evidence_strength = (
                    "strong" if eligible and model_confidence.get("strength") == "strong" and verifier_samples >= min_samples * 2 and score >= legacy_score
                    else "moderate" if eligible and not evidence_stale
                    else "sparse" if evidence_sparse
                    else "stale" if evidence_stale
                    else "blocked"
                )
                evidence["route_state"] = {
                    "eligible": eligible,
                    "strength": evidence_strength,
                    "model_confidence": model_confidence,
                    "provider_confidence": provider_confidence,
                    "snapshot_age_hours": round(snapshot_age.total_seconds() / 3600.0, 4) if snapshot_age else None,
                }
                evidence["canary"] = {
                    "policy": canary_policy,
                    "allocation": canary_allocation,
                    "eval_gate": eval_gate,
                }
                evidence["comparison"] = comparison
                evidence["selection_notes"] = {
                    "legacy_route": candidate.provider == legacy_provider and candidate.model == legacy_model_name,
                    "hard_constraint_exclusion": candidate.exclusion_reason,
                    "exclusion_reason": candidate.exclusion_reason or (
                        "evidence_sparse" if evidence_sparse else "evidence_stale" if evidence_stale else None
                    ),
                }
                scored_candidates.append({
                    "provider": candidate.provider,
                    "model": candidate.model,
                    "reasoning_effort": candidate.reasoning_effort,
                    "score": score,
                    "eligible": eligible,
                    "exclusion_reason": candidate.exclusion_reason or (
                        "evidence_sparse" if evidence_sparse else "evidence_stale" if evidence_stale else None
                    ),
                    "evidence": evidence,
                    "comparison": comparison,
                    "is_legacy": candidate.provider == legacy_provider and candidate.model == legacy_model_name,
                })

            eligible_candidates = [c for c in scored_candidates if c["eligible"]]
            shadow_winner = max(eligible_candidates, key=lambda c: (c["score"], c["is_legacy"]), default=None)
            legacy_candidate = next((c for c in scored_candidates if c["is_legacy"]), None)
            if legacy_candidate is None:
                legacy_candidate = {
                    "provider": legacy_provider,
                    "model": legacy_model_name,
                    "reasoning_effort": legacy_reasoning_effort,
                    "score": legacy_score,
                    "eligible": True,
                    "exclusion_reason": None,
                    "evidence": {
                        **legacy_evidence,
                        "route_state": {
                            "eligible": True,
                            "strength": legacy_health_context["model_confidence"].get("strength") or legacy_health_context["provider_confidence"].get("strength"),
                            "model_confidence": legacy_health_context["model_confidence"],
                            "provider_confidence": legacy_health_context["provider_confidence"],
                            "snapshot_age_hours": None,
                        },
                        "comparison": {
                            "score_delta": 0.0,
                            "candidate_vs_legacy_cost_delta": 0.0,
                            "verifier_delta": 0.0,
                            "health_delta": 0.0,
                            "legacy_score": round(legacy_score, 4),
                        },
                    },
                    "is_legacy": True,
                }

            selected_candidate = shadow_winner or legacy_candidate
            fallback_used = shadow_winner is None
            fallback_reason = "no_eligible_candidates" if shadow_winner is None else None
            decision_mode = "shadow"
            applied = False
            if selected_candidate is None:
                selected_candidate = legacy_candidate
                fallback_used = True
                fallback_reason = fallback_reason or "legacy_route_missing"

            if flags["force_legacy"]:
                decision_mode = "legacy"
                selected_candidate = legacy_candidate
                fallback_used = True
                fallback_reason = "force_legacy"
            elif flags["active"]:
                decision_mode = "active"
                applied = True
                if selected_candidate and not selected_candidate["is_legacy"]:
                    canary_gate = _canary_gate_status(
                        selected_candidate=selected_candidate,
                        legacy_candidate=legacy_candidate,
                        allocation=canary_allocation,
                        eval_gate=eval_gate,
                        policy=canary_policy,
                    )
                    route_state = selected_candidate.get("evidence", {}).get("route_state", {})
                    strong_enough = route_state.get("strength") == "strong"
                    score_delta = _safe_float(selected_candidate.get("comparison", {}).get("score_delta"), 0.0)
                    if rollback_reason:
                        fallback_reason = rollback_reason
                        selected_candidate = legacy_candidate
                        fallback_used = True
                    elif not canary_gate.get("ok"):
                        fallback_reason = canary_gate.get("reason") or "canary_gate_failed"
                        selected_candidate.setdefault("evidence", {})["canary_gate"] = canary_gate
                        selected_candidate = legacy_candidate
                        fallback_used = True
                    elif selected_candidate["provider"] != legacy_provider and not flags["allow_provider_switch"]:
                        fallback_reason = "provider_switch_disabled"
                        selected_candidate = legacy_candidate
                        fallback_used = True
                    elif (
                        selected_candidate["provider"] == legacy_provider
                        and selected_candidate["model"] != legacy_model_name
                        and not flags["allow_model_switch_within_provider"]
                    ):
                        fallback_reason = "model_switch_disabled"
                        selected_candidate = legacy_candidate
                        fallback_used = True
                    elif selected_candidate["provider"] == legacy_provider and selected_candidate["model"] != legacy_model_name:
                        if not strong_enough or score_delta < 0.05:
                            fallback_reason = "canary_evidence_not_strong"
                            selected_candidate = legacy_candidate
                            fallback_used = True
                    elif selected_candidate["provider"] != legacy_provider:
                        if not strong_enough or score_delta < 0.1:
                            fallback_reason = "cross_provider_canary_not_strong"
                            selected_candidate = legacy_candidate
                            fallback_used = True
                if selected_candidate is legacy_candidate and fallback_reason is None and shadow_winner is None:
                    fallback_reason = "legacy_route_used"
            else:
                decision_mode = "shadow"
                selected_candidate = shadow_winner or legacy_candidate

            selected_provider = selected_candidate["provider"]
            selected_model = selected_candidate["model"]
            selected_reasoning_effort = selected_candidate.get("reasoning_effort") or legacy_reasoning_effort

            route_summary = {
                "decision_mode": decision_mode,
                "applied": applied,
                "fallback_used": fallback_used,
                "fallback_reason": fallback_reason,
                "canary": {
                    "policy": _jsonable(canary_policy),
                    "allocation": _jsonable(canary_allocation),
                    "eval_gate": _jsonable(eval_gate),
                    "rollback_reason": rollback_reason,
                },
                "candidate_count": len(scored_candidates),
                "eligible_candidate_count": len(eligible_candidates),
                "legacy": {
                    "provider": legacy_provider,
                    "model": legacy_model_name,
                    "score": round(legacy_score, 4),
                    "evidence_strength": legacy_candidate.get("evidence", {}).get("route_state", {}).get("strength"),
                },
                "selected": {
                    "provider": selected_provider,
                    "model": selected_model,
                    "score": round(_safe_float(selected_candidate.get("score"), 0.0), 4),
                    "evidence_strength": selected_candidate.get("evidence", {}).get("route_state", {}).get("strength"),
                    "is_legacy": bool(selected_candidate.get("is_legacy")),
                },
                "shadow_winner": {
                    "provider": shadow_winner["provider"],
                    "model": shadow_winner["model"],
                    "score": round(_safe_float(shadow_winner.get("score"), 0.0), 4),
                    "evidence_strength": shadow_winner.get("evidence", {}).get("route_state", {}).get("strength"),
                } if shadow_winner else None,
            }

            inputs = {
                "task_family": task_family,
                "lane": lane,
                "skill_name": skill_name,
                "run_id": run_id,
                "legacy_provider": legacy_provider,
                "legacy_model": legacy_model,
                "legacy_reasoning_effort": legacy_reasoning_effort,
                "contract_complexity": contract_complexity,
                "risk_class": risk_class,
                "budget": _jsonable(budget or {}),
                "genome_signals": _jsonable(genome_signals or {}),
                "flags": flags,
                "canary_policy": _jsonable(canary_policy),
                "canary_allocation": _jsonable(canary_allocation),
                "eval_gate": _jsonable(eval_gate),
                "rollback_reason": rollback_reason,
                "route_summary": _jsonable(route_summary),
            }
            if experiment_name:
                inputs["experiment_name"] = experiment_name

            logger.info(
                "Routing decision task=%s lane=%s selected=%s/%s mode=%s fallback=%s reason=%s",
                task_family,
                lane,
                selected_provider,
                selected_model,
                decision_mode,
                fallback_used,
                fallback_reason or "none",
            )
            logger.debug("Routing decision trace: %s", route_summary)

            result = RoutingDecisionResult(
                run_id=run_id,
                task_family=task_family,
                lane=lane,
                decision_mode=decision_mode,
                selected_provider=selected_provider,
                selected_model=selected_model,
                selected_reasoning_effort=selected_reasoning_effort,
                legacy_provider=legacy_provider,
                legacy_model=legacy_model,
                legacy_reasoning_effort=legacy_reasoning_effort,
                inputs=inputs,
                candidate_scores=scored_candidates,
                constraints={
                    **constraints,
                    "provider_resolution": {
                        "provider": provider_resolution.provider,
                        "source": provider_resolution.source,
                        "explicit": provider_resolution.explicit,
                    },
                    "hard_constraints": {
                        "provider": provider_resolution.provider if provider_resolution.explicit else None,
                        "model_name": None,
                    },
                    "route_summary": _jsonable(route_summary),
                    "fallback_reason": fallback_reason,
                    "legacy_route": {
                        "provider": legacy_provider,
                        "model": legacy_model_name,
                    },
                    "experiment_name": experiment_name,
                    "canary_policy": _jsonable(canary_policy),
                    "canary_allocation": _jsonable(canary_allocation),
                    "eval_gate": _jsonable(eval_gate),
                    "rollback_reason": rollback_reason,
                },
                experiment_id=experiment.id if experiment else None,
                applied=applied,
                fallback_used=fallback_used,
            )
            return persist_routing_decision(uow.session, result)
    except Exception as exc:
        logger.warning("Marketplace routing failed, using legacy route: %s", exc)
        return RoutingDecisionResult(
            run_id=run_id,
            task_family=task_family,
            lane=lane,
            decision_mode="legacy",
            selected_provider=legacy_provider,
            selected_model=legacy_model.split("/", 1)[-1],
            selected_reasoning_effort=legacy_reasoning_effort,
            legacy_provider=legacy_provider,
            legacy_model=legacy_model,
            legacy_reasoning_effort=legacy_reasoning_effort,
            inputs={
                "task_family": task_family,
                "lane": lane,
                "skill_name": skill_name,
                "run_id": run_id,
                "legacy_provider": legacy_provider,
                "legacy_model": legacy_model,
                "fallback_reason": "routing_error",
                "error": str(exc),
                "route_summary": {
                    "decision_mode": "legacy",
                    "fallback_used": True,
                    "fallback_reason": "routing_error",
                },
            },
            candidate_scores=[],
            constraints={
                "fallback_reason": "routing_error",
                "route_summary": {
                    "decision_mode": "legacy",
                    "fallback_used": True,
                    "fallback_reason": "routing_error",
                },
            },
            experiment_id=None,
            applied=False,
            fallback_used=True,
        )


def persist_routing_decision(session, decision: RoutingDecisionResult) -> RoutingDecisionResult:
    """Persist or update the latest routing decision for a run."""
    try:
        existing = None
        if decision.run_id is not None:
            existing = session.execute(
                select(RoutingDecision).where(RoutingDecision.run_id == decision.run_id)
            ).scalar_one_or_none()
        if existing:
            row = existing
        else:
            row = RoutingDecision(
                run_id=decision.run_id,
                task_family=decision.task_family,
                lane=decision.lane,
                decision_mode=decision.decision_mode,
                selected_provider=decision.selected_provider,
                selected_model=decision.selected_model,
                selected_reasoning_effort=decision.selected_reasoning_effort,
                inputs=_jsonable(decision.inputs),
                candidate_scores=_jsonable(decision.candidate_scores),
                constraints=_jsonable(decision.constraints),
                experiment_id=decision.experiment_id,
                applied=decision.applied,
                fallback_used=decision.fallback_used,
                post_run_outcome=_jsonable(decision.post_run_outcome) if decision.post_run_outcome is not None else None,
            )
            session.add(row)
            if hasattr(session, "flush"):
                session.flush()
        row.task_family = decision.task_family
        row.lane = decision.lane
        row.decision_mode = decision.decision_mode
        row.selected_provider = decision.selected_provider
        row.selected_model = decision.selected_model
        row.selected_reasoning_effort = decision.selected_reasoning_effort
        row.inputs = _jsonable(decision.inputs)
        row.candidate_scores = _jsonable(decision.candidate_scores)
        row.constraints = _jsonable(decision.constraints)
        row.experiment_id = decision.experiment_id
        row.applied = decision.applied
        row.fallback_used = decision.fallback_used
        row.post_run_outcome = _jsonable(decision.post_run_outcome) if decision.post_run_outcome is not None else None
        decision_dict = RoutingDecisionResult(
            **{**decision.__dict__, "decision_id": getattr(row, "id", decision.decision_id)}
        )
        return decision_dict
    except Exception as exc:
        logger.debug("Failed to persist routing decision: %s", exc)
        return decision


def get_routing_marketplace_snapshot(
    *,
    user_id: str | None = None,
    org_id: str | None = None,
    provider: str | None = None,
) -> dict[str, Any]:
    """Return a compact snapshot for runtime settings and debugging surfaces."""
    flags = get_routing_marketplace_flags()
    snapshot: dict[str, Any] = {
        "flags": flags,
        "user_id": user_id,
        "org_id": org_id,
        "provider": provider,
        "healthy": False,
        "latest_health_snapshots": [],
        "latest_decisions": [],
    }
    try:
        with UnitOfWork() as uow:
            health_rows = uow.session.execute(
                select(ProviderHealthSnapshot).order_by(ProviderHealthSnapshot.window_end.desc(), ProviderHealthSnapshot.id.desc()).limit(5)
            ).scalars().all()
            decision_rows = uow.session.execute(
                select(RoutingDecision).order_by(RoutingDecision.created_at.desc(), RoutingDecision.id.desc()).limit(5)
            ).scalars().all()
            snapshot["latest_health_snapshots"] = [
                _jsonable({
                    "provider": row.provider,
                    "model": row.model,
                    "window_start": row.window_start,
                    "window_end": row.window_end,
                    "p50_latency_ms": row.p50_latency_ms,
                    "p95_latency_ms": row.p95_latency_ms,
                    "error_rate": row.error_rate,
                    "auth_fail_rate": row.auth_fail_rate,
                    "rate_limit_rate": row.rate_limit_rate,
                    "sample_count": row.sample_count,
                    "source": row.source,
                })
                for row in health_rows
            ]
            latest_decisions: list[dict[str, Any]] = []
            for row in decision_rows:
                inputs = row.inputs if isinstance(row.inputs, dict) else {}
                constraints = row.constraints if isinstance(row.constraints, dict) else {}
                route_summary = inputs.get("route_summary") if isinstance(inputs, dict) else None
                if not isinstance(route_summary, dict):
                    route_summary = constraints.get("route_summary") if isinstance(constraints, dict) else None
                if not isinstance(route_summary, dict):
                    route_summary = {}
                legacy = route_summary.get("legacy") if isinstance(route_summary.get("legacy"), dict) else {}
                selected = route_summary.get("selected") if isinstance(route_summary.get("selected"), dict) else {}
                shadow_winner = route_summary.get("shadow_winner") if isinstance(route_summary.get("shadow_winner"), dict) else None
                latest_decisions.append(_jsonable({
                    "run_id": row.run_id,
                    "task_family": row.task_family,
                    "lane": row.lane,
                    "decision_mode": row.decision_mode,
                    "selected_provider": row.selected_provider,
                    "selected_model": row.selected_model,
                    "selected_reasoning_effort": row.selected_reasoning_effort,
                    "applied": row.applied,
                    "fallback_used": row.fallback_used,
                    "fallback_reason": constraints.get("fallback_reason") or route_summary.get("fallback_reason"),
                    "candidate_count": route_summary.get("candidate_count"),
                    "eligible_candidate_count": route_summary.get("eligible_candidate_count"),
                    "legacy_score": legacy.get("score"),
                    "selected_score": selected.get("score"),
                    "selected_over_legacy_delta": (
                        round(float(selected.get("score")) - float(legacy.get("score")), 4)
                        if isinstance(selected.get("score"), (int, float)) and isinstance(legacy.get("score"), (int, float))
                        else None
                    ),
                    "shadow_winner": shadow_winner,
                    "route_summary": route_summary,
                    "created_at": row.created_at,
                }))
            snapshot["latest_decisions"] = latest_decisions
            snapshot["healthy"] = bool(health_rows or decision_rows)
    except Exception as exc:
        logger.debug("Routing snapshot unavailable: %s", exc)
    return snapshot
