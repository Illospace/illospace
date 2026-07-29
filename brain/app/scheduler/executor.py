"""Scheduler executor loop and control helpers."""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import re
import shlex
import socket
import subprocess
from datetime import date, datetime, timezone
from importlib import import_module
from pathlib import Path
from typing import Any, Callable, Sequence
from urllib.parse import quote
from zoneinfo import ZoneInfo

from brain.kernel.common.time import ensure_utc

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from brain.contracts.scheduler_handoff import (
    AGENT_RUN_COMPLETION_MODE,
    DetachedAgentRunHandoff,
    DetachedAgentRunHandoffError,
    parse_detached_agent_run_handoff,
)
from brain.platform.async_io import run_blocking, run_subprocess
from brain.platform.db.models.scheduler import (
    OWNER_MODE_SCHEDULER,
    SchedulerJob,
    SchedulerLease,
    SchedulerRun,
    SchedulerRunStep,
)
from brain.systems.cortex.thread_links import public_app_base_url
from brain.systems.slack.client import slack_web_client_from_runtime
from brain.app.scheduler.catalog import normalize_owner_mode
from brain.app.scheduler.contracts import validate_scheduler_run_contract
from brain.app.scheduler.detached_agent_runs import (
    async_mark_detached_run_dispatched,
    async_reconcile_detached_runs,
)
from brain.app.scheduler.scheduler_failure_guard import (
    async_record_scheduler_job_failure,
    async_reset_scheduler_job_failure_guard,
)
from brain.systems.failure_guard.core import (
    FailureGuardEvaluation,
    serialize_failure_guard,
)
from brain.systems.failure_guard.slack_delivery import (
    FailureAlertPresentation,
    FailureAlertSubject,
    SlackFailureAlertPolicy,
    async_deliver_failure_alert,
)
from brain.app.scheduler.planner import async_materialize_due_runs
from brain.app.scheduler.programs import (
    NIGHTLY_SLEEP_STEP_KEYS,
    WRAPPER_STEP_KEY,
    build_scheduler_step_plan,
    nightly_commands,
    nightly_commands_for_step,
    scheduler_program_completion_mode,
)
from brain.app.scheduler.runtime import (
    LEASE_TTL_SECONDS,
    RUN_STATUS_CLAIMED,
    RUN_STATUS_PAUSED,
    RUN_STATUS_RECORDED,
    RUN_STATUS_RETRYABLE,
    RUN_STATUS_RUNNING,
    RUN_STATUS_SETTLED_FAILURE,
    RUN_STATUS_SETTLED_SUCCESS,
    async_claim_next_due_run,
    async_claim_run,
    async_ensure_run_steps,
    async_finish_run,
    async_find_scheduler_job,
    async_heartbeat_lease,
    async_retry_run,
    async_set_scheduler_job_load_shed as async_set_scheduler_job_load_shed_state,
    async_set_scheduler_job_owner_mode as async_set_scheduler_job_owner_mode_state,
    async_set_scheduler_job_pause_state,
    async_update_run_step,
    normalize_retry_policy,
    retry_available,
    retry_available_at,
    trace_id_for_run_id,
    trace_id_for_scheduler_run_id,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
Runner = Callable[..., Any]
logger = logging.getLogger(__name__)


def _positive_float_env(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError:
        return default
    return value if value > 0 else default


DEFAULT_DRAIN_ADMISSION_BUDGET_SECONDS = _positive_float_env(
    "SCHEDULER_DRAIN_ADMISSION_BUDGET_SECONDS",
    30.0,
)

_FINAL_RUN_STATUSES = {
    RUN_STATUS_SETTLED_SUCCESS,
    RUN_STATUS_SETTLED_FAILURE,
    "blocked",
    "cancelled",
    "failed",
    "skipped",
    "superseded",
}
_SUCCESS_HANDLER_STATUSES = {"ok", "recorded", "skipped", "success", "succeeded", "completed"}
_BLOCKED_HANDLER_STATUSES = {"blocked"}


def _utcnow(now: datetime | None = None) -> datetime:
    return ensure_utc(now)


async def _async_run_command(
    command: Sequence[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout_seconds: int | None = None,
) -> subprocess.CompletedProcess[str]:
    return await run_subprocess(
        list(command),
        cwd=str(cwd or REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout_seconds,
    )


def _job_identity(job: SchedulerJob) -> str:
    return " ".join(
        [
            job.job_key.lower(),
            job.family.lower(),
            job.program_key.lower(),
            (job.handler_ref or "").lower(),
            ((job.default_payload or {}).get("name") or "").lower(),
        ]
    )


def _safe_timezone_name(timezone_name: str) -> ZoneInfo | None:
    try:
        return ZoneInfo(timezone_name)
    except Exception:
        return None


def _target_date_for_run(job: SchedulerJob, run: SchedulerRun) -> date:
    tz = _safe_timezone_name(job.timezone)
    if tz is None:
        return run.scheduled_for.astimezone(timezone.utc).date()
    return run.scheduled_for.astimezone(tz).date()


def _shell_env(
    job: SchedulerJob,
    run: SchedulerRun,
    step_key: str,
    *,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("PYTHONUNBUFFERED", "1")
    env["SCHEDULER_JOB_KEY"] = job.job_key
    env["SCHEDULER_FAMILY"] = job.family
    env["SCHEDULER_RUN_ID"] = str(run.id)
    env["SCHEDULER_STEP_KEY"] = step_key
    env["SCHEDULER_SCHEDULED_FOR"] = run.scheduled_for.isoformat()
    env["SCHEDULER_TARGET_DATE"] = _target_date_for_run(job, run).isoformat()
    if extra:
        env.update(extra)
    return env


def _tail(text: str | None, limit: int = 2000) -> str | None:
    if text is None:
        return None
    if len(text) <= limit:
        return text
    return text[-limit:]


def _command_summary(proc: Any) -> dict[str, Any]:
    summary = {
        "returncode": int(getattr(proc, "returncode", 1)),
        "stdout_tail": _tail(getattr(proc, "stdout", None)),
        "stderr_tail": _tail(getattr(proc, "stderr", None)),
    }
    exception = _command_exception(
        stdout=getattr(proc, "stdout", None),
        stderr=getattr(proc, "stderr", None),
    )
    if exception:
        summary["exception"] = exception
    return summary


def _json_objects(text_value: str | None) -> list[dict[str, Any]]:
    objects: list[dict[str, Any]] = []
    for line in reversed(str(text_value or "").splitlines()):
        try:
            value = json.loads(line.strip())
        except (TypeError, ValueError):
            continue
        if isinstance(value, dict):
            objects.append(value)
    return objects


def _failed_result(payload: dict[str, Any]) -> dict[str, Any] | None:
    results = payload.get("results")
    if isinstance(results, list):
        for item in reversed(results):
            if isinstance(item, dict) and str(item.get("status") or "").lower() in {
                "error",
                "failed",
            }:
                nested = _failed_result(item)
                return nested or item
    if str(payload.get("status") or "").lower() in {"error", "failed"}:
        return payload
    if payload.get("error") and payload.get("ok") is False:
        return payload
    return None


_TRACEBACK_EXCEPTION_LINE = re.compile(
    r"^(?P<type>[A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception)):\s*(?P<message>.+)$"
)


def _command_exception(*, stdout: str | None, stderr: str | None) -> dict[str, str] | None:
    for payload in _json_objects(stdout):
        failure = _failed_result(payload)
        if failure is None:
            continue
        message = str(failure.get("error") or "").strip()
        if not message:
            continue
        exception_type = str(failure.get("exception_type") or "").strip()
        return {
            **({"type": exception_type} if exception_type else {}),
            "message": message,
        }
    for line in reversed(str(stderr or "").splitlines()):
        match = _TRACEBACK_EXCEPTION_LINE.match(line.strip())
        if match:
            return {"type": match.group("type"), "message": match.group("message")}
    return None


def _command_failure_error_text(summary: dict[str, Any]) -> str:
    exception = summary.get("exception")
    if isinstance(exception, dict):
        message = str(exception.get("message") or "").strip()
        exception_type = str(exception.get("type") or "").strip()
        if message:
            return f"{exception_type}: {message}" if exception_type else message
    return f"Command exited with status {int(summary.get('returncode') or 1)}"


def _python_one_liner(code: str) -> list[str]:
    return ["python3", "-c", code]


def _is_callable_handler(job: SchedulerJob) -> bool:
    if job.handler_kind == "python_callable":
        return True
    return ":" in (job.handler_ref or "")


def _resolve_handler(handler_ref: str) -> Callable[[dict[str, Any]], Any]:
    module_name, separator, attr_name = handler_ref.partition(":")
    if not separator:
        module_name, separator, attr_name = handler_ref.rpartition(".")
    if not module_name or not attr_name:
        raise ValueError(f"Invalid handler reference: {handler_ref}")

    module = import_module(module_name)
    handler = getattr(module, attr_name)
    if not callable(handler):
        raise TypeError(f"Handler reference is not callable: {handler_ref}")
    return handler


def _invoke_handler(handler: Callable[..., Any], payload: dict[str, Any], *, now: datetime) -> Any:
    signature = inspect.signature(handler)
    kwargs: dict[str, Any] = {}
    if "now" in signature.parameters or any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in signature.parameters.values()
    ):
        kwargs["now"] = now
    return handler(payload, **kwargs)


def _normalize_handler_result(result: Any) -> dict[str, Any]:
    if result is None:
        return {"status": "recorded"}
    if isinstance(result, dict):
        return result
    return {"status": "recorded", "value": result}


def _final_status_from_handler_result(result: dict[str, Any]) -> str:
    handler_status = str(result.get("status") or "recorded").lower()
    if handler_status in _BLOCKED_HANDLER_STATUSES:
        return "blocked"
    if handler_status in _SUCCESS_HANDLER_STATUSES:
        return RUN_STATUS_SETTLED_SUCCESS
    if handler_status in {"failed", "error"}:
        return RUN_STATUS_SETTLED_FAILURE
    return RUN_STATUS_SETTLED_SUCCESS


async def _async_block_invalid_contract_run(
    session: AsyncSession,
    job: SchedulerJob,
    run: SchedulerRun,
    *,
    contract: dict[str, Any],
    contract_errors: list[str],
    now: datetime,
) -> SchedulerRun:
    run.task_contract = contract
    await async_finish_run(
        session,
        run,
        job=job,
        status="blocked",
        result_summary={
            "reason": "contract_invalid",
            "contract_errors": contract_errors,
            "task_contract": contract,
        },
        error_text="; ".join(contract_errors),
        now=now,
    )
    return run


def _nightly_wrapper_commands(target_date: date, *, split_steps: bool) -> list[list[str]]:
    if split_steps:
        return [
            _python_one_liner(
                f"print('nightly scheduler wrapper initialized for {target_date.isoformat()}')"
            )
        ]

    return nightly_commands(target_date)


def _nightly_step_commands(step_key: str, target_date: date) -> list[list[str]]:
    return nightly_commands_for_step(step_key, target_date)


def _command_to_argv(command: Any) -> list[str]:
    if isinstance(command, (list, tuple)):
        return [str(part) for part in command]
    if isinstance(command, str) and command.strip():
        return shlex.split(command)
    return []


def _builtin_commands(job: SchedulerJob, run: SchedulerRun, step_key: str) -> list[list[str]]:
    identity = _job_identity(job)
    target_date = _target_date_for_run(job, run)
    split_steps = bool((job.default_payload or {}).get("scheduler_split_steps"))

    if "curiosity" in identity:
        return [["python3", "-m", "brain.jobs.pipelines.curiosity"]]

    if "nightly" in identity or "sleep" in identity:
        if step_key == WRAPPER_STEP_KEY:
            return _nightly_wrapper_commands(target_date, split_steps=split_steps)
        if step_key in NIGHTLY_SLEEP_STEP_KEYS:
            return _nightly_step_commands(step_key, target_date)

    return []


def _commands_for_step(
    job: SchedulerJob,
    run: SchedulerRun,
    step_key: str,
    step_spec: dict[str, Any],
) -> list[list[str]]:
    commands = step_spec.get("commands")
    if isinstance(commands, list) and commands:
        argv_commands = [_command_to_argv(command) for command in commands]
        return [command for command in argv_commands if command]

    command = step_spec.get("command")
    argv = _command_to_argv(command)
    if argv:
        return [argv]

    payload = step_spec.get("payload")
    if isinstance(payload, dict):
        commands = payload.get("commands")
        if isinstance(commands, list) and commands:
            argv_commands = [_command_to_argv(command) for command in commands]
            return [command for command in argv_commands if command]
        command = payload.get("command")
        argv = _command_to_argv(command)
        if argv:
            return [argv]

    builtin = _builtin_commands(job, run, step_key)
    if builtin:
        return builtin

    handler_ref = step_spec.get("handler_ref") or job.handler_ref
    argv = _command_to_argv(handler_ref)
    if argv:
        return [argv]

    return []


def _call_sync_runner_with_fallback(
    runner: Runner,
    command: Sequence[str],
    *,
    env: dict[str, str],
    timeout_seconds: int | None,
) -> Any:
    try:
        return runner(command, cwd=REPO_ROOT, env=env, timeout_seconds=timeout_seconds)
    except TypeError:
        return runner(command, cwd=REPO_ROOT)


async def _call_runner(
    runner: Runner,
    command: Sequence[str],
    *,
    env: dict[str, str],
    timeout_seconds: int | None,
) -> Any:
    if inspect.iscoroutinefunction(runner):
        try:
            return await runner(command, cwd=REPO_ROOT, env=env, timeout_seconds=timeout_seconds)
        except TypeError:
            return await runner(command, cwd=REPO_ROOT)

    result = await run_blocking(
        _call_sync_runner_with_fallback,
        runner,
        command,
        env=env,
        timeout_seconds=timeout_seconds,
    )
    if inspect.isawaitable(result):
        return await result
    return result


def _retryable_failure_summary(
    job: SchedulerJob,
    run: SchedulerRun,
    *,
    base_summary: dict[str, Any],
    now: datetime,
) -> tuple[str, dict[str, Any]]:
    retry_policy = normalize_retry_policy(job.retry_policy)
    next_retry_at = retry_available_at(job, run, now=now)
    retry_summary = {
        **base_summary,
        "retry_policy": retry_policy,
        "retry_exhausted": not retry_available(job, run),
        "next_retry_at": next_retry_at.isoformat() if next_retry_at else None,
    }
    if retry_available(job, run):
        return RUN_STATUS_RETRYABLE, retry_summary
    return RUN_STATUS_SETTLED_FAILURE, retry_summary


async def async_deliver_scheduler_failure_alert(
    *,
    job_key: str,
    run_id: int,
    evaluation: FailureGuardEvaluation,
    error_text: str,
) -> None:
    """Preserve the scheduler card contract through the shared delivery path."""
    crossed_edges = evaluation.crossed_edges
    if not crossed_edges:
        raise ValueError(
            "scheduler failure alert requires at least one crossed edge"
        )
    if len(crossed_edges) == 1:
        presentation = FailureAlertPresentation(
            title=crossed_edges[0].alert_title,
            summary=crossed_edges[0].alert_summary,
        )
    else:
        presentation = FailureAlertPresentation(
            title="Scheduler job failure guard alert",
            summary="\n".join(
                (
                    "Triggers crossed:",
                    *(
                        f"- {edge.kind}: {edge.alert_summary}"
                        for edge in crossed_edges
                    ),
                )
            ),
        )

    await async_deliver_failure_alert(
        policy=SlackFailureAlertPolicy(
            provide_client=slack_web_client_from_runtime,
            requested_by="scheduler_failure_alert",
            reason="Deliver a repeated scheduler job failure alert to the team.",
            channel=(
                os.getenv("ILLO_SCHEDULER_FAILURE_ALERT_CHANNEL", "").strip()
                or "#alerts"
            ),
            unknown_error_text="Unknown scheduler failure",
        ),
        subject=FailureAlertSubject(
            identity_label="Job key",
            identity=job_key,
            url_label="Job",
            url=(
                f"{public_app_base_url()}/api/system/scheduler"
                f"?job_key={quote(job_key, safe='')}&run_id={run_id}"
            ),
            link_label="open scheduler state",
        ),
        presentation=presentation,
        error_text=error_text,
    )


async def _async_apply_failure_guard(
    session: AsyncSession,
    job: SchedulerJob,
    run: SchedulerRun,
    *,
    failure_key: str,
    error_text: str,
    now: datetime,
) -> None:
    guard = await async_record_scheduler_job_failure(
        session,
        job,
        failure_identity=f"{failure_key}\n{error_text}",
        error_text=error_text,
        now=now,
    )
    run.result_summary = {
        **(run.result_summary or {}),
        "failure_guard": serialize_failure_guard(guard),
    }
    crossed_edges = guard.crossed_edges
    if crossed_edges:
        alert_title = (
            crossed_edges[0].alert_title
            if len(crossed_edges) == 1
            else "Scheduler job combined failure guard"
        )
        logger.error(
            "%s alert: job_key=%s run_id=%s crossed_triggers=%s "
            "failure_signature=%s error=%s",
            alert_title,
            job.job_key,
            run.id,
            ",".join(str(edge.kind) for edge in crossed_edges),
            guard.failure_signature,
            error_text,
        )
        try:
            await async_deliver_scheduler_failure_alert(
                job_key=job.job_key,
                run_id=run.id,
                evaluation=guard,
                error_text=error_text,
            )
        except Exception:
            logger.exception(
                "Scheduler job failure-guard Slack delivery failed: "
                "job_key=%s run_id=%s",
                job.job_key,
                run.id,
            )
    await session.flush()


async def _async_run_step(
    session: AsyncSession,
    run: SchedulerRun,
    job: SchedulerJob,
    step: SchedulerRunStep,
    step_spec: dict[str, Any],
    *,
    runner: Runner,
    now: datetime,
    resume: bool,
) -> dict[str, Any]:
    if step.status == RUN_STATUS_SETTLED_SUCCESS and resume:
        return {"ok": True, "step_key": step.step_key, "skipped": True, "results": []}

    if step.status == RUN_STATUS_RECORDED:
        step.attempt = max(1, int(step.attempt or 1))
    else:
        step.attempt = int(step.attempt or 1) + 1

    await async_update_run_step(session, step, status=RUN_STATUS_RUNNING, started_at=step.started_at or now)
    if run.lease_id is not None:
        await async_heartbeat_lease(
            session,
            run.lease_id,
            lease_ttl_seconds=max(60, int(job.timeout_seconds or LEASE_TTL_SECONDS)),
            now=now,
        )

    commands = _commands_for_step(job, run, step.step_key, step_spec)
    if not commands:
        error_text = f"No command found for scheduler step '{step.step_key}'"
        await async_update_run_step(
            session,
            step,
            status=RUN_STATUS_RETRYABLE,
            finished_at=now,
            result_summary={"results": []},
            error_text=error_text,
        )
        return {"ok": False, "step_key": step.step_key, "results": [], "error": error_text}

    env = _shell_env(job, run, step.step_key)
    results: list[dict[str, Any]] = []
    completion_mode = str(
        step_spec.get("completion_mode") or scheduler_program_completion_mode(job)
    ).strip().lower()
    detached_handoff: DetachedAgentRunHandoff | None = None
    for command in commands:
        try:
            proc = await _call_runner(
                runner,
                command,
                env=env,
                timeout_seconds=job.timeout_seconds,
            )
        except Exception as exc:
            error_text = f"{type(exc).__name__}: {exc}"
            results.append(
                {
                    "command": list(command),
                    "exception_type": type(exc).__name__,
                    "error": str(exc),
                }
            )
            await async_update_run_step(
                session,
                step,
                status=RUN_STATUS_RETRYABLE,
                finished_at=now,
                result_summary={"results": results},
                error_text=error_text,
            )
            return {
                "ok": False,
                "step_key": step.step_key,
                "results": results,
                "error": error_text,
            }
        summary = {"command": list(command), **_command_summary(proc)}
        results.append(summary)
        if int(getattr(proc, "returncode", 1)) != 0:
            error_text = _command_failure_error_text(summary)
            await async_update_run_step(
                session,
                step,
                status=RUN_STATUS_RETRYABLE,
                finished_at=now,
                result_summary={"results": results},
                error_text=error_text,
            )
            return {"ok": False, "step_key": step.step_key, "results": results, "error": error_text}
        if completion_mode == AGENT_RUN_COMPLETION_MODE:
            try:
                detached_handoff = parse_detached_agent_run_handoff(
                    getattr(proc, "stdout", None)
                )
            except DetachedAgentRunHandoffError as exc:
                error_text = f"{type(exc).__name__}: {exc}"
                await async_update_run_step(
                    session,
                    step,
                    status=RUN_STATUS_RETRYABLE,
                    finished_at=now,
                    result_summary={"results": results},
                    error_text=error_text,
                )
                return {
                    "ok": False,
                    "step_key": step.step_key,
                    "results": results,
                    "error": error_text,
                }
            summary["scheduler_agent_run_id"] = detached_handoff.agent_run_id

    if detached_handoff is not None:
        return {
            "ok": True,
            "step_key": step.step_key,
            "results": results,
            "agent_run_id": detached_handoff.agent_run_id,
            "_detached_handoff": detached_handoff,
        }
    await async_update_run_step(
        session,
        step,
        status=RUN_STATUS_SETTLED_SUCCESS,
        finished_at=now,
        result_summary={"results": results},
        error_text=None,
    )
    return {"ok": True, "step_key": step.step_key, "results": results}


async def async_run_scheduler_run(
    session: AsyncSession,
    run_id: int,
    *,
    owner_id: str | None = None,
    runner: Runner = _async_run_command,
    resume: bool = True,
    now: datetime | None = None,
) -> SchedulerRun:
    now = _utcnow(now)
    run = await session.get(SchedulerRun, run_id)
    if run is None:
        raise ValueError(f"Scheduler run {run_id} not found")
    if run.status == RUN_STATUS_PAUSED:
        raise ValueError(f"Scheduler run {run_id} is paused")
    if run.status == RUN_STATUS_SETTLED_SUCCESS:
        return run

    job = await session.get(SchedulerJob, run.job_id)
    if job is None:
        raise ValueError(f"Scheduler job {run.job_id} not found")

    contract, contract_errors = validate_scheduler_run_contract(job, run)
    if contract_errors:
        return await _async_block_invalid_contract_run(
            session,
            job,
            run,
            contract=contract,
            contract_errors=contract_errors,
            now=now,
        )
    run.task_contract = contract

    lease_ttl_seconds = max(60, int(job.timeout_seconds or LEASE_TTL_SECONDS))
    if run.status not in {RUN_STATUS_CLAIMED, RUN_STATUS_RUNNING} or run.lease_id is None:
        run, _lease = await async_claim_run(
            session,
            run.id,
            owner_id=owner_id,
            lease_ttl_seconds=lease_ttl_seconds,
            now=now,
        )
    else:
        lease = await async_heartbeat_lease(
            session,
            run.lease_id,
            lease_ttl_seconds=lease_ttl_seconds,
            now=now,
        )
        if lease is None:
            run, _lease = await async_claim_run(
                session,
                run.id,
                owner_id=owner_id,
                lease_ttl_seconds=lease_ttl_seconds,
                now=now,
            )

    step_plan = build_scheduler_step_plan(job)
    steps = await async_ensure_run_steps(session, run, step_plan)
    step_by_key = {step.step_key: step for step in steps}

    run.status = RUN_STATUS_RUNNING
    if run.started_at is None:
        run.started_at = now
    job.last_started_at = now
    await session.flush()

    step_results: list[dict[str, Any]] = []
    detached_dispatch: tuple[
        SchedulerRunStep,
        DetachedAgentRunHandoff,
        dict[str, Any],
    ] | None = None
    for step_spec in sorted(step_plan, key=lambda item: int(item.get("sequence_no", 0))):
        step_key = str(step_spec["step_key"])
        step = step_by_key[step_key]
        result = await _async_run_step(
            session,
            run,
            job,
            step,
            step_spec,
            runner=runner,
            now=now,
            resume=resume,
        )
        handoff = result.pop("_detached_handoff", None)
        step_results.append(result)
        if not result["ok"]:
            failure_status, failure_summary = _retryable_failure_summary(
                job,
                run,
                base_summary={
                    "failed_step": step_key,
                    "resume_available": True,
                    "steps": step_results,
                },
                now=now,
            )
            await async_finish_run(
                session,
                run,
                job=job,
                status=failure_status,
                result_summary=failure_summary,
                error_text=result["error"],
                now=now,
            )
            await _async_apply_failure_guard(
                session,
                job,
                run,
                failure_key=step_key,
                error_text=result["error"],
                now=now,
            )
            return run
        if handoff is not None:
            detached_dispatch = (step, handoff, result)

    if detached_dispatch is not None:
        detached_step, handoff, step_result = detached_dispatch
        return await async_mark_detached_run_dispatched(
            session,
            run,
            detached_step,
            handoff=handoff,
            step_result=step_result,
            step_results=step_results,
            now=now,
        )

    await async_finish_run(
        session,
        run,
        job=job,
        status=RUN_STATUS_SETTLED_SUCCESS,
        result_summary={"steps": step_results},
        error_text=None,
        now=now,
    )
    await async_reset_scheduler_job_failure_guard(session, job, now=now)
    return run


async def async_claim_scheduler_run(
    session: AsyncSession,
    run_id: int,
    *,
    owner_id: str,
    owner_host: str | None = None,
    owner_pid: int | None = None,
    lease_seconds: int = 60,
    now: datetime | None = None,
) -> SchedulerLease | None:
    """Claim a scheduler run for handler-based execution using an async session."""
    now = _utcnow(now)
    run = await session.get(SchedulerRun, run_id)
    if run is None:
        return None
    if run.status in _FINAL_RUN_STATUSES:
        return await session.get(SchedulerLease, run.lease_id) if run.lease_id else None

    if run.lease_id:
        existing = await session.get(SchedulerLease, run.lease_id)
        if existing is not None and existing.released_at is None and existing.expires_at > now:
            return existing

    _run, lease = await async_claim_run(
        session,
        run.id,
        owner_id=owner_id,
        lease_ttl_seconds=lease_seconds,
        now=now,
    )
    if owner_host is not None:
        lease.owner_host = owner_host
    if owner_pid is not None:
        lease.owner_pid = owner_pid
    await session.flush()
    return lease


async def async_release_scheduler_lease(
    session: AsyncSession,
    lease: SchedulerLease | None,
    *,
    reason: str,
    now: datetime | None = None,
) -> SchedulerLease | None:
    """Release a scheduler lease while preserving run.lease_id for audit trails."""
    if lease is None:
        return None
    now = _utcnow(now)
    if lease.released_at is None:
        lease.released_at = now
        lease.release_reason = reason
        await session.flush()
    return lease


async def async_execute_scheduler_run(
    session: AsyncSession,
    run_id: int,
    *,
    owner_id: str,
    owner_host: str | None = None,
    owner_pid: int | None = None,
    lease_seconds: int = 60,
    now: datetime | None = None,
) -> SchedulerRun | None:
    """Execute callable scheduler runs without bypassing the scheduler control plane."""
    now = _utcnow(now)
    run = await session.get(SchedulerRun, run_id)
    if run is None:
        return None
    if run.status in _FINAL_RUN_STATUSES:
        return run
    if run.status == RUN_STATUS_PAUSED:
        raise ValueError(f"Scheduler run {run_id} is paused")

    job = await session.get(SchedulerJob, run.job_id)
    if job is None:
        run.status = RUN_STATUS_SETTLED_FAILURE
        run.error_text = "Scheduler job not found"
        run.finished_at = now
        await session.flush()
        return run

    contract, contract_errors = validate_scheduler_run_contract(job, run)
    if contract_errors:
        return await _async_block_invalid_contract_run(
            session,
            job,
            run,
            contract=contract,
            contract_errors=contract_errors,
            now=now,
        )
    run.task_contract = contract

    if not _is_callable_handler(job):
        return await async_run_scheduler_run(session, run_id, owner_id=owner_id, now=now)

    lease = await async_claim_scheduler_run(
        session,
        run.id,
        owner_id=owner_id,
        owner_host=owner_host or socket.gethostname(),
        owner_pid=owner_pid or os.getpid(),
        lease_seconds=max(lease_seconds, int(job.timeout_seconds or lease_seconds)),
        now=now,
    )
    if lease is None:
        return run

    step = await session.scalar(
        select(SchedulerRunStep).where(
            SchedulerRunStep.run_id == run.id,
            SchedulerRunStep.step_key == "handler_execute",
        )
    )
    if step is None:
        step = SchedulerRunStep(
            run_id=run.id,
            step_key="handler_execute",
            sequence_no=1,
            status=RUN_STATUS_RUNNING,
            attempt=1,
            started_at=now,
            agent_run_id=run.agent_run_id,
            trace_id=run.trace_id or trace_id_for_scheduler_run_id(run.id),
        )
        session.add(step)
    else:
        step.status = RUN_STATUS_RUNNING
        step.started_at = step.started_at or now
        step.attempt = max(1, int(step.attempt or 1))
    await session.flush()

    run.status = "executing"
    if not run.trace_id:
        run.trace_id = trace_id_for_scheduler_run_id(run.id)
    run.started_at = run.started_at or now
    job.last_started_at = now

    payload = run.payload or job.default_payload or {}
    try:
        handler = _resolve_handler(job.handler_ref)
        raw_result = _invoke_handler(handler, payload, now=now)
        normalized_result = _normalize_handler_result(raw_result)
        final_status = _final_status_from_handler_result(normalized_result)
        handler_failed = final_status == RUN_STATUS_SETTLED_FAILURE
        handler_error_text = str(
            normalized_result.get("error")
            or normalized_result.get("reason")
            or "handler failed"
        )
        run.result_summary = {
            "handler_ref": job.handler_ref,
            "handler_status": normalized_result.get("status"),
            "handler_result": normalized_result,
            "execution": {
                "owner_id": owner_id,
                "owner_host": lease.owner_host,
                "owner_pid": lease.owner_pid,
                "lease_id": lease.id,
            },
        }
        if handler_failed:
            final_status, run.result_summary = _retryable_failure_summary(
                job,
                run,
                base_summary=run.result_summary,
                now=now,
            )
        run.agent_run_id = (
            int(normalized_result["run_id"])
            if normalized_result.get("run_id") is not None
            else run.agent_run_id
        )
        if run.agent_run_id is not None:
            run.trace_id = trace_id_for_run_id(run.agent_run_id)
        run.status = final_status
        if final_status == "blocked":
            run.error_text = str(normalized_result.get("reason") or normalized_result.get("message") or "blocked")
        elif handler_failed:
            run.error_text = handler_error_text
        else:
            run.error_text = None

        step.status = "completed" if final_status == RUN_STATUS_SETTLED_SUCCESS else final_status
        step.finished_at = now
        step.agent_run_id = run.agent_run_id
        step.trace_id = run.trace_id
        step.result_summary = run.result_summary
        step.error_text = run.error_text
    except Exception as exc:
        failure_status, failure_summary = _retryable_failure_summary(
            job,
            run,
            base_summary={
                "handler_ref": job.handler_ref,
                "handler_status": "error",
                "handler_result": {"status": "failed", "error": str(exc)},
                "execution": {
                    "owner_id": owner_id,
                    "owner_host": lease.owner_host,
                    "owner_pid": lease.owner_pid,
                    "lease_id": lease.id,
                },
            },
            now=now,
        )
        run.status = failure_status
        run.error_text = f"{type(exc).__name__}: {exc}"
        run.result_summary = failure_summary
        step.status = run.status
        step.finished_at = now
        step.result_summary = run.result_summary
        step.error_text = run.error_text
    finally:
        run.finished_at = now
        job.last_finished_at = now
        await async_release_scheduler_lease(session, lease, reason=f"run_{run.status}", now=now)
        await session.flush()

    if run.status in {RUN_STATUS_RETRYABLE, RUN_STATUS_SETTLED_FAILURE}:
        await _async_apply_failure_guard(
            session,
            job,
            run,
            failure_key=f"handler_execute:{job.handler_ref}",
            error_text=run.error_text or "handler failed",
            now=now,
        )
    elif run.status == RUN_STATUS_SETTLED_SUCCESS:
        await async_reset_scheduler_job_failure_guard(session, job, now=now)

    return run


async def async_run_scheduler_job(
    session: AsyncSession,
    job_identifier: str,
    *,
    owner_id: str | None = None,
    runner: Runner = _async_run_command,
    now: datetime | None = None,
    allowed_owner_modes: tuple[str, ...] = (OWNER_MODE_SCHEDULER,),
) -> dict[str, Any]:
    now = _utcnow(now)
    await async_materialize_due_runs(
        session,
        now=now,
        allowed_owner_modes=allowed_owner_modes,
        job_keys=(job_identifier,),
    )
    claimed = await async_claim_next_due_run(
        session,
        now=now,
        allowed_owner_modes=allowed_owner_modes,
        job_keys=(job_identifier,),
        owner_id=owner_id,
        lease_ttl_seconds=LEASE_TTL_SECONDS,
    )
    if claimed is None:
        return {"ok": False, "job_key": job_identifier, "reason": "no due run"}

    run, _lease = claimed
    await async_run_scheduler_run(session, run.id, owner_id=owner_id, runner=runner, now=now)
    await session.flush()
    return {"ok": True, "job_key": job_identifier, "run_id": run.id, "status": run.status}


async def async_resume_scheduler_run(
    session: AsyncSession,
    run_id: int,
    *,
    owner_id: str | None = None,
    runner: Runner = _async_run_command,
    now: datetime | None = None,
) -> SchedulerRun:
    return await async_run_scheduler_run(session, run_id, owner_id=owner_id, runner=runner, resume=True, now=now)


async def async_retry_scheduler_run(
    session: AsyncSession,
    run_id: int,
    *,
    now: datetime | None = None,
) -> SchedulerRun:
    return await async_retry_run(session, run_id, now=now)


async def async_set_scheduler_job_paused(
    session: AsyncSession,
    identifier: str,
    *,
    paused: bool,
    reason: str | None = None,
    now: datetime | None = None,
) -> SchedulerJob:
    return await async_set_scheduler_job_pause_state(session, identifier, paused=paused, reason=reason, now=now)


async def async_set_scheduler_job_owner_mode(
    session: AsyncSession,
    identifier: str,
    *,
    owner_mode: str,
) -> SchedulerJob:
    return await async_set_scheduler_job_owner_mode_state(session, identifier, owner_mode=owner_mode)


async def async_set_scheduler_job_load_shed(
    session: AsyncSession,
    identifier: str,
    *,
    load_shed_policy: dict[str, Any] | None = None,
    max_concurrency: int | None = None,
    pause_new_runs: bool | None = None,
    reason: str | None = None,
) -> SchedulerJob:
    job = await async_find_scheduler_job(session, identifier)
    if job is None:
        raise ValueError(f"Scheduler job '{identifier}' not found")

    if max_concurrency is not None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be >= 1")
        job.max_concurrency = max_concurrency

    if load_shed_policy is None:
        policy = dict(job.load_shed_policy or {})
    else:
        policy = dict(load_shed_policy or {})
    if pause_new_runs is not None:
        policy["pause_new_runs"] = pause_new_runs
    if reason is not None:
        policy["reason"] = reason

    updated = await async_set_scheduler_job_load_shed_state(session, identifier, load_shed_policy=policy)
    await session.flush()
    return updated


async def async_drain_scheduler(
    session: AsyncSession,
    *,
    owner_mode: str = OWNER_MODE_SCHEDULER,
    job_key: str | None = None,
    max_runs: int = 10,
    resume: bool = True,
    owner_id: str | None = None,
    runner: Runner = _async_run_command,
    now: datetime | None = None,
    allowed_owner_modes: tuple[str, ...] | None = None,
    admission_budget_seconds: float | None = None,
) -> dict[str, Any]:
    """Start due runs within one bounded scheduler-tick admission budget."""
    now = _utcnow(now)
    modes = allowed_owner_modes or (normalize_owner_mode(owner_mode),)
    budget_seconds = (
        DEFAULT_DRAIN_ADMISSION_BUDGET_SECONDS
        if admission_budget_seconds is None
        else float(admission_budget_seconds)
    )
    if budget_seconds <= 0:
        raise ValueError("admission_budget_seconds must be greater than zero")
    loop = asyncio.get_running_loop()
    admission_deadline = loop.time() + budget_seconds

    await async_reconcile_detached_runs(
        session,
        retryable_failure_summary=_retryable_failure_summary,
        apply_failure_guard=_async_apply_failure_guard,
        now=now,
    )

    await async_materialize_due_runs(
        session,
        now=now,
        allowed_owner_modes=modes,
        job_keys=(job_key,) if job_key else None,
    )

    results: list[dict[str, Any]] = []
    executed = 0
    budget_exhausted = False
    while executed < max_runs:
        if loop.time() >= admission_deadline:
            budget_exhausted = True
            break
        candidate = await async_claim_next_due_run(
            session,
            allowed_owner_modes=modes,
            job_keys=(job_key,) if job_key else None,
            owner_id=owner_id,
            lease_ttl_seconds=LEASE_TTL_SECONDS,
            now=now,
        )
        if candidate is None:
            break
        run, _lease = candidate
        await async_run_scheduler_run(
            session,
            run.id,
            owner_id=owner_id,
            runner=runner,
            resume=resume,
            now=now,
        )
        results.append(
            {
                "run_id": run.id,
                "job_id": run.job_id,
                "status": run.status,
                "error_text": run.error_text,
            }
        )
        executed += 1
    await session.flush()
    result = {"ok": True, "executed": executed, "results": results}
    if budget_exhausted:
        result["budget_exhausted"] = True
    return result
