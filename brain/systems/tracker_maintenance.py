"""Ordered tracker-integrity maintenance before coordinator sweeps."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import logging
from typing import Any


UWEAR_COORDINATOR_CYCLE_NAME = "Uwear Ticket Coordinator Check-ins"
SNAPSHOT_NAMESPACE = "tracker_maintenance"
logger = logging.getLogger("illo.tracker_maintenance")


@dataclass(frozen=True, slots=True)
class TrackerMaintenanceStep:
    name: str
    run: Callable[[Any, str], Awaitable[dict]]
    failure_summary: dict[str, object]


async def _harvest_alert_resolution(session: Any, org_id: str) -> dict:
    from brain.systems.alert_resolution import run_alert_resolution_harvest

    return await run_alert_resolution_harvest(
        session,
        org_id=org_id,
    )


async def _reconcile_production_gate(session: Any, org_id: str) -> dict:
    from brain.systems.staging_only_closure import (
        run_staging_only_closure_sweep,
    )

    return await run_staging_only_closure_sweep(
        session,
        org_id=org_id,
    )


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


async def maybe_run_tracker_maintenance(
    session: Any,
    *,
    cycle: Any,
    run: Any,
) -> dict[str, dict] | None:
    """Run each named step once, in order, and persist one namespaced snapshot."""

    if cycle.name != UWEAR_COORDINATOR_CYCLE_NAME:
        return None
    summaries: dict[str, dict] = {}
    for step in TRACKER_MAINTENANCE_STEPS:
        try:
            summary = await step.run(session, str(cycle.org_id))
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
    run.context_snapshot = {
        **dict(run.context_snapshot or {}),
        SNAPSHOT_NAMESPACE: summaries,
    }
    return summaries
