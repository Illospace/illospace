"""Shared coercion helpers for narrow data-normalization call sites."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any, TypeVar

T = TypeVar("T")



def as_mapping(value: Any) -> dict[str, Any]:
    """Return ``value`` as a plain dict when it is a mapping, else an empty dict."""

    return dict(value) if isinstance(value, Mapping) else {}


def mapping_view(value: Any) -> Mapping[str, Any]:
    """Return ``value`` when it is a mapping without copying, else an empty mapping."""

    return value if isinstance(value, Mapping) else {}


def object_to_dict(value: Any) -> dict[str, Any]:
    """Best-effort object-to-dict conversion used by persistence/row adapters."""

    if value is None:
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    if hasattr(value, "to_dict"):
        result = value.to_dict()
        if isinstance(result, Mapping):
            return dict(result)
    if hasattr(value, "model_dump"):
        result = value.model_dump()
        if isinstance(result, Mapping):
            return dict(result)
    if hasattr(value, "_mapping"):
        return dict(value._mapping)
    if hasattr(value, "__dict__"):
        return {key: item for key, item in vars(value).items() if not key.startswith("_")}
    return {}


def optional_text(value: Any) -> str | None:
    """Return stripped text or ``None`` when ``value`` is missing/blank."""

    if value is None:
        return None
    text = str(value).strip()
    return text or None


def drop_none(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return a dict without keys whose values are ``None``."""

    return {key: value for key, value in payload.items() if value is not None}


def clamp(value: float | int | None, lower: float = 0.0, upper: float = 1.0) -> float:
    """Clamp numeric ``value`` between ``lower`` and ``upper``.

    ``None`` is treated as ``0.0`` to match the previous local helper variants
    used by scoring code.
    """

    if value is None:
        value = 0.0
    return max(lower, min(upper, float(value)))


def coerce_datetime(value: Any, *, allow_epoch: bool = False, utc: bool = False) -> datetime | None:
    """Parse common datetime shapes with legacy-compatible defaults.

    By default this accepts ``datetime`` instances and ISO strings, preserves the
    original timezone object, and attaches UTC to naive datetimes. Set
    ``allow_epoch`` for numeric epoch seconds and ``utc`` to normalize aware
    results to UTC.
    """

    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    elif allow_epoch and isinstance(value, (int, float)):
        try:
            dt = datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc) if utc else dt


def coerce_float(value: Any, default: T | float | None = None) -> float | T | None:
    """Return ``float(value)`` or ``default`` for missing/unparseable input."""

    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def coerce_int(value: Any, default: T | int = 0) -> int | T:
    """Return ``int(value)`` or ``default`` for missing/unparseable input."""

    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def int_or_none(value: Any, *, strict: bool = False) -> int | None:
    """Return an integer or ``None`` for unparseable input.

    ``strict=True`` preserves evidence-style parsing that rejects booleans,
    decimals, signs, and whitespace-only strings.  The default mirrors the
    broader helper copies used by learning modules: ``int(value)`` when possible.
    """

    if strict:
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, str) and value.strip().isdigit():
            return int(value.strip())
        return None
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
