"""Database-backed policy for Cycle receipt-silence detection."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import json
import logging

from sqlalchemy.ext.asyncio import AsyncSession

from brain.systems.runtime_settings.memory import _async_read_runtime_config_value


logger = logging.getLogger(__name__)

CYCLE_SILENCE_RUNTIME_SETTINGS_KEY = "runtime_cycle_silence"
DEFAULT_CYCLE_SILENCE_GRACE_MINUTES = 30
MAX_CYCLE_SILENCE_GRACE_MINUTES = 10_080


@dataclass(frozen=True, slots=True)
class CycleSilencePolicy:
    grace_margin: timedelta


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
    "CycleSilencePolicy",
    "async_cycle_silence_policy",
]
