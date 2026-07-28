"""Ordered tracker-integrity maintenance before coordinator sweeps."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import logging
from typing import Any

from sqlalchemy import select

from brain.platform.db.models.domain import Domain


UWEAR_COORDINATOR_CYCLE_NAME = "Uwear Ticket Coordinator Check-ins"
SNAPSHOT_NAMESPACE = "tracker_maintenance"
TRACKER_DOMAIN_SLUG = "github-ticket-tracker"
logger = logging.getLogger("illo.tracker_maintenance")


@dataclass(frozen=True, slots=True)
class TrackerMaintenanceStep:
    name: str
    run: Callable[[Any, str, bool], Awaitable[dict]]
    failure_summary: dict[str, object]


async def _harvest_alert_resolution(
    session: Any,
    org_id: str,
    _notifications_enabled: bool,
) -> dict:
    from brain.systems.alert_resolution import run_alert_resolution_harvest

    return await run_alert_resolution_harvest(
        session,
        org_id=org_id,
    )


async def _reconcile_production_gate(
    session: Any,
    org_id: str,
    notifications_enabled: bool,
) -> dict:
    from brain.systems.staging_only_closure import (
        run_staging_only_closure_sweep,
    )

    kwargs = {} if notifications_enabled else {"notify": False}
    return await run_staging_only_closure_sweep(session, org_id=org_id, **kwargs)


TRACKER_MAINTENANCE_STEPS = (
    TrackerMaintenanceStep(
        name="alert_resolution_harvest",
        run=_harvest_alert_resolution,
        failure_summary={
            "updated": 0,
            "movements": [],
            "errors": [],
        },
    ),
    TrackerMaintenanceStep(
        name="production_gate_reconciliation",
        run=_reconcile_production_gate,
        failure_summary={
            "updated": 0,
            "flagged": 0,
            "messages_posted": 0,
            "errors": [],
        },
    ),
)


async def run_tracker_maintenance(
    session: Any,
    *,
    org_id: str,
    notifications_enabled: bool = True,
) -> dict[str, dict]:
    """Run the canonical tracker maintenance pipeline for one org."""

    summaries: dict[str, dict] = {}
    for step in TRACKER_MAINTENANCE_STEPS:
        try:
            begin_nested = getattr(session, "begin_nested", None)
            if callable(begin_nested):
                async with begin_nested():
                    summary = await step.run(
                        session,
                        str(org_id),
                        notifications_enabled,
                    )
            else:
                summary = await step.run(
                    session,
                    str(org_id),
                    notifications_enabled,
                )
        except Exception as exc:  # noqa: BLE001 - maintenance never blocks launch
            logger.exception(
                "coordinator tracker-maintenance step %s failed safely",
                step.name,
            )
            summary = {
                **step.failure_summary,
                "errors": [str(exc)],
            }
        summaries[step.name] = summary
    return summaries


async def run_cold_start_tracker_maintenance(
    session: Any,
) -> dict[str, object]:
    """Reconcile every live tracker org without lane-specific Slack posts."""

    org_ids = list(
        (
            await session.scalars(
                select(Domain.org_id)
                .where(
                    Domain.slug == TRACKER_DOMAIN_SLUG,
                    Domain.archived_at.is_(None),
                )
                .distinct()
                .order_by(Domain.org_id.asc())
            )
        ).all()
    )
    summaries: dict[str, dict[str, dict]] = {}
    for org_id in org_ids:
        summaries[str(org_id)] = await run_tracker_maintenance(
            session,
            org_id=str(org_id),
            notifications_enabled=False,
        )
    return {
        "orgs": len(org_ids),
        "summaries": summaries,
    }


async def maybe_run_tracker_maintenance(
    session: Any,
    *,
    cycle: Any,
    run: Any,
) -> dict[str, dict] | None:
    """Run each named step once, in order, and persist one namespaced snapshot."""

    if cycle.name != UWEAR_COORDINATOR_CYCLE_NAME:
        return None
    summaries = await run_tracker_maintenance(
        session,
        org_id=str(cycle.org_id),
    )
    run.context_snapshot = {
        **dict(run.context_snapshot or {}),
        SNAPSHOT_NAMESPACE: summaries,
    }
    return summaries
