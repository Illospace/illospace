"""Scheduler due-run materialization and cron timing helpers."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from brain.platform.db.models.scheduler import (
    OWNER_MODE_SCHEDULER,
    SchedulerJob,
    SchedulerRun,
)
from brain.systems.runs.cortex.recording import trace_id_for_scheduler_run_id
from brain.app.scheduler.runtime import job_matches_identifier
from brain.app.scheduler.contracts import validate_scheduler_run_contract
from brain.app.scheduler.runtime import (
    RUN_STATUS_RECORDED,
    RUN_STATUS_SHELVED,
    active_lease_count,
    async_active_lease_count,
)


def _parse_cron_field(field: str, value: int, *, allow_sunday_seven: bool = False) -> bool:
    if field == "*":
        return True

    for part in field.split(","):
        part = part.strip()
        if not part:
            continue
        if part.startswith("*/"):
            step = int(part[2:])
            if step > 0 and value % step == 0:
                return True
            continue
        if "-" in part:
            start, end = part.split("-", 1)
            if int(start) <= value <= int(end):
                return True
            continue
        try:
            parsed = int(part)
        except ValueError:
            continue
        if parsed == value:
            return True
        if allow_sunday_seven and value == 0 and parsed == 7:
            return True
    return False


def _cron_matches(expr: str, when_local: datetime) -> bool:
    """Match the scheduler's deliberately small five-field cron subset.

    Supported syntax is numeric five-field cron with ``*``, comma lists,
    ``*/n`` steps, and numeric ranges. Names, seconds/year fields, ``L``,
    ``W``, and ``#`` are intentionally out of scope for this scheduler pass.
    """
    parts = expr.strip().split()
    if len(parts) != 5:
        raise ValueError(f"Invalid cron expression '{expr}'. Expected 5 fields.")

    minute, hour, dom, month, dow = parts
    cron_dow = (when_local.weekday() + 1) % 7
    return (
        _parse_cron_field(minute, when_local.minute)
        and _parse_cron_field(hour, when_local.hour)
        and _parse_cron_field(dom, when_local.day)
        and _parse_cron_field(month, when_local.month)
        and _parse_cron_field(dow, cron_dow, allow_sunday_seven=True)
    )


def next_run_after(expr: str, timezone_name: str, after_utc: datetime) -> datetime:
    """Return the next fire time strictly after ``after_utc``."""
    if after_utc.tzinfo is None:
        raise ValueError("after_utc must be timezone-aware")

    tz = timezone.utc
    try:
        tz = ZoneInfo(timezone_name)
    except Exception:
        tz = timezone.utc

    candidate = after_utc.astimezone(tz).replace(second=0, microsecond=0) + timedelta(
        minutes=1
    )
    for _ in range(60 * 24 * 366):
        if _cron_matches(expr, candidate):
            return candidate.astimezone(timezone.utc)
        candidate += timedelta(minutes=1)
    raise ValueError(f"Could not resolve next run for '{expr}' in timezone '{timezone_name}'")


def materialize_due_runs(
    session: Session,
    *,
    now: datetime | None = None,
    allowed_owner_modes: tuple[str, ...] = (OWNER_MODE_SCHEDULER,),
    job_keys: tuple[str, ...] | None = None,
) -> list[SchedulerRun]:
    """Record due scheduler-owned runs without executing them."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")

    if not allowed_owner_modes:
        return []

    normalized_job_keys = tuple(job_keys or ())

    jobs = session.scalars(
        select(SchedulerJob)
        .where(
            SchedulerJob.enabled.is_(True),
            SchedulerJob.owner_mode.in_(allowed_owner_modes),
        )
        .order_by(SchedulerJob.priority.desc(), SchedulerJob.id.asc())
    ).all()

    created: list[SchedulerRun] = []
    for job in jobs:
        if normalized_job_keys and not any(job_matches_identifier(job, key) for key in normalized_job_keys):
            continue
        scheduled_for = job.next_run_at
        if scheduled_for is None:
            try:
                scheduled_for = next_run_after(
                    job.cron_expr, job.timezone, now - timedelta(minutes=1)
                )
            except ValueError:
                continue
            job.next_run_at = scheduled_for
        if scheduled_for > now:
            continue

        policy = (job.misfire_policy or "record").strip().lower()
        if policy not in {"record", "skip", "catch_up"}:
            policy = "record"

        if policy == "skip":
            job.next_run_at = next_run_after(job.cron_expr, job.timezone, now)
            continue

        fires: list[datetime] = [scheduled_for]
        if policy == "catch_up":
            next_fire = scheduled_for
            try:
                max_catch_up_runs = int((job.load_shed_policy or {}).get("max_catch_up_runs", 100))
            except (TypeError, ValueError):
                max_catch_up_runs = 100
            while len(fires) < max(1, max_catch_up_runs):
                try:
                    next_fire = next_run_after(job.cron_expr, job.timezone, next_fire)
                except ValueError:
                    next_fire = None
                    break
                if next_fire is None or next_fire > now:
                    break
                fires.append(next_fire)

        last_next_fire = None
        for fire_at in fires:
            idempotency_key = f"{job.job_key}:{fire_at.isoformat()}"
            existing = session.scalar(
                select(SchedulerRun).where(SchedulerRun.idempotency_key == idempotency_key)
            )
            try:
                last_next_fire = next_run_after(job.cron_expr, job.timezone, fire_at)
            except ValueError:
                last_next_fire = None
            if existing is not None:
                continue

            contract, contract_errors = validate_scheduler_run_contract(job, payload=job.default_payload or {})
            active_runs = active_lease_count(session, job.id, now=now)
            status = RUN_STATUS_RECORDED
            result_summary = None
            load_shed_policy = job.load_shed_policy or {}
            if contract_errors:
                status = RUN_STATUS_SHELVED
                result_summary = {
                    "reason": "contract_invalid",
                    "contract_errors": contract_errors,
                }
            elif bool(load_shed_policy.get("pause_new_runs")):
                status = RUN_STATUS_SHELVED
                result_summary = {
                    "reason": "pause_new_runs",
                    "load_shed_policy": load_shed_policy,
                }
            elif active_runs >= max(int(job.max_concurrency or 1), 1):
                status = RUN_STATUS_SHELVED
                result_summary = {
                    "reason": "max_concurrency",
                    "active_leases": int(active_runs),
                    "max_concurrency": job.max_concurrency,
                }

            run = SchedulerRun(
                job_id=job.id,
                scheduled_for=fire_at,
                window_start=fire_at,
                window_end=fire_at,
                status=status,
                attempt=1,
                idempotency_key=idempotency_key,
                payload=job.default_payload or {},
                result_summary=result_summary,
                task_contract=contract,
            )
            session.add(run)
            created.append(run)

        if last_next_fire is None:
            try:
                last_next_fire = next_run_after(job.cron_expr, job.timezone, fires[-1])
            except ValueError:
                last_next_fire = None
        job.next_run_at = last_next_fire

    session.flush()
    for run in created:
        if not run.trace_id:
            run.trace_id = trace_id_for_scheduler_run_id(run.id)
    if created:
        session.flush()
    return created


async def async_materialize_due_runs(
    session: AsyncSession,
    *,
    now: datetime | None = None,
    allowed_owner_modes: tuple[str, ...] = (OWNER_MODE_SCHEDULER,),
    job_keys: tuple[str, ...] | None = None,
) -> list[SchedulerRun]:
    """Record due scheduler-owned runs without executing them using an async session."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")

    if not allowed_owner_modes:
        return []

    normalized_job_keys = tuple(job_keys or ())

    jobs = (
        await session.scalars(
            select(SchedulerJob)
            .where(
                SchedulerJob.enabled.is_(True),
                SchedulerJob.owner_mode.in_(allowed_owner_modes),
            )
            .order_by(SchedulerJob.priority.desc(), SchedulerJob.id.asc())
        )
    ).all()

    created: list[SchedulerRun] = []
    for job in jobs:
        if normalized_job_keys and not any(job_matches_identifier(job, key) for key in normalized_job_keys):
            continue
        scheduled_for = job.next_run_at
        if scheduled_for is None:
            try:
                scheduled_for = next_run_after(
                    job.cron_expr, job.timezone, now - timedelta(minutes=1)
                )
            except ValueError:
                continue
            job.next_run_at = scheduled_for
        if scheduled_for > now:
            continue

        policy = (job.misfire_policy or "record").strip().lower()
        if policy not in {"record", "skip", "catch_up"}:
            policy = "record"

        if policy == "skip":
            job.next_run_at = next_run_after(job.cron_expr, job.timezone, now)
            continue

        fires: list[datetime] = [scheduled_for]
        if policy == "catch_up":
            next_fire = scheduled_for
            try:
                max_catch_up_runs = int((job.load_shed_policy or {}).get("max_catch_up_runs", 100))
            except (TypeError, ValueError):
                max_catch_up_runs = 100
            while len(fires) < max(1, max_catch_up_runs):
                try:
                    next_fire = next_run_after(job.cron_expr, job.timezone, next_fire)
                except ValueError:
                    next_fire = None
                    break
                if next_fire is None or next_fire > now:
                    break
                fires.append(next_fire)

        last_next_fire = None
        for fire_at in fires:
            idempotency_key = f"{job.job_key}:{fire_at.isoformat()}"
            existing = await session.scalar(
                select(SchedulerRun).where(SchedulerRun.idempotency_key == idempotency_key)
            )
            try:
                last_next_fire = next_run_after(job.cron_expr, job.timezone, fire_at)
            except ValueError:
                last_next_fire = None
            if existing is not None:
                continue

            contract, contract_errors = validate_scheduler_run_contract(job, payload=job.default_payload or {})
            active_runs = await async_active_lease_count(session, job.id, now=now)
            status = RUN_STATUS_RECORDED
            result_summary = None
            load_shed_policy = job.load_shed_policy or {}
            if contract_errors:
                status = RUN_STATUS_SHELVED
                result_summary = {
                    "reason": "contract_invalid",
                    "contract_errors": contract_errors,
                }
            elif bool(load_shed_policy.get("pause_new_runs")):
                status = RUN_STATUS_SHELVED
                result_summary = {
                    "reason": "pause_new_runs",
                    "load_shed_policy": load_shed_policy,
                }
            elif active_runs >= max(int(job.max_concurrency or 1), 1):
                status = RUN_STATUS_SHELVED
                result_summary = {
                    "reason": "max_concurrency",
                    "active_leases": int(active_runs),
                    "max_concurrency": job.max_concurrency,
                }

            run = SchedulerRun(
                job_id=job.id,
                scheduled_for=fire_at,
                window_start=fire_at,
                window_end=fire_at,
                status=status,
                attempt=1,
                idempotency_key=idempotency_key,
                payload=job.default_payload or {},
                result_summary=result_summary,
                task_contract=contract,
            )
            session.add(run)
            created.append(run)

        if last_next_fire is None:
            try:
                last_next_fire = next_run_after(job.cron_expr, job.timezone, fires[-1])
            except ValueError:
                last_next_fire = None
        job.next_run_at = last_next_fire

    await session.flush()
    for run in created:
        if not run.trace_id:
            run.trace_id = trace_id_for_scheduler_run_id(run.id)
    if created:
        await session.flush()
    return created
