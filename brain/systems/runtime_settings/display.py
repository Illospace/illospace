from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from .memory import _async_read_runtime_config_value, _async_write_runtime_config_value
from .schemas import RuntimeDisplayRead, RuntimeDisplayUpdate

logger = logging.getLogger(__name__)

RUNTIME_DISPLAY_SETTINGS_KEY = "runtime_display"
DISPLAY_TIMEZONE_SETTING_KEY = "display_timezone"
DEFAULT_DISPLAY_TIMEZONE = "America/New_York"
RUNTIME_PREFERENCE_RECEIPT_KIND = "runtime_preference_write_receipt"
_MAX_WRITE_RECEIPTS = 20

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
    write_receipts: tuple["RuntimePreferenceWriteReceipt", ...] = ()

    def stored_settings(self) -> dict[str, object]:
        payload: dict[str, object] = {
            DISPLAY_TIMEZONE_SETTING_KEY: self.display_timezone,
        }
        if self.write_receipts:
            payload["write_receipts"] = [
                receipt.to_payload()
                for receipt in self.write_receipts[-_MAX_WRITE_RECEIPTS:]
            ]
        return payload


@dataclass(frozen=True)
class RuntimePreferenceWriteReceipt:
    receipt_id: str
    run_id: int | None
    org_id: str
    actor_user_id: str
    setting: str
    value: str
    recorded_at: str
    storage_key: str = RUNTIME_DISPLAY_SETTINGS_KEY
    kind: str = RUNTIME_PREFERENCE_RECEIPT_KIND

    @classmethod
    def from_payload(cls, payload: object) -> "RuntimePreferenceWriteReceipt | None":
        if not isinstance(payload, dict):
            return None
        if str(payload.get("kind") or "") != RUNTIME_PREFERENCE_RECEIPT_KIND:
            return None
        receipt_id = str(payload.get("receipt_id") or "").strip()
        org_id = str(payload.get("org_id") or "").strip()
        actor_user_id = str(payload.get("actor_user_id") or "").strip()
        setting = str(payload.get("setting") or "").strip()
        value = str(payload.get("value") or "").strip()
        recorded_at = str(payload.get("recorded_at") or "").strip()
        if not all((receipt_id, org_id, actor_user_id, setting, value, recorded_at)):
            return None
        raw_run_id = payload.get("run_id")
        try:
            run_id = int(raw_run_id) if raw_run_id not in (None, "") else None
        except (TypeError, ValueError):
            return None
        return cls(
            receipt_id=receipt_id,
            run_id=run_id,
            org_id=org_id,
            actor_user_id=actor_user_id,
            setting=setting,
            value=value,
            recorded_at=recorded_at,
            storage_key=str(payload.get("storage_key") or RUNTIME_DISPLAY_SETTINGS_KEY),
        )

    def to_payload(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "receipt_id": self.receipt_id,
            "run_id": self.run_id,
            "org_id": self.org_id,
            "actor_user_id": self.actor_user_id,
            "setting": self.setting,
            "value": self.value,
            "recorded_at": self.recorded_at,
            "storage_key": self.storage_key,
        }


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
    raw_receipts = data.get("write_receipts")
    receipt_payloads = raw_receipts if isinstance(raw_receipts, list) else []
    receipts = tuple(
        receipt
        for item in receipt_payloads[-_MAX_WRITE_RECEIPTS:]
        if (receipt := RuntimePreferenceWriteReceipt.from_payload(item)) is not None
    )
    return RuntimeDisplayConfig(
        display_timezone=timezone_name,
        write_receipts=receipts,
    )


async def async_get_runtime_display(session: AsyncSession) -> RuntimeDisplayRead:
    config = await async_get_runtime_display_config(session)
    return RuntimeDisplayRead(display_timezone=config.display_timezone)


async def async_update_runtime_display(
    session: AsyncSession,
    user: object,
    update: RuntimeDisplayUpdate,
    *,
    write_receipt: RuntimePreferenceWriteReceipt | None = None,
) -> RuntimeDisplayRead:
    del user  # The keyed runtime-settings store is installation-scoped.
    current = await async_get_runtime_display_config(session)
    receipts = current.write_receipts
    if write_receipt is not None:
        receipts = (*receipts, write_receipt)[-_MAX_WRITE_RECEIPTS:]
    config = RuntimeDisplayConfig(
        display_timezone=normalize_display_timezone(update.display_timezone),
        write_receipts=receipts,
    )
    await _async_write_runtime_config_value(
        session,
        RUNTIME_DISPLAY_SETTINGS_KEY,
        json.dumps(config.stored_settings(), sort_keys=True),
    )
    return RuntimeDisplayRead(display_timezone=config.display_timezone)


def format_display_timestamp(
    source_time: datetime,
    display_timezone: str,
) -> str:
    """Render one UTC instant with a DST-aware display time and reconcilable UTC."""

    if source_time.tzinfo is None:
        source_utc = source_time.replace(tzinfo=timezone.utc)
    else:
        source_utc = source_time.astimezone(timezone.utc)
    local = source_utc.astimezone(ZoneInfo(normalize_display_timezone(display_timezone)))
    zone_label = local.tzname() or display_timezone
    return (
        f"{local:%m-%d %H:%M} {zone_label} "
        f"({source_utc:%H:%M} UTC)"
    )


_ISO_UTC_TIMESTAMP = re.compile(
    r"\b(?P<stamp>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}"
    r"(?::\d{2}(?:\.\d+)?)?(?:Z|\+00:00))\b",
    re.IGNORECASE,
)
_CLOCK_UTC_TIMESTAMP = re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\s+UTC\b", re.IGNORECASE)


def utc_only_timestamp_lines(
    body: str,
    display_timezone: str,
) -> tuple[str, ...]:
    """Return final-payload lines whose UTC timestamps lack the display-zone pair."""

    timezone_name = normalize_display_timezone(display_timezone)
    if timezone_name == "UTC":
        return ()
    invalid: list[str] = []
    for line in str(body or "").splitlines():
        iso_matches = list(_ISO_UTC_TIMESTAMP.finditer(line))
        clock_matches = list(_CLOCK_UTC_TIMESTAMP.finditer(line))
        if not iso_matches and not clock_matches:
            continue

        valid_line = True
        for match in iso_matches:
            source = datetime.fromisoformat(match.group("stamp").replace("Z", "+00:00"))
            if format_display_timestamp(source, timezone_name) not in line:
                valid_line = False
                break
        if valid_line and clock_matches:
            if timezone_name == DEFAULT_DISPLAY_TIMEZONE:
                zone_tokens = {"ET", "EST", "EDT"}
            else:
                clock = datetime.now(timezone.utc)
                zone = ZoneInfo(timezone_name)
                zone_tokens = {
                    timezone_name,
                    datetime(clock.year, 1, 15, tzinfo=timezone.utc)
                    .astimezone(zone)
                    .tzname()
                    or timezone_name,
                    datetime(clock.year, 7, 15, tzinfo=timezone.utc)
                    .astimezone(zone)
                    .tzname()
                    or timezone_name,
                }
            local_clock = re.compile(
                r"\b\d{1,2}:\d{2}(?::\d{2})?\s+(?:"
                + "|".join(re.escape(token) for token in sorted(zone_tokens))
                + r")\b",
                re.IGNORECASE,
            )
            if local_clock.search(line) is None:
                valid_line = False
        if not valid_line:
            invalid.append(line.strip())
    return tuple(invalid)


__all__ = [
    "DEFAULT_DISPLAY_TIMEZONE",
    "DISPLAY_TIMEZONE_SETTING_KEY",
    "RUNTIME_DISPLAY_SETTINGS_KEY",
    "RUNTIME_PREFERENCE_RECEIPT_KIND",
    "RuntimeDisplayConfig",
    "RuntimePreferenceWriteReceipt",
    "async_get_runtime_display",
    "async_get_runtime_display_config",
    "async_update_runtime_display",
    "format_display_timestamp",
    "normalize_display_timezone",
    "utc_only_timestamp_lines",
]
