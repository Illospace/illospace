#!/usr/bin/env python3
"""Illo Brain scheduler control CLI."""
from __future__ import annotations

import argparse
import asyncio
import json
import time
from datetime import datetime, timedelta

from brain.app.scheduler.catalog import (
    async_list_scheduler_jobs,
    async_list_scheduler_runs,
    async_sync_scheduler_catalog,
    normalize_owner_mode,
)
from brain.app.scheduler.daemon import (
    async_scheduler_daemon_startup,
    async_scheduler_daemon_tick,
    async_scheduler_health_snapshot,
)
from brain.app.scheduler.executor import (
    async_drain_scheduler,
    async_retry_scheduler_run,
    async_resume_scheduler_run,
    async_run_scheduler_job,
    async_set_scheduler_job_load_shed,
    async_set_scheduler_job_owner_mode,
    async_set_scheduler_job_paused,
)
from brain.app.scheduler.planner import async_materialize_due_runs
from brain.app.scheduler.runtime import make_lease_owner
from brain.platform.db.repositories.unit_of_work import UnitOfWork
from brain.systems.cycles.service import async_finalize_cycle_run_from_run
from brain.systems.runs.cortex.runner import (
    reap_stale_active_runs,
    settle_terminal_root_run_async,
)
from brain.systems.runs.deadlines import sweep_agent_run_deadlines

_AGENT_RUN_DEADLINE_SWEEP_INTERVAL_SECONDS = 60.0
_AGENT_RUN_DEADLINE_SWEEP_LIMIT = 25
_STALE_RUN_REAP_INTERVAL_SECONDS = 60.0
_STALE_RUN_REAP_LIMIT = 25
_monotonic = time.monotonic


def _emit(payload: dict, *, compact: bool = False) -> None:
    if compact:
        print(json.dumps(payload, separators=(",", ":"), default=str))
        return
    print(json.dumps(payload, indent=2, default=str))


def _now_from_args(args: argparse.Namespace) -> datetime | None:
    raw = getattr(args, "now", None)
    if not raw:
        return None
    return datetime.fromisoformat(raw)


async def _reap_stale_active_runs_if_due(*, next_reap_at: float) -> float:
    now = _monotonic()
    if now < next_reap_at:
        return next_reap_at

    try:
        reaped = await reap_stale_active_runs(limit=_STALE_RUN_REAP_LIMIT)
    except Exception as exc:
        _emit(
            {
                "event": "agent_run_stale_reap_failed",
                "ok": False,
                "error": str(exc),
            },
            compact=True,
        )
    else:
        _emit(
            {
                "event": "agent_run_stale_reap",
                "ok": True,
                "reaped": reaped,
            },
            compact=True,
        )
    return now + _STALE_RUN_REAP_INTERVAL_SECONDS


async def _enforce_agent_run_deadlines_if_due(
    *,
    next_sweep_at: float,
    now: float | None = None,
) -> float:
    monotonic_now = _monotonic() if now is None else float(now)
    if monotonic_now < next_sweep_at:
        return next_sweep_at

    try:
        async with UnitOfWork() as uow:
            result = await sweep_agent_run_deadlines(
                uow.session,
                limit=_AGENT_RUN_DEADLINE_SWEEP_LIMIT,
            )
        for run_id in result.expired_run_ids:
            async with UnitOfWork() as uow:
                await settle_terminal_root_run_async(uow.session, int(run_id))
            await async_finalize_cycle_run_from_run(
                int(run_id),
                status="expired",
                error="Agent run deadline elapsed",
            )
    except Exception as exc:
        _emit(
            {
                "event": "agent_run_deadline_sweep_failed",
                "ok": False,
                "error": str(exc),
            },
            compact=True,
        )
    else:
        _emit(
            {
                "event": "agent_run_deadline_sweep",
                "ok": True,
                "closeout_requested": result.closeout_requested,
                "expired": result.expired,
            },
            compact=True,
        )
    return monotonic_now + _AGENT_RUN_DEADLINE_SWEEP_INTERVAL_SECONDS


async def cmd_status(args: argparse.Namespace) -> int:
    async with UnitOfWork() as uow:
        snapshot = await async_scheduler_health_snapshot(
            uow.session,
            owner_mode=args.owner_mode,
            recent_run_limit=args.recent_run_limit,
            now=_now_from_args(args),
        )
    _emit(snapshot)
    return 0


async def cmd_state(args: argparse.Namespace) -> int:
    async with UnitOfWork() as uow:
        payload = {
            "jobs": await async_list_scheduler_jobs(uow.session),
            "runs": await async_list_scheduler_runs(uow.session, limit=args.limit),
        }
    _emit(payload)
    return 0


async def cmd_sync(args: argparse.Namespace) -> int:
    async with UnitOfWork() as uow:
        result = await async_sync_scheduler_catalog(
            uow.session,
            owner_mode=args.owner_mode,
            job_keys=tuple(args.job_keys) if args.job_keys else None,
            now=_now_from_args(args),
        )
    _emit({"ok": True, **result})
    return 0


async def cmd_materialize(args: argparse.Namespace) -> int:
    async with UnitOfWork() as uow:
        runs = await async_materialize_due_runs(
            uow.session,
            allowed_owner_modes=(args.owner_mode,),
            job_keys=tuple(args.job_keys) if args.job_keys else None,
            now=_now_from_args(args),
        )
    _emit({"ok": True, "recorded": len(runs), "run_ids": [run.id for run in runs]})
    return 0


async def cmd_run_job(args: argparse.Namespace) -> int:
    allowed_owner_modes = (
        (normalize_owner_mode(args.owner_mode),)
        if args.owner_mode
        else ("scheduler",)
    )
    async with UnitOfWork() as uow:
        result = await async_run_scheduler_job(
            uow.session,
            args.job_key,
            owner_id=make_lease_owner(label=args.owner_label),
            now=_now_from_args(args),
            allowed_owner_modes=allowed_owner_modes,
        )
    _emit(result)
    return 0 if result.get("ok") else 1


async def cmd_drain(args: argparse.Namespace) -> int:
    async with UnitOfWork() as uow:
        result = await async_drain_scheduler(
            uow.session,
            owner_mode=args.owner_mode,
            job_key=args.job_key,
            max_runs=args.max_runs,
            resume=args.resume,
            owner_id=make_lease_owner(label=args.owner_label),
            now=_now_from_args(args),
        )
    _emit(result)
    return 0


async def cmd_pause_job(args: argparse.Namespace) -> int:
    async with UnitOfWork() as uow:
        job = await async_set_scheduler_job_paused(
            uow.session,
            args.job_key,
            paused=True,
            reason=args.reason,
            now=_now_from_args(args),
        )
    _emit({"ok": True, "job_key": job.job_key, "enabled": job.enabled, "pause_reason": job.pause_reason})
    return 0


async def cmd_resume_job(args: argparse.Namespace) -> int:
    async with UnitOfWork() as uow:
        job = await async_set_scheduler_job_paused(
            uow.session,
            args.job_key,
            paused=False,
            reason=None,
            now=_now_from_args(args),
        )
    _emit({"ok": True, "job_key": job.job_key, "enabled": job.enabled, "pause_reason": job.pause_reason})
    return 0


async def cmd_owner_mode(args: argparse.Namespace) -> int:
    async with UnitOfWork() as uow:
        job = await async_set_scheduler_job_owner_mode(
            uow.session,
            args.job_key,
            owner_mode=normalize_owner_mode(args.owner_mode),
        )
    _emit({"ok": True, "job_key": job.job_key, "owner_mode": job.owner_mode})
    return 0


async def cmd_load_shed(args: argparse.Namespace) -> int:
    policy = json.loads(args.load_shed_policy) if args.load_shed_policy else None
    async with UnitOfWork() as uow:
        job = await async_set_scheduler_job_load_shed(
            uow.session,
            args.job_key,
            load_shed_policy=policy,
            max_concurrency=args.max_concurrency,
            pause_new_runs=args.pause_new_runs,
            reason=args.reason,
        )
    _emit(
        {
            "ok": True,
            "job_key": job.job_key,
            "max_concurrency": job.max_concurrency,
            "load_shed_policy": job.load_shed_policy or {},
        }
    )
    return 0


async def cmd_resume_run(args: argparse.Namespace) -> int:
    async with UnitOfWork() as uow:
        run = await async_resume_scheduler_run(
            uow.session,
            args.run_id,
            owner_id=make_lease_owner(label=args.owner_label),
            now=_now_from_args(args),
        )
    _emit({"ok": True, "id": run.id, "status": run.status})
    return 0


async def cmd_retry_run(args: argparse.Namespace) -> int:
    async with UnitOfWork() as uow:
        run = await async_retry_scheduler_run(uow.session, args.run_id, now=_now_from_args(args))
    _emit({"ok": True, "id": run.id, "status": run.status, "parent_run_id": run.parent_run_id})
    return 0


async def cmd_daemon(args: argparse.Namespace) -> int:
    tick = 0
    next_deadline_sweep_at = 0.0
    next_stale_reap_at = 0.0
    try:
        async with UnitOfWork() as uow:
            startup = await async_scheduler_daemon_startup(
                uow.session,
                owner_mode=args.owner_mode,
                now=_now_from_args(args),
                cold_start_gap_threshold=timedelta(
                    seconds=args.cold_start_gap_threshold_seconds
                ),
            )
        _emit({"event": "scheduler_startup", **startup})
        while True:
            next_deadline_sweep_at = await _enforce_agent_run_deadlines_if_due(
                next_sweep_at=next_deadline_sweep_at,
            )
            next_stale_reap_at = await _reap_stale_active_runs_if_due(
                next_reap_at=next_stale_reap_at,
            )
            async with UnitOfWork() as uow:
                result = await async_scheduler_daemon_tick(
                    uow.session,
                    owner_mode=args.owner_mode,
                    job_key=args.job_key,
                    max_runs=args.max_runs,
                    resume=args.resume,
                    now=_now_from_args(args),
                )
            tick += 1
            _emit({"tick": tick, **result}, compact=True)
            if args.once:
                return 0
            await asyncio.sleep(max(1, args.poll_interval_seconds))
    except KeyboardInterrupt:
        _emit({"ok": True, "stopped": "keyboard_interrupt", "ticks": tick})
        return 0
    except Exception as exc:
        _emit({"ok": False, "error": str(exc), "ticks": tick})
        return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python3 -m brain.app.cli.scheduler", description="Scheduler control plane")
    sub = parser.add_subparsers(dest="command", required=True)

    p_status = sub.add_parser("status", help="Show scheduler health and lag")
    p_status.add_argument("--owner-mode", default="scheduler", help="Job owner mode to inspect")
    p_status.add_argument("--recent-run-limit", type=int, default=20, help="How many recent runs to include")
    p_status.add_argument("--now", default=None, help="Optional ISO timestamp override")
    p_status.set_defaults(func=cmd_status)

    p_state = sub.add_parser("state", help="Show raw scheduler jobs and runs")
    p_state.add_argument("--limit", type=int, default=20, help="How many recent runs to include")
    p_state.set_defaults(func=cmd_state)

    p_sync = sub.add_parser("sync", help="Ensure built-in scheduler jobs exist")
    p_sync.add_argument("--owner-mode", default="scheduler", choices=["scheduler"], help="Owner mode to sync")
    p_sync.add_argument("--now", default=None, help="Optional ISO timestamp override")
    p_sync.add_argument("job_keys", nargs="*", help="Optional job keys to sync")
    p_sync.set_defaults(func=cmd_sync)

    p_materialize = sub.add_parser("materialize", help="Record due runs without executing them")
    p_materialize.add_argument("--owner-mode", default="scheduler", choices=["scheduler"], help="Owner mode to materialize")
    p_materialize.add_argument("--now", default=None, help="Optional ISO timestamp override")
    p_materialize.add_argument("job_keys", nargs="*", help="Optional job keys to materialize")
    p_materialize.set_defaults(func=cmd_materialize)

    p_run_job = sub.add_parser("run-job", help="Materialize and execute one due job")
    p_run_job.add_argument("--job-key", required=True, help="Scheduler job key")
    p_run_job.add_argument("--owner-label", default="scheduler", help="Lease owner label")
    p_run_job.add_argument("--owner-mode", default=None, choices=["scheduler"], help="Optional owner-mode filter")
    p_run_job.add_argument("--now", default=None, help="Optional ISO timestamp override")
    p_run_job.set_defaults(func=cmd_run_job)

    p_drain = sub.add_parser("drain", help="Materialize and execute due runs once")
    p_drain.add_argument("--owner-mode", default="scheduler", choices=["scheduler"], help="Owner mode to drain")
    p_drain.add_argument("--owner-label", default="scheduler", help="Lease owner label")
    p_drain.add_argument("--job-key", default=None, help="Optional job filter")
    p_drain.add_argument("--max-runs", type=int, default=10, help="Maximum runs to execute")
    p_drain.add_argument("--no-resume", dest="resume", action="store_false", help="Do not resume completed steps")
    p_drain.add_argument("--now", default=None, help="Optional ISO timestamp override")
    p_drain.set_defaults(resume=True, func=cmd_drain)

    p_pause = sub.add_parser("pause", aliases=["pause-job"], help="Pause a scheduler job")
    p_pause.add_argument("job_key", help="Scheduler job key")
    p_pause.add_argument("--reason", default="manual_pause", help="Pause reason")
    p_pause.add_argument("--now", default=None, help="Optional ISO timestamp override")
    p_pause.set_defaults(func=cmd_pause_job)

    p_resume = sub.add_parser("resume", aliases=["resume-job"], help="Resume a paused scheduler job")
    p_resume.add_argument("job_key", help="Scheduler job key")
    p_resume.add_argument("--now", default=None, help="Optional ISO timestamp override")
    p_resume.set_defaults(func=cmd_resume_job)

    p_owner_mode = sub.add_parser("set-owner-mode", aliases=["cutover"], help="Change a job owner mode")
    p_owner_mode.add_argument("job_key", help="Scheduler job key")
    p_owner_mode.add_argument(
        "--owner-mode",
        required=True,
        choices=["scheduler"],
        help="Target owner mode",
    )
    p_owner_mode.set_defaults(func=cmd_owner_mode)

    p_load_shed = sub.add_parser("set-load-shed", aliases=["load-shed"], help="Adjust load-shedding controls")
    p_load_shed.add_argument("job_key", help="Scheduler job key")
    p_load_shed.add_argument("--max-concurrency", type=int, default=None, help="New concurrency ceiling")
    p_load_shed_pause = p_load_shed.add_mutually_exclusive_group()
    p_load_shed_pause.add_argument(
        "--pause-new-runs",
        dest="pause_new_runs",
        action="store_true",
        help="Shelve newly materialized runs via load shedding",
    )
    p_load_shed_pause.add_argument(
        "--resume-new-runs",
        dest="pause_new_runs",
        action="store_false",
        help="Stop load-shedding newly materialized runs",
    )
    p_load_shed.add_argument("--reason", default=None, help="Optional load-shed reason")
    p_load_shed.add_argument("--load-shed-policy", default=None, help="Explicit JSON policy override")
    p_load_shed.set_defaults(pause_new_runs=None)
    p_load_shed.set_defaults(func=cmd_load_shed)

    p_resume_run = sub.add_parser("resume-run", help="Resume a persisted run")
    p_resume_run.add_argument("--run-id", type=int, required=True)
    p_resume_run.add_argument("--owner-label", default="scheduler", help="Lease owner label")
    p_resume_run.add_argument("--now", default=None, help="Optional ISO timestamp override")
    p_resume_run.set_defaults(func=cmd_resume_run)

    p_retry_run = sub.add_parser("retry-run", help="Clone a run into a new attempt")
    p_retry_run.add_argument("--run-id", type=int, required=True)
    p_retry_run.add_argument("--now", default=None, help="Optional ISO timestamp override")
    p_retry_run.set_defaults(func=cmd_retry_run)

    p_daemon = sub.add_parser("daemon", help="Run the always-on scheduler loop")
    p_daemon.add_argument("--owner-mode", default="scheduler", choices=["scheduler"], help="Owner mode to drain")
    p_daemon.add_argument("--job-key", default=None, help="Optional job filter")
    p_daemon.add_argument("--max-runs", type=int, default=10, help="Maximum runs per tick")
    p_daemon.add_argument("--poll-interval-seconds", type=int, default=30, help="Seconds between daemon ticks")
    p_daemon.add_argument(
        "--cold-start-gap-threshold-seconds",
        type=int,
        default=3600,
        help="Minimum stale liveness-checkpoint age that triggers reconciliation",
    )
    p_daemon.add_argument("--no-resume", dest="resume", action="store_false", help="Do not resume completed steps")
    p_daemon.add_argument("--once", action="store_true", help="Run one tick and exit")
    p_daemon.add_argument("--now", default=None, help="Optional ISO timestamp override")
    p_daemon.set_defaults(resume=True, func=cmd_daemon)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(asyncio.run(args.func(args)))


if __name__ == "__main__":
    raise SystemExit(main())
