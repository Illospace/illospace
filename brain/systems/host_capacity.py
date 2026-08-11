"""Measure host storage, persist its trend, and guard the warning edge."""
from __future__ import annotations

import logging
import os
import shutil
import stat
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


logger = logging.getLogger(__name__)

HOST_CAPACITY_CHECK_TYPE = "host_capacity"
HOST_CAPACITY_ALERT_KEY = "host_capacity_warn"
_CAPACITY_WARN_TRIGGER_KIND = FailureGuardTriggerKind(HOST_CAPACITY_ALERT_KEY)


class TopConsumer(TypedDict):
    path: str
    bytes_used: int


class HostCapacityDetails(TypedDict):
    mount: str
    pct_used: float
    bytes_free: int
    top_consumers: list[TopConsumer]


@dataclass(frozen=True, slots=True)
class HostCapacityMeasurement:
    """One live filesystem and workspace-volume measurement."""

    mount: str
    pct_used: float
    bytes_free: int
    bytes_total: int
    top_consumers: tuple[TopConsumer, ...]

    def details(self) -> HostCapacityDetails:
        """Return the exact durable memory-health detail shape."""
        return {
            "mount": self.mount,
            "pct_used": self.pct_used,
            "bytes_free": self.bytes_free,
            "top_consumers": [dict(consumer) for consumer in self.top_consumers],
        }


@dataclass(slots=True)
class _HostCapacityLatchStore:
    """Expose the scheduler-global capacity latch to the neutral guard."""

    session: AsyncSession

    async def load_latches(self):
        latch = await self.session.get(SchedulerAlertLatch, HOST_CAPACITY_ALERT_KEY)
        return {_CAPACITY_WARN_TRIGGER_KIND: latch} if latch is not None else {}

    async def create_latch(
        self,
        trigger_kind: FailureGuardTriggerKind,
        alerted_at: datetime,
    ) -> SchedulerAlertLatch:
        if trigger_kind != _CAPACITY_WARN_TRIGGER_KIND:
            raise ValueError(f"Unsupported host-capacity trigger: {trigger_kind}")
        latch = SchedulerAlertLatch(
            alert_key=HOST_CAPACITY_ALERT_KEY,
            alerted_at=alerted_at,
        )
        self.session.add(latch)
        await self.session.flush()
        return latch


def _directory_size_bytes(path: Path) -> int:
    """Return regular-file bytes below a path without following symlinks."""
    total = 0
    pending = [path]
    while pending:
        current = pending.pop()
        try:
            entries = os.scandir(current)
        except OSError as exc:
            logger.warning("Host capacity could not scan %s: %s", current, exc)
            continue
        with entries:
            for entry in entries:
                try:
                    entry_stat = entry.stat(follow_symlinks=False)
                except OSError as exc:
                    logger.warning(
                        "Host capacity could not stat %s: %s",
                        entry.path,
                        exc,
                    )
                    continue
                if stat.S_ISREG(entry_stat.st_mode):
                    total += entry_stat.st_size
                elif stat.S_ISDIR(entry_stat.st_mode):
                    pending.append(Path(entry.path))
    return total


def _workspace_top_consumers(
    workspace_root: Path,
    *,
    limit: int,
) -> tuple[TopConsumer, ...]:
    consumers: list[TopConsumer] = []
    for child in workspace_root.iterdir():
        try:
            child_stat = child.stat(follow_symlinks=False)
        except OSError as exc:
            logger.warning("Host capacity could not stat %s: %s", child, exc)
            continue
        if stat.S_ISREG(child_stat.st_mode):
            bytes_used = child_stat.st_size
        elif stat.S_ISDIR(child_stat.st_mode):
            bytes_used = _directory_size_bytes(child)
        else:
            continue
        consumers.append({"path": child.name, "bytes_used": bytes_used})
    consumers.sort(key=lambda item: (-item["bytes_used"], item["path"]))
    return tuple(consumers[:limit])


def measure_host_capacity(
    workspace_root: str | Path,
    *,
    top_consumer_limit: int = 10,
) -> HostCapacityMeasurement:
    """Measure the workspace filesystem and its largest direct consumers."""
    root = Path(workspace_root).expanduser().resolve(strict=True)
    usage = shutil.disk_usage(root)
    pct_used = round((usage.used / usage.total) * 100, 1) if usage.total else 0.0
    mount = root
    while mount.parent != mount and not os.path.ismount(mount):
        mount = mount.parent
    return HostCapacityMeasurement(
        mount=str(mount),
        pct_used=pct_used,
        bytes_free=int(usage.free),
        bytes_total=int(usage.total),
        top_consumers=_workspace_top_consumers(
            root,
            limit=max(1, int(top_consumer_limit)),
        ),
    )


async def async_measure_host_capacity(
    workspace_root: str | Path,
) -> HostCapacityMeasurement:
    """Measure capacity without blocking the caller's event loop."""
    return await run_blocking(measure_host_capacity, workspace_root)


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
        "warn_percent": policy.capacity_warn_percent,
        "critical_percent": policy.capacity_critical_percent,
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
    if status == "ok":
        await session.execute(
            delete(SchedulerAlertLatch).where(
                SchedulerAlertLatch.alert_key == HOST_CAPACITY_ALERT_KEY
            )
        )

    trigger = FailureGuardTriggerResult(
        kind=_CAPACITY_WARN_TRIGGER_KIND,
        active=status != "ok",
        public_details={
            "pct_used": measurement.pct_used,
            **_thresholds(policy),
        },
        alert_title=(
            "Host capacity critical"
            if status == "critical"
            else "Host capacity warning"
        ),
        alert_summary=(
            f"{measurement.pct_used}% used on {measurement.mount}; "
            f"{measurement.bytes_free} bytes free."
        ),
    )
    guard = await async_evaluate_failure_edges(
        results=(trigger,),
        failure_signature=None,
        last_error=None,
        now=observed_at,
        store=store,
        new_edge_mode="detect",
    )

    alert_sent = False
    if guard.crossed_edges and deliver_alert is not None:
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
                    title=trigger.alert_title,
                    summary=trigger.alert_summary,
                ),
                error_text=(
                    f"Storage use crossed the active policy warning threshold "
                    f"of {policy.capacity_warn_percent}%."
                ),
            )
        except Exception:  # noqa: BLE001 - leave the edge open for the next tick
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
) -> dict[str, Any]:
    """Return live capacity plus recent durable measurements as a trend."""
    normalized_limit = max(1, min(int(limit), 168))
    measurement = await async_measure_host_capacity(workspace_root)
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
    return {
        "current": {
            **measurement.details(),
            "bytes_total": measurement.bytes_total,
            "status": _capacity_status(measurement.pct_used, policy),
        },
        "thresholds": _thresholds(policy),
        "trend": trend,
    }


__all__ = [
    "HOST_CAPACITY_ALERT_KEY",
    "HOST_CAPACITY_CHECK_TYPE",
    "HostCapacityMeasurement",
    "async_measure_host_capacity",
    "async_read_host_capacity",
    "measure_host_capacity",
    "record_host_capacity",
]
