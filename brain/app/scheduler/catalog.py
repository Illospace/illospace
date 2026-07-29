"""Scheduler catalog helpers.

The scheduler is the canonical owner for recurring work. Legacy ``cron_jobs``
rows are migrated by Alembic, but runtime sync never reads or writes that table.
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from brain.platform.db.models.scheduler import OWNER_MODE_SCHEDULER, SchedulerJob, SchedulerRun
from brain.app.scheduler.scheduler_failure_guard import (
    async_read_scheduler_failure_guard,
    scheduler_failure_guard_registry,
)
from brain.systems.failure_guard.core import (
    FailureGuardEvaluation,
    serialize_failure_guard,
)
from brain.app.scheduler.planner import next_run_after
from brain.app.scheduler.runtime import (
    normalize_owner_mode as _normalize_owner_mode,
    normalize_run_status,
)

DEFAULT_SCHEDULER_TIMEZONE = os.getenv("SCHEDULER_DEFAULT_TIMEZONE", "America/Toronto")
CATALOG_HANDLER_KIND = "scheduler_builtin"
CATALOG_RETIRE_REASON = "removed from scheduler catalog"

SCHEDULER_CATALOG: tuple[dict[str, Any], ...] = (
    {
        "job_key": "nightly_sleep",
        "family": "nightly_sleep",
        "program_key": "nightly_sleep",
        "handler_kind": CATALOG_HANDLER_KIND,
        "handler_ref": "brain.app.scheduler.programs:nightly_sleep",
        "cron_expr": "0 3 * * *",
        "default_payload": {
            "name": "Nightly Sleep",
            "description": "Full consolidation, learning, reflection, and journal cycle",
            "scheduler_split_steps": True,
            "night_budget": {
                "mode": "advisory",
                "planner": "brain.systems.learning.night_budget:build_night_budget_plan",
                "work_types": [
                    "memory_conflict_resolution",
                    "repo_summary_refresh",
                    "skill_eval",
                    "context_policy_eval",
                    "reflection_dream",
                ],
            },
        },
        "task_contract": {
            "memory_scope": {"visibility": "system"},
            "allowed_actions": ["scheduler.run"],
            "output_channel": "scheduler",
            "success_criteria": ["Nightly scheduler cycle settles successfully"],
        },
        "priority": 100,
        "max_concurrency": 1,
        "timeout_seconds": 14400,
        "retry_policy": {"max_attempts": 2, "backoff_seconds": 0},
    },
    {
        "job_key": "curiosity_cron",
        "family": "curiosity_cron",
        "program_key": "curiosity",
        "handler_kind": CATALOG_HANDLER_KIND,
        "handler_ref": "brain.app.scheduler.programs:curiosity",
        "cron_expr": "0 22 * * *",
        "default_payload": {
            "name": "Curiosity Engine",
            "description": "Daily curiosity reading and encoding cycle",
        },
        "task_contract": {
            "memory_scope": {"visibility": "system"},
            "allowed_actions": ["scheduler.run"],
            "output_channel": "scheduler",
            "success_criteria": ["Curiosity scheduler cycle settles successfully"],
        },
        "priority": 80,
        "max_concurrency": 1,
        "timeout_seconds": 7200,
        "retry_policy": {"max_attempts": 2, "backoff_seconds": 0},
    },
    {
        "job_key": "uwear_staging_promotion_pr",
        "family": "uwear_staging_promotion_pr",
        "program_key": "uwear_staging_promotion_pr",
        "handler_kind": CATALOG_HANDLER_KIND,
        "handler_ref": "brain.app.scheduler.programs:uwear_staging_promotion_pr",
        "cron_expr": "0 * * * *",
        "timezone": "UTC",
        "default_payload": {
            "name": "Uwear Staging Promotion PR",
            "description": (
                "Hourly staging-to-main promotion pull request reconciliation; "
                "wakes the promotion-readiness cycle when the promotable SHA "
                "pair moves past its last completed evaluation"
            ),
            "action_manifest": ["create_github_pull_request"],
        },
        "task_contract": {
            "memory_scope": {"visibility": "system"},
            "allowed_actions": ["scheduler.run", "create_github_pull_request"],
            "output_channel": "scheduler",
            "success_criteria": [
                "Each configured repository with staging ahead has one open promotion pull request",
                "The readiness cycle is woken when the promotable SHA pair has moved",
            ],
        },
        "priority": 90,
        "max_concurrency": 1,
        "timeout_seconds": 300,
        "retry_policy": {"max_attempts": 1, "backoff_seconds": 0},
        "misfire_policy": "skip",
    },
    {
        "job_key": "uwear_aws_health_scan",
        "family": "uwear_aws_health_scan",
        "program_key": "uwear_aws_health_scan",
        "handler_kind": CATALOG_HANDLER_KIND,
        "handler_ref": "brain.app.scheduler.programs:uwear_aws_health_scan",
        "cron_expr": "30 * * * *",
        "timezone": "UTC",
        "default_payload": {
            "name": "Uwear AWS Health Scan",
            "description": "Hourly read-only AWS production health scan",
        },
        "task_contract": {
            "memory_scope": {"visibility": "system"},
            "allowed_actions": ["scheduler.run"],
            "output_channel": "scheduler",
            "success_criteria": ["AWS health scan agent run completes successfully"],
        },
        "priority": 90,
        "max_concurrency": 1,
        "timeout_seconds": 900,
        "retry_policy": {"max_attempts": 2, "backoff_seconds": 120},
        "misfire_policy": "skip",
    },
    {
        "job_key": "illo_external_heartbeat",
        "family": "illo_external_heartbeat",
        "program_key": "illo_external_heartbeat",
        "handler_kind": CATALOG_HANDLER_KIND,
        "handler_ref": "brain.app.scheduler.programs:illo_external_heartbeat",
        "cron_expr": "*/5 * * * *",
        "timezone": "UTC",
        "default_payload": {
            "name": "Illo External Heartbeat",
            "description": "Publish minimal liveness state outside the Illo host",
        },
        "task_contract": {
            "memory_scope": {"visibility": "system"},
            "allowed_actions": ["scheduler.run"],
            "output_channel": "scheduler",
            "success_criteria": ["External heartbeat is published or cleanly skipped"],
        },
        "priority": 100,
        "max_concurrency": 1,
        "timeout_seconds": 60,
        "retry_policy": {"max_attempts": 2, "backoff_seconds": 30},
        "misfire_policy": "skip",
    },
    {
        "job_key": "knowledge_index_sync",
        "family": "knowledge_index_sync",
        "program_key": "knowledge_index_sync",
        "handler_kind": CATALOG_HANDLER_KIND,
        "handler_ref": "brain.app.scheduler.programs:knowledge_index_sync",
        "cron_expr": "*/30 * * * *",
        "timezone": "UTC",
        "default_payload": {
            "name": "Knowledge Index Sync",
            "description": "Incrementally index configured company knowledge sources",
        },
        "task_contract": {
            "memory_scope": {"visibility": "system"},
            "allowed_actions": ["scheduler.run"],
            "output_channel": "scheduler",
            "success_criteria": [
                "Every registered source reports ingestion, skip, failure, and truncation counts"
            ],
        },
        "priority": 80,
        "max_concurrency": 1,
        "timeout_seconds": 900,
        "retry_policy": {"max_attempts": 2, "backoff_seconds": 120},
        "misfire_policy": "skip",
    },
)


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    return slug or "job"


def _program_key_from_job(job_name: str, script_path: str | None, command: str | None) -> str:
    if script_path:
        return _slugify(Path(script_path).stem)
    if command:
        first = command.strip().split()[0]
        return _slugify(Path(first).stem)
    return _slugify(job_name)


def _default_task_contract(job_key: str) -> dict[str, Any]:
    return {
        "memory_scope": {"visibility": "system"},
        "allowed_actions": ["scheduler.run"],
        "output_channel": "scheduler",
        "success_criteria": [f"{job_key} recurring job settles successfully"],
    }


def normalize_owner_mode(owner_mode: str | None, *, default: str = OWNER_MODE_SCHEDULER) -> str:
    """Return a supported owner mode or raise."""
    return _normalize_owner_mode(owner_mode or default)


def _serialize_job(
    job: SchedulerJob,
    failure_guard: FailureGuardEvaluation,
) -> dict[str, Any]:
    return {
        "id": job.id,
        "job_key": job.job_key,
        "family": job.family,
        "program_key": job.program_key,
        "handler_kind": job.handler_kind,
        "handler_ref": job.handler_ref,
        "cron_expr": job.cron_expr,
        "timezone": job.timezone,
        "enabled": job.enabled,
        "owner_mode": job.owner_mode,
        "priority": job.priority,
        "max_concurrency": job.max_concurrency,
        "timeout_seconds": job.timeout_seconds,
        "retry_policy": job.retry_policy or {},
        "misfire_policy": job.misfire_policy,
        "load_shed_policy": job.load_shed_policy or {},
        "default_payload": job.default_payload or {},
        "target_binding_selector": job.target_binding_selector or {},
        "next_run_at": job.next_run_at.isoformat() if job.next_run_at else None,
        "pause_reason": job.pause_reason,
        "last_started_at": job.last_started_at.isoformat() if job.last_started_at else None,
        "last_finished_at": job.last_finished_at.isoformat() if job.last_finished_at else None,
        "failure_guard": serialize_failure_guard(failure_guard),
        "created_at": job.created_at.isoformat() if getattr(job, "created_at", None) else None,
    }


def _serialize_run(run: SchedulerRun) -> dict[str, Any]:
    return {
        "id": run.id,
        "job_id": run.job_id,
        "scheduled_for": run.scheduled_for.isoformat(),
        "window_start": run.window_start.isoformat(),
        "window_end": run.window_end.isoformat(),
        "status": normalize_run_status(run.status),
        "attempt": run.attempt,
        "idempotency_key": run.idempotency_key,
        "payload": run.payload or {},
        "result_summary": run.result_summary or {},
        "error_text": run.error_text,
        "lease_id": run.lease_id,
        "agent_run_id": run.agent_run_id,
        "trace_id": run.trace_id,
        "parent_run_id": run.parent_run_id,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "created_at": run.created_at.isoformat() if getattr(run, "created_at", None) else None,
    }


async def _async_upsert_scheduler_job_row(
    session: AsyncSession,
    *,
    job_key: str,
    family: str,
    program_key: str,
    handler_kind: str,
    handler_ref: str,
    cron_expr: str,
    timezone_name: str,
    enabled: bool,
    owner_mode: str,
    default_payload: dict[str, Any],
    pause_reason: str | None = None,
    priority: int = 100,
    max_concurrency: int = 1,
    timeout_seconds: int | None = None,
    retry_policy: dict[str, Any] | None = None,
    misfire_policy: str = "record",
    load_shed_policy: dict[str, Any] | None = None,
    target_binding_selector: dict[str, Any] | None = None,
    task_contract: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> SchedulerJob:
    now = now or datetime.now(timezone.utc)
    next_fire = None
    if enabled:
        try:
            next_fire = next_run_after(cron_expr, timezone_name, now)
        except Exception:
            next_fire = None
    job = await session.scalar(select(SchedulerJob).where(SchedulerJob.job_key == job_key))
    if job is None:
        job = SchedulerJob(
            job_key=job_key,
            family=family,
            program_key=program_key,
            handler_kind=handler_kind,
            handler_ref=handler_ref,
            cron_expr=cron_expr,
            timezone=timezone_name,
            enabled=enabled,
            owner_mode=owner_mode,
            priority=priority,
            max_concurrency=max_concurrency,
            timeout_seconds=timeout_seconds,
            retry_policy=retry_policy or {"max_attempts": 2, "backoff_seconds": 0},
            misfire_policy=misfire_policy,
            load_shed_policy=load_shed_policy or {},
            default_payload=default_payload,
            target_binding_selector=target_binding_selector or {},
            task_contract=task_contract or {},
            next_run_at=next_fire,
            pause_reason=pause_reason,
        )
        session.add(job)
        await session.flush()
        return job

    job.family = family
    job.program_key = program_key
    job.handler_kind = handler_kind
    job.handler_ref = handler_ref
    job.cron_expr = cron_expr
    job.timezone = timezone_name
    job.enabled = enabled
    job.owner_mode = owner_mode
    job.priority = priority
    job.max_concurrency = max_concurrency
    job.timeout_seconds = timeout_seconds
    job.retry_policy = retry_policy or {"max_attempts": 2, "backoff_seconds": 0}
    job.misfire_policy = misfire_policy
    job.load_shed_policy = load_shed_policy or {}
    job.default_payload = default_payload
    job.target_binding_selector = target_binding_selector or {}
    job.task_contract = task_contract or {}
    job.next_run_at = next_fire
    job.pause_reason = pause_reason
    await session.flush()
    return job


async def async_upsert_scheduler_job(
    session: AsyncSession,
    *,
    job_key: str,
    cron_expr: str,
    handler_ref: str,
    family: str | None = None,
    program_key: str | None = None,
    handler_kind: str = "command",
    enabled: bool = True,
    owner_mode: str = OWNER_MODE_SCHEDULER,
    timezone_name: str = DEFAULT_SCHEDULER_TIMEZONE,
    default_payload: dict[str, Any] | None = None,
    task_contract: dict[str, Any] | None = None,
    pause_reason: str | None = None,
    priority: int = 100,
    max_concurrency: int = 1,
    timeout_seconds: int | None = None,
    retry_policy: dict[str, Any] | None = None,
    misfire_policy: str = "record",
    load_shed_policy: dict[str, Any] | None = None,
    target_binding_selector: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> SchedulerJob:
    """Create or update a scheduler-owned recurring job using an async session."""
    normalized_key = _slugify(job_key)
    payload = dict(default_payload or {})
    payload.setdefault("name", job_key)
    contract = dict(task_contract or {})
    if not contract:
        contract = _default_task_contract(normalized_key)
    resolved_program_key = program_key or _program_key_from_job(job_key, None, handler_ref)
    return await _async_upsert_scheduler_job_row(
        session,
        job_key=normalized_key,
        family=_slugify(family or normalized_key),
        program_key=_slugify(resolved_program_key),
        handler_kind=handler_kind,
        handler_ref=str(handler_ref),
        cron_expr=str(cron_expr),
        timezone_name=timezone_name,
        enabled=enabled,
        owner_mode=normalize_owner_mode(owner_mode),
        default_payload=payload,
        pause_reason=pause_reason if not enabled else None,
        priority=priority,
        max_concurrency=max_concurrency,
        timeout_seconds=timeout_seconds,
        retry_policy=retry_policy,
        misfire_policy=misfire_policy,
        load_shed_policy=load_shed_policy,
        target_binding_selector=target_binding_selector,
        task_contract=contract,
        now=now,
    )


async def async_retire_scheduler_job(
    session: AsyncSession,
    identifier: str,
    *,
    reason: str,
) -> SchedulerJob | None:
    """Soft-disable a scheduler job while preserving history using an async session."""
    from brain.app.scheduler.runtime import async_find_scheduler_job

    job = await async_find_scheduler_job(session, identifier)
    if job is None:
        return None
    job.enabled = False
    job.pause_reason = reason
    await session.flush()
    return job


async def async_sync_scheduler_catalog(
    session: AsyncSession,
    *,
    owner_mode: str = OWNER_MODE_SCHEDULER,
    timezone_name: str = DEFAULT_SCHEDULER_TIMEZONE,
    now: datetime | None = None,
    job_keys: tuple[str, ...] | None = None,
) -> dict[str, int]:
    """Reconcile built-in jobs and soft-retire removed entries on a full sync."""
    now = now or datetime.now(timezone.utc)
    owner_mode = normalize_owner_mode(owner_mode)
    if owner_mode != OWNER_MODE_SCHEDULER:
        raise ValueError("Legacy cron/mirror owner modes are retired; use owner_mode='scheduler'.")

    selected_keys = {_slugify(key) for key in job_keys or ()}
    catalog_job_keys = {
        _slugify(str(definition["job_key"])) for definition in SCHEDULER_CATALOG
    }
    upserted = 0
    for definition in SCHEDULER_CATALOG:
        aliases = {
            _slugify(str(definition["job_key"])),
            _slugify(str(definition["family"])),
            _slugify(str(definition["program_key"])),
        }
        if selected_keys and not (aliases & selected_keys):
            continue
        await async_upsert_scheduler_job(
            session,
            job_key=str(definition["job_key"]),
            family=str(definition["family"]),
            program_key=str(definition["program_key"]),
            handler_kind=str(definition["handler_kind"]),
            handler_ref=str(definition["handler_ref"]),
            cron_expr=str(definition["cron_expr"]),
            owner_mode=owner_mode,
            timezone_name=str(definition.get("timezone") or timezone_name),
            default_payload=dict(definition.get("default_payload") or {}),
            task_contract=dict(definition.get("task_contract") or {}),
            priority=int(definition.get("priority") or 100),
            max_concurrency=int(definition.get("max_concurrency") or 1),
            timeout_seconds=definition.get("timeout_seconds"),
            retry_policy=dict(definition.get("retry_policy") or {}),
            misfire_policy=str(definition.get("misfire_policy") or "record"),
            load_shed_policy=dict(definition.get("load_shed_policy") or {}),
            target_binding_selector=dict(definition.get("target_binding_selector") or {}),
            now=now,
        )
        upserted += 1

    retired = 0
    if not selected_keys:
        catalog_jobs = await session.scalars(
            select(SchedulerJob).where(
                SchedulerJob.owner_mode == owner_mode,
                SchedulerJob.handler_kind == CATALOG_HANDLER_KIND,
            )
        )
        for job in catalog_jobs.all():
            if _slugify(job.job_key) in catalog_job_keys:
                continue
            if not job.enabled and job.pause_reason == CATALOG_RETIRE_REASON:
                continue
            retired_job = await async_retire_scheduler_job(
                session,
                job.job_key,
                reason=CATALOG_RETIRE_REASON,
            )
            if retired_job is not None:
                retired += 1
    await session.flush()
    return {"upserted": upserted, "retired": retired}


async def async_list_scheduler_jobs(
    session: AsyncSession,
    *,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    now = now or datetime.now(timezone.utc)
    registry = scheduler_failure_guard_registry()
    result = await session.scalars(
        select(SchedulerJob).order_by(SchedulerJob.family.asc(), SchedulerJob.id.asc())
    )
    jobs = []
    for job in result.all():
        failure_guard = await async_read_scheduler_failure_guard(
            session,
            job,
            now=now,
            registry=registry,
        )
        jobs.append(_serialize_job(job, failure_guard))
    return jobs


async def async_list_scheduler_runs(
    session: AsyncSession,
    *,
    limit: int = 50,
) -> list[dict[str, Any]]:
    result = await session.scalars(
        select(SchedulerRun)
        .order_by(SchedulerRun.scheduled_for.desc(), SchedulerRun.id.desc())
        .limit(limit)
    )
    return [_serialize_run(run) for run in result.all()]
