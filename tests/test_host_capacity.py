"""Scheduled host-capacity measurement, alert, and tool tests."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select

from brain.platform.db.models.memory_health import MemoryHealthLog
from brain.platform.db.models.scheduler import SchedulerAlertLatch
from brain.platform.db.models.storage_policy import StoragePolicy
from brain.systems.host_capacity import (
    HOST_CAPACITY_ALERT_KEY,
    HOST_CAPACITY_CHECK_TYPE,
    HostCapacityMeasurement,
    async_read_host_capacity,
    measure_host_capacity,
    record_host_capacity,
)
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


def _measurement(*, pct_used: float) -> HostCapacityMeasurement:
    return HostCapacityMeasurement(
        mount="/srv/illo-workspace",
        pct_used=pct_used,
        bytes_free=49_000_000_000,
        bytes_total=1_800_000_000_000,
        top_consumers=(
            {"path": "ideas", "bytes_used": 700_000_000_000},
            {"path": "uploads", "bytes_used": 300_000_000_000},
        ),
    )


async def _add_policy(session, *, warn: int, critical: int) -> None:
    session.add(
        StoragePolicy(
            finished_workspace_retention_hours=24,
            project_draft_retention_hours=48,
            canvas_quiet_hours=12,
            capacity_warn_percent=warn,
            capacity_critical_percent=critical,
            automatic_reclamation_allowed=False,
            rationale="Capacity test policy",
            source_type="test",
            is_active=True,
        )
    )
    await session.flush()


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


async def test_record_host_capacity_uses_policy_thresholds_and_persists_row_shape(
    session,
    monkeypatch,
):
    async def fake_measure(_workspace_root):
        return _measurement(pct_used=74.2)

    monkeypatch.setattr(
        "brain.systems.host_capacity.async_measure_host_capacity",
        fake_measure,
    )
    await _add_policy(session, warn=73, critical=91)

    result = await record_host_capacity(
        session,
        workspace_root="/srv/illo-workspace",
        deliver_alert=None,
    )
    row = await session.scalar(select(MemoryHealthLog))

    assert result["status"] == "warn"
    assert result["thresholds"] == {
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
    }


async def test_host_capacity_warn_alert_latch_fires_exactly_once_across_two_over_threshold_ticks(
    session,
    monkeypatch,
):
    async def fake_measure(_workspace_root):
        return _measurement(pct_used=88.0)

    deliveries = []

    async def fake_deliver(**kwargs):
        deliveries.append(kwargs)

    monkeypatch.setattr(
        "brain.systems.host_capacity.async_measure_host_capacity",
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

    latch = await session.get(SchedulerAlertLatch, HOST_CAPACITY_ALERT_KEY)
    row_count = await session.scalar(
        select(func.count()).select_from(MemoryHealthLog)
    )

    assert first["alert_sent"] is True
    assert second["alert_sent"] is False
    assert len(deliveries) == 1
    assert deliveries[0]["policy"].channel == "#alerts"
    assert latch is not None
    assert latch.alerted_at == datetime(2026, 8, 9, 12, 0)
    assert row_count == 2


async def test_read_host_capacity_returns_live_inventory_and_saved_trend(
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

    async def fake_measure(_workspace_root):
        return _measurement(pct_used=75.5)

    monkeypatch.setattr(
        "brain.systems.host_capacity.async_measure_host_capacity",
        fake_measure,
    )

    result = await async_read_host_capacity(
        session,
        workspace_root="/srv/illo-workspace",
        limit=2,
    )

    assert result["current"] == {
        **_measurement(pct_used=75.5).details(),
        "bytes_total": 1_800_000_000_000,
        "status": "warn",
    }
    assert result["thresholds"] == {
        "warn_percent": 73,
        "critical_percent": 91,
    }
    assert [point["pct_used"] for point in result["trend"]] == [74.0, 62.0]


async def test_read_host_capacity_tool_is_registered_for_illo(monkeypatch):
    from brain.systems.runs.tool_catalog.registry import get_tool_registration
    from brain.systems.runs.tool_definitions import COORDINATOR_TOOLS, WORKER_TOOLS
    from brain.systems.runs.tool_handlers import _get_tool_handlers

    names = lambda tools: {tool["name"] for tool in tools}
    registration = get_tool_registration("read_host_capacity")

    assert "read_host_capacity" in names(COORDINATOR_TOOLS)
    assert "read_host_capacity" in names(WORKER_TOOLS)
    assert registration is not None
    assert registration.permission == "read_runtime"
    assert registration.side_effect_class == "read_only"
    assert registration.context_route is not None
    assert "disk capacity" in registration.context_route.domains

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
