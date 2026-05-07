"""Shared time helpers."""

from __future__ import annotations

from datetime import datetime, timezone


def utcnow() -> datetime:
    """Return a timezone-aware UTC timestamp."""

    return datetime.now(timezone.utc)


def ensure_utc(value: datetime | None = None) -> datetime:
    """Return ``value`` normalized to UTC, requiring timezone-aware inputs."""

    dt = value or utcnow()
    if dt.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    return dt.astimezone(timezone.utc)
