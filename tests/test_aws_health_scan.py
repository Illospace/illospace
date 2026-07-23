"""Tests for the scheduler-launched AWS health scan pipeline."""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from brain.jobs.pipelines import aws_health_scan


@pytest.mark.asyncio
async def test_pipeline_exits_nonzero_when_headless_run_spawn_fails(monkeypatch, capsys):
    spawn = AsyncMock(side_effect=RuntimeError("admission unavailable"))
    monkeypatch.setattr(aws_health_scan, "spawn_health_scan_run", spawn)

    exit_code = await aws_health_scan.async_main()

    assert exit_code != 0
    spawn.assert_awaited_once_with()
    assert "AWS health scan spawn failed: admission unavailable" in capsys.readouterr().err
