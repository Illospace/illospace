"""Registry-backed Cycle execution-policy resolution."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol

from brain.platform.db.models.cycle import Cycle, CycleRun
from brain.systems.cycles.execution_effects import CycleExecutionEffect
from brain.systems.cycles.promotion_readiness import (
    PROMOTION_READINESS_POLICY,
    async_apply_promotion_readiness_gate,
)

logger = logging.getLogger(__name__)


class CycleExecutionGate(Protocol):
    """One execution-policy gate with the dispatcher call contract."""

    async def __call__(
        self,
        session: Any,
        *,
        cycle: Cycle,
        run: CycleRun,
    ) -> CycleExecutionEffect | None: ...


@dataclass(frozen=True, slots=True)
class CycleExecutionPolicyRegistration:
    """One key-to-gate registration."""

    key: str
    gate: CycleExecutionGate


@dataclass(frozen=True, slots=True)
class CycleExecutionPolicyRegistry:
    """Resolve one durable policy key to exactly one execution gate."""

    registrations: tuple[CycleExecutionPolicyRegistration, ...]

    def __post_init__(self) -> None:
        keys = [
            _normalized_policy_key(registration.key)
            for registration in self.registrations
        ]
        if any(key is None for key in keys):
            raise ValueError("Cycle execution policy registration requires a key")
        seen: set[str] = set()
        for key in keys:
            assert key is not None
            if key in seen:
                raise ValueError(
                    "Ambiguous Cycle execution policy key: "
                    f"{key!r} resolves to {keys.count(key)} gates"
                )
            seen.add(key)

    def resolve(self, key: str) -> CycleExecutionGate:
        normalized_key = _normalized_policy_key(key)
        for registration in self.registrations:
            if _normalized_policy_key(registration.key) == normalized_key:
                return registration.gate
        raise ValueError(
            f"Unknown Cycle execution policy key: {normalized_key!r}"
        )


def _normalized_policy_key(value: str | None) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


_DEFAULT_CYCLE_EXECUTION_POLICY_REGISTRY = CycleExecutionPolicyRegistry(
    registrations=(
        CycleExecutionPolicyRegistration(
            PROMOTION_READINESS_POLICY.execution_policy_key,
            async_apply_promotion_readiness_gate,
        ),
    )
)


def cycle_execution_policy_registry() -> CycleExecutionPolicyRegistry:
    """Return every registered Cycle execution-policy gate."""
    return _DEFAULT_CYCLE_EXECUTION_POLICY_REGISTRY


def validate_cycle_execution_policy_key(value: str | None) -> str | None:
    """Return a canonical configured key, rejecting unknown keys."""

    if value is None:
        return None
    normalized_key = _normalized_policy_key(value)
    if normalized_key is None:
        raise ValueError(
            "Cycle execution policy key must be null or a registered non-empty key"
        )
    cycle_execution_policy_registry().resolve(normalized_key)
    return normalized_key


async def async_apply_cycle_execution_policy(
    session: Any,
    *,
    cycle: Cycle,
    run: CycleRun,
) -> CycleExecutionEffect | None:
    """Apply the configured policy, or fail the run when it cannot resolve."""

    configured_key = getattr(cycle, "execution_policy_key", None)
    if configured_key is None:
        return None
    try:
        key = _normalized_policy_key(configured_key)
        if key is None:
            raise ValueError(
                "Cycle execution policy key must be null or a registered non-empty key"
            )
        gate = cycle_execution_policy_registry().resolve(key)
    except ValueError as exc:
        error = str(exc)
        key = str(configured_key).strip()
        context_snapshot = dict(run.context_snapshot or {})
        context_snapshot["execution_policy"] = {
            "key": key,
            "outcome": "configuration_error",
            "error": error,
        }
        run.context_snapshot = context_snapshot
        logger.error(
            "cycle_execution_policy_configuration_error",
            extra={
                "execution_policy_key": key,
                "cycle_id": cycle.id,
                "cycle_run_id": run.id,
                "error": error,
            },
        )
        return CycleExecutionEffect.finalize(status="failed", error=error)
    return await gate(session, cycle=cycle, run=run)


__all__ = [
    "CycleExecutionGate",
    "CycleExecutionPolicyRegistration",
    "CycleExecutionPolicyRegistry",
    "async_apply_cycle_execution_policy",
    "cycle_execution_policy_registry",
    "validate_cycle_execution_policy_key",
]
