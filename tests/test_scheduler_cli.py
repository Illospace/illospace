"""Scheduler CLI output tests."""
from __future__ import annotations

from brain.app.cli.scheduler import _emit, build_parser


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
