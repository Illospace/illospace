"""Scheduled host-capacity measurement, alert, and tool tests."""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import func, select

from brain.platform.db.models.memory_health import MemoryHealthLog
from brain.platform.db.models.scheduler import SchedulerAlertLatch
from brain.platform.db.models.storage_policy import StoragePolicy
from brain.systems.host_capacity import (
    HOST_CAPACITY_ALERT_KEY,
    HOST_CAPACITY_CRITICAL_ALERT_KEY,
    HOST_CAPACITY_RECLAMATION_DISABLED_ALERT_KEY,
    HOST_CAPACITY_CHECK_TYPE,
    HostCapacityMeasurement,
    HostDiskCapacity,
    _measure_host_capacities,
    async_read_host_capacity,
    measure_host_capacity,
    record_host_capacity,
)
from brain.systems.workspace_inventory import inventory_workspace
from tests.scheduler_test_support import _patch_sqlite_for_pg_types


pytestmark = pytest.mark.asyncio


@pytest.fixture
async def session(async_sqlite_session_factory):
    _patch_sqlite_for_pg_types()
    sqlite3.register_adapter(dict, json.dumps)
    return await async_sqlite_session_factory(
        [
            MemoryHealthLog.__table__,
            SchedulerAlertLatch.__table__,
            StoragePolicy.__table__,
        ]
    )


def _measurement(
    *,
    pct_used: float,
    mount: str = "/srv/illo-workspace",
    include_inventory: bool = True,
) -> HostCapacityMeasurement:
    return HostCapacityMeasurement(
        mount=mount,
        pct_used=pct_used,
        bytes_free=49_000_000_000,
        bytes_total=1_800_000_000_000,
        top_consumers=(
            (
                {"path": "ideas", "bytes_used": 700_000_000_000},
                {"path": "uploads", "bytes_used": 300_000_000_000},
            )
            if include_inventory
            else ()
        ),
    )


def _latch_key(base_key: str, mount: str = "/srv/illo-workspace") -> str:
    return f"{base_key}:{mount}"


def _only_mount(result):
    assert len(result["mounts"]) == 1
    return result["mounts"][0]


async def _add_policy(
    session,
    *,
    warn: int,
    critical: int,
    automatic_reclamation_allowed: bool = True,
) -> None:
    session.add(
        StoragePolicy(
            finished_workspace_retention_hours=24,
            project_draft_retention_hours=48,
            canvas_quiet_hours=12,
            capacity_warn_percent=warn,
            capacity_critical_percent=critical,
            automatic_reclamation_allowed=automatic_reclamation_allowed,
            rationale="Capacity test policy",
            source_type="test",
            is_active=True,
        )
    )
    await session.flush()


def _patch_capacity_sequence(monkeypatch, *pct_used: float) -> None:
    measurements = iter(pct_used)

    async def fake_measure(_workspace_root, _host_root):
        return (_measurement(pct_used=next(measurements)),)

    monkeypatch.setattr(
        "brain.systems.host_capacity._async_measure_host_capacities",
        fake_measure,
    )


async def test_measure_host_capacity_inventories_largest_workspace_consumers(
    tmp_path,
):
    ideas = tmp_path / "ideas"
    uploads = tmp_path / "uploads"
    ideas.mkdir()
    uploads.mkdir()
    (ideas / "large.bin").write_bytes(b"x" * 12)
    (uploads / "small.bin").write_bytes(b"x" * 4)

    measurement = measure_host_capacity(tmp_path, top_consumer_limit=2)

    assert measurement.bytes_total > measurement.bytes_free
    assert measurement.top_consumers == (
        {"path": "ideas", "bytes_used": 12},
        {"path": "uploads", "bytes_used": 4},
    )
    assert measurement.inventory_scan_errors == ()


async def test_workspace_inventory_returns_structured_scan_errors(
    tmp_path,
    monkeypatch,
):
    workspace_root = tmp_path / "workspaces"
    accessible = workspace_root / "accessible"
    blocked = workspace_root / "blocked"
    workspace_root.mkdir()
    accessible.mkdir()
    blocked.mkdir()
    (accessible / "kept.bin").write_bytes(b"kept")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "not-counted.bin").write_bytes(b"outside")
    (workspace_root / "outside-link").symlink_to(outside, target_is_directory=True)
    real_scandir = os.scandir

    def fake_scandir(path):
        if Path(path) == blocked:
            raise PermissionError("blocked for test")
        return real_scandir(path)

    monkeypatch.setattr("brain.systems.workspace_inventory.os.scandir", fake_scandir)

    inventory = inventory_workspace(workspace_root)

    assert inventory.complete is False
    assert inventory.bytes_used == 4
    assert inventory.scan_errors == (
        {
            "path": str(blocked),
            "operation": "scan",
            "message": "blocked for test",
        },
    )


async def test_record_host_capacity_uses_policy_thresholds_and_persists_row_shape(
    session,
    monkeypatch,
):
    async def fake_measure(_workspace_root, _host_root):
        return (_measurement(pct_used=74.2),)

    monkeypatch.setattr(
        "brain.systems.host_capacity._async_measure_host_capacities",
        fake_measure,
    )
    await _add_policy(session, warn=73, critical=91)

    result = await record_host_capacity(
        session,
        workspace_root="/srv/illo-workspace",
        deliver_alert=None,
    )
    row = await session.scalar(select(MemoryHealthLog))

    mount_result = _only_mount(result)
    assert mount_result["status"] == "warn"
    assert mount_result["thresholds"] == {
        "warn_percent": 73,
        "critical_percent": 91,
    }
    assert row is not None
    assert row.check_type == HOST_CAPACITY_CHECK_TYPE
    assert row.status == "warn"
    assert row.details == {
        "mount": "/srv/illo-workspace",
        "pct_used": 74.2,
        "bytes_free": 49_000_000_000,
        "top_consumers": [
            {"path": "ideas", "bytes_used": 700_000_000_000},
            {"path": "uploads", "bytes_used": 300_000_000_000},
        ],
        "inventory_scan_errors": [],
    }


async def test_two_distinct_mounts_each_produce_a_capacity_row(
    session,
    monkeypatch,
):
    async def fake_measure(_workspace_root, _host_root):
        return (
            _measurement(pct_used=61.0),
            _measurement(pct_used=42.0, mount="/", include_inventory=False),
        )

    monkeypatch.setattr(
        "brain.systems.host_capacity._async_measure_host_capacities",
        fake_measure,
    )
    await _add_policy(session, warn=70, critical=90)

    result = await record_host_capacity(
        session,
        workspace_root="/srv/illo-workspace",
        deliver_alert=None,
    )
    rows = list(await session.scalars(select(MemoryHealthLog)))

    assert {row.details["mount"] for row in rows} == {
        "/",
        "/srv/illo-workspace",
    }
    assert len(rows) == 2
    assert set(result) == {"mounts"}
    assert {mount["details"]["mount"] for mount in result["mounts"]} == {
        "/",
        "/srv/illo-workspace",
    }
    root_row = next(row for row in rows if row.details["mount"] == "/")
    assert root_row.details["top_consumers"] == []
    assert root_row.details["inventory_scan_errors"] == []


async def test_paths_on_same_mount_produce_one_row_and_one_alert(
    session,
    monkeypatch,
):
    shared = _measurement(pct_used=80.0, mount="/shared")

    def fake_workspace_measurement(_workspace_root, *, top_consumer_limit=10):
        assert top_consumer_limit == 10
        return shared

    def fake_disk_measurement(_path):
        return HostDiskCapacity(
            mount="/shared",
            pct_used=80.0,
            bytes_free=shared.bytes_free,
            bytes_total=shared.bytes_total,
        )

    monkeypatch.setattr(
        "brain.systems.host_capacity.measure_host_capacity",
        fake_workspace_measurement,
    )
    monkeypatch.setattr(
        "brain.systems.host_capacity.measure_host_disk_capacity",
        fake_disk_measurement,
    )
    await _add_policy(session, warn=70, critical=90)
    deliveries = []

    async def fake_deliver(**kwargs):
        deliveries.append(kwargs)

    result = await record_host_capacity(
        session,
        workspace_root="/workspaces",
        deliver_alert=fake_deliver,
    )
    row_count = await session.scalar(
        select(func.count()).select_from(MemoryHealthLog)
    )

    assert len(_measure_host_capacities("/workspaces", "/")) == 1
    assert row_count == 1
    assert len(result["mounts"]) == 1
    assert len(deliveries) == 1
    assert deliveries[0]["subject"].identity == "/shared"


async def test_critical_mount_alerts_while_other_mount_warn_is_latched(
    session,
    monkeypatch,
):
    ticks = iter(
        (
            (
                _measurement(pct_used=80.0),
                _measurement(pct_used=60.0, mount="/", include_inventory=False),
            ),
            (
                _measurement(pct_used=80.0),
                _measurement(pct_used=95.0, mount="/", include_inventory=False),
            ),
        )
    )

    async def fake_measure(_workspace_root, _host_root):
        return next(ticks)

    monkeypatch.setattr(
        "brain.systems.host_capacity._async_measure_host_capacities",
        fake_measure,
    )
    await _add_policy(session, warn=70, critical=90)
    deliveries = []

    async def fake_deliver(**kwargs):
        deliveries.append(kwargs)

    await record_host_capacity(
        session,
        workspace_root="/srv/illo-workspace",
        deliver_alert=fake_deliver,
    )
    second = await record_host_capacity(
        session,
        workspace_root="/srv/illo-workspace",
        deliver_alert=fake_deliver,
    )

    assert [delivery["presentation"].title for delivery in deliveries] == [
        "Host capacity warning",
        "Host capacity critical",
    ]
    assert [delivery["subject"].identity for delivery in deliveries] == [
        "/srv/illo-workspace",
        "/",
    ]
    second_by_mount = {
        mount["details"]["mount"]: mount for mount in second["mounts"]
    }
    assert second_by_mount["/srv/illo-workspace"]["alert_sent"] is False
    assert second_by_mount["/"]["alert_sent"] is True
    assert await session.get(
        SchedulerAlertLatch,
        _latch_key(HOST_CAPACITY_ALERT_KEY),
    )
    assert await session.get(
        SchedulerAlertLatch,
        _latch_key(HOST_CAPACITY_CRITICAL_ALERT_KEY, "/"),
    )


async def test_root_exhaustion_alerts_when_workspace_is_at_95_9_percent(
    session,
    monkeypatch,
):
    async def fake_measure(_workspace_root, _host_root):
        return (
            _measurement(pct_used=95.9),
            _measurement(pct_used=100.0, mount="/", include_inventory=False),
        )

    monkeypatch.setattr(
        "brain.systems.host_capacity._async_measure_host_capacities",
        fake_measure,
    )
    await _add_policy(session, warn=90, critical=99)
    deliveries = []

    async def fake_deliver(**kwargs):
        deliveries.append(kwargs)

    await record_host_capacity(
        session,
        workspace_root="/srv/illo-workspace",
        deliver_alert=fake_deliver,
    )
    rows = list(await session.scalars(select(MemoryHealthLog)))
    root_row = next(row for row in rows if row.details["mount"] == "/")

    assert root_row.status == "critical"
    assert root_row.details["pct_used"] == 100.0
    assert any(
        delivery["subject"].identity == "/"
        and delivery["presentation"].title == "Host capacity critical"
        for delivery in deliveries
    )


async def test_host_capacity_warn_alert_latch_fires_exactly_once_across_two_over_threshold_ticks(
    session,
    monkeypatch,
):
    async def fake_measure(_workspace_root, _host_root):
        return (_measurement(pct_used=88.0),)

    deliveries = []

    async def fake_deliver(**kwargs):
        deliveries.append(kwargs)

    monkeypatch.setattr(
        "brain.systems.host_capacity._async_measure_host_capacities",
        fake_measure,
    )
    await _add_policy(session, warn=70, critical=90)

    first = await record_host_capacity(
        session,
        workspace_root="/srv/illo-workspace",
        now=datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc),
        deliver_alert=fake_deliver,
    )
    second = await record_host_capacity(
        session,
        workspace_root="/srv/illo-workspace",
        now=datetime(2026, 8, 9, 13, 0, tzinfo=timezone.utc),
        deliver_alert=fake_deliver,
    )

    latch = await session.get(
        SchedulerAlertLatch,
        _latch_key(HOST_CAPACITY_ALERT_KEY),
    )
    row_count = await session.scalar(
        select(func.count()).select_from(MemoryHealthLog)
    )

    assert _only_mount(first)["alert_sent"] is True
    assert _only_mount(second)["alert_sent"] is False
    assert len(deliveries) == 1
    assert deliveries[0]["policy"].channel == "#alerts"
    assert latch is not None
    assert latch.alerted_at == datetime(2026, 8, 9, 12, 0)
    assert row_count == 2


@pytest.mark.parametrize("pct_used", [80.0, 95.0])
async def test_unhealthy_capacity_with_reclamation_disabled_alerts_once_and_latches(
    session,
    monkeypatch,
    pct_used,
):
    _patch_capacity_sequence(monkeypatch, 60.0, pct_used, pct_used)
    await _add_policy(
        session,
        warn=70,
        critical=90,
        automatic_reclamation_allowed=False,
    )
    deliveries = []

    async def fake_deliver(**kwargs):
        deliveries.append(kwargs)

    ok = await record_host_capacity(
        session,
        workspace_root="/srv/illo-workspace",
        now=datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc),
        deliver_alert=fake_deliver,
    )
    first_unhealthy = await record_host_capacity(
        session,
        workspace_root="/srv/illo-workspace",
        now=datetime(2026, 9, 1, 11, 0, tzinfo=timezone.utc),
        deliver_alert=fake_deliver,
    )
    second_unhealthy = await record_host_capacity(
        session,
        workspace_root="/srv/illo-workspace",
        now=datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc),
        deliver_alert=fake_deliver,
    )

    latch = await session.get(
        SchedulerAlertLatch,
        _latch_key(HOST_CAPACITY_RECLAMATION_DISABLED_ALERT_KEY),
    )
    assert _only_mount(ok)["alert_sent"] is False
    assert _only_mount(first_unhealthy)["alert_sent"] is True
    assert _only_mount(second_unhealthy)["alert_sent"] is False
    assert len(deliveries) == 1
    assert deliveries[0]["presentation"].title == (
        "Host capacity risk: automatic reclamation disabled"
    )
    assert latch is not None
    assert latch.alerted_at == datetime(2026, 9, 1, 11, 0)


async def test_host_capacity_warn_to_critical_delivers_one_critical_alert(
    session,
    monkeypatch,
):
    _patch_capacity_sequence(monkeypatch, 80.0, 95.0)
    await _add_policy(session, warn=70, critical=90)
    deliveries = []

    async def fake_deliver(**kwargs):
        deliveries.append(kwargs)

    await record_host_capacity(
        session,
        workspace_root="/srv/illo-workspace",
        now=datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc),
        deliver_alert=fake_deliver,
    )
    deliveries.clear()

    result = await record_host_capacity(
        session,
        workspace_root="/srv/illo-workspace",
        now=datetime(2026, 9, 1, 11, 0, tzinfo=timezone.utc),
        deliver_alert=fake_deliver,
    )

    critical_latch = await session.get(
        SchedulerAlertLatch,
        _latch_key(HOST_CAPACITY_CRITICAL_ALERT_KEY),
    )
    assert _only_mount(result)["alert_sent"] is True
    assert len(deliveries) == 1
    assert deliveries[0]["presentation"].title == "Host capacity critical"
    assert critical_latch is not None


async def test_host_capacity_cold_critical_delivers_only_highest_severity_and_latches_both_edges(
    session,
    monkeypatch,
):
    _patch_capacity_sequence(monkeypatch, 95.0)
    await _add_policy(session, warn=70, critical=90)
    deliveries = []

    async def fake_deliver(**kwargs):
        deliveries.append(kwargs)

    await record_host_capacity(
        session,
        workspace_root="/srv/illo-workspace",
        now=datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc),
        deliver_alert=fake_deliver,
    )

    latch_keys = set(
        await session.scalars(
            select(SchedulerAlertLatch.alert_key).where(
                SchedulerAlertLatch.alert_key.in_(
                    (
                        _latch_key(HOST_CAPACITY_ALERT_KEY),
                        _latch_key(HOST_CAPACITY_CRITICAL_ALERT_KEY),
                    )
                )
            )
        )
    )
    assert [item["presentation"].title for item in deliveries] == [
        "Host capacity critical",
    ]
    assert deliveries[0]["error_text"] == (
        "Storage use crossed the active policy critical threshold of 90%."
    )
    assert latch_keys == {
        _latch_key(HOST_CAPACITY_ALERT_KEY),
        _latch_key(HOST_CAPACITY_CRITICAL_ALERT_KEY),
    }


async def test_host_capacity_second_consecutive_critical_tick_does_not_alert(
    session,
    monkeypatch,
):
    _patch_capacity_sequence(monkeypatch, 95.0, 95.0)
    await _add_policy(session, warn=70, critical=90)
    deliveries = []

    async def fake_deliver(**kwargs):
        deliveries.append(kwargs)

    await record_host_capacity(
        session,
        workspace_root="/srv/illo-workspace",
        now=datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc),
        deliver_alert=fake_deliver,
    )

    result = await record_host_capacity(
        session,
        workspace_root="/srv/illo-workspace",
        now=datetime(2026, 9, 1, 11, 0, tzinfo=timezone.utc),
        deliver_alert=fake_deliver,
    )

    assert _only_mount(result)["alert_sent"] is False
    assert [item["presentation"].title for item in deliveries] == [
        "Host capacity critical",
    ]


async def test_host_capacity_ok_clears_both_latches_and_rearms_warning(
    session,
    monkeypatch,
):
    _patch_capacity_sequence(monkeypatch, 80.0, 95.0, 60.0, 80.0)
    await _add_policy(session, warn=70, critical=90)
    deliveries = []

    async def fake_deliver(**kwargs):
        deliveries.append(kwargs)

    for hour in (10, 11):
        await record_host_capacity(
            session,
            workspace_root="/srv/illo-workspace",
            now=datetime(2026, 9, 1, hour, 0, tzinfo=timezone.utc),
            deliver_alert=fake_deliver,
        )

    assert await session.get(
        SchedulerAlertLatch,
        _latch_key(HOST_CAPACITY_ALERT_KEY),
    )
    assert await session.get(
        SchedulerAlertLatch,
        _latch_key(HOST_CAPACITY_CRITICAL_ALERT_KEY),
    )

    ok_result = await record_host_capacity(
        session,
        workspace_root="/srv/illo-workspace",
        now=datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc),
        deliver_alert=fake_deliver,
    )

    assert _only_mount(ok_result)["alert_sent"] is False
    assert (
        await session.get(
            SchedulerAlertLatch,
            _latch_key(HOST_CAPACITY_ALERT_KEY),
        )
        is None
    )
    assert (
        await session.get(
            SchedulerAlertLatch,
            _latch_key(HOST_CAPACITY_CRITICAL_ALERT_KEY),
        )
        is None
    )

    warning_result = await record_host_capacity(
        session,
        workspace_root="/srv/illo-workspace",
        now=datetime(2026, 9, 1, 13, 0, tzinfo=timezone.utc),
        deliver_alert=fake_deliver,
    )

    assert _only_mount(warning_result)["alert_sent"] is True
    assert [item["presentation"].title for item in deliveries] == [
        "Host capacity warning",
        "Host capacity critical",
        "Host capacity warning",
    ]


async def test_host_capacity_critical_rearms_only_after_falling_to_warning(
    session,
    monkeypatch,
):
    _patch_capacity_sequence(monkeypatch, 80.0, 95.0, 95.0, 80.0, 95.0)
    await _add_policy(session, warn=70, critical=90)
    deliveries = []

    async def fake_deliver(**kwargs):
        deliveries.append(kwargs)

    for hour in (10, 11):
        await record_host_capacity(
            session,
            workspace_root="/srv/illo-workspace",
            now=datetime(2026, 9, 1, hour, 0, tzinfo=timezone.utc),
            deliver_alert=fake_deliver,
        )
    critical_latch = await session.get(
        SchedulerAlertLatch,
        _latch_key(HOST_CAPACITY_CRITICAL_ALERT_KEY),
    )
    assert critical_latch is not None

    consecutive_critical = await record_host_capacity(
        session,
        workspace_root="/srv/illo-workspace",
        now=datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc),
        deliver_alert=fake_deliver,
    )
    assert _only_mount(consecutive_critical)["alert_sent"] is False
    assert (
        await session.get(
            SchedulerAlertLatch,
            _latch_key(HOST_CAPACITY_CRITICAL_ALERT_KEY),
        )
    ).alerted_at == critical_latch.alerted_at

    warning_result = await record_host_capacity(
        session,
        workspace_root="/srv/illo-workspace",
        now=datetime(2026, 9, 1, 13, 0, tzinfo=timezone.utc),
        deliver_alert=fake_deliver,
    )
    assert _only_mount(warning_result)["alert_sent"] is False
    assert await session.get(
        SchedulerAlertLatch,
        _latch_key(HOST_CAPACITY_ALERT_KEY),
    )
    assert (
        await session.get(
            SchedulerAlertLatch,
            _latch_key(HOST_CAPACITY_CRITICAL_ALERT_KEY),
        )
        is None
    )

    critical_again = await record_host_capacity(
        session,
        workspace_root="/srv/illo-workspace",
        now=datetime(2026, 9, 1, 14, 0, tzinfo=timezone.utc),
        deliver_alert=fake_deliver,
    )

    assert _only_mount(critical_again)["alert_sent"] is True
    assert [item["presentation"].title for item in deliveries] == [
        "Host capacity warning",
        "Host capacity critical",
        "Host capacity critical",
    ]


async def test_read_host_capacity_returns_live_disk_and_persisted_inventory(
    session,
    monkeypatch,
):
    await _add_policy(session, warn=73, critical=91)
    session.add_all(
        [
            MemoryHealthLog(
                check_type=HOST_CAPACITY_CHECK_TYPE,
                status="ok",
                details={**_measurement(pct_used=62.0).details()},
                created_at=datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc),
            ),
            MemoryHealthLog(
                check_type=HOST_CAPACITY_CHECK_TYPE,
                status="warn",
                details={**_measurement(pct_used=74.0).details()},
                created_at=datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc),
            ),
        ]
    )
    await session.flush()

    async def fail_if_inventory_walks(_workspace_root):
        raise AssertionError("default read must not walk the workspace")

    async def fake_disk(_workspace_root):
        return HostDiskCapacity(
            mount="/srv/illo-workspace",
            pct_used=75.5,
            bytes_free=48_000_000_000,
            bytes_total=1_800_000_000_000,
        )

    monkeypatch.setattr(
        "brain.systems.host_capacity.async_measure_host_capacity",
        fail_if_inventory_walks,
    )
    monkeypatch.setattr(
        "brain.systems.host_capacity.async_measure_host_disk_capacity",
        fake_disk,
    )

    result = await async_read_host_capacity(
        session,
        workspace_root="/srv/illo-workspace",
        limit=2,
    )

    assert result["current"] == {
        "mount": "/srv/illo-workspace",
        "pct_used": 75.5,
        "bytes_free": 48_000_000_000,
        "bytes_total": 1_800_000_000_000,
        "status": "warn",
        "top_consumers": [
            {"path": "ideas", "bytes_used": 700_000_000_000},
            {"path": "uploads", "bytes_used": 300_000_000_000},
        ],
        "inventory_scan_errors": [],
        "inventory_observed_at": "2026-08-09T12:00:00+00:00",
    }
    assert result["thresholds"] == {
        "warn_percent": 73,
        "critical_percent": 91,
    }
    assert [point["pct_used"] for point in result["trend"]] == [74.0, 62.0]


async def test_read_host_capacity_filters_root_row_from_workspace_trend(
    session,
    monkeypatch,
):
    await _add_policy(session, warn=73, critical=91)
    session.add_all(
        [
            MemoryHealthLog(
                check_type=HOST_CAPACITY_CHECK_TYPE,
                status="warn",
                details=_measurement(pct_used=75.0).details(),
                created_at=datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc),
            ),
            MemoryHealthLog(
                check_type=HOST_CAPACITY_CHECK_TYPE,
                status="critical",
                details=_measurement(
                    pct_used=100.0,
                    mount="/",
                    include_inventory=False,
                ).details(),
                created_at=datetime(2026, 9, 1, 13, 0, tzinfo=timezone.utc),
            ),
        ]
    )
    await session.flush()

    async def fake_disk(_workspace_root):
        return HostDiskCapacity(
            mount="/srv/illo-workspace",
            pct_used=75.5,
            bytes_free=48_000_000_000,
            bytes_total=1_800_000_000_000,
        )

    monkeypatch.setattr(
        "brain.systems.host_capacity.async_measure_host_disk_capacity",
        fake_disk,
    )

    result = await async_read_host_capacity(
        session,
        workspace_root="/srv/illo-workspace",
        limit=1,
    )

    assert [point["mount"] for point in result["trend"]] == [
        "/srv/illo-workspace"
    ]
    assert result["current"]["top_consumers"] == list(
        _measurement(pct_used=75.0).top_consumers
    )
    assert result["current"]["inventory_observed_at"] == (
        "2026-09-01T12:00:00+00:00"
    )


async def test_read_host_capacity_refreshes_inventory_only_when_requested(
    session,
    monkeypatch,
):
    await _add_policy(session, warn=73, critical=91)
    refreshed = _measurement(pct_used=75.5)
    calls = 0

    async def fake_measure(_workspace_root):
        nonlocal calls
        calls += 1
        return refreshed

    async def fail_if_disk_is_measured_separately(_workspace_root):
        raise AssertionError("refresh should reuse the full measurement")

    monkeypatch.setattr(
        "brain.systems.host_capacity.async_measure_host_capacity",
        fake_measure,
    )
    monkeypatch.setattr(
        "brain.systems.host_capacity.async_measure_host_disk_capacity",
        fail_if_disk_is_measured_separately,
    )

    result = await async_read_host_capacity(
        session,
        workspace_root="/srv/illo-workspace",
        refresh_inventory=True,
    )

    assert calls == 1
    assert result["current"]["top_consumers"] == list(refreshed.top_consumers)
    assert result["current"]["inventory_observed_at"].endswith("+00:00")


async def test_read_host_capacity_tool_is_registered_for_illo(monkeypatch):
    from brain.systems.runs.tool_catalog.registry import get_tool_registration
    from brain.systems.runs.tool_definitions import COORDINATOR_TOOLS, WORKER_TOOLS
    from brain.systems.runs.tool_handlers import _get_tool_handlers

    names = lambda tools: {tool["name"] for tool in tools}
    definition = next(
        tool for tool in COORDINATOR_TOOLS if tool["name"] == "read_host_capacity"
    )
    registration = get_tool_registration("read_host_capacity")

    assert "read_host_capacity" in names(COORDINATOR_TOOLS)
    assert "read_host_capacity" in names(WORKER_TOOLS)
    assert registration is not None
    assert registration.permission == "read_runtime"
    assert registration.side_effect_class == "read_only"
    assert registration.context_route is not None
    assert "disk capacity" in registration.context_route.domains
    assert (
        definition["input_schema"]["properties"]["refresh_inventory"]["default"]
        is False
    )

    captured = {}

    class FakeUnitOfWork:
        session = object()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    async def fake_read(session, **kwargs):
        captured.update(session=session, **kwargs)
        return {"current": {"pct_used": 75.5}, "trend": []}

    monkeypatch.setattr(
        "brain.platform.db.repositories.unit_of_work.UnitOfWork",
        FakeUnitOfWork,
    )
    monkeypatch.setattr(
        "brain.systems.host_capacity.async_read_host_capacity",
        fake_read,
    )

    result = await _get_tool_handlers()["read_host_capacity"](limit=12)

    assert result == {"current": {"pct_used": 75.5}, "trend": []}
    assert captured["limit"] == 12
    assert captured["refresh_inventory"] is False
