#!/usr/bin/env python3
"""Illo Brain scheduler control CLI."""
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime

from brain.platform.db.repositories.unit_of_work import UnitOfWork, open_unit_of_work
from brain.app.scheduler.catalog import list_scheduler_jobs, list_scheduler_runs, normalize_owner_mode, sync_scheduler_catalog
from brain.app.scheduler.daemon import scheduler_daemon_tick, scheduler_health_snapshot
from brain.app.scheduler.executor import (
    drain_scheduler,
    retry_scheduler_run,
    resume_scheduler_run,
    run_scheduler_job,
    set_scheduler_job_load_shed,
    set_scheduler_job_owner_mode,
    set_scheduler_job_paused,
)
from brain.app.scheduler.planner import materialize_due_runs
from brain.app.scheduler.runtime import make_lease_owner


def _emit(payload: dict) -> None:
    print(json.dumps(payload, indent=2, default=str))


def _now_from_args(args: argparse.Namespace) -> datetime | None:
    raw = getattr(args, "now", None)
    if not raw:
        return None
    return datetime.fromisoformat(raw)


def cmd_status(args: argparse.Namespace) -> int:
    with open_unit_of_work(UnitOfWork) as uow:
        snapshot = scheduler_health_snapshot(
            uow.session,
            owner_mode=args.owner_mode,
            recent_run_limit=args.recent_run_limit,
            now=_now_from_args(args),
        )
    _emit(snapshot)
    return 0


def cmd_state(args: argparse.Namespace) -> int:
    with open_unit_of_work(UnitOfWork) as uow:
        payload = {
            "jobs": list_scheduler_jobs(uow.session),
            "runs": list_scheduler_runs(uow.session, limit=args.limit),
        }
    _emit(payload)
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    with open_unit_of_work(UnitOfWork) as uow:
        result = sync_scheduler_catalog(
            uow.session,
            owner_mode=args.owner_mode,
            job_keys=tuple(args.job_keys) if args.job_keys else None,
            now=_now_from_args(args),
        )
    _emit({"ok": True, **result})
    return 0


def cmd_materialize(args: argparse.Namespace) -> int:
    with open_unit_of_work(UnitOfWork) as uow:
        runs = materialize_due_runs(
            uow.session,
            allowed_owner_modes=(args.owner_mode,),
            job_keys=tuple(args.job_keys) if args.job_keys else None,
            now=_now_from_args(args),
        )
    _emit({"ok": True, "recorded": len(runs), "run_ids": [run.id for run in runs]})
    return 0


def cmd_run_job(args: argparse.Namespace) -> int:
    allowed_owner_modes = (
        (normalize_owner_mode(args.owner_mode),)
        if args.owner_mode
        else ("scheduler",)
    )
    with open_unit_of_work(UnitOfWork) as uow:
        result = run_scheduler_job(
            uow.session,
            args.job_key,
            owner_id=make_lease_owner(label=args.owner_label),
            now=_now_from_args(args),
            allowed_owner_modes=allowed_owner_modes,
        )
    _emit(result)
    return 0 if result.get("ok") else 1


def cmd_drain(args: argparse.Namespace) -> int:
    with open_unit_of_work(UnitOfWork) as uow:
        result = drain_scheduler(
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


def cmd_pause_job(args: argparse.Namespace) -> int:
    with open_unit_of_work(UnitOfWork) as uow:
        job = set_scheduler_job_paused(
            uow.session,
            args.job_key,
            paused=True,
            reason=args.reason,
            now=_now_from_args(args),
        )
    _emit({"ok": True, "job_key": job.job_key, "enabled": job.enabled, "pause_reason": job.pause_reason})
    return 0


def cmd_resume_job(args: argparse.Namespace) -> int:
    with open_unit_of_work(UnitOfWork) as uow:
        job = set_scheduler_job_paused(
            uow.session,
            args.job_key,
            paused=False,
            reason=None,
            now=_now_from_args(args),
        )
    _emit({"ok": True, "job_key": job.job_key, "enabled": job.enabled, "pause_reason": job.pause_reason})
    return 0


def cmd_owner_mode(args: argparse.Namespace) -> int:
    with open_unit_of_work(UnitOfWork) as uow:
        job = set_scheduler_job_owner_mode(
            uow.session,
            args.job_key,
            owner_mode=normalize_owner_mode(args.owner_mode),
        )
    _emit({"ok": True, "job_key": job.job_key, "owner_mode": job.owner_mode})
    return 0


def cmd_load_shed(args: argparse.Namespace) -> int:
    policy = json.loads(args.load_shed_policy) if args.load_shed_policy else None
    with open_unit_of_work(UnitOfWork) as uow:
        job = set_scheduler_job_load_shed(
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


def cmd_resume_run(args: argparse.Namespace) -> int:
    with open_unit_of_work(UnitOfWork) as uow:
        run = resume_scheduler_run(
            uow.session,
            args.run_id,
            owner_id=make_lease_owner(label=args.owner_label),
            now=_now_from_args(args),
        )
    _emit({"ok": True, "id": run.id, "status": run.status})
    return 0


def cmd_retry_run(args: argparse.Namespace) -> int:
    with open_unit_of_work(UnitOfWork) as uow:
        run = retry_scheduler_run(uow.session, args.run_id, now=_now_from_args(args))
    _emit({"ok": True, "id": run.id, "status": run.status, "parent_run_id": run.parent_run_id})
    return 0


def cmd_daemon(args: argparse.Namespace) -> int:
    tick = 0
    try:
        while True:
            with open_unit_of_work(UnitOfWork) as uow:
                result = scheduler_daemon_tick(
                    uow.session,
                    owner_mode=args.owner_mode,
                    job_key=args.job_key,
                    max_runs=args.max_runs,
                    resume=args.resume,
                    now=_now_from_args(args),
                )
            tick += 1
            _emit({"tick": tick, **result})
            if args.once:
                return 0
            time.sleep(max(1, args.poll_interval_seconds))
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
    p_daemon.add_argument("--no-resume", dest="resume", action="store_false", help="Do not resume completed steps")
    p_daemon.add_argument("--once", action="store_true", help="Run one tick and exit")
    p_daemon.add_argument("--now", default=None, help="Optional ISO timestamp override")
    p_daemon.set_defaults(resume=True, func=cmd_daemon)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
