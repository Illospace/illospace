"""System router — system info, metrics, consolidation, retrieval stats, scheduler management."""
from __future__ import annotations

import logging
import os
import platform
import re as _re
import shutil
import subprocess
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from starlette.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, load_only

from brain.app.api.auth import get_current_user
from brain.app.api.authorization import can_manage_run, can_manage_scheduler
from brain.app.api.deps import get_db, rate_limit
from brain.app.api.db_utils import run_db
from brain.app.api.routers.costs import _fetch_agent_api_call_rows, _provider_model_key
from brain.app.api.schemas.system import ConsolidationRunRead, DailyMetricsRead
from brain.platform.db.repositories.memories import EdgeRepository, MemoryRepository
from brain.platform.db.repositories.skills import SkillRepository
from brain.platform.db.repositories.system import (
    ConsolidationRunRepository,
    DailyMetricsRepository,
    RetrievalLogRepository,
)
from brain.systems.runs.cortex.recording import trace_id_for_run_id, trace_id_for_scheduler_run_id
from brain.platform.db.models.agent_run import AgentRunArtifactRow, AgentRunEventRow, AgentRunRow as AgentRun
from brain.platform.db.models.idea import Idea, IdeaThread
from brain.platform.db.models.scheduler import SchedulerRun, SchedulerRunStep
from brain.systems.runs.predict_rlm_backend import get_agent_worker_backend_settings
from brain.app.ops.health import (
    deep_health_snapshot,
    liveness_health_snapshot,
    readiness_health_snapshot,
)
from brain.app.scheduler.catalog import (
    list_scheduler_jobs,
    retire_scheduler_job,
    sync_scheduler_catalog,
    upsert_scheduler_job,
)
from brain.app.scheduler.daemon import scheduler_daemon_tick, scheduler_health_snapshot
from brain.app.scheduler.executor import (
    retry_scheduler_run,
    resume_scheduler_run,
    set_scheduler_job_load_shed as set_scheduler_job_load_shed_state,
    set_scheduler_job_owner_mode as set_scheduler_job_owner_mode_state,
    set_scheduler_job_paused,
)
from brain.app.scheduler.planner import materialize_due_runs

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api",
    tags=["system"],
    dependencies=[Depends(rate_limit)],
)

_TRACE_RUN_LOAD_COLUMNS = (
    AgentRun.id,
    AgentRun.trace_id,
    AgentRun.thread_id,
    AgentRun.org_id,
    AgentRun.user_id,
    AgentRun.profile,
    AgentRun.recipe,
    AgentRun.status,
    AgentRun.target_ref,
    AgentRun.model_policy,
    AgentRun.started_at,
    AgentRun.completed_at,
    AgentRun.created_at,
)

def _trace_iso(value: Any) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else None


def _rollback_quietly(db: Session) -> None:
    try:
        db.rollback()
    except Exception:
        pass


def _safe_scalars(db: Session, stmt) -> list[Any]:
    try:
        return list(db.scalars(stmt).all())
    except SQLAlchemyError:
        _rollback_quietly(db)
        return []


def _safe_scalar(db: Session, stmt) -> Any | None:
    try:
        return db.scalar(stmt)
    except SQLAlchemyError:
        _rollback_quietly(db)
        return None


def _trace_lookup_run_id(trace_id: str | None) -> int | None:
    if not trace_id:
        return None
    prefix = "run:"
    if not trace_id.startswith(prefix):
        return None
    try:
        return int(trace_id[len(prefix):])
    except ValueError:
        return None


def _caller_has_global_trace_visibility(user: dict[str, Any] | None) -> bool:
    return bool(user and user.get("internal") and can_manage_run(user))


def _idea_visible_to_user(db: Session, idea_id: str | None, user: dict[str, Any] | None) -> bool:
    if _caller_has_global_trace_visibility(user):
        return True
    if not idea_id:
        return False
    org_id = str(user.get("org_id")) if user and user.get("org_id") else None
    if not org_id:
        return False
    idea_org_id = _safe_scalar(
        db,
        select(Idea.org_id).where(Idea.id == str(idea_id)),
    )
    return bool(idea_org_id and str(idea_org_id) == org_id)


def _run_visible_to_user(db: Session, run: AgentRun, user: dict[str, Any] | None) -> bool:
    if _caller_has_global_trace_visibility(user):
        return True
    user_id = str(user.get("id")) if user and user.get("id") else None
    if user_id and getattr(run, "user_id", None) and str(run.user_id) == user_id:
        return True
    idea_id = getattr(run, "thread_id", None) or getattr(run, "idea_id", None)
    return _idea_visible_to_user(db, idea_id, user)


def _require_trace_run(
    db: Session,
    *,
    user: dict[str, Any] | None,
    trace_id: str | None = None,
    run_id: int | None = None,
) -> AgentRun:
    load_options = [load_only(*_TRACE_RUN_LOAD_COLUMNS)]
    run = (
        db.get(AgentRun, run_id, options=load_options)
        if run_id is not None
        else None
    )
    if run is None:
        parsed_run_id = _trace_lookup_run_id(trace_id)
        if parsed_run_id is not None:
            run = db.get(AgentRun, parsed_run_id, options=load_options)
    if run is None and trace_id:
        run = _safe_scalar(
            db,
            select(AgentRun)
            .options(load_only(*_TRACE_RUN_LOAD_COLUMNS))
            .where(AgentRun.trace_id == trace_id),
        )
    if run is None:
        raise HTTPException(status_code=404, detail="Trace not found")
    if not _run_visible_to_user(db, run, user):
        raise HTTPException(status_code=404, detail="Trace not found")
    return run


def _artifact_kind(artifact: Any) -> str:
    if isinstance(artifact, dict):
        return str(artifact.get("type") or artifact.get("kind") or "unknown")
    if isinstance(artifact, str):
        return "text"
    return "unknown"


def _call_value(call: Any, key: str) -> Any:
    if isinstance(call, dict):
        return call.get(key)
    return getattr(call, key, None)


def _trace_llm_summary(calls: list[Any]) -> dict[str, Any]:
    totals = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read": 0,
        "cache_write": 0,
        "latency_ms": 0,
    }
    by_model: dict[str, dict[str, Any]] = {}
    for call in calls:
        input_tokens = int(_call_value(call, "tokens_input") or 0)
        output_tokens = int(_call_value(call, "tokens_output") or 0)
        cache_read = int(_call_value(call, "cache_read") or 0)
        cache_write = int(_call_value(call, "cache_write") or 0)
        latency_ms = int(_call_value(call, "latency_ms") or 0)
        totals["input_tokens"] += input_tokens
        totals["output_tokens"] += output_tokens
        totals["cache_read"] += cache_read
        totals["cache_write"] += cache_write
        totals["latency_ms"] += latency_ms
        provider, model, provider_model = _provider_model_key(_call_value(call, "model"))
        row = by_model.setdefault(
            provider_model,
            {
                "provider": provider,
                "model": model,
                "provider_model": provider_model,
                "calls": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read": 0,
                "cache_write": 0,
                "latency_ms": 0,
            },
        )
        row["calls"] += 1
        row["input_tokens"] += input_tokens
        row["output_tokens"] += output_tokens
        row["cache_read"] += cache_read
        row["cache_write"] += cache_write
        row["latency_ms"] += latency_ms
    return {
        "count": len(calls),
        "total_tokens": totals["input_tokens"] + totals["output_tokens"],
        **totals,
        "models": sorted(by_model.values(), key=lambda row: row["calls"], reverse=True),
    }


def _trace_tool_summary(tool_calls: list[Any]) -> dict[str, Any]:
    tools: dict[tuple[str, str], dict[str, Any]] = {}
    for call in tool_calls:
        key = (getattr(call, "tool_name", None) or "unknown", getattr(call, "source", None) or "runner")
        row = tools.setdefault(
            key,
            {"tool_name": key[0], "source": key[1], "calls": 0},
        )
        row["calls"] += 1
    return {
        "count": len(tool_calls),
        "tools": sorted(tools.values(), key=lambda row: row["calls"], reverse=True),
    }


def _trace_llm_summary_for_run(db: Session, run_id: int) -> dict[str, Any]:
    try:
        rows = db.execute(
            text(
                """
                SELECT
                    model,
                    COUNT(*) AS calls,
                    COALESCE(SUM(tokens_input), 0) AS input_tokens,
                    COALESCE(SUM(tokens_output), 0) AS output_tokens,
                    COALESCE(SUM(cache_read), 0) AS cache_read,
                    COALESCE(SUM(cache_write), 0) AS cache_write,
                    COALESCE(SUM(latency_ms), 0) AS latency_ms
                FROM agent_api_calls
                WHERE run_id = :run_id
                GROUP BY model
                """
            ),
            {"run_id": run_id},
        ).mappings().all()
    except SQLAlchemyError:
        _rollback_quietly(db)
        return _trace_llm_summary(_fetch_agent_api_call_rows(db, run_id))

    totals = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read": 0,
        "cache_write": 0,
        "latency_ms": 0,
    }
    by_model: dict[str, dict[str, Any]] = {}
    call_count = 0
    for row in rows:
        calls = int(row["calls"] or 0)
        call_count += calls
        provider, model, provider_model = _provider_model_key(row["model"])
        target = by_model.setdefault(
            provider_model,
            {
                "provider": provider,
                "model": model,
                "provider_model": provider_model,
                "calls": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read": 0,
                "cache_write": 0,
                "latency_ms": 0,
            },
        )
        target["calls"] += calls
        for key in totals:
            value = int(row[key] or 0)
            target[key] += value
            totals[key] += value

    return {
        "count": call_count,
        "total_tokens": totals["input_tokens"] + totals["output_tokens"],
        **totals,
        "models": sorted(by_model.values(), key=lambda item: item["calls"], reverse=True),
    }


def _trace_tool_summary_for_run(
    db: Session,
    *,
    trace_id: str,
    run_id: int,
) -> dict[str, Any]:
    try:
        rows = db.execute(
            select(AgentRunEventRow.payload)
            .where(
                AgentRunEventRow.run_id == run_id,
                AgentRunEventRow.event_type.in_(["run.tool_started", "run.tool_completed", "run.tool_failed"]),
            )
        ).all()
    except SQLAlchemyError:
        _rollback_quietly(db)
        return _trace_tool_summary([])

    counts: dict[tuple[str, str], int] = defaultdict(int)
    for row in rows:
        payload = getattr(row, "payload", None) or {
            "tool_name": getattr(row, "tool_name", None),
            "source": getattr(row, "source", None),
        }
        key = (str(payload.get("tool_name") or "unknown"), str(payload.get("source") or "runner"))
        counts[key] += 1
    tools = [{"tool_name": key[0], "source": key[1], "calls": count} for key, count in counts.items()]
    return {
        "count": sum(row["calls"] for row in tools),
        "tools": sorted(tools, key=lambda row: row["calls"], reverse=True),
    }


def _trace_verifier_summary(rows: list[Any]) -> dict[str, Any]:
    statuses: dict[str, int] = defaultdict(int)
    latest = []
    for row in rows:
        statuses[row.status or "unknown"] += 1
        if len(latest) < 10:
            latest.append(
                {
                    "id": row.id,
                    "trace_id": row.trace_id or trace_id_for_run_id(row.run_id),
                    "run_id": row.run_id,
                    "node_id": getattr(row, "node_id", None),
                    "bundle_name": row.bundle_name,
                    "verifier_type": row.verifier_type,
                    "status": row.status,
                    "severity": row.severity,
                    "started_at": _trace_iso(row.started_at),
                    "completed_at": _trace_iso(row.completed_at),
                }
            )
    return {"count": len(rows), "statuses": dict(statuses), "latest": latest}


def _trace_verifier_summary_for_run(
    db: Session,
    *,
    trace_id: str,
    run_id: int,
) -> dict[str, Any]:
    try:
        rows = db.execute(
            select(AgentRunEventRow.payload)
            .where(
                AgentRunEventRow.run_id == run_id,
                AgentRunEventRow.event_type.in_([
                    "run.verifier_completed",
                    "run.verification_completed",
                    "run.verification_result",
                    "run.gate_completed",
                ]),
            )
        ).all()
    except SQLAlchemyError:
        _rollback_quietly(db)
        return {"count": 0, "statuses": {}, "latest": []}

    statuses: dict[str, int] = defaultdict(int)
    latest: list[dict[str, Any]] = []
    total = 0
    for row in rows:
        if hasattr(row, "count") and hasattr(row, "status"):
            count = int(getattr(row, "count") or 0)
            statuses[str(getattr(row, "status") or "unknown")] += count
            total += count
            continue
        payload = getattr(row, "payload", None) or {}
        if not isinstance(payload, dict):
            continue
        status = str(payload.get("status") or "unknown")
        statuses[status] += 1
        total += 1
        if len(latest) < 10:
            latest.append({
                "trace_id": payload.get("trace_id") or trace_id,
                "run_id": run_id,
                "node_id": payload.get("node_id") or payload.get("step_id"),
                "bundle_name": payload.get("bundle_name") or payload.get("gate"),
                "verifier_type": payload.get("verifier_type") or payload.get("type"),
                "status": status,
                "severity": payload.get("severity"),
                "started_at": payload.get("started_at"),
                "completed_at": payload.get("completed_at"),
            })
    return {"count": total, "statuses": dict(statuses), "latest": latest}


def _trace_recording_versions(db: Session, run_id: int) -> tuple[Any | None, Any | None]:
    return None, None


def _trace_artifact_summary(
    db: Session,
    run_id: int,
    *,
    output_type: str | None,
) -> dict[str, Any]:
    rows = _safe_scalars(
        db,
        select(AgentRunArtifactRow.artifact_type).where(AgentRunArtifactRow.run_id == run_id),
    )
    counts: dict[str, int] = defaultdict(int)
    for row in rows:
        counts[str(row or "unknown")] += 1
    return {
        "execution_artifact_count": len(rows),
        "execution_artifact_types": dict(counts),
        "has_output_artifact": bool(rows),
        "output_type": output_type,
    }


def _build_trace_summary(
    db: Session,
    *,
    user: dict[str, Any] | None,
    trace_id: str | None = None,
    run_id: int | None = None,
) -> dict[str, Any]:
    run = _require_trace_run(
        db,
        user=user,
        trace_id=trace_id,
        run_id=run_id,
    )
    resolved_trace_id = run.trace_id or trace_id_for_run_id(run.id)

    scheduler_runs = _safe_scalars(
        db,
        select(SchedulerRun)
        .options(load_only(
            SchedulerRun.id,
            SchedulerRun.trace_id,
            SchedulerRun.job_id,
            SchedulerRun.status,
            SchedulerRun.agent_run_id,
            SchedulerRun.parent_run_id,
            SchedulerRun.scheduled_for,
            SchedulerRun.started_at,
            SchedulerRun.finished_at,
        ))
        .where(
            (SchedulerRun.trace_id == resolved_trace_id)
            | (SchedulerRun.agent_run_id == run.id)
        )
        .order_by(SchedulerRun.scheduled_for.desc(), SchedulerRun.id.desc()),
    )
    scheduler_run_ids = [run.id for run in scheduler_runs]
    scheduler_steps = []
    if scheduler_run_ids:
        scheduler_steps = _safe_scalars(
            db,
            select(SchedulerRunStep)
            .options(load_only(
                SchedulerRunStep.id,
                SchedulerRunStep.trace_id,
                SchedulerRunStep.run_id,
                SchedulerRunStep.step_key,
                SchedulerRunStep.sequence_no,
                SchedulerRunStep.status,
                SchedulerRunStep.agent_run_id,
                SchedulerRunStep.started_at,
                SchedulerRunStep.finished_at,
            ))
            .where(SchedulerRunStep.run_id.in_(scheduler_run_ids))
            .order_by(SchedulerRunStep.run_id.asc(), SchedulerRunStep.sequence_no.asc()),
        )

    llm_summary = _trace_llm_summary_for_run(db, run.id)
    tool_summary = _trace_tool_summary_for_run(
        db,
        trace_id=resolved_trace_id,
        run_id=run.id,
    )
    verifier_summary = _trace_verifier_summary_for_run(
        db,
        trace_id=resolved_trace_id,
        run_id=run.id,
    )
    run_summary_version, flight_recorder_version = _trace_recording_versions(db, run.id)
    artifact_summary = _trace_artifact_summary(
        db,
        run.id,
        output_type="reply",
    )
    provider, model, provider_model = _provider_model_key((run.model_policy or {}).get("model"))

    return {
        "trace_id": resolved_trace_id,
        "run": {
            "id": run.id,
            "trace_id": resolved_trace_id,
            "idea_id": getattr(run, "thread_id", None) or getattr(run, "idea_id", None),
            "event": (run.target_ref or {}).get("event"),
            "status": run.status,
            "profile": run.profile,
            "recipe": run.recipe,
            "provider": provider,
            "model": model,
            "provider_model": provider_model,
            "output_type": artifact_summary["output_type"],
            "has_output_artifact": artifact_summary["has_output_artifact"],
            "started_at": _trace_iso(run.started_at),
            "completed_at": _trace_iso(run.completed_at),
            "created_at": _trace_iso(run.created_at),
            "tokens_total": None,
            "estimated_cost": None,
        },
        "scheduler": {
            "runs": [
                {
                    "id": run.id,
                    "trace_id": run.trace_id or trace_id_for_scheduler_run_id(run.id),
                    "job_id": run.job_id,
                    "status": run.status,
                    "agent_run_id": run.agent_run_id,
                    "parent_run_id": run.parent_run_id,
                    "scheduled_for": _trace_iso(run.scheduled_for),
                    "started_at": _trace_iso(run.started_at),
                    "finished_at": _trace_iso(run.finished_at),
                }
                for run in scheduler_runs
            ],
            "step_count": len(scheduler_steps),
            "steps": [
                {
                    "id": step.id,
                    "trace_id": step.trace_id or resolved_trace_id,
                    "scheduler_run_id": step.run_id,
                    "step_key": step.step_key,
                    "sequence_no": step.sequence_no,
                    "status": step.status,
                    "agent_run_id": getattr(step, "agent_run_id", None),
                    "started_at": _trace_iso(step.started_at),
                    "finished_at": _trace_iso(step.finished_at),
                }
                for step in scheduler_steps[:50]
            ],
        },
        "llm_calls": llm_summary,
        "tool_calls": tool_summary,
        "verification": verifier_summary,
        "artifacts": {
            "execution_artifact_count": artifact_summary["execution_artifact_count"],
            "execution_artifact_types": artifact_summary["execution_artifact_types"],
            "has_output_artifact": artifact_summary["has_output_artifact"],
            "output_type": artifact_summary["output_type"],
        },
        "recordings": {
            "run_summary_recorded": run_summary_version is not None,
            "flight_recorder_recorded": flight_recorder_version is not None,
            "run_summary_schema_version": run_summary_version,
            "flight_recorder_schema_version": flight_recorder_version,
        },
    }


def _normalize_llm_model_value(value: Any) -> Any:
    """Strip provider prefixes from persisted model names without touching local-model slugs."""
    if not isinstance(value, str):
        return value
    trimmed = value.strip()
    for prefix in ("anthropic/", "openai/", "anthropic:", "openai:"):
        if trimmed.startswith(prefix):
            return trimmed[len(prefix):]
    return trimmed


def _human_size(nbytes: float) -> str:
    """Convert bytes to human-readable string."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(nbytes) < 1024:
            return f"{nbytes:.1f} {unit}"
        nbytes /= 1024
    return f"{nbytes:.1f} PB"


def _get_uptime() -> str | None:
    """Return system uptime as a human-readable string."""
    try:
        # Linux: /proc/uptime
        proc_uptime = Path("/proc/uptime")
        if proc_uptime.exists():
            secs = float(proc_uptime.read_text().split()[0])
        else:
            # macOS: sysctl kern.boottime
            result = subprocess.run(
                ["sysctl", "-n", "kern.boottime"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                # Output like: { sec = 1742000000, usec = 0 } ...
                import re
                m = re.search(r"sec\s*=\s*(\d+)", result.stdout)
                if m:
                    secs = time.time() - int(m.group(1))
                else:
                    return None
            else:
                return None

        days, rem = divmod(int(secs), 86400)
        hours, rem = divmod(rem, 3600)
        minutes, _ = divmod(rem, 60)
        parts = []
        if days:
            parts.append(f"{days}d")
        if hours:
            parts.append(f"{hours}h")
        parts.append(f"{minutes}m")
        return " ".join(parts)
    except Exception:
        return None


def _get_disk_info() -> dict | None:
    """Return disk usage for the project root."""
    try:
        usage = shutil.disk_usage(Path(__file__).resolve().parent)
        return {
            "used": _human_size(usage.used),
            "available": _human_size(usage.free),
            "total": _human_size(usage.total),
            "pct_used": round(usage.used / usage.total * 100, 1),
        }
    except Exception:
        return None


def _run_event_consumer_running() -> bool | None:
    try:
        from brain.app.api.main import _run_event_consumer_running as _is_running
        return _is_running()
    except Exception:
        return None


@router.get("/health/live")
def health_live():
    """Cheap liveness probe: process is up, no dependency checks."""
    return liveness_health_snapshot()


@router.get("/health/ready")
async def health_ready(
    db: AsyncSession = Depends(get_db),
):
    """Readiness probe: DB, Alembic head, and event backbone are usable."""
    snapshot = await readiness_health_snapshot(
        consumer_running=_run_event_consumer_running(),
        session=db,
    )
    return JSONResponse(
        status_code=200 if snapshot["ready"] else 503,
        content=snapshot,
    )


@router.get("/health/deep")
async def health_deep(
    db: AsyncSession = Depends(get_db),
):
    """Deep product health snapshot for operators."""
    return await deep_health_snapshot(
        consumer_running=_run_event_consumer_running(),
        session=db,
    )


def _get_gpu_info() -> dict | None:
    """Return GPU info via nvidia-smi, or None if unavailable."""
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.used,memory.total,utilization.gpu,temperature.gpu",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return None
        line = result.stdout.strip().split("\n")[0]
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 5:
            return None
        return {
            "status": "ok",
            "name": parts[0],
            "vram_used": f"{parts[1]} MiB",
            "vram_total": f"{parts[2]} MiB",
            "utilization": float(parts[3]),
            "temperature": float(parts[4]),
        }
    except Exception:
        return None


def _get_embedding_info() -> dict:
    """Return embedding backend status and config."""
    from brain.kernel.config import EMBEDDING_BACKEND, EMBEDDING_DIM, EMBEDDING_API_PROVIDER, EMBEDDING_API_MODEL, EMBEDDING_CPU_MODEL
    from brain.systems.memory.embeddings import server_health

    try:
        health = server_health()
    except Exception:
        health = None
    result = {
        "backend": EMBEDDING_BACKEND,
        "dimensions": EMBEDDING_DIM,
        "status": (health.get("status", "unknown") if isinstance(health, dict) else "not_running"),
        "server_health": health if isinstance(health, dict) else None,
    }
    if EMBEDDING_BACKEND == "gpu":
        try:
            from brain.platform.gpu.config import build_worker_manifests
            emb_worker = next((w for w in build_worker_manifests() if w.name == "embedding"), None)
            result["model"] = emb_worker.model_path.rsplit("/", 1)[-1] if emb_worker else "unknown"
        except Exception:
            result["model"] = "unknown"
        result["device"] = "cuda" if _get_gpu_info() is not None else "cpu"
    elif EMBEDDING_BACKEND == "cpu":
        result["model"] = EMBEDDING_CPU_MODEL
        result["device"] = "cpu"
    elif EMBEDDING_BACKEND == "api":
        result["provider"] = EMBEDDING_API_PROVIDER
        result["model"] = EMBEDDING_API_MODEL
        from brain.kernel.config import EMBEDDING_API_KEY
        result["api_key_set"] = bool(EMBEDDING_API_KEY)
    return result


def _get_database_info(db: Session) -> dict:
    """Return database status and stats."""
    try:
        mem_repo = MemoryRepository(db)
        edge_repo = EdgeRepository(db)

        # DB size via pg_database_size
        try:
            row = db.execute(
                text("SELECT pg_size_pretty(pg_database_size(current_database())) AS s")
            ).fetchone()
            db_size = row[0] if row else "unknown"
        except Exception:
            db_size = "unknown"

        return {
            "status": "ok",
            "size": db_size,
            "memory_count": mem_repo.count_active(),
            "archived_count": mem_repo.count_archived(),
            "edge_count": edge_repo.count_all(),
        }
    except Exception as exc:
        return {"status": "error", "error": str(exc)}


def _get_config_info() -> dict:
    """Return key brain config values (non-sensitive)."""
    from brain.kernel.config import (
        DB_HOST, DB_PORT, DB_NAME,
        EMBEDDING_BACKEND, EMBEDDING_DIM, DEFAULT_RETRIEVAL_LIMIT, RERANK_CANDIDATES,
        AUTO_EDGE_K, AUTO_EDGE_MIN_SIM, DECAY_RATE, DECAY_THRESHOLD,
        GPU_SERVER_URL,
    )
    return {
        "db_host": DB_HOST,
        "db_port": DB_PORT,
        "db_name": DB_NAME,
        "embedding_dim": EMBEDDING_DIM,
        "retrieval_limit": DEFAULT_RETRIEVAL_LIMIT,
        "rerank_candidates": RERANK_CANDIDATES,
        "auto_edge_k": AUTO_EDGE_K,
        "auto_edge_min_sim": AUTO_EDGE_MIN_SIM,
        "decay_rate": DECAY_RATE,
        "decay_threshold": DECAY_THRESHOLD,
        "embedding_backend": EMBEDDING_BACKEND,
    }


@router.get("/system")
async def system_info(
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    def _info(sync_db: Session):
        def _safe(fn, *args):
            try:
                return fn(*args)
            except Exception as e:
                return {"status": "error", "error": str(e)}

        return {
            "version": "6.0.0",
            "python": platform.python_version(),
            "platform": platform.system(),
            "uptime": _safe(_get_uptime),
            "disk": _safe(_get_disk_info),
            "database": _safe(_get_database_info, sync_db),
            "gpu": _safe(_get_gpu_info),
            "embedding": _safe(_get_embedding_info),
            "llm": _safe(_get_llm_info, user, sync_db),
            "config": _safe(_get_config_info),
        }

    return await run_db(db, _info)


@router.get("/system/traces/by-run/{run_id}")
async def trace_summary_by_run(
    run_id: int,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    """Return a privacy-filtered execution skeleton for one run trace."""
    return await run_db(db, lambda sync_db: _build_trace_summary(sync_db, user=user, run_id=run_id))


@router.get("/system/traces/by-reply/{thread_id}")
async def trace_summary_by_reply(
    thread_id: int,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    """Resolve a user-visible Cortex reply to its run trace skeleton."""
    def _summary(sync_db: Session):
        thread = sync_db.get(IdeaThread, thread_id)
        if not thread or not _idea_visible_to_user(sync_db, thread.idea_id, user):
            raise HTTPException(status_code=404, detail="Reply not found")
        metadata = thread.metadata_ if isinstance(thread.metadata_, dict) else {}
        run_id = metadata.get("run_id")
        if run_id is not None:
            try:
                return _build_trace_summary(sync_db, user=user, run_id=int(run_id))
            except (TypeError, ValueError):
                pass
        trace_id = metadata.get("trace_id")
        if isinstance(trace_id, str) and trace_id.strip():
            return _build_trace_summary(sync_db, user=user, trace_id=trace_id.strip())
        raise HTTPException(status_code=404, detail="Reply has no trace metadata")

    return await run_db(db, _summary)


@router.get("/system/traces/{trace_id}")
async def trace_summary(
    trace_id: str,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    """Return a privacy-filtered execution skeleton for one trace key."""
    return await run_db(db, lambda sync_db: _build_trace_summary(sync_db, user=user, trace_id=trace_id))


def _get_llm_info(user: dict, db: Session | None = None) -> dict | None:
    """Return provider/model runtime config for system introspection."""
    org_id = user.get("org_id")
    if not org_id or db is None:
        return None
    try:
        from brain.platform.providers.model_policy import (
            get_model_for_tier,
            get_provider_model_maps,
            resolve_default_provider,
        )
        from brain.platform.provider_health import provider_health_snapshot
        def _build() -> dict | None:
            from brain.platform.db.models.org import Org
            from brain.platform.db.models.org import User

            org = db.get(Org, org_id)
            db_user = db.get(User, user.get("id")) if user.get("id") else None
            if not org:
                return None
            config = org.memory_model_config or {}
            org_default = config.get("default_provider") or resolve_default_provider(user_id=user.get("id"), org_id=org_id)
            user_default = getattr(db_user, "default_provider", None) if db_user else None
            low_model = _normalize_llm_model_value(
                get_model_for_tier(
                    "low",
                    include_provider_prefix=False,
                    user_id=user.get("id"),
                    org_id=org_id,
                )
            )
            backend_settings = get_agent_worker_backend_settings(user_id=user.get("id"), org_id=org_id).to_dict()
            return {
                "default_provider": org_default,
                "org_default_provider": org_default,
                "user_default_provider": user_default,
                "effective_provider": resolve_default_provider(user_id=user.get("id"), org_id=org_id),
                "harvest_model": low_model,
                "consolidation_model": low_model,
                "provider_model_mappings": get_provider_model_maps(user_id=user.get("id"), org_id=org_id),
                "provider_health": provider_health_snapshot(),
                **backend_settings,
            }

        return _build()
    except Exception:
        return None


@router.get("/overview")
async def overview(
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    def _overview(sync_db: Session):
        mem_repo = MemoryRepository(sync_db)
        edge_repo = EdgeRepository(sync_db)
        consol_repo = ConsolidationRunRepository(sync_db)

        skill_summary, skill_count, executions = SkillRepository(sync_db).overview_summary(limit=10)
        skill_summary = [
            {"name": row["name"], "maturity": row["maturity"]}
            for row in skill_summary
        ]

        recent_consols = consol_repo.list_recent(limit=1)
        last_consolidation = None
        if recent_consols:
            c = recent_consols[0]
            last_consolidation = {
                "status": c.status,
                "run_date": c.run_date.isoformat() if c.run_date else None,
                "completed_at": c.completed_at.isoformat() if c.completed_at else None,
                "memories_created": c.memories_created,
                "edges_created": c.edges_created,
                "memories_decayed": c.memories_decayed,
            }

        memory_trend = []
        try:
            rows = sync_db.execute(text(
                "SELECT d::date AS day, COUNT(m.id) AS c "
                "FROM generate_series(CURRENT_DATE - INTERVAL '13 days', CURRENT_DATE, '1 day') AS d "
                "LEFT JOIN memories m ON m.created_at::date = d::date AND NOT m.archived "
                "GROUP BY d::date ORDER BY d::date"
            )).mappings().all()
            memory_trend = [{"date": r["day"].isoformat(), "count": r["c"]} for r in rows]
        except Exception:
            logger.debug("memory_trend query failed", exc_info=True)

        return {
            "memories": mem_repo.count_active(),
            "edges": edge_repo.count_all(),
            "skills": skill_count,
            "executions": executions,
            "memory_types": mem_repo.count_by_type(),
            "skill_summary": skill_summary,
            "recent_activity": mem_repo.recent_activity(),
            "last_consolidation": last_consolidation,
            "retrieval_accuracy": mem_repo.retrieval_accuracy(),
            "memory_trend": memory_trend,
        }

    return await run_db(db, _overview)


@router.get("/metrics", response_model=list[DailyMetricsRead])
async def list_metrics(
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    return await run_db(db, lambda sync_db: DailyMetricsRepository(sync_db).list_recent())


@router.get("/consolidations", response_model=list[ConsolidationRunRead])
async def list_consolidations(
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    return await run_db(db, lambda sync_db: ConsolidationRunRepository(sync_db).list_recent())


@router.get("/retrieval")
async def retrieval_stats(
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    return await run_db(db, lambda sync_db: RetrievalLogRepository(sync_db).list_recent())


def _scheduler_snapshot(db: Session) -> dict[str, Any]:
    return scheduler_health_snapshot(db)


@router.get("/system/scheduler")
async def scheduler_state(
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    """Return the DB-backed scheduler state."""
    return await run_db(db, _scheduler_snapshot)


@router.get("/system/scheduler/health")
async def scheduler_health(
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    """Return the scheduler health snapshot."""
    return await run_db(db, _scheduler_snapshot)


class SchedulerSyncRequest(BaseModel):
    owner_mode: str = "scheduler"
    job_keys: list[str] = Field(default_factory=list)


@router.post("/system/scheduler/sync")
async def sync_scheduler_state(
    data: SchedulerSyncRequest | None = None,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    """Ensure built-in scheduler jobs exist without touching legacy cron tables."""
    if not can_manage_scheduler(user):
        raise HTTPException(status_code=403, detail="Permission denied")
    owner_mode = (data.owner_mode if data else "scheduler") or "scheduler"
    job_keys = tuple((data.job_keys if data else []) or [])
    def _sync(sync_db: Session):
        try:
            return sync_scheduler_catalog(sync_db, owner_mode=owner_mode, job_keys=job_keys)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    return await run_db(db, _sync)


class SchedulerMaterializeRequest(BaseModel):
    owner_mode: str = "scheduler"
    job_keys: list[str] = Field(default_factory=list)


@router.post("/system/scheduler/materialize")
async def materialize_scheduler_runs(
    data: SchedulerMaterializeRequest | None = None,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    """Record due runs in the DB without executing them."""
    if not can_manage_scheduler(user):
        raise HTTPException(status_code=403, detail="Permission denied")
    owner_mode = (data.owner_mode if data else "scheduler") or "scheduler"
    if owner_mode != "scheduler":
        raise HTTPException(status_code=400, detail="Legacy cron/mirror owner modes are retired")
    job_keys = tuple((data.job_keys if data else []) or [])
    def _materialize(sync_db: Session):
        runs = materialize_due_runs(
            sync_db,
            allowed_owner_modes=(owner_mode,),
            job_keys=job_keys or None,
        )
        return {"recorded": len(runs), "runs": [run.id for run in runs]}

    return await run_db(db, _materialize)


class SchedulerDrainRequest(BaseModel):
    owner_mode: str = "scheduler"
    job_key: str | None = None
    max_runs: int = 10
    resume: bool = True


@router.post("/system/scheduler/drain")
async def drain_scheduler_runs(
    data: SchedulerDrainRequest | None = None,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    """Run one scheduler tick for the requested owner mode."""
    if not can_manage_scheduler(user):
        raise HTTPException(status_code=403, detail="Permission denied")
    payload = data or SchedulerDrainRequest()
    if payload.owner_mode != "scheduler":
        raise HTTPException(status_code=400, detail="Legacy cron/mirror owner modes are retired")
    return await run_db(
        db,
        lambda sync_db: scheduler_daemon_tick(
            sync_db,
            owner_mode=payload.owner_mode,
            job_key=payload.job_key,
            max_runs=payload.max_runs,
            resume=payload.resume,
        )
    )


# ═══════════════════════════════════════════
# Legacy Cron Compatibility
# ═══════════════════════════════════════════

def _human_schedule(expr: str) -> str:
    """Convert a cron expression to a human-readable description."""
    parts = expr.strip().split()
    if len(parts) < 5:
        return expr

    minute, hour, dom, month, dow = parts[:5]

    # Common patterns
    if minute == "*" and hour == "*":
        return "every minute"
    if minute == "0" and hour == "*":
        return "every hour"
    if minute == "0" and hour == "0" and dom == "*" and dow == "*":
        return "daily at midnight"
    if minute == "0" and hour == "0" and dow == "0":
        return "weekly (Sunday midnight)"
    if minute == "0" and hour == "0" and dom == "1":
        return "monthly (1st at midnight)"

    # Interval patterns
    m_interval = _re.match(r"\*/(\d+)", minute)
    if m_interval and hour == "*":
        return f"every {m_interval.group(1)} minutes"

    h_interval = _re.match(r"\*/(\d+)", hour)
    if h_interval and minute == "0":
        return f"every {h_interval.group(1)} hours"

    # Specific time
    if minute.isdigit() and hour.isdigit():
        h = int(hour)
        ampm = "AM" if h < 12 else "PM"
        display_h = h % 12 or 12
        day_str = ""
        if dow != "*":
            days = {
                "0": "Sun", "1": "Mon", "2": "Tue", "3": "Wed",
                "4": "Thu", "5": "Fri", "6": "Sat", "7": "Sun",
            }
            day_str = f" on {days.get(dow, dow)}"
        return f"at {display_h}:{minute.zfill(2)} {ampm}{day_str}"

    return expr


@router.get("/system/cron-jobs")
async def list_cron_jobs(
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    """Compatibility view backed by scheduler jobs, not OS crontab."""
    def _list(sync_db: Session):
        jobs = []
        for job in list_scheduler_jobs(sync_db):
            payload = job.get("default_payload") or {}
            jobs.append(
                {
                    "id": job["id"],
                    "name": payload.get("name") or job["job_key"],
                    "description": payload.get("description") or "",
                    "schedule": job["cron_expr"],
                    "script_path": payload.get("script_path"),
                    "command": payload.get("command") or job["handler_ref"],
                    "phases": payload.get("phases") or [],
                    "enabled": job["enabled"],
                    "created_by": payload.get("created_by") or "scheduler",
                    "owner_mode": job["owner_mode"],
                    "schedule_human": _human_schedule(job["cron_expr"]),
                    "last_run": {
                        "started_at": job["last_started_at"],
                        "finished_at": job["last_finished_at"],
                    },
                }
            )
        return jobs

    return await run_db(db, _list)


def _tail_text_lines(path: Path, line_count: int) -> list[str]:
    """Read the last N lines without loading large log files into memory."""
    if line_count <= 0:
        return []

    chunk_size = 8192
    chunks: list[bytes] = []
    newline_count = 0
    with path.open("rb") as fh:
        fh.seek(0, os.SEEK_END)
        position = fh.tell()
        while position > 0 and newline_count <= line_count:
            read_size = min(chunk_size, position)
            position -= read_size
            fh.seek(position)
            chunk = fh.read(read_size)
            chunks.append(chunk)
            newline_count += chunk.count(b"\n")

    text = b"".join(reversed(chunks)).decode(errors="replace").strip()
    return text.split("\n")[-line_count:] if text else []


@router.get("/system/cron-logs")
def cron_logs(
    job: str = Query("", description="Job name to filter logs for"),
    lines: int = Query(100, ge=1, le=500, description="Number of tail lines per file"),
    files: int = Query(5, ge=1, le=25, description="Maximum files per log directory"),
    user: dict[str, Any] = Depends(get_current_user),
):
    """Return log entries for a cron job."""
    log_dirs = [
        Path("/var/log"),
        Path(__file__).resolve().parents[4] / "ops" / "logs",
        Path.home() / ".local" / "share" / "illo" / "logs",
    ]

    entries: list[dict] = []
    for log_dir in log_dirs:
        if not log_dir.exists():
            continue
        # Look for files matching the job name
        for f in sorted(log_dir.glob(f"*{job}*"), reverse=True)[:files]:
            try:
                stat = f.stat()
                entries.append({
                    "file": str(f),
                    "lines": _tail_text_lines(f, lines),
                    "size": stat.st_size,
                    "modified": stat.st_mtime,
                })
            except Exception:
                continue

    return entries


class SchedulerJobUpsertRequest(BaseModel):
    job_key: str
    cron_expr: str
    handler_ref: str
    family: str | None = None
    program_key: str | None = None
    handler_kind: str = "command"
    timezone: str = "America/Toronto"
    enabled: bool = True
    description: str = ""
    default_payload: dict[str, Any] = Field(default_factory=dict)
    task_contract: dict[str, Any] = Field(default_factory=dict)
    priority: int = 100
    max_concurrency: int = 1
    timeout_seconds: int | None = None
    retry_policy: dict[str, Any] = Field(default_factory=dict)
    misfire_policy: str = "record"
    load_shed_policy: dict[str, Any] = Field(default_factory=dict)
    target_binding_selector: dict[str, Any] = Field(default_factory=dict)


@router.post("/system/scheduler/jobs")
async def upsert_scheduler_job_route(
    data: SchedulerJobUpsertRequest,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    """Create or update a scheduler-owned recurring job."""
    if not can_manage_scheduler(user):
        raise HTTPException(status_code=403, detail="Permission denied")
    payload = dict(data.default_payload or {})
    payload.setdefault("name", data.job_key)
    if data.description:
        payload.setdefault("description", data.description)
    def _upsert(sync_db: Session):
        try:
            job = upsert_scheduler_job(
                sync_db,
                job_key=data.job_key,
                family=data.family,
                program_key=data.program_key,
                handler_kind=data.handler_kind,
                handler_ref=data.handler_ref,
                cron_expr=data.cron_expr,
                timezone_name=data.timezone,
                default_payload=payload,
                task_contract=data.task_contract,
                enabled=data.enabled,
                priority=data.priority,
                max_concurrency=data.max_concurrency,
                timeout_seconds=data.timeout_seconds,
                retry_policy=data.retry_policy,
                misfire_policy=data.misfire_policy,
                load_shed_policy=data.load_shed_policy,
                target_binding_selector=data.target_binding_selector,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "job_key": job.job_key, "schedule_human": _human_schedule(job.cron_expr)}

    return await run_db(db, _upsert)


@router.delete("/system/scheduler/jobs/{job_key}")
async def retire_scheduler_job_route(
    job_key: str,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    """Retire a scheduler job without deleting historical runs."""
    if not can_manage_scheduler(user):
        raise HTTPException(status_code=403, detail="Permission denied")
    def _retire(sync_db: Session):
        job = retire_scheduler_job(sync_db, job_key, reason="retired via scheduler API")
        if job is None:
            raise HTTPException(status_code=404, detail=f"Scheduler job '{job_key}' not found")
        return {"ok": True, "job_key": job.job_key, "enabled": job.enabled, "pause_reason": job.pause_reason}

    return await run_db(db, _retire)


class SchedulerJobPauseRequest(BaseModel):
    reason: str | None = None


class SchedulerJobOwnerModeRequest(BaseModel):
    owner_mode: str


class SchedulerJobLoadShedRequest(BaseModel):
    load_shed_policy: dict[str, Any] = Field(default_factory=dict)
    max_concurrency: int | None = None
    pause_new_runs: bool | None = None
    reason: str | None = None


@router.patch("/system/scheduler/jobs/{job_key}/pause")
@router.post("/system/scheduler/jobs/{job_key}/pause")
async def pause_scheduler_job(
    job_key: str,
    data: SchedulerJobPauseRequest | None = None,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    if not can_manage_scheduler(user):
        raise HTTPException(status_code=403, detail="Permission denied")
    def _pause(sync_db: Session):
        try:
            job = set_scheduler_job_paused(sync_db, job_key, paused=True, reason=(data.reason if data else None))
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"ok": True, "job_key": job.job_key, "enabled": job.enabled, "pause_reason": job.pause_reason}

    return await run_db(db, _pause)


@router.patch("/system/scheduler/jobs/{job_key}/resume")
@router.post("/system/scheduler/jobs/{job_key}/resume")
async def resume_scheduler_job(
    job_key: str,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    if not can_manage_scheduler(user):
        raise HTTPException(status_code=403, detail="Permission denied")
    def _resume(sync_db: Session):
        try:
            job = set_scheduler_job_paused(sync_db, job_key, paused=False, reason=None)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"ok": True, "job_key": job.job_key, "enabled": job.enabled, "pause_reason": job.pause_reason}

    return await run_db(db, _resume)


@router.patch("/system/scheduler/jobs/{job_key}/owner-mode")
@router.post("/system/scheduler/jobs/{job_key}/owner-mode")
async def set_scheduler_job_owner_mode_endpoint(
    job_key: str,
    data: SchedulerJobOwnerModeRequest,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    if not can_manage_scheduler(user):
        raise HTTPException(status_code=403, detail="Permission denied")
    def _set(sync_db: Session):
        try:
            job = set_scheduler_job_owner_mode_state(sync_db, job_key, owner_mode=data.owner_mode)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "job_key": job.job_key, "owner_mode": job.owner_mode}

    return await run_db(db, _set)


@router.patch("/system/scheduler/jobs/{job_key}/load-shed")
@router.post("/system/scheduler/jobs/{job_key}/load-shed")
async def set_scheduler_job_load_shed_endpoint(
    job_key: str,
    data: SchedulerJobLoadShedRequest,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    if not can_manage_scheduler(user):
        raise HTTPException(status_code=403, detail="Permission denied")
    def _set(sync_db: Session):
        try:
            job = set_scheduler_job_load_shed_state(
                sync_db,
                job_key,
                load_shed_policy=data.load_shed_policy or None,
                max_concurrency=data.max_concurrency,
                pause_new_runs=data.pause_new_runs,
                reason=data.reason,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "ok": True,
            "job_key": job.job_key,
            "max_concurrency": job.max_concurrency,
            "load_shed_policy": job.load_shed_policy or {},
        }

    return await run_db(db, _set)


@router.post("/system/scheduler/runs/{run_id}/resume")
async def resume_scheduler_run_route(
    run_id: int,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    if not can_manage_scheduler(user):
        raise HTTPException(status_code=403, detail="Permission denied")
    def _resume(sync_db: Session):
        try:
            run = resume_scheduler_run(sync_db, run_id, owner_id=f"api:{user.get('id', 'owner')}")
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"ok": True, "run": run.id, "status": run.status}

    return await run_db(db, _resume)


@router.post("/system/scheduler/runs/{run_id}/retry")
async def retry_scheduler_run_route(
    run_id: int,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    if not can_manage_scheduler(user):
        raise HTTPException(status_code=403, detail="Permission denied")
    def _retry(sync_db: Session):
        try:
            run = retry_scheduler_run(sync_db, run_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"ok": True, "run": run.id, "status": run.status, "parent_run_id": run.parent_run_id}

    return await run_db(db, _retry)
