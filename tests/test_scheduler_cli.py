"""Scheduler CLI output tests."""
from __future__ import annotations

import ast
from pathlib import Path

from brain.app.cli.scheduler import _emit, build_parser


REPO_ROOT = Path(__file__).resolve().parents[1]


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
