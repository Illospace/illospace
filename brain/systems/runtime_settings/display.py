from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from brain.platform.db.models.org import User

from .memory import _async_read_runtime_config_value, _async_write_runtime_config_value
from .schemas import RuntimeDisplayRead, RuntimeDisplayUpdate

logger = logging.getLogger(__name__)

RUNTIME_DISPLAY_SETTINGS_KEY = "runtime_display"
DISPLAY_TIMEZONE_SETTING_KEY = "display_timezone"
DEFAULT_DISPLAY_TIMEZONE = "America/New_York"

_DISPLAY_TIMEZONE_ALIASES = {
    "eastern": DEFAULT_DISPLAY_TIMEZONE,
    "eastern time": DEFAULT_DISPLAY_TIMEZONE,
    "et": DEFAULT_DISPLAY_TIMEZONE,
    "est": DEFAULT_DISPLAY_TIMEZONE,
    "edt": DEFAULT_DISPLAY_TIMEZONE,
    "us/eastern": DEFAULT_DISPLAY_TIMEZONE,
}


@dataclass(frozen=True)
class RuntimeDisplayConfig:
    display_timezone: str = DEFAULT_DISPLAY_TIMEZONE

    def stored_settings(self) -> dict[str, str]:
        return {DISPLAY_TIMEZONE_SETTING_KEY: self.display_timezone}


def normalize_display_timezone(value: object) -> str:
    candidate = str(value or "").strip()
    candidate = _DISPLAY_TIMEZONE_ALIASES.get(candidate.lower(), candidate)
    if not candidate:
        raise HTTPException(status_code=400, detail="display_timezone is required.")
    try:
        ZoneInfo(candidate)
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown display_timezone: {candidate}",
        ) from exc
    return candidate


async def async_get_runtime_display_config(session: AsyncSession) -> RuntimeDisplayConfig:
    raw = await _async_read_runtime_config_value(session, RUNTIME_DISPLAY_SETTINGS_KEY)
    if not raw:
        return RuntimeDisplayConfig()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("Ignoring invalid runtime display settings JSON")
        return RuntimeDisplayConfig()
    if not isinstance(data, dict):
        return RuntimeDisplayConfig()
    try:
        timezone_name = normalize_display_timezone(data.get(DISPLAY_TIMEZONE_SETTING_KEY))
    except HTTPException:
        logger.warning("Ignoring invalid persisted display_timezone")
        timezone_name = DEFAULT_DISPLAY_TIMEZONE
    return RuntimeDisplayConfig(display_timezone=timezone_name)


async def async_get_runtime_display(session: AsyncSession) -> RuntimeDisplayRead:
    config = await async_get_runtime_display_config(session)
    return RuntimeDisplayRead(display_timezone=config.display_timezone)


async def async_update_runtime_display(
    session: AsyncSession,
    user: User,
    update: RuntimeDisplayUpdate,
) -> RuntimeDisplayRead:
    del user  # The keyed runtime-settings store is installation-scoped.
    config = RuntimeDisplayConfig(
        display_timezone=normalize_display_timezone(update.display_timezone),
    )
    await _async_write_runtime_config_value(
        session,
        RUNTIME_DISPLAY_SETTINGS_KEY,
        json.dumps(config.stored_settings(), sort_keys=True),
    )
    return RuntimeDisplayRead(display_timezone=config.display_timezone)


__all__ = [
    "DEFAULT_DISPLAY_TIMEZONE",
    "DISPLAY_TIMEZONE_SETTING_KEY",
    "RUNTIME_DISPLAY_SETTINGS_KEY",
    "RuntimeDisplayConfig",
    "async_get_runtime_display",
    "async_get_runtime_display_config",
    "async_update_runtime_display",
    "normalize_display_timezone",
]
