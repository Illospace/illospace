"""Scheduler run, lease, and step helpers."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import re
import socket
from typing import Any

from brain.kernel.common.time import ensure_utc

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from brain.platform.db.models.scheduler import (
    OWNER_MODE_CRON,
    OWNER_MODE_MIRROR,
    OWNER_MODE_SCHEDULER,
    SchedulerJob,
    SchedulerLease,
    SchedulerRun,
    SchedulerRunStep,
)
RUN_STATUS_RECORDED = "recorded"
RUN_STATUS_SHELVED = "shelved"
RUN_STATUS_PAUSED = "paused"
RUN_STATUS_CLAIMED = "claimed"
RUN_STATUS_RUNNING = "running"
RUN_STATUS_SETTLED_SUCCESS = "settled_success"
RUN_STATUS_SETTLED_FAILURE = "settled_failure"
RUN_STATUS_RETRYABLE = "retryable"
RUN_STATUS_EXPIRED = "expired"
RUN_STATUS_EXECUTING = "executing"

RETRY_POLICY_DEFAULT_MAX_ATTEMPTS = 2
RETRY_POLICY_DEFAULT_BACKOFF_SECONDS = 0

LEASE_TTL_SECONDS = int(os.getenv("SCHEDULER_LEASE_TTL_SECONDS", "7200"))

_PROJECT_ROOT = Path(__file__).resolve().parents[3]

_OWNER_MODES = {
    OWNER_MODE_CRON,
    OWNER_MODE_MIRROR,
    OWNER_MODE_SCHEDULER,
}


def trace_id_for_run_id(run_id: int | str | None) -> str | None:
    try:
        value = int(run_id)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return f"run:{value}"


def trace_id_for_scheduler_run_id(run_id: int | str | None) -> str | None:
    return None if run_id is None else f"scheduler-run:{run_id}"


def _slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def make_lease_owner(*, label: str = "scheduler") -> str:
    host = socket.gethostname().split(".")[0]
    return f"{label}:{host}:{os.getpid()}"


def normalize_owner_mode(owner_mode: str) -> str:
    mode = (owner_mode or "").strip().lower()
    if mode not in _OWNER_MODES:
        raise ValueError(f"Unknown owner mode: {owner_mode}")
    return mode


def _utcnow(now: datetime | None = None) -> datetime:
    return ensure_utc(now)


def normalize_run_status(status: str | None) -> str:
    """Return the canonical scheduler run status vocabulary."""
    normalized = (status or RUN_STATUS_RECORDED).strip().lower()
    if normalized == "shed":
        return RUN_STATUS_SHELVED
    return normalized


def normalize_retry_policy(policy: dict[str, Any] | None) -> dict[str, int]:
    """Return deterministic retry settings for a scheduler job.

    ``max_attempts`` counts executions of the same scheduler run. Retries reuse
    the run row so persisted successful steps can be skipped on resume.
    """
    raw = dict(policy or {})
    try:
        max_attempts = int(raw.get("max_attempts", RETRY_POLICY_DEFAULT_MAX_ATTEMPTS))
    except (TypeError, ValueError):
        max_attempts = RETRY_POLICY_DEFAULT_MAX_ATTEMPTS
    try:
        backoff_seconds = int(raw.get("backoff_seconds", RETRY_POLICY_DEFAULT_BACKOFF_SECONDS))
    except (TypeError, ValueError):
        backoff_seconds = RETRY_POLICY_DEFAULT_BACKOFF_SECONDS
    return {
        "max_attempts": max(1, max_attempts),
        "backoff_seconds": max(0, backoff_seconds),
    }


def retry_available(job: SchedulerJob, run: SchedulerRun) -> bool:
    policy = normalize_retry_policy(job.retry_policy)
    return int(run.attempt or 1) < policy["max_attempts"]


def retry_available_at(job: SchedulerJob, run: SchedulerRun, *, now: datetime) -> datetime | None:
    if not retry_available(job, run):
        return None
    summary = run.result_summary or {}
    raw_next_retry_at = summary.get("next_retry_at")
    if raw_next_retry_at:
        try:
            parsed = datetime.fromisoformat(str(raw_next_retry_at))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except ValueError:
            pass
    policy = normalize_retry_policy(job.retry_policy)
    return now + timedelta(seconds=policy["backoff_seconds"])


async def async_active_lease_count(
    session: AsyncSession,
    job_id: int,
    *,
    now: datetime | None = None,
) -> int:
    now = _utcnow(now)
    return int(
        await session.scalar(
            select(func.count())
            .select_from(SchedulerLease)
            .join(SchedulerRun, SchedulerLease.run_id == SchedulerRun.id)
            .where(
                SchedulerRun.job_id == job_id,
                SchedulerLease.released_at.is_(None),
                SchedulerLease.expires_at > now,
            )
        )
        or 0
    )


def _job_aliases(job: SchedulerJob) -> set[str]:
    aliases = {job.job_key, job.family, job.program_key}
    handler_ref = getattr(job, "handler_ref", "") or ""
    if handler_ref:
        aliases.add(Path(handler_ref).stem)
        aliases.add(Path(handler_ref).name)
    payload = job.default_payload or {}
    payload_name = payload.get("name")
    if payload_name:
        aliases.add(_slugify(str(payload_name)))
    return {alias for alias in aliases if alias}


def job_matches_identifier(job: SchedulerJob, identifier: str) -> bool:
    needle = _slugify(identifier)
    aliases = _job_aliases(job)
    return identifier in aliases or needle in aliases or _slugify(job.job_key) == needle


async def async_find_scheduler_job(session: AsyncSession, identifier: str) -> SchedulerJob | None:
    result = await session.scalars(select(SchedulerJob))
    for job in result.all():
        if job_matches_identifier(job, identifier):
            return job
    return None


def _lease_payload(owner_id: str | None = None) -> tuple[str, str, int]:
    owner_id = owner_id or make_lease_owner()
    host = socket.gethostname()
    pid = os.getpid()
    return owner_id, host, pid


async def async_claim_run(
    session: AsyncSession,
    run_id: int,
    *,
    owner_id: str | None = None,
    lease_ttl_seconds: int = LEASE_TTL_SECONDS,
    now: datetime | None = None,
) -> tuple[SchedulerRun, SchedulerLease]:
    now = _utcnow(now)
    run = await session.get(SchedulerRun, run_id)
    if run is None:
        raise ValueError(f"Scheduler run {run_id} not found")
    if run.status in {RUN_STATUS_SETTLED_SUCCESS, RUN_STATUS_SETTLED_FAILURE}:
        raise ValueError(f"Scheduler run {run_id} is already settled")
    if run.status == RUN_STATUS_PAUSED:
        raise ValueError(f"Scheduler run {run_id} is paused")
    previous_status = normalize_run_status(run.status)

    if run.lease_id:
        lease = await session.get(SchedulerLease, run.lease_id)
        if lease is not None and lease.released_at is None and lease.expires_at > now:
            raise ValueError(f"Scheduler run {run_id} is already leased")
        if lease is not None and lease.released_at is None:
            lease.released_at = now
            lease.release_reason = "reclaimed after expiry"

    owner_id, host, pid = _lease_payload(owner_id)
    lease = await session.scalar(select(SchedulerLease).where(SchedulerLease.run_id == run.id))
    if lease is None:
        lease = SchedulerLease(
            run_id=run.id,
            owner_id=owner_id,
            owner_host=host,
            owner_pid=pid,
            acquired_at=now,
            heartbeat_at=now,
            expires_at=now + timedelta(seconds=lease_ttl_seconds),
        )
        session.add(lease)
    else:
        lease.owner_id = owner_id
        lease.owner_host = host
        lease.owner_pid = pid
        lease.acquired_at = now
        lease.heartbeat_at = now
        lease.expires_at = now + timedelta(seconds=lease_ttl_seconds)
        lease.released_at = None
        lease.release_reason = None
    await session.flush()

    run.lease_id = lease.id
    run.status = RUN_STATUS_CLAIMED
    if run.agent_run_id is not None:
        run.trace_id = trace_id_for_run_id(run.agent_run_id)
    elif not run.trace_id:
        run.trace_id = trace_id_for_scheduler_run_id(run.id)
    if previous_status in {RUN_STATUS_RETRYABLE, RUN_STATUS_EXPIRED}:
        run.attempt = int(run.attempt or 1) + 1
        run.finished_at = None
    if run.started_at is None:
        run.started_at = now
    job = await session.get(SchedulerJob, run.job_id)
    if job is not None:
        job.last_started_at = now
    await session.flush()
    return run, lease


async def async_claim_next_due_run(
    session: AsyncSession,
    *,
    now: datetime | None = None,
    allowed_owner_modes: tuple[str, ...] = (OWNER_MODE_SCHEDULER,),
    job_keys: tuple[str, ...] | None = None,
    owner_id: str | None = None,
    lease_ttl_seconds: int = LEASE_TTL_SECONDS,
) -> tuple[SchedulerRun, SchedulerLease] | None:
    now = _utcnow(now)
    if not allowed_owner_modes:
        return None

    requested_job_keys = tuple(job_keys or ())
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

    for job in jobs:
        if requested_job_keys and not any(job_matches_identifier(job, key) for key in requested_job_keys):
            continue
        if await async_active_lease_count(session, job.id, now=now) >= max(int(job.max_concurrency or 1), 1):
            continue

        candidates = (
            await session.scalars(
                select(SchedulerRun)
                .where(
                    SchedulerRun.job_id == job.id,
                    SchedulerRun.lease_id.is_(None),
                    SchedulerRun.status.in_(
                        [
                            RUN_STATUS_RECORDED,
                            RUN_STATUS_RETRYABLE,
                            RUN_STATUS_EXPIRED,
                            "shed",
                        ]
                    ),
                    SchedulerRun.scheduled_for <= now,
                )
                .order_by(SchedulerRun.scheduled_for.asc(), SchedulerRun.id.asc())
            )
        ).all()
        run = None
        for candidate in candidates:
            status = normalize_run_status(candidate.status)
            if status == RUN_STATUS_RECORDED:
                run = candidate
                break
            if status == RUN_STATUS_SHELVED:
                continue
            retry_at = retry_available_at(job, candidate, now=now)
            if retry_at is not None and retry_at <= now:
                run = candidate
                break
        if run is None:
            continue
        return await async_claim_run(
            session,
            run.id,
            owner_id=owner_id,
            lease_ttl_seconds=lease_ttl_seconds,
            now=now,
        )
    return None


async def async_heartbeat_lease(
    session: AsyncSession,
    lease_id: int,
    *,
    lease_ttl_seconds: int = LEASE_TTL_SECONDS,
    now: datetime | None = None,
) -> SchedulerLease | None:
    now = _utcnow(now)
    lease = await session.get(SchedulerLease, lease_id)
    if lease is None or lease.released_at is not None:
        return None
    lease.heartbeat_at = now
    lease.expires_at = now + timedelta(seconds=lease_ttl_seconds)
    await session.flush()
    return lease


async def async_release_lease(
    session: AsyncSession,
    lease_id: int,
    *,
    reason: str,
    now: datetime | None = None,
) -> SchedulerLease | None:
    now = _utcnow(now)
    lease = await session.get(SchedulerLease, lease_id)
    if lease is None:
        return None
    if lease.released_at is None:
        lease.released_at = now
        lease.release_reason = reason
    run = await session.get(SchedulerRun, lease.run_id)
    if run is not None and run.lease_id == lease.id:
        run.lease_id = None
    await session.flush()
    return lease


async def async_reclaim_expired_leases(
    session: AsyncSession,
    *,
    now: datetime | None = None,
) -> list[SchedulerRun]:
    now = _utcnow(now)
    expired_leases = (
        await session.scalars(
            select(SchedulerLease).where(
                SchedulerLease.released_at.is_(None),
                SchedulerLease.expires_at <= now,
            )
        )
    ).all()

    reclaimed: list[SchedulerRun] = []
    for lease in expired_leases:
        run = await session.get(SchedulerRun, lease.run_id)
        if run is None:
            continue
        lease.released_at = now
        lease.release_reason = "lease expired"
        if run.lease_id == lease.id:
            run.lease_id = None
        if normalize_run_status(run.status) not in {RUN_STATUS_SETTLED_SUCCESS, RUN_STATUS_SETTLED_FAILURE}:
            run.status = RUN_STATUS_EXPIRED
            run.finished_at = now
            if run.error_text is None:
                run.error_text = "lease expired"
            job = await session.get(SchedulerJob, run.job_id)
            if job is not None and retry_available(job, run):
                retry_at = retry_available_at(job, run, now=now)
                run.result_summary = {
                    **(run.result_summary or {}),
                    "retry_policy": normalize_retry_policy(job.retry_policy),
                    "retry_reason": "lease_expired",
                    "next_retry_at": retry_at.isoformat() if retry_at else None,
                }
            reclaimed.append(run)

    await session.flush()
    return reclaimed


async def async_ensure_run_steps(
    session: AsyncSession,
    run: SchedulerRun,
    step_defs: list[dict[str, Any]],
) -> list[SchedulerRunStep]:
    steps: list[SchedulerRunStep] = []
    for index, spec in enumerate(step_defs, start=1):
        step_key = str(spec["step_key"])
        sequence_no = int(spec.get("sequence_no", index))
        step = await session.scalar(
            select(SchedulerRunStep).where(
                SchedulerRunStep.run_id == run.id,
                SchedulerRunStep.step_key == step_key,
            )
        )
        if step is None:
            step = SchedulerRunStep(
                run_id=run.id,
                step_key=step_key,
                sequence_no=sequence_no,
                status=RUN_STATUS_RECORDED,
                attempt=1,
            )
            session.add(step)
        else:
            step.sequence_no = sequence_no
        steps.append(step)

    await session.flush()
    steps.sort(key=lambda step: step.sequence_no)
    return steps


async def async_update_run_step(
    session: AsyncSession,
    step: SchedulerRunStep,
    **kwargs: Any,
) -> SchedulerRunStep:
    if kwargs.get("started_at") is not None:
        step.started_at = kwargs["started_at"]
    if kwargs.get("finished_at") is not None:
        step.finished_at = kwargs["finished_at"]
    if kwargs.get("result_summary") is not None:
        step.result_summary = kwargs["result_summary"]
    if kwargs.get("error_text") is not None:
        step.error_text = kwargs["error_text"]
    if kwargs.get("agent_run_id") is not None:
        step.agent_run_id = kwargs["agent_run_id"]
    linked_agent_run_id = kwargs.get("agent_run_id") if kwargs.get("agent_run_id") is not None else step.agent_run_id
    if linked_agent_run_id is not None:
        step.trace_id = trace_id_for_run_id(linked_agent_run_id)
    elif not step.trace_id:
        run = await session.get(SchedulerRun, step.run_id)
        step.trace_id = (
            getattr(run, "trace_id", None)
            if run is not None
            else None
        ) or trace_id_for_scheduler_run_id(step.run_id)
    step.status = kwargs["status"]
    await session.flush()
    return step


async def async_finish_run(
    session: AsyncSession,
    run: SchedulerRun,
    *,
    job: SchedulerJob | None,
    status: str,
    result_summary: dict[str, Any] | None = None,
    error_text: str | None = None,
    now: datetime | None = None,
) -> SchedulerRun:
    now = _utcnow(now)
    run.status = status
    run.result_summary = result_summary or {}
    run.error_text = error_text
    run.finished_at = now
    if run.agent_run_id is not None:
        run.trace_id = trace_id_for_run_id(run.agent_run_id)
    elif not run.trace_id:
        run.trace_id = trace_id_for_scheduler_run_id(run.id)
    if job is not None:
        job.last_finished_at = now
        if status == RUN_STATUS_SETTLED_SUCCESS:
            job.pause_reason = None if job.enabled else job.pause_reason
    if run.lease_id:
        await async_release_lease(session, run.lease_id, reason=f"run {status}", now=now)
    await session.flush()
    return run


async def async_retry_run(
    session: AsyncSession,
    run_id: int,
    *,
    now: datetime | None = None,
) -> SchedulerRun:
    _utcnow(now)
    run = await session.get(SchedulerRun, run_id)
    if run is None:
        raise ValueError(f"Scheduler run {run_id} not found")
    if run.status == RUN_STATUS_SETTLED_SUCCESS:
        raise ValueError("Cannot retry a successful run")

    clone = SchedulerRun(
        job_id=run.job_id,
        scheduled_for=run.scheduled_for,
        window_start=run.window_start,
        window_end=run.window_end,
        status=RUN_STATUS_RECORDED,
        attempt=run.attempt + 1,
        idempotency_key=f"{run.idempotency_key}:retry:{run.attempt + 1}",
        payload=run.payload or {},
        trace_id=run.trace_id or trace_id_for_scheduler_run_id(run.id),
        parent_run_id=run.id,
    )
    session.add(clone)
    await session.flush()
    return clone


async def async_set_scheduler_job_pause_state(
    session: AsyncSession,
    identifier: str,
    *,
    paused: bool,
    reason: str | None = None,
    now: datetime | None = None,
) -> SchedulerJob:
    now = _utcnow(now)
    job = await async_find_scheduler_job(session, identifier)
    if job is None:
        raise ValueError(f"Scheduler job '{identifier}' not found")

    job.enabled = not paused
    if paused:
        job.pause_reason = reason or "paused via scheduler control"
    else:
        job.pause_reason = None
        if job.next_run_at is None or job.next_run_at <= now:
            try:
                from brain.app.scheduler.planner import next_run_after

                job.next_run_at = next_run_after(job.cron_expr, job.timezone, now)
            except Exception:
                job.next_run_at = None
    await session.flush()
    return job


async def async_set_scheduler_job_owner_mode(
    session: AsyncSession,
    identifier: str,
    *,
    owner_mode: str,
) -> SchedulerJob:
    job = await async_find_scheduler_job(session, identifier)
    if job is None:
        raise ValueError(f"Scheduler job '{identifier}' not found")
    normalized = normalize_owner_mode(owner_mode)
    if normalized != OWNER_MODE_SCHEDULER:
        raise ValueError("Legacy cron/mirror owner modes are retired; scheduler is the only writable owner mode")
    job.owner_mode = normalized
    await session.flush()
    return job


async def async_set_scheduler_job_load_shed(
    session: AsyncSession,
    identifier: str,
    *,
    load_shed_policy: dict[str, Any],
) -> SchedulerJob:
    job = await async_find_scheduler_job(session, identifier)
    if job is None:
        raise ValueError(f"Scheduler job '{identifier}' not found")
    job.load_shed_policy = load_shed_policy or {}
    await session.flush()
    return job
