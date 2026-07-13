"""Illo Brain — Packet outcome reporting (pure).

The pillar-5 seed, coordinator edition (spec: illo-handoff-packets slice
07): did packets change behavior? Minted vs launched vs ignored, and how
fast a launch followed a mint — data for the next "what's the next most
valuable step?" conversation instead of vibes.

Definitions (review-hardened, slice 04/07):

- **"Launched" means ``launch_count > 0``, never status** — supersede
  archives rows, and slice 04's per-target marking (codex → redirect,
  claude → copy-button POST, page views never count) is what keeps the
  count honest.
- **A supersede chain counts as ONE job**: refreshed revisions link via
  ``metadata_["supersedes"]`` / ``["superseded_by"]``; the job is launched
  if ANY revision launched, its mint time is the FIRST revision's, and its
  owner is the NEWEST revision's.
- Chains partially outside the loaded window collapse over the rows in
  hand — good enough for a weekly digest line, documented rather than
  hidden.

Pure: rows in, summary out; the clock is a parameter.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Any

IGNORED_AFTER_HOURS = 48


@dataclass(frozen=True)
class OutcomeSummary:
    minted: int
    launched: int
    ignored: int  # old enough to have been seen, never launched
    pending: int  # not launched yet, but younger than the ignore horizon
    median_minutes_to_launch: float | None
    per_member: dict[str, dict[str, int]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "minted": self.minted,
            "launched": self.launched,
            "ignored": self.ignored,
            "pending": self.pending,
            "median_minutes_to_launch": self.median_minutes_to_launch,
            "per_member": dict(self.per_member),
        }


def _meta(row: Any) -> dict[str, Any]:
    return dict(getattr(row, "metadata_", None) or {})


def _ts(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return None


def _chains(rows: list[Any]) -> list[list[Any]]:
    """Group rows into supersede chains (union over the rows in hand)."""
    by_id = {str(getattr(r, "id", "")): r for r in rows}
    root_of: dict[str, str] = {}

    def find_root(row_id: str) -> str:
        seen: set[str] = set()
        current = row_id
        while current not in seen:
            seen.add(current)
            if current in root_of:
                current = root_of[current]
                continue
            row = by_id.get(current)
            older = str(_meta(row).get("supersedes") or "") if row is not None else ""
            if older and older in by_id:
                current = older
            else:
                break
        return current

    grouped: dict[str, list[Any]] = {}
    for row_id, row in by_id.items():
        root = find_root(row_id)
        root_of[row_id] = root
        grouped.setdefault(root, []).append(row)
    return list(grouped.values())


def packet_outcomes(
    handoff_rows: list[Any],
    *,
    now: datetime,
    ignored_after_hours: int = IGNORED_AFTER_HOURS,
) -> OutcomeSummary:
    launched_count = 0
    ignored = 0
    pending = 0
    launch_deltas: list[float] = []
    per_member: dict[str, dict[str, int]] = {}

    chains = _chains(list(handoff_rows or []))
    for chain in chains:
        chain.sort(key=lambda r: (_ts(getattr(r, "created_at", None)) or now))
        minted_at = _ts(getattr(chain[0], "created_at", None))
        owner = str(_meta(chain[-1]).get("owner_user_id") or "") or "unclaimed"
        member = per_member.setdefault(owner, {"minted": 0, "launched": 0})
        member["minted"] += 1

        # NOTE: the model records only last_launched_at (no first-launch
        # column), so a relaunch days later shifts a chain's reported launch
        # time; min() across chain rows partially mitigates. Schema change
        # if precision ever matters.
        launched_rows = [r for r in chain if int(getattr(r, "launch_count", 0) or 0) > 0]
        if launched_rows:
            launched_count += 1
            member["launched"] += 1
            launch_times = [
                t for t in (_ts(getattr(r, "last_launched_at", None)) for r in launched_rows) if t
            ]
            if minted_at and launch_times:
                delta = (min(launch_times) - minted_at).total_seconds() / 60.0
                if delta >= 0:
                    launch_deltas.append(delta)
        elif minted_at and (now - minted_at) > timedelta(hours=ignored_after_hours):
            ignored += 1
        else:
            pending += 1

    return OutcomeSummary(
        minted=len(chains),
        launched=launched_count,
        ignored=ignored,
        pending=pending,
        median_minutes_to_launch=round(median(launch_deltas), 1) if launch_deltas else None,
        per_member=per_member,
    )


def format_outcomes_line(summary: OutcomeSummary) -> str | None:
    """The digest footer: one line, no new section. None when nothing to say."""
    if not summary.minted:
        return None
    parts = [f"{summary.minted} minted", f"{summary.launched} launched"]
    if summary.ignored:
        parts.append(f"{summary.ignored} ignored >{IGNORED_AFTER_HOURS}h")
    if summary.median_minutes_to_launch is not None:
        minutes = summary.median_minutes_to_launch
        rendered = f"{minutes / 60:.1f}h" if minutes >= 90 else f"{minutes:.0f}m"
        parts.append(f"median {rendered} to launch")
    return "Packets: " + " · ".join(parts)


async def load_packet_handoffs(session: Any, *, org_id: str, since: datetime) -> list[Any]:
    """Packet-minted handoff rows for the window (thin, read-only)."""
    from sqlalchemy import select

    from brain.platform.db.models.launch_handoff import LaunchHandoff

    return (
        (
            await session.execute(
                select(LaunchHandoff).where(
                    LaunchHandoff.org_id == str(org_id),
                    LaunchHandoff.source_surface == "inbound_triage",
                    LaunchHandoff.created_at >= since,
                )
            )
        )
        .scalars()
        .all()
    )
