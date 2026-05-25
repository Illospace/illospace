"""Visibility helpers for AgentRun rows and projections."""

from __future__ import annotations

from typing import Any


def run_metadata(run: Any | None) -> dict[str, Any]:
    if run is None:
        return {}
    metadata = getattr(run, "metadata_", None)
    if not isinstance(metadata, dict):
        metadata = getattr(run, "metadata", None)
    return dict(metadata or {}) if isinstance(metadata, dict) else {}


def run_is_headless(run: Any | None) -> bool:
    return bool(run_metadata(run).get("headless"))


def visible_run_rows(rows: list[Any]) -> list[Any]:
    return [row for row in rows if not run_is_headless(row)]


async def fetch_visible_run_rows(
    session: Any,
    stmt: Any,
    *,
    limit: int | None = None,
    batch_size: int | None = None,
) -> list[Any]:
    """Fetch non-headless runs without letting hidden rows consume visible limits."""

    if limit is None:
        result = await session.scalars(stmt)
        return visible_run_rows(list(result.all()))

    target = max(0, int(limit))
    if target == 0:
        return []

    size = max(int(batch_size or 0), target * 2, 20)
    visible: list[Any] = []
    offset = 0
    while len(visible) < target:
        result = await session.scalars(stmt.limit(size).offset(offset))
        rows = list(result.all())
        if not rows:
            break
        visible.extend(row for row in rows if not run_is_headless(row))
        if len(rows) < size:
            break
        offset += size
    return visible[:target]

