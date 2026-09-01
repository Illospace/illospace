"""Measure host storage, persist its trend, and guard the warning edge."""
from __future__ import annotations

import logging
import os
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, TypedDict

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from brain.kernel.common.time import assume_utc, ensure_utc
from brain.platform.async_io import run_blocking
from brain.platform.db.models.memory_health import MemoryHealthLog
from brain.platform.db.models.scheduler import SchedulerAlertLatch
from brain.platform.db.repositories.memory_health import MemoryHealthRepository
from brain.systems.cortex.thread_links import public_app_base_url
from brain.systems.failure_guard.core import (
    FailureGuardTriggerKind,
    FailureGuardTriggerResult,
    async_evaluate_failure_edges,
    async_latch_failure_edges,
)
from brain.systems.failure_guard.slack_delivery import (
    FailureAlertPresentation,
    FailureAlertSubject,
    SlackFailureAlertPolicy,
    async_deliver_failure_alert,
)
from brain.systems.slack.client import slack_web_client_from_runtime
from brain.systems.storage_policy import StoragePolicyValues, async_get_storage_policy
from brain.systems.workspace_inventory import (
    WorkspaceConsumer,
    WorkspaceScanError,
    inventory_workspace,
)


logger = logging.getLogger(__name__)

HOST_CAPACITY_CHECK_TYPE = "host_capacity"
HOST_CAPACITY_ALERT_KEY = "host_capacity_warn"
HOST_CAPACITY_CRITICAL_ALERT_KEY = "host_capacity_critical"


@dataclass(frozen=True, slots=True)
class _CapacitySeverity:
    status: str
    alert_key: str
    trigger_kind: FailureGuardTriggerKind
    alert_title: str
    error_severity: str
    priority: int
    threshold_percent: Callable[[StoragePolicyValues], int]


_CAPACITY_SEVERITIES = (
    _CapacitySeverity(
        status="warn",
        alert_key=HOST_CAPACITY_ALERT_KEY,
        trigger_kind=FailureGuardTriggerKind(HOST_CAPACITY_ALERT_KEY),
        alert_title="Host capacity warning",
        error_severity="warning",
        priority=1,
        threshold_percent=lambda policy: policy.capacity_warn_percent,
    ),
    _CapacitySeverity(
        status="critical",
        alert_key=HOST_CAPACITY_CRITICAL_ALERT_KEY,
        trigger_kind=FailureGuardTriggerKind(HOST_CAPACITY_CRITICAL_ALERT_KEY),
        alert_title="Host capacity critical",
        error_severity="critical",
        priority=2,
        threshold_percent=lambda policy: policy.capacity_critical_percent,
    ),
)


def _capacity_severity_for_kind(
    trigger_kind: FailureGuardTriggerKind,
) -> _CapacitySeverity:
    severity = next(
        (
            severity
            for severity in _CAPACITY_SEVERITIES
            if severity.trigger_kind == trigger_kind
        ),
        None,
    )
    if severity is None:
        raise ValueError(f"Unsupported host-capacity trigger: {trigger_kind}")
    return severity


def _capacity_priority(status: str) -> int:
    if status == "ok":
        return 0
    return next(
        severity.priority
        for severity in _CAPACITY_SEVERITIES
        if severity.status == status
    )


class HostCapacityDetails(TypedDict):
    mount: str
    pct_used: float
    bytes_free: int
    top_consumers: list[WorkspaceConsumer]
    inventory_scan_errors: list[WorkspaceScanError]


@dataclass(frozen=True, slots=True)
class HostDiskCapacity:
    """Cheap live capacity values for the workspace filesystem."""

    mount: str
    pct_used: float
    bytes_free: int
    bytes_total: int


@dataclass(frozen=True, slots=True)
class HostCapacityMeasurement:
    """One live filesystem and workspace-volume measurement."""

    mount: str
    pct_used: float
    bytes_free: int
    bytes_total: int
    top_consumers: tuple[WorkspaceConsumer, ...]
    inventory_scan_errors: tuple[WorkspaceScanError, ...] = ()

    def details(self) -> HostCapacityDetails:
        """Return the exact durable memory-health detail shape."""
        return {
            "mount": self.mount,
            "pct_used": self.pct_used,
            "bytes_free": self.bytes_free,
            "top_consumers": [dict(consumer) for consumer in self.top_consumers],
            "inventory_scan_errors": [
                dict(error) for error in self.inventory_scan_errors
            ],
        }


@dataclass(slots=True)
class _HostCapacityLatchStore:
    """Expose the scheduler-global capacity latch to the neutral guard."""

    session: AsyncSession

    async def load_latches(self):
        latches = await self.session.scalars(
            select(SchedulerAlertLatch).where(
                SchedulerAlertLatch.alert_key.in_(
                    tuple(
                        severity.alert_key
                        for severity in _CAPACITY_SEVERITIES
                    )
                )
            )
        )
        return {
            FailureGuardTriggerKind(latch.alert_key): latch
            for latch in latches
        }

    async def create_latch(
        self,
        trigger_kind: FailureGuardTriggerKind,
        alerted_at: datetime,
    ) -> SchedulerAlertLatch:
        severity = _capacity_severity_for_kind(trigger_kind)
        latch = SchedulerAlertLatch(
            alert_key=severity.alert_key,
            alerted_at=alerted_at,
        )
        self.session.add(latch)
        await self.session.flush()
        return latch


def measure_host_disk_capacity(workspace_root: str | Path) -> HostDiskCapacity:
    """Read live filesystem capacity without walking the workspace tree."""
    root = Path(workspace_root).expanduser().resolve(strict=True)
    usage = shutil.disk_usage(root)
    pct_used = round((usage.used / usage.total) * 100, 1) if usage.total else 0.0
    mount = root
    while mount.parent != mount and not os.path.ismount(mount):
        mount = mount.parent
    return HostDiskCapacity(
        mount=str(mount),
        pct_used=pct_used,
        bytes_free=int(usage.free),
        bytes_total=int(usage.total),
    )


def measure_host_capacity(
    workspace_root: str | Path,
    *,
    top_consumer_limit: int = 10,
) -> HostCapacityMeasurement:
    """Measure the workspace filesystem and its largest direct consumers."""
    root = Path(workspace_root).expanduser().resolve(strict=True)
    disk = measure_host_disk_capacity(root)
    inventory = inventory_workspace(
        root,
        top_consumer_limit=top_consumer_limit,
    )
    return HostCapacityMeasurement(
        mount=disk.mount,
        pct_used=disk.pct_used,
        bytes_free=disk.bytes_free,
        bytes_total=disk.bytes_total,
        top_consumers=inventory.top_consumers,
        inventory_scan_errors=inventory.scan_errors,
    )


async def async_measure_host_capacity(
    workspace_root: str | Path,
) -> HostCapacityMeasurement:
    """Measure capacity without blocking the caller's event loop."""
    return await run_blocking(measure_host_capacity, workspace_root)


async def async_measure_host_disk_capacity(
    workspace_root: str | Path,
) -> HostDiskCapacity:
    """Read live filesystem capacity without blocking the event loop."""
    return await run_blocking(measure_host_disk_capacity, workspace_root)


def _capacity_status(
    pct_used: float,
    policy: StoragePolicyValues,
) -> str:
    if pct_used >= policy.capacity_critical_percent:
        return "critical"
    if pct_used >= policy.capacity_warn_percent:
        return "warn"
    return "ok"


def _thresholds(policy: StoragePolicyValues) -> dict[str, int]:
    return {
        f"{severity.status}_percent": severity.threshold_percent(policy)
        for severity in _CAPACITY_SEVERITIES
    }


def _default_alert_policy() -> SlackFailureAlertPolicy:
    return SlackFailureAlertPolicy(
        provide_client=slack_web_client_from_runtime,
        requested_by="host_capacity",
        reason="Deliver a host-capacity warning to the team.",
        channel=(
            os.getenv("ILLO_SCHEDULER_FAILURE_ALERT_CHANNEL", "").strip()
            or "#alerts"
        ),
        unknown_error_text="Host storage crossed its warning threshold",
    )


AlertDelivery = Callable[..., Awaitable[None]]


async def record_host_capacity(
    session: AsyncSession,
    *,
    workspace_root: str | Path,
    now: datetime | None = None,
    deliver_alert: AlertDelivery | None = async_deliver_failure_alert,
    alert_policy: SlackFailureAlertPolicy | None = None,
) -> dict[str, Any]:
    """Persist one capacity row and alert once when policy becomes unhealthy."""
    observed_at = ensure_utc(now)
    measurement = await async_measure_host_capacity(workspace_root)
    policy = await async_get_storage_policy(session)
    status = _capacity_status(measurement.pct_used, policy)
    details = measurement.details()
    entry = await MemoryHealthRepository(session).a_log_check(
        HOST_CAPACITY_CHECK_TYPE,
        status,
        details,
    )

    store = _HostCapacityLatchStore(session)
    current_priority = _capacity_priority(status)
    alert_keys_to_clear = tuple(
        severity.alert_key
        for severity in _CAPACITY_SEVERITIES
        if severity.priority > current_priority
    )
    if alert_keys_to_clear:
        await session.execute(
            delete(SchedulerAlertLatch).where(
                SchedulerAlertLatch.alert_key.in_(alert_keys_to_clear)
            )
        )

    alert_summary = (
        f"{measurement.pct_used}% used on {measurement.mount}; "
        f"{measurement.bytes_free} bytes free."
    )
    triggers = tuple(
        FailureGuardTriggerResult(
            kind=severity.trigger_kind,
            active=current_priority >= severity.priority,
            public_details={
                "pct_used": measurement.pct_used,
                **_thresholds(policy),
            },
            alert_title=severity.alert_title,
            alert_summary=alert_summary,
        )
        for severity in _CAPACITY_SEVERITIES
    )
    guard = await async_evaluate_failure_edges(
        results=triggers,
        failure_signature=None,
        last_error=None,
        now=observed_at,
        store=store,
        new_edge_mode="detect",
    )

    alert_sent = False
    highest_edge = max(
        guard.crossed_edges,
        key=lambda edge: _capacity_severity_for_kind(edge.kind).priority,
        default=None,
    )
    if deliver_alert is not None and highest_edge is not None:
        severity = _capacity_severity_for_kind(highest_edge.kind)
        try:
            await deliver_alert(
                policy=alert_policy or _default_alert_policy(),
                subject=FailureAlertSubject(
                    identity_label="Mount",
                    identity=measurement.mount,
                    url_label="Illo",
                    url=f"{public_app_base_url()}/api/system",
                    link_label="open system state",
                ),
                presentation=FailureAlertPresentation(
                    title=highest_edge.alert_title,
                    summary=highest_edge.alert_summary,
                ),
                error_text=(
                    "Storage use crossed the active policy "
                    f"{severity.error_severity} threshold of "
                    f"{severity.threshold_percent(policy)}%."
                ),
            )
        except Exception:  # noqa: BLE001 - retry the edge on the next tick
            logger.exception("Host-capacity Slack delivery failed")
        else:
            await async_latch_failure_edges(
                guard,
                alerted_at=observed_at,
                store=store,
            )
            alert_sent = True

    return {
        "id": entry.id,
        "check_type": entry.check_type,
        "status": entry.status,
        "details": details,
        "thresholds": _thresholds(policy),
        "alert_sent": alert_sent,
    }


async def async_read_host_capacity(
    session: AsyncSession,
    *,
    workspace_root: str | Path,
    limit: int = 24,
    refresh_inventory: bool = False,
) -> dict[str, Any]:
    """Return live disk capacity and the latest durable workspace inventory."""
    normalized_limit = max(1, min(int(limit), 168))
    if refresh_inventory:
        refreshed = await async_measure_host_capacity(workspace_root)
        disk = HostDiskCapacity(
            mount=refreshed.mount,
            pct_used=refreshed.pct_used,
            bytes_free=refreshed.bytes_free,
            bytes_total=refreshed.bytes_total,
        )
    else:
        refreshed = None
        disk = await async_measure_host_disk_capacity(workspace_root)
    policy = await async_get_storage_policy(session)
    rows = await session.scalars(
        select(MemoryHealthLog)
        .where(
            MemoryHealthLog.check_type == HOST_CAPACITY_CHECK_TYPE,
            MemoryHealthLog.org_id.is_(None),
        )
        .order_by(MemoryHealthLog.created_at.desc(), MemoryHealthLog.id.desc())
        .limit(normalized_limit)
    )
    trend = [
        {
            "observed_at": assume_utc(row.created_at).isoformat(),
            "status": row.status,
            **(row.details or {}),
        }
        for row in rows.all()
    ]
    latest = trend[0] if trend else None
    if refreshed is not None:
        top_consumers = [dict(item) for item in refreshed.top_consumers]
        scan_errors = [dict(error) for error in refreshed.inventory_scan_errors]
        inventory_observed_at = ensure_utc().isoformat()
    else:
        top_consumers = list((latest or {}).get("top_consumers") or [])
        scan_errors = list((latest or {}).get("inventory_scan_errors") or [])
        inventory_observed_at = (latest or {}).get("observed_at")
    return {
        "current": {
            "mount": disk.mount,
            "pct_used": disk.pct_used,
            "bytes_free": disk.bytes_free,
            "bytes_total": disk.bytes_total,
            "status": _capacity_status(disk.pct_used, policy),
            "top_consumers": top_consumers,
            "inventory_scan_errors": scan_errors,
            "inventory_observed_at": inventory_observed_at,
        },
        "thresholds": _thresholds(policy),
        "trend": trend,
    }


__all__ = [
    "HOST_CAPACITY_ALERT_KEY",
    "HOST_CAPACITY_CRITICAL_ALERT_KEY",
    "HOST_CAPACITY_CHECK_TYPE",
    "HostDiskCapacity",
    "HostCapacityMeasurement",
    "async_measure_host_disk_capacity",
    "async_measure_host_capacity",
    "async_read_host_capacity",
    "measure_host_capacity",
    "measure_host_disk_capacity",
    "record_host_capacity",
]
