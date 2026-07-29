"""Scheduler CLI output tests."""
from __future__ import annotations

import json
from types import SimpleNamespace

import brain.app.cli.scheduler as scheduler_cli
from brain.app.cli.scheduler import _emit, build_parser


class _FakeUnitOfWork:
    session = object()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc_info):
        return False


def _daemon_args(**overrides):
    values = {
        "owner_mode": "scheduler",
        "job_key": None,
        "max_runs": 10,
        "resume": True,
        "once": False,
        "poll_interval_seconds": 15,
        "now": None,
        "cold_start_gap_threshold_seconds": 3600,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


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


async def test_stale_run_reaper_uses_independent_cadence_and_emits_count(monkeypatch, capsys):
    monotonic_times = iter((100.0, 159.9, 160.0))
    calls: list[int] = []

    async def fake_reap_stale_active_runs(*, limit):
        calls.append(limit)
        return len(calls)

    monkeypatch.setattr(scheduler_cli, "_monotonic", lambda: next(monotonic_times))
    monkeypatch.setattr(scheduler_cli, "reap_stale_active_runs", fake_reap_stale_active_runs)

    next_reap_at = 0.0
    next_reap_at = await scheduler_cli._reap_stale_active_runs_if_due(next_reap_at=next_reap_at)
    assert next_reap_at == 160.0

    next_reap_at = await scheduler_cli._reap_stale_active_runs_if_due(next_reap_at=next_reap_at)
    assert next_reap_at == 160.0

    next_reap_at = await scheduler_cli._reap_stale_active_runs_if_due(next_reap_at=next_reap_at)
    assert next_reap_at == 220.0
    assert calls == [25, 25]

    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert events == [
        {"event": "agent_run_stale_reap", "ok": True, "reaped": 1},
        {"event": "agent_run_stale_reap", "ok": True, "reaped": 2},
    ]


async def test_stale_run_reaper_failure_emits_and_daemon_keeps_ticking(monkeypatch, capsys):
    tick_calls = 0
    sleep_calls = 0

    async def fake_startup(*_args, **_kwargs):
        return {"ok": True}

    async def fake_tick(*_args, **_kwargs):
        nonlocal tick_calls
        tick_calls += 1
        return {"ok": True}

    async def fail_reap_stale_active_runs(*, limit):
        assert limit == 25
        raise RuntimeError("reaper unavailable")

    async def stop_after_second_tick(_seconds):
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls == 2:
            raise KeyboardInterrupt

    monotonic_times = iter((100.0, 101.0))
    monkeypatch.setattr(scheduler_cli, "UnitOfWork", _FakeUnitOfWork)
    monkeypatch.setattr(scheduler_cli, "async_scheduler_daemon_startup", fake_startup)
    monkeypatch.setattr(scheduler_cli, "async_scheduler_daemon_tick", fake_tick)
    monkeypatch.setattr(scheduler_cli, "reap_stale_active_runs", fail_reap_stale_active_runs)
    monkeypatch.setattr(scheduler_cli, "_monotonic", lambda: next(monotonic_times))
    monkeypatch.setattr(scheduler_cli.asyncio, "sleep", stop_after_second_tick)

    assert await scheduler_cli.cmd_daemon(_daemon_args()) == 0
    assert tick_calls == 2

    output = capsys.readouterr().out
    assert (
        '{"event":"agent_run_stale_reap_failed","ok":false,"error":"reaper unavailable"}'
        in output
    )
    assert '"stopped": "keyboard_interrupt"' in output
    assert '"ticks": 2' in output
