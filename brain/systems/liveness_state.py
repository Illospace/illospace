"""Minimal process liveness state shared by internal and public consumers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from brain.platform.db.models.agent_run import AgentRunRow


_KNOWN_SURFACES = {
    "ai_timeline",
    "api",
    "cortex",
    "headless",
    "illo",
    "mcp",
    "scheduler",
    "slack",
    "thread_discussion",
}


@dataclass(frozen=True)
class LivenessSnapshot:
    """A bounded snapshot that does not expose run content or customer data."""

    ts: str
    last_run_id: int | None
    last_surface: str

    def as_public_dict(self) -> dict[str, str | int | None]:
        return {
            "ts": self.ts,
            "last_run_id": self.last_run_id,
            "last_surface": self.last_surface,
        }


def _utc_z(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def _coarse_surface(run: AgentRunRow | Any | None) -> str:
    if run is None:
        return "unknown"
    metadata = run.metadata_ if isinstance(getattr(run, "metadata_", None), dict) else {}
    target_ref = run.target_ref if isinstance(getattr(run, "target_ref", None), dict) else {}
    for key in (
        "originating_surface",
        "source_surface",
        "triggering_surface",
        "origin",
    ):
        value = str(metadata.get(key) or target_ref.get(key) or "").strip().lower()
        if value in _KNOWN_SURFACES:
            return value
    return "unknown"


def build_liveness_snapshot(
    latest_run: AgentRunRow | Any | None,
    *,
    now: datetime | None = None,
) -> LivenessSnapshot:
    """Build the complete bounded snapshot shared by all liveness surfaces."""

    clock = now or datetime.now(timezone.utc)
    run_id = getattr(latest_run, "id", None)
    return LivenessSnapshot(
        ts=_utc_z(clock),
        last_run_id=int(run_id) if run_id is not None else None,
        last_surface=_coarse_surface(latest_run),
    )


async def latest_liveness_snapshot(
    session: AsyncSession,
    *,
    now: datetime | None = None,
) -> LivenessSnapshot:
    """Read the latest run and return the shared bounded snapshot."""

    latest_run = await session.scalar(
        select(AgentRunRow)
        .order_by(AgentRunRow.created_at.desc(), AgentRunRow.id.desc())
        .limit(1)
    )
    return build_liveness_snapshot(latest_run, now=now)


__all__ = [
    "LivenessSnapshot",
    "build_liveness_snapshot",
    "latest_liveness_snapshot",
]
