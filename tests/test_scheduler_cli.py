"""Scheduler CLI output tests."""
from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

from brain.app.cli import scheduler
from brain.app.cli.scheduler import _emit, build_parser
from brain.app.scheduler.read_models import SchedulerOverdueCandidate


REPO_ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)


class _FakeUnitOfWork:
    session = object()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return None


def test_compact_tick_emission_is_one_bounded_line(capsys):
    _emit(
        {
            "tick": 42,
            "ok": True,
            "owner_mode": "scheduler",
            "reclaimed": 0,
            "reclaimed_run_ids": [],
            "drain": {"ok": True, "executed": 0, "results": []},
        },
        compact=True,
    )

    output = capsys.readouterr().out
    assert output.count("\n") == 1
    assert len(output) < 250
    assert '"snapshot"' not in output


def test_daemon_cold_start_threshold_is_operator_configurable():
    args = build_parser().parse_args(
        ["daemon", "--cold-start-gap-threshold-seconds", "900", "--once"]
    )

    assert args.cold_start_gap_threshold_seconds == 900


def test_daemon_hosts_do_not_import_agent_run_maintenance_owners():
    forbidden_modules = {
        "brain.app.scheduler.stale_run_reaper",
        "brain.systems.runs.cortex.runner",
        "brain.systems.runs.deadlines",
    }
    for relative_path in (
        "brain/app/cli/scheduler.py",
        "brain/app/scheduler/daemon.py",
    ):
        tree = ast.parse((REPO_ROOT / relative_path).read_text())
        imported_modules = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        imported_modules.update(
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module is not None
        )

        assert forbidden_modules.isdisjoint(imported_modules), relative_path


async def test_healthcheck_exits_nonzero_for_job_at_freeze_threshold(
    monkeypatch,
    capsys,
):
    async def candidates(_session, *, now):
        assert now == NOW
        return (
            SchedulerOverdueCandidate(
                job_key="frozen-job",
                next_run_at=now - timedelta(minutes=10),
            ),
            SchedulerOverdueCandidate(
                job_key="busy-but-alive-job",
                next_run_at=now - timedelta(minutes=9, seconds=59),
            ),
        )

    monkeypatch.setattr(scheduler, "UnitOfWork", _FakeUnitOfWork)
    monkeypatch.setattr(scheduler, "async_scheduler_overdue_candidates", candidates)
    monkeypatch.delenv("SCHEDULER_SELF_HEAL_AFTER_MINUTES", raising=False)
    args = build_parser().parse_args(["healthcheck", "--now", NOW.isoformat()])

    result = await scheduler.cmd_healthcheck(args)

    payload = json.loads(capsys.readouterr().out)
    assert result == 1
    assert payload["ok"] is False
    assert payload["threshold_seconds"] == 600
    assert payload["frozen_jobs"] == [
        {
            "job_key": "frozen-job",
            "lag_seconds": 600,
            "next_run_at": (NOW - timedelta(minutes=10)).isoformat(),
        }
    ]


async def test_healthcheck_is_healthy_when_no_job_reaches_threshold(
    monkeypatch,
    capsys,
):
    async def candidates(_session, *, now):
        return (
            SchedulerOverdueCandidate(
                job_key="recently-overdue-job",
                next_run_at=now - timedelta(minutes=4),
            ),
        )

    monkeypatch.setattr(scheduler, "UnitOfWork", _FakeUnitOfWork)
    monkeypatch.setattr(scheduler, "async_scheduler_overdue_candidates", candidates)
    monkeypatch.setenv("SCHEDULER_SELF_HEAL_AFTER_MINUTES", "5")
    args = build_parser().parse_args(["healthcheck", "--now", NOW.isoformat()])

    result = await scheduler.cmd_healthcheck(args)

    payload = json.loads(capsys.readouterr().out)
    assert result == 0
    assert payload["ok"] is True
    assert payload["threshold_seconds"] == 300
    assert payload["frozen_jobs"] == []
