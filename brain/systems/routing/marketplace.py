"""Historical routing-marketplace observability.

The marketplace route selector was never called by a live run path and has
been removed. This module keeps the read-only snapshot consumed by runtime
introspection so existing health and decision history remains inspectable.
"""

from __future__ import annotations

import inspect
import logging
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from brain.kernel.common.env import env_flag as _shared_env_flag
from brain.kernel.common.env import env_float as _shared_env_float
from brain.kernel.common.env import env_int as _shared_env_int
from brain.platform.db.models.routing import ProviderHealthSnapshot, RoutingDecision
from brain.platform.db.repositories.unit_of_work import UnitOfWork

logger = logging.getLogger("routing.marketplace")


def _env_flag(name: str, default: bool) -> bool:
    return _shared_env_flag(
        name,
        default=default,
        false_values={"0", "false", "no", "off", ""},
    )


def _env_int(name: str, default: int) -> int:
    return _shared_env_int(name, default)


def _env_float(name: str, default: float) -> float:
    return _shared_env_float(name, default)


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def _session_execute(session: Any, *args: Any, **kwargs: Any) -> Any:
    return await _maybe_await(session.execute(*args, **kwargs))


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    return value


def get_routing_marketplace_flags() -> dict[str, Any]:
    """Return the legacy rollout flags alongside historical snapshots."""

    active = _env_flag("CORTEX_ROUTING_MARKETPLACE_ACTIVE", False)
    shadow = _env_flag(
        "CORTEX_ROUTING_MARKETPLACE_SHADOW",
        True if not active else False,
    )
    force_legacy = _env_flag("CORTEX_ROUTING_FORCE_LEGACY", False)
    return {
        "shadow": shadow or not active,
        "active": active,
        "force_legacy": force_legacy,
        "allow_provider_switch": _env_flag(
            "CORTEX_ROUTING_ALLOW_PROVIDER_SWITCH",
            False,
        ),
        "allow_model_switch_within_provider": _env_flag(
            "CORTEX_ROUTING_ALLOW_MODEL_SWITCH_WITHIN_PROVIDER",
            False,
        ),
        "require_min_samples": _env_int(
            "CORTEX_ROUTING_REQUIRE_MIN_SAMPLES",
            5,
        ),
        "lookback_hours": _env_int(
            "CORTEX_ROUTING_HEALTH_LOOKBACK_HOURS",
            24,
        ),
        "stale_after_hours": _env_int(
            "CORTEX_ROUTING_STALE_AFTER_HOURS",
            24,
        ),
        "include_warm_state": _env_flag(
            "CORTEX_ROUTING_INCLUDE_WARM_STATE",
            False,
        ),
        "canary_percent": _env_float(
            "CORTEX_ROUTING_CANARY_PERCENT",
            10.0,
        ),
        "require_eval_pass": _env_flag(
            "CORTEX_ROUTING_REQUIRE_EVAL_PASS",
            True,
        ),
        "min_eval_score": _env_float(
            "CORTEX_ROUTING_MIN_EVAL_SCORE",
            0.85,
        ),
        "min_verifier_pass_rate": _env_float(
            "CORTEX_ROUTING_MIN_VERIFIER_PASS_RATE",
            0.85,
        ),
        "max_cost_ratio": _env_float(
            "CORTEX_ROUTING_MAX_COST_RATIO",
            0.0,
        ),
        "max_p95_latency_ms": _env_int(
            "CORTEX_ROUTING_MAX_P95_LATENCY_MS",
            0,
        ),
        "rollback_window_decisions": _env_int(
            "CORTEX_ROUTING_ROLLBACK_WINDOW_DECISIONS",
            10,
        ),
        "rollback_min_failed_decisions": _env_int(
            "CORTEX_ROUTING_ROLLBACK_MIN_FAILED_DECISIONS",
            3,
        ),
    }


async def get_routing_marketplace_snapshot(
    session: AsyncSession | None = None,
    *,
    user_id: str | None = None,
    org_id: str | None = None,
    provider: str | None = None,
) -> dict[str, Any]:
    """Return historical marketplace health and decision rows."""

    snapshot: dict[str, Any] = {
        "flags": get_routing_marketplace_flags(),
        "user_id": user_id,
        "org_id": org_id,
        "provider": provider,
        "healthy": False,
        "latest_health_snapshots": [],
        "latest_decisions": [],
    }
    try:
        if session is None:
            async with UnitOfWork() as uow:
                return await get_routing_marketplace_snapshot(
                    uow.session,
                    user_id=user_id,
                    org_id=org_id,
                    provider=provider,
                )
        health_rows = (
            await _session_execute(
                session,
                select(ProviderHealthSnapshot)
                .order_by(
                    ProviderHealthSnapshot.window_end.desc(),
                    ProviderHealthSnapshot.id.desc(),
                )
                .limit(5),
            )
        ).scalars().all()
        decision_rows = (
            await _session_execute(
                session,
                select(RoutingDecision)
                .order_by(
                    RoutingDecision.created_at.desc(),
                    RoutingDecision.id.desc(),
                )
                .limit(5),
            )
        ).scalars().all()
        snapshot["latest_health_snapshots"] = [
            _jsonable(
                {
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
            )
            for row in health_rows
        ]
        latest_decisions: list[dict[str, Any]] = []
        for row in decision_rows:
            inputs = row.inputs if isinstance(row.inputs, dict) else {}
            constraints = row.constraints if isinstance(row.constraints, dict) else {}
            route_summary = inputs.get("route_summary")
            if not isinstance(route_summary, dict):
                route_summary = constraints.get("route_summary")
            if not isinstance(route_summary, dict):
                route_summary = {}
            legacy = (
                route_summary.get("legacy")
                if isinstance(route_summary.get("legacy"), dict)
                else {}
            )
            selected = (
                route_summary.get("selected")
                if isinstance(route_summary.get("selected"), dict)
                else {}
            )
            shadow_winner = (
                route_summary.get("shadow_winner")
                if isinstance(route_summary.get("shadow_winner"), dict)
                else None
            )
            latest_decisions.append(
                _jsonable(
                    {
                        "run_id": row.run_id,
                        "task_family": row.task_family,
                        "lane": row.lane,
                        "decision_mode": row.decision_mode,
                        "selected_provider": row.selected_provider,
                        "selected_model": row.selected_model,
                        "applied": row.applied,
                        "fallback_used": row.fallback_used,
                        "fallback_reason": (
                            constraints.get("fallback_reason")
                            or route_summary.get("fallback_reason")
                        ),
                        "candidate_count": route_summary.get("candidate_count"),
                        "eligible_candidate_count": route_summary.get(
                            "eligible_candidate_count"
                        ),
                        "legacy_score": legacy.get("score"),
                        "selected_score": selected.get("score"),
                        "selected_over_legacy_delta": (
                            round(
                                float(selected.get("score"))
                                - float(legacy.get("score")),
                                4,
                            )
                            if isinstance(selected.get("score"), (int, float))
                            and isinstance(legacy.get("score"), (int, float))
                            else None
                        ),
                        "shadow_winner": shadow_winner,
                        "route_summary": route_summary,
                        "created_at": row.created_at,
                    }
                )
            )
        snapshot["latest_decisions"] = latest_decisions
        snapshot["healthy"] = bool(health_rows or decision_rows)
    except Exception as exc:
        logger.debug("Routing snapshot unavailable: %s", exc)
    return snapshot
