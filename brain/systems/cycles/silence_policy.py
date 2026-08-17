"""Policy and pure evaluation for Cycle receipt-silence detection."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import json
import logging
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from brain.kernel.common.time import assume_utc_optional
from brain.systems.cycles.common import cycle_executor_binding
from brain.systems.cycles.schedules import compute_latest_run_at
from brain.systems.runtime_settings.memory import _async_read_runtime_config_value


logger = logging.getLogger(__name__)

CYCLE_SILENCE_RUNTIME_SETTINGS_KEY = "runtime_cycle_silence"
DEFAULT_CYCLE_SILENCE_GRACE_MINUTES = 30
MAX_CYCLE_SILENCE_GRACE_MINUTES = 10_080


@dataclass(frozen=True, slots=True)
class CycleSilencePolicy:
    grace_margin: timedelta


@dataclass(frozen=True, slots=True)
class CycleSilenceCandidate:
    cycle_id: int
    name: str
    binding: str
    expected_at: datetime
    last_receipt_at: datetime | None
    grace_margin: timedelta


class CycleReceiptSnapshot(Protocol):
    cycle_id: int
    name: str
    executor_binding: str | None
    schedule_expr: str
    timezone: str
    receipt_monitoring_started_at: datetime | None
    created_at: datetime | None
    last_receipt_at: datetime | None


def evaluate_cycle_silence_candidate(
    snapshot: CycleReceiptSnapshot,
    *,
    now: datetime,
    grace_margin: timedelta,
) -> CycleSilenceCandidate | None:
    """Return an overdue candidate using only the supplied snapshot and time."""
    expected_at = compute_latest_run_at(
        snapshot.schedule_expr,
        snapshot.timezone,
        at_or_before=now - grace_margin,
    )
    if expected_at is None:
        return None
    monitoring_started_at = assume_utc_optional(
        snapshot.receipt_monitoring_started_at or snapshot.created_at
    )
    if monitoring_started_at is None or expected_at < monitoring_started_at:
        return None
    normalized_receipt = assume_utc_optional(snapshot.last_receipt_at)
    if normalized_receipt is not None and normalized_receipt >= expected_at:
        return None
    return CycleSilenceCandidate(
        cycle_id=snapshot.cycle_id,
        name=snapshot.name,
        binding=cycle_executor_binding(snapshot),
        expected_at=expected_at,
        last_receipt_at=normalized_receipt,
        grace_margin=grace_margin,
    )


def _grace_minutes(value: object) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= MAX_CYCLE_SILENCE_GRACE_MINUTES
    ):
        raise ValueError(
            "grace_minutes must be an integer between 1 and "
            f"{MAX_CYCLE_SILENCE_GRACE_MINUTES}"
        )
    return value


async def async_cycle_silence_policy(
    session: AsyncSession,
) -> CycleSilencePolicy:
    """Load the live installation policy, falling back safely when invalid."""
    grace_minutes = DEFAULT_CYCLE_SILENCE_GRACE_MINUTES
    raw = await _async_read_runtime_config_value(
        session,
        CYCLE_SILENCE_RUNTIME_SETTINGS_KEY,
    )
    if raw:
        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError("runtime cycle silence settings must be an object")
            grace_minutes = _grace_minutes(payload.get("grace_minutes"))
        except (json.JSONDecodeError, ValueError):
            logger.warning("Ignoring invalid runtime Cycle silence settings")
    return CycleSilencePolicy(grace_margin=timedelta(minutes=grace_minutes))


__all__ = [
    "CYCLE_SILENCE_RUNTIME_SETTINGS_KEY",
    "CycleSilenceCandidate",
    "CycleSilencePolicy",
    "async_cycle_silence_policy",
    "evaluate_cycle_silence_candidate",
]
