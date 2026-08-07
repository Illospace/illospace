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


async def test_scheduler_enforces_agent_run_deadlines_without_a_worker(
    monkeypatch,
    capsys,
):
    calls: list[tuple[str, int]] = []

    async def sweep(_session, *, limit):
        calls.append(("sweep", limit))
        return SimpleNamespace(
            closeout_requested=1,
            expired=1,
            expired_run_ids=(42,),
        )

    async def settle(_session, run_id):
        calls.append(("settle", run_id))
        return None

    async def finalize(run_id, *, status, error):
        assert status == "expired"
        assert error == "Agent run deadline elapsed"
        calls.append(("finalize", run_id))

    monkeypatch.setattr(scheduler_cli, "UnitOfWork", _FakeUnitOfWork)
    monkeypatch.setattr(scheduler_cli, "sweep_agent_run_deadlines", sweep)
    monkeypatch.setattr(scheduler_cli, "settle_terminal_root_run_async", settle)
    monkeypatch.setattr(scheduler_cli, "async_finalize_cycle_run_from_run", finalize)

    next_sweep_at = await scheduler_cli._enforce_agent_run_deadlines_if_due(
        next_sweep_at=0.0,
        now=100.0,
    )

    assert next_sweep_at == 160.0
    assert calls == [("sweep", 25), ("settle", 42), ("finalize", 42)]
    assert json.loads(capsys.readouterr().out) == {
        "event": "agent_run_deadline_sweep",
        "ok": True,
        "closeout_requested": 1,
        "expired": 1,
    }


async def test_daemon_does_not_host_agent_run_maintenance(monkeypatch):
    tick_calls = 0

    async def fake_startup(*_args, **_kwargs):
        return {"ok": True}

    async def fake_tick(*_args, **_kwargs):
        nonlocal tick_calls
        tick_calls += 1
        return {"ok": True}

    async def maintenance_must_not_run(**_kwargs):
        raise AssertionError("the scheduler daemon must not host agent-run maintenance")

    monkeypatch.setattr(scheduler_cli, "UnitOfWork", _FakeUnitOfWork)
    monkeypatch.setattr(scheduler_cli, "async_scheduler_daemon_startup", fake_startup)
    monkeypatch.setattr(scheduler_cli, "async_scheduler_daemon_tick", fake_tick)
    monkeypatch.setattr(
        scheduler_cli,
        "_reap_stale_active_runs_if_due",
        maintenance_must_not_run,
    )
    monkeypatch.setattr(
        scheduler_cli,
        "_enforce_agent_run_deadlines_if_due",
        maintenance_must_not_run,
    )

    assert await scheduler_cli.cmd_daemon(_daemon_args(once=True)) == 0
    assert tick_calls == 1
