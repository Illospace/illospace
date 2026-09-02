"""Measure host storage, persist its trend, and alert once per capacity edge."""
from __future__ import annotations

import logging
import os
import shutil
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
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
HOST_CAPACITY_RECLAMATION_DISABLED_ALERT_KEY = (
    "host_capacity_reclamation_disabled"
)


@dataclass(frozen=True, slots=True)
class _CapacityAlert:
    """One latched host-capacity alert and its Slack presentation."""

    alert_key: str
    alert_title: str
    error_text: Callable[[StoragePolicyValues], str]


@dataclass(frozen=True, slots=True)
class _CapacityLevel(_CapacityAlert):
    """One rung of the capacity ladder.

    Rank is the position in ``_CAPACITY_LEVELS``; ``status`` is the durable
    memory-health status written when this is the highest crossed rung.
    """

    status: str
    threshold_percent: Callable[[StoragePolicyValues], int]


# Ordered lowest to highest. Adding a rung is one row here: status, thresholds
# and the alert ladder all derive from this tuple.
_CAPACITY_LEVELS: tuple[_CapacityLevel, ...] = (
    _CapacityLevel(
        alert_key=HOST_CAPACITY_ALERT_KEY,
        alert_title="Host capacity warning",
        error_text=lambda policy: (
            "Storage use crossed the active policy warning threshold of "
            f"{policy.capacity_warn_percent}%."
        ),
        status="warn",
        threshold_percent=lambda policy: policy.capacity_warn_percent,
    ),
    _CapacityLevel(
        alert_key=HOST_CAPACITY_CRITICAL_ALERT_KEY,
        alert_title="Host capacity critical",
        error_text=lambda policy: (
            "Storage use crossed the active policy critical threshold of "
            f"{policy.capacity_critical_percent}%."
        ),
        status="critical",
        threshold_percent=lambda policy: policy.capacity_critical_percent,
    ),
)

# Not a rung: a policy-risk alert that fires when any rung is crossed while the
# automatic reclamation safety net is switched off.
_RECLAMATION_RISK_ALERT = _CapacityAlert(
    alert_key=HOST_CAPACITY_RECLAMATION_DISABLED_ALERT_KEY,
    alert_title="Host capacity risk: automatic reclamation disabled",
    error_text=lambda _policy: (
        "Host capacity is unhealthy while automatic workspace reclamation "
        "is disabled by policy."
    ),
)

_CAPACITY_ALERTS: tuple[_CapacityAlert, ...] = (
    *_CAPACITY_LEVELS,
    _RECLAMATION_RISK_ALERT,
)


def _mount_alert_key(alert_key: str, mount: str) -> str:
    """Return one scheduler-latch key scoped to a resolved mount."""
    scoped_key = f"{alert_key}:{mount}"
    if len(scoped_key) <= 80:
        return scoped_key
    mount_digest = sha256(mount.encode("utf-8")).hexdigest()[:16]
    digest_suffix = f":{mount_digest}"
    mount_length = 80 - len(alert_key) - 1 - len(digest_suffix)
    return f"{alert_key}:{mount[:mount_length]}{digest_suffix}"


def _reclamation_risk_active(status: str, policy: StoragePolicyValues) -> bool:
    return status != "ok" and not policy.automatic_reclamation_allowed


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
    """Expose one mount's capacity latches to the neutral guard."""

    session: AsyncSession
    mount: str

    def _alert_keys(self) -> dict[FailureGuardTriggerKind, _CapacityAlert]:
        return {
            FailureGuardTriggerKind(
                _mount_alert_key(alert.alert_key, self.mount)
            ): alert
            for alert in _CAPACITY_ALERTS
        }

    async def load_latches(self):
        alerts_by_kind = self._alert_keys()
        latches = await self.session.scalars(
            select(SchedulerAlertLatch).where(
                SchedulerAlertLatch.alert_key.in_(tuple(alerts_by_kind))
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
        if trigger_kind not in self._alert_keys():
            raise ValueError(f"Unsupported host-capacity trigger: {trigger_kind}")
        latch = SchedulerAlertLatch(
            alert_key=str(trigger_kind),
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


def measure_host_capacities(
    workspace_root: str | Path,
    monitored_paths: Iterable[str | Path],
) -> tuple[HostCapacityMeasurement, ...]:
    """Measure the workspace mount and each additional distinct mount."""
    workspace = measure_host_capacity(workspace_root)
    measurements_by_mount = {workspace.mount: workspace}
    for path in monitored_paths:
        disk = measure_host_disk_capacity(path)
        measurements_by_mount.setdefault(
            disk.mount,
            HostCapacityMeasurement(
                mount=disk.mount,
                pct_used=disk.pct_used,
                bytes_free=disk.bytes_free,
                bytes_total=disk.bytes_total,
                top_consumers=(),
            ),
        )
    return tuple(measurements_by_mount.values())


async def async_measure_host_capacities(
    workspace_root: str | Path,
    monitored_paths: Iterable[str | Path],
) -> tuple[HostCapacityMeasurement, ...]:
    """Measure distinct host mounts without blocking the event loop."""
    return await run_blocking(
        measure_host_capacities,
        workspace_root,
        tuple(monitored_paths),
    )


def _crossed_levels(
    pct_used: float,
    policy: StoragePolicyValues,
) -> tuple[_CapacityLevel, ...]:
    """Return every ladder rung whose threshold ``pct_used`` has reached."""
    return tuple(
        level
        for level in _CAPACITY_LEVELS
        if pct_used >= level.threshold_percent(policy)
    )


def _status_for(crossed_levels: tuple[_CapacityLevel, ...]) -> str:
    """The highest crossed rung names the status; no rung means ``"ok"``."""
    return crossed_levels[-1].status if crossed_levels else "ok"


def _capacity_status(
    pct_used: float,
    policy: StoragePolicyValues,
) -> str:
    return _status_for(_crossed_levels(pct_used, policy))


def _thresholds(policy: StoragePolicyValues) -> dict[str, int]:
    """Return ``{"warn_percent": int, "critical_percent": int}`` from policy."""
    return {
        f"{level.status}_percent": level.threshold_percent(policy)
        for level in _CAPACITY_LEVELS
    }


def _default_alert_policy() -> SlackFailureAlertPolicy:
    return SlackFailureAlertPolicy(
        provide_client=slack_web_client_from_runtime,
        requested_by="host_capacity",
        reason=(
            "Deliver a host-capacity alert (warning, critical, or "
            "reclamation-disabled risk) to the team."
        ),
        channel=(
            os.getenv("ILLO_SCHEDULER_FAILURE_ALERT_CHANNEL", "").strip()
            or "#alerts"
        ),
        unknown_error_text="Host storage capacity crossed an alert threshold",
    )


AlertDelivery = Callable[..., Awaitable[None]]


async def _record_mount_capacity(
    session: AsyncSession,
    *,
    measurement: HostCapacityMeasurement,
    policy: StoragePolicyValues,
    observed_at: datetime,
    deliver_alert: AlertDelivery | None,
    alert_policy: SlackFailureAlertPolicy | None,
) -> dict[str, Any]:
    """Persist and alert for one already de-duplicated mount measurement."""
    thresholds = _thresholds(policy)
    active_levels = _crossed_levels(measurement.pct_used, policy)
    status = _status_for(active_levels)
    details = measurement.details()
    entry = await MemoryHealthRepository(session).a_log_check(
        HOST_CAPACITY_CHECK_TYPE,
        status,
        details,
    )

    store = _HostCapacityLatchStore(session, measurement.mount)
    alert_activity: tuple[tuple[_CapacityAlert, bool], ...] = (
        *((level, level in active_levels) for level in _CAPACITY_LEVELS),
        (_RECLAMATION_RISK_ALERT, _reclamation_risk_active(status, policy)),
    )
    alert_keys_to_clear = tuple(
        _mount_alert_key(alert.alert_key, measurement.mount)
        for alert, active in alert_activity
        if not active
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
            kind=FailureGuardTriggerKind(
                _mount_alert_key(alert.alert_key, measurement.mount)
            ),
            active=active,
            public_details={
                "pct_used": measurement.pct_used,
                **thresholds,
            },
            alert_title=alert.alert_title,
            alert_summary=alert_summary,
        )
        for alert, active in alert_activity
    )
    guard = await async_evaluate_failure_edges(
        results=triggers,
        failure_signature=None,
        last_error=None,
        now=observed_at,
        store=store,
        new_edge_mode="detect",
    )

    # Delivery precedence: one Slack message per mount and tick. A freshly crossed
    # reclamation-risk edge outranks every ladder rung because it names the
    # missing safety net; otherwise deliver the highest crossed rung. Every
    # crossed edge is latched once that one delivery succeeds.
    delivery_order = (_RECLAMATION_RISK_ALERT, *reversed(_CAPACITY_LEVELS))
    crossed_by_kind = {edge.kind: edge for edge in guard.crossed_edges}
    delivered_alert = next(
        (
            alert
            for alert in delivery_order
            if FailureGuardTriggerKind(
                _mount_alert_key(alert.alert_key, measurement.mount)
            )
            in crossed_by_kind
        ),
        None,
    )

    alert_sent = False
    if deliver_alert is not None and delivered_alert is not None:
        delivered_kind = FailureGuardTriggerKind(
            _mount_alert_key(delivered_alert.alert_key, measurement.mount)
        )
        edge = crossed_by_kind[delivered_kind]
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
                    title=edge.alert_title,
                    summary=edge.alert_summary,
                ),
                error_text=delivered_alert.error_text(policy),
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
        "thresholds": thresholds,
        "alert_sent": alert_sent,
    }


async def record_host_capacity(
    session: AsyncSession,
    *,
    workspace_root: str | Path,
    monitored_paths: Iterable[str | Path] | None = None,
    now: datetime | None = None,
    deliver_alert: AlertDelivery | None = async_deliver_failure_alert,
    alert_policy: SlackFailureAlertPolicy | None = None,
) -> dict[str, Any]:
    """Persist one row per mount and maintain independent alert latches."""
    observed_at = ensure_utc(now)
    additional_paths = (Path("/"), *(monitored_paths or ()))
    measurements = await async_measure_host_capacities(
        workspace_root,
        additional_paths,
    )
    policy = await async_get_storage_policy(session)
    mount_results = [
        await _record_mount_capacity(
            session,
            measurement=measurement,
            policy=policy,
            observed_at=observed_at,
            deliver_alert=deliver_alert,
            alert_policy=alert_policy,
        )
        for measurement in measurements
    ]
    return {
        **mount_results[0],
        "mounts": mount_results,
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
        inventory_point = next(
            (point for point in trend if point.get("mount") == disk.mount),
            None,
        )
        if inventory_point is None:
            inventory_row = await session.scalar(
                select(MemoryHealthLog)
                .where(
                    MemoryHealthLog.check_type == HOST_CAPACITY_CHECK_TYPE,
                    MemoryHealthLog.org_id.is_(None),
                    MemoryHealthLog.details["mount"].as_string() == disk.mount,
                )
                .order_by(
                    MemoryHealthLog.created_at.desc(),
                    MemoryHealthLog.id.desc(),
                )
                .limit(1)
            )
            inventory_point = (
                {
                    "observed_at": assume_utc(inventory_row.created_at).isoformat(),
                    **(inventory_row.details or {}),
                }
                if inventory_row is not None
                else latest
            )
        top_consumers = list((inventory_point or {}).get("top_consumers") or [])
        scan_errors = list(
            (inventory_point or {}).get("inventory_scan_errors") or []
        )
        inventory_observed_at = (inventory_point or {}).get("observed_at")
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
    "HOST_CAPACITY_RECLAMATION_DISABLED_ALERT_KEY",
    "HOST_CAPACITY_CHECK_TYPE",
    "HostDiskCapacity",
    "HostCapacityMeasurement",
    "async_measure_host_capacities",
    "async_measure_host_disk_capacity",
    "async_measure_host_capacity",
    "async_read_host_capacity",
    "measure_host_capacities",
    "measure_host_capacity",
    "measure_host_disk_capacity",
    "record_host_capacity",
]
