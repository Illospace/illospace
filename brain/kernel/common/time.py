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


def assume_utc(value: datetime | None = None) -> datetime:
    """Normalize to UTC, treating a naive value as UTC.

    Unlike :func:`ensure_utc`, this helper accepts database timestamps that
    have no timezone information and assumes that they already represent UTC.
    """

    dt = value or utcnow()
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def assume_utc_optional(value: datetime | None) -> datetime | None:
    """Normalize an optional timestamp to UTC while preserving ``None``.

    Unlike :func:`assume_utc`, which replaces ``None`` with the current time,
    this helper returns ``None`` unchanged. Unlike :func:`ensure_utc`, it
    accepts naive timestamps and assumes that they already represent UTC.
    """

    return None if value is None else assume_utc(value)
