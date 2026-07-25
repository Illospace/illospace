"""Tests for the scheduler-launched AWS health scan pipeline."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from brain.jobs.pipelines import aws_health_scan


async def _spawned_message(monkeypatch, *, now: datetime, last_success: datetime) -> str:
    session = MagicMock()
    session.scalar = AsyncMock(return_value=last_success)
    uow = MagicMock()
    uow.session = session
    uow.skills.get_by_name = AsyncMock(
        return_value=SimpleNamespace(skill_installation_id=52, thinking_tier="low")
    )
    uow.__aenter__ = AsyncMock(return_value=uow)
    uow.__aexit__ = AsyncMock(return_value=False)

    monkeypatch.setattr(aws_health_scan, "UnitOfWork", lambda: uow)
    monkeypatch.setattr(
        aws_health_scan,
        "_skill_actor",
        AsyncMock(return_value=SimpleNamespace(id="user-1", org_id="org-1")),
    )

    admitted_events = []

    async def _admit_work(admission_session, event):
        assert admission_session is session
        admitted_events.append(event)
        return SimpleNamespace(ok=True, run_id=123)

    monkeypatch.setattr(aws_health_scan, "admit_work", _admit_work)

    assert await aws_health_scan.spawn_health_scan_run(now=now) == 123
    session.scalar.assert_awaited_once()
    return admitted_events[0].payload["message"]


@pytest.mark.asyncio
async def test_pipeline_emits_coverage_since_for_stale_last_success(monkeypatch):
    now = datetime(2026, 7, 25, 13, 30, tzinfo=timezone.utc)
    last_success = now - timedelta(minutes=71)

    message = await _spawned_message(monkeypatch, now=now, last_success=last_success)

    assert f"coverage-since: {last_success.isoformat()}" in message


@pytest.mark.asyncio
async def test_pipeline_omits_coverage_since_when_last_success_is_fresh(monkeypatch):
    now = datetime(2026, 7, 25, 13, 30, tzinfo=timezone.utc)
    last_success = now - timedelta(minutes=70)

    message = await _spawned_message(monkeypatch, now=now, last_success=last_success)

    assert "coverage-since:" not in message


@pytest.mark.asyncio
async def test_pipeline_caps_coverage_since_at_six_hours(monkeypatch):
    now = datetime(2026, 7, 25, 13, 30, tzinfo=timezone.utc)

    message = await _spawned_message(
        monkeypatch,
        now=now,
        last_success=now - timedelta(hours=8),
    )

    assert f"coverage-since: {(now - timedelta(hours=6)).isoformat()}" in message


@pytest.mark.asyncio
async def test_pipeline_exits_nonzero_when_headless_run_spawn_fails(monkeypatch, capsys):
    spawn = AsyncMock(side_effect=RuntimeError("admission unavailable"))
    monkeypatch.setattr(aws_health_scan, "spawn_health_scan_run", spawn)

    exit_code = await aws_health_scan.async_main()

    assert exit_code != 0
    spawn.assert_awaited_once_with()
    assert "AWS health scan spawn failed: admission unavailable" in capsys.readouterr().err
