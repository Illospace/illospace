"""Shared evidence-health markers for incomplete worker fan-outs."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from brain.systems.runs.events import run_event
from brain.systems.runs.store import AsyncAgentRunStore


def evidence_health_with_failure(
    evidence_health: Any,
    failure: Mapping[str, Any],
) -> tuple[dict[str, Any], bool]:
    """Add one stable missing-evidence marker without duplicating replays."""

    health = dict(evidence_health or {}) if isinstance(evidence_health, dict) else {}
    failures = [
        dict(item)
        for item in health.get("failures") or []
        if isinstance(item, dict)
    ]
    identity = _failure_identity(failure)
    added = not any(_failure_identity(item) == identity for item in failures)
    if added:
        failures.append(dict(failure))

    missing_shards = [
        str(item.get("shard") or item.get("repo") or "").strip()
        for item in failures
    ]
    health["status"] = "degraded"
    health["completeness"] = "unavailable"
    health["failures"] = failures[:20]
    health["failure_count"] = len(failures)
    health["missing_shards"] = list(dict.fromkeys(filter(None, missing_shards)))[:20]
    return health, added


def evidence_health_for_completed_fanout(
    evidence_health: Any,
    *,
    worker_shards: list[str],
) -> dict[str, Any]:
    """Mark a standalone fan-out complete without erasing broader degradation."""

    health = dict(evidence_health or {}) if isinstance(evidence_health, dict) else {}
    if health.get("failures") or health.get("status") == "degraded":
        return health
    # A scheduled Cycle owns a broader pending receipt. Worker completion alone
    # must not claim those other expected checks have completed.
    if health.get("status") == "pending":
        return health
    health["status"] = "ok"
    health["completeness"] = "complete"
    health["worker_shards"] = list(dict.fromkeys(filter(None, worker_shards)))[:20]
    return health


async def record_parent_evidence_failures(
    session: Any,
    *,
    parent: Any,
    failures: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Persist fan-out degradation on the parent and its Cycle receipt."""

    if not failures:
        return []
    parent_metadata = (
        dict(parent.metadata_ or {})
        if isinstance(getattr(parent, "metadata_", None), dict)
        else {}
    )
    added_failures: list[dict[str, Any]] = []
    health = parent_metadata.get("evidence_health")
    for failure in failures:
        health, added = evidence_health_with_failure(health, failure)
        if added:
            added_failures.append(failure)
    parent_metadata["evidence_health"] = health
    parent.metadata_ = parent_metadata

    cycle_run_id = (
        parent_metadata.get("cycle_run_id")
        if parent_metadata.get("source") == "cycle"
        else None
    )
    if cycle_run_id:
        try:
            from brain.platform.db.models.cycle import CycleRun

            cycle_run = await session.get(
                CycleRun,
                int(cycle_run_id),
                with_for_update=True,
            )
        except (TypeError, ValueError):
            cycle_run = None
        if cycle_run is not None:
            context_snapshot = dict(cycle_run.context_snapshot or {})
            cycle_health = context_snapshot.get("evidence_health")
            for failure in failures:
                cycle_health, _ = evidence_health_with_failure(
                    cycle_health,
                    failure,
                )
            context_snapshot["evidence_health"] = cycle_health
            cycle_run.context_snapshot = context_snapshot

    store = AsyncAgentRunStore(session)
    for failure in added_failures:
        await store.append_event(
            run_event(
                int(parent.id),
                "run.worker_failed",
                failure,
                root_run_id=parent.root_run_id or parent.id,
                producer="spawn_worker",
            )
        )
    return added_failures


def _failure_identity(failure: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        failure.get("worker_run_id") or failure.get("child_run_id"),
        failure.get("shard") or failure.get("repo"),
        failure.get("stage"),
        failure.get("configuration_error"),
    )


__all__ = [
    "evidence_health_for_completed_fanout",
    "evidence_health_with_failure",
    "record_parent_evidence_failures",
]
