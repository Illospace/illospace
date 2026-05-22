"""Health tier snapshots for API probes and operator diagnostics."""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from brain.platform.db.models.run import AgentRun
from brain.platform.provider_health import provider_health_snapshot
from brain.app.scheduler.daemon import async_scheduler_health_snapshot
from brain.systems.runtime_settings.embedding_diagnostics import (
    embedding_backend_label,
    embedding_provider_label,
)
from brain.systems.runtime_settings.memory import async_get_embedding_info

APP_VERSION = "6.0.0"
DEFAULT_DB_TIMEOUT_MS = 1500
DEFAULT_DEEP_TIMEOUT_MS = 2500
DEFAULT_STUCK_RUN_SECONDS = 15 * 60
DEFAULT_RECENT_FAILURE_WINDOW_MINUTES = 60
DEFAULT_RECENT_FAILURE_LIMIT = 10

_SECRET_PATTERNS = (
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"(?i)((?:api[_-]?key|token|password|secret|credential)\s*[=:]\s*)[^\s,;]+"),
    re.compile(r"\bsk-ant-[A-Za-z0-9._-]{6,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9._-]{6,}\b"),
)


@dataclass(frozen=True)
class HealthCheck:
    """One operator-readable health check result."""

    name: str
    status: str
    summary: str
    latency_ms: int
    details: dict[str, Any] = field(default_factory=dict)
    remediation: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "status": self.status,
            "ok": self.ok,
            "summary": self.summary,
            "latency_ms": self.latency_ms,
            "details": self.details,
        }
        if self.remediation:
            payload["remediation"] = self.remediation
        return payload


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso_now() -> str:
    return _utc_now().isoformat()


def _elapsed_ms(start: float) -> int:
    return int((time.monotonic() - start) * 1000)


def _timeout_ms(env_name: str, default: int) -> int:
    try:
        return max(100, int(os.getenv(env_name, str(default))))
    except Exception:
        return default


def _int_env(name: str, default: int, *, minimum: int = 0) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except Exception:
        return default


def _redact_secretish(text_value: str) -> str:
    redacted = text_value
    for pattern in _SECRET_PATTERNS:
        if "bearer" in pattern.pattern.lower() or "api" in pattern.pattern.lower():
            redacted = pattern.sub(lambda match: f"{match.group(1)}[redacted]", redacted)
        else:
            redacted = pattern.sub("[redacted]", redacted)
    return redacted


def sanitize_for_health(value: Any) -> Any:
    """Recursively remove secret-looking values from health output."""
    if isinstance(value, str):
        return _redact_secretish(value)
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if re.search(r"(?i)(secret|token|password|api[_-]?key|credential)", key_text):
                result[key_text] = bool(item) if isinstance(item, bool) else "[redacted]" if item else item
            else:
                result[key_text] = sanitize_for_health(item)
        return result
    if isinstance(value, list):
        return [sanitize_for_health(item) for item in value]
    if isinstance(value, tuple):
        return [sanitize_for_health(item) for item in value]
    return value


async def _rollback_health_session(session: AsyncSession) -> None:
    try:
        await session.rollback()
    except Exception:
        pass


async def _apply_statement_timeout(session: AsyncSession, timeout_ms: int) -> None:
    """Best-effort PostgreSQL statement timeout for health probes."""
    try:
        await session.execute(text(f"SET LOCAL statement_timeout = {int(timeout_ms)}"))
    except Exception:
        await _rollback_health_session(session)


def _result_scalars(result: Any) -> set[str]:
    return {str(value) for value in result.scalars().all()}


def _alembic_head_revisions() -> set[str]:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
    config = Config(os.path.join(root, "alembic.ini"))
    script = ScriptDirectory.from_config(config)
    return set(script.get_heads())


async def _database_check(session: AsyncSession, *, timeout_ms: int = DEFAULT_DB_TIMEOUT_MS) -> HealthCheck:
    start = time.monotonic()
    try:
        await _apply_statement_timeout(session, timeout_ms)
        result = await session.execute(text("SELECT 1"))
        result.scalar()
        return HealthCheck(
            name="database",
            status="ok",
            summary="database accepts simple queries",
            latency_ms=_elapsed_ms(start),
        )
    except Exception as exc:
        await _rollback_health_session(session)
        return HealthCheck(
            name="database",
            status="failed",
            summary="database query failed",
            latency_ms=_elapsed_ms(start),
            details={"error": str(exc)},
            remediation="Verify DATABASE_URL/DB_* settings and PostgreSQL availability.",
        )


async def _migration_head_check(session: AsyncSession, *, timeout_ms: int = DEFAULT_DB_TIMEOUT_MS) -> HealthCheck:
    start = time.monotonic()
    try:
        head_revisions = _alembic_head_revisions()
        await _apply_statement_timeout(session, timeout_ms)
        current_revisions = _result_scalars(
            await session.execute(text("SELECT version_num FROM alembic_version"))
        )
        if current_revisions == head_revisions:
            return HealthCheck(
                name="migration_head",
                status="ok",
                summary="database is stamped at Alembic head",
                latency_ms=_elapsed_ms(start),
                details={
                    "current": sorted(current_revisions),
                    "head": sorted(head_revisions),
                },
            )
        return HealthCheck(
            name="migration_head",
            status="failed",
            summary="database is not at Alembic head",
            latency_ms=_elapsed_ms(start),
            details={
                "current": sorted(current_revisions) or ["<none>"],
                "head": sorted(head_revisions),
            },
            remediation="Run `python3 -m alembic upgrade head` before routing traffic.",
        )
    except Exception as exc:
        await _rollback_health_session(session)
        return HealthCheck(
            name="migration_head",
            status="failed",
            summary="could not verify Alembic migration head",
            latency_ms=_elapsed_ms(start),
            details={"error": str(exc)},
            remediation="Confirm the alembic_version table exists and migrations are runnable.",
        )


async def _event_backbone_health_check(
    session: AsyncSession,
    *,
    consumer_running: bool | None,
    timeout_ms: int = DEFAULT_DB_TIMEOUT_MS,
) -> HealthCheck:
    start = time.monotonic()
    try:
        from brain.app.api.ws.run_events import DEFAULT_CONSUMER_NAME
        from brain.systems.runs.event_log import async_run_event_backbone_status

        await _apply_statement_timeout(session, timeout_ms)
        status = await async_run_event_backbone_status(
            session,
            DEFAULT_CONSUMER_NAME,
            consumer_running=consumer_running,
        )
        backbone_health = status.get("health")
        if backbone_health in {"healthy", "lagging"}:
            summary = "run event backbone is replay-safe"
            if backbone_health == "lagging":
                summary = f"run event backbone has {status.get('lag', 0)} pending event(s)"
            return HealthCheck(
                name="event_backbone",
                status="ok",
                summary=summary,
                latency_ms=_elapsed_ms(start),
                details=status,
            )
        return HealthCheck(
            name="event_backbone",
            status="failed",
            summary=f"run event backbone is {backbone_health or 'not healthy'}",
            latency_ms=_elapsed_ms(start),
            details=status,
            remediation="Check the API run-event fanout task and consumer error logs.",
        )
    except Exception as exc:
        await _rollback_health_session(session)
        return HealthCheck(
            name="event_backbone",
            status="failed",
            summary="run event backbone check failed",
            latency_ms=_elapsed_ms(start),
            details={"error": str(exc), "consumer_running": consumer_running},
            remediation="Verify agent-run event schema and API fanout startup.",
        )


def _skipped_check(name: str, summary: str) -> HealthCheck:
    return HealthCheck(name=name, status="skipped", summary=summary, latency_ms=0)


def _checks_to_payload(checks: dict[str, HealthCheck]) -> dict[str, Any]:
    return {name: sanitize_for_health(check.to_dict()) for name, check in checks.items()}


def _failures(checks: dict[str, HealthCheck]) -> list[dict[str, str]]:
    return [
        {"check": name, "status": check.status, "summary": check.summary}
        for name, check in checks.items()
        if check.status == "failed"
    ]


def liveness_health_snapshot() -> dict[str, Any]:
    return {
        "tier": "live",
        "status": "alive",
        "ok": True,
        "generated_at": _iso_now(),
        "version": APP_VERSION,
        "checks": {
            "process": {
                "status": "ok",
                "ok": True,
                "summary": "API process is running",
                "latency_ms": 0,
                "details": {},
            }
        },
    }


async def readiness_health_snapshot(
    *,
    consumer_running: bool | None = None,
    session: AsyncSession | None = None,
) -> dict[str, Any]:
    timeout_ms = _timeout_ms("HEALTH_READY_DB_TIMEOUT_MS", DEFAULT_DB_TIMEOUT_MS)
    checks: dict[str, HealthCheck] = {}
    try:
        if session is None:
            raise RuntimeError("health checks require an explicit database session")
        checks["database"] = await _database_check(session, timeout_ms=timeout_ms)
        if checks["database"].ok:
            checks["migration_head"] = await _migration_head_check(session, timeout_ms=timeout_ms)
            checks["event_backbone"] = await _event_backbone_health_check(
                session,
                consumer_running=consumer_running,
                timeout_ms=timeout_ms,
            )
        else:
            checks["migration_head"] = _skipped_check("migration_head", "blocked by database failure")
            checks["event_backbone"] = _skipped_check("event_backbone", "blocked by database failure")
    except Exception as exc:
        if session is not None:
            await _rollback_health_session(session)
        checks["database"] = HealthCheck(
            name="database",
            status="failed",
            summary="database session could not be opened",
            latency_ms=0,
            details={"error": str(exc)},
            remediation="Verify database connectivity and SQLAlchemy engine configuration.",
        )
        checks["migration_head"] = _skipped_check("migration_head", "blocked by database failure")
        checks["event_backbone"] = _skipped_check("event_backbone", "blocked by database failure")

    ready = not _failures(checks)
    return {
        "tier": "ready",
        "status": "ready" if ready else "not_ready",
        "ok": ready,
        "ready": ready,
        "generated_at": _iso_now(),
        "version": APP_VERSION,
        "checks": _checks_to_payload(checks),
        "failures": _failures(checks),
    }


async def _embedding_health_check(session: AsyncSession | None) -> HealthCheck:
    start = time.monotonic()
    try:
        if session is None:
            raise RuntimeError("embedding health checks require an explicit database session")

        info = await async_get_embedding_info(session)
        runtime_status = str(info.get("status") or "unknown")
        details = {
            key: value
            for key, value in info.items()
            if key not in {"detail", "remediation"}
        }
        remediation = info.get("remediation")
        if runtime_status == "ready":
            return HealthCheck(
                name="embedding",
                status="ok",
                summary=f"{embedding_backend_label(info)} embedding backend is ready",
                latency_ms=_elapsed_ms(start),
                details=details,
            )

        if runtime_status == "initializing":
            return HealthCheck(
                name="embedding",
                status="degraded",
                summary="GPU embedding backend is initializing",
                latency_ms=_elapsed_ms(start),
                details=details,
                remediation=str(remediation) if remediation else None,
            )

        summary = str(info.get("detail") or f"embedding backend is {runtime_status}")
        if runtime_status == "missing_key":
            summary = f"{embedding_provider_label(info)} embedding credentials missing"
        return HealthCheck(
            name="embedding",
            status="failed",
            summary=summary,
            latency_ms=_elapsed_ms(start),
            details=details,
            remediation=str(remediation) if remediation else None,
        )
    except Exception as exc:
        if session is not None:
            await _rollback_health_session(session)
        return HealthCheck(
            name="embedding",
            status="failed",
            summary="embedding health check failed",
            latency_ms=_elapsed_ms(start),
            details={"error": str(exc)},
            remediation="Verify embedding backend configuration and service availability.",
        )

def _provider_health_check() -> HealthCheck:
    start = time.monotonic()
    try:
        snapshot = provider_health_snapshot()
        summary = snapshot.get("summary", {})
        degraded = 0
        unavailable = 0
        for operation in summary.values():
            if isinstance(operation, dict):
                degraded += int(operation.get("degraded", 0) or 0)
                unavailable += int(operation.get("unavailable", 0) or 0)
        if unavailable:
            status = "failed"
            summary_text = f"{unavailable} provider/model operation(s) unavailable"
        elif degraded:
            status = "degraded"
            summary_text = f"{degraded} provider/model operation(s) degraded"
        else:
            status = "ok"
            summary_text = "no provider degradation recorded"
        return HealthCheck(
            name="providers",
            status=status,
            summary=summary_text,
            latency_ms=_elapsed_ms(start),
            details=snapshot,
            remediation="Review provider credentials, rate limits, and fallback policy." if status != "ok" else None,
        )
    except Exception as exc:
        return HealthCheck(
            name="providers",
            status="failed",
            summary="provider health snapshot failed",
            latency_ms=_elapsed_ms(start),
            details={"error": str(exc)},
            remediation="Inspect provider health recording and runtime settings.",
        )


async def _scheduler_health_check(session: AsyncSession | None = None) -> HealthCheck:
    start = time.monotonic()
    timeout_ms = _timeout_ms("HEALTH_DEEP_DB_TIMEOUT_MS", DEFAULT_DEEP_TIMEOUT_MS)
    try:
        if session is None:
            raise RuntimeError("health checks require an explicit database session")
        await _apply_statement_timeout(session, timeout_ms)
        snapshot = await async_scheduler_health_snapshot(session)
        scheduler_status = ((snapshot.get("health") or {}).get("status") or "unknown").lower()
        if scheduler_status in {"healthy", "idle"}:
            status = "ok"
            summary = "scheduler has no blocking lag"
            remediation = None
        else:
            status = "degraded"
            summary = f"scheduler is {scheduler_status}"
            remediation = "Inspect /api/system/scheduler/health and resume or drain delayed runs."
        return HealthCheck(
            name="scheduler",
            status=status,
            summary=summary,
            latency_ms=_elapsed_ms(start),
            details={
                "health": snapshot.get("health"),
                "summary": snapshot.get("summary"),
                "lag": snapshot.get("lag"),
                "pause": snapshot.get("pause"),
            },
            remediation=remediation,
        )
    except Exception as exc:
        if session is not None:
            await _rollback_health_session(session)
        return HealthCheck(
            name="scheduler",
            status="failed",
            summary="scheduler health check failed",
            latency_ms=_elapsed_ms(start),
            details={"error": str(exc)},
            remediation="Verify scheduler migrations and scheduler control-plane tables.",
        )


def _run_row_payload(run: AgentRun, *, now: datetime | None = None) -> dict[str, Any]:
    now = now or _utc_now()
    updated_at = run.updated_at
    if updated_at is not None and updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=timezone.utc)
    inactivity_age = int((now - updated_at).total_seconds()) if updated_at else None
    metadata = run.metadata_ if isinstance(run.metadata_, dict) else {}
    return {
        "id": run.id,
        "idea_id": str(run.thread_id) if run.thread_id is not None else None,
        "thread_id": run.thread_id,
        "profile": run.profile,
        "recipe": run.recipe,
        "status": run.status,
        "created_at": run.created_at.isoformat() if run.created_at else None,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "completed_at": run.completed_at.isoformat() if run.completed_at else None,
        "updated_at": run.updated_at.isoformat() if run.updated_at else None,
        "inactivity_age_seconds": inactivity_age,
        "error": metadata.get("error"),
    }


async def _run_health_check(session: AsyncSession | None = None) -> HealthCheck:
    start = time.monotonic()
    timeout_ms = _timeout_ms("HEALTH_DEEP_DB_TIMEOUT_MS", DEFAULT_DEEP_TIMEOUT_MS)
    stuck_after_seconds = _int_env(
        "HEALTH_STUCK_RUN_SECONDS",
        DEFAULT_STUCK_RUN_SECONDS,
        minimum=1,
    )
    failure_window_minutes = _int_env(
        "HEALTH_RECENT_FAILURE_WINDOW_MINUTES",
        DEFAULT_RECENT_FAILURE_WINDOW_MINUTES,
        minimum=1,
    )
    recent_limit = _int_env("HEALTH_RECENT_FAILURE_LIMIT", DEFAULT_RECENT_FAILURE_LIMIT, minimum=1)
    now = _utc_now()
    stuck_cutoff = now - timedelta(seconds=stuck_after_seconds)
    failure_cutoff = now - timedelta(minutes=failure_window_minutes)
    try:
        if session is None:
            raise RuntimeError("health checks require an explicit database session")
        await _apply_statement_timeout(session, timeout_ms)
        stuck_stmt = (
            select(AgentRun)
            .where(
                AgentRun.status.in_(["starting", "running", "paused", "verifying"]),
                func.coalesce(AgentRun.updated_at, AgentRun.started_at, AgentRun.created_at) <= stuck_cutoff,
            )
            .order_by(AgentRun.created_at.asc())
            .limit(recent_limit)
        )
        stuck_result = await session.scalars(stuck_stmt)
        stuck_runs = list(stuck_result.all())
        recent_failure_clause = and_(
            AgentRun.status.in_(["failed"]),
            func.coalesce(AgentRun.completed_at, AgentRun.created_at) >= failure_cutoff,
        )
        recent_failure_result = await session.scalars(
            select(AgentRun)
            .where(recent_failure_clause)
            .order_by(func.coalesce(AgentRun.completed_at, AgentRun.created_at).desc())
            .limit(recent_limit)
        )
        recent_failures = list(recent_failure_result.all())
        recent_failure_count = await session.scalar(
            select(func.count()).select_from(AgentRun).where(recent_failure_clause)
        ) or 0

        status = "ok"
        reasons: list[str] = []
        if stuck_runs:
            status = "degraded"
            reasons.append(f"{len(stuck_runs)} stuck run(es)")
        if recent_failure_count:
            status = "degraded"
            reasons.append(f"{int(recent_failure_count)} recent failed run(es)")

        return HealthCheck(
            name="run",
            status=status,
            summary=", ".join(reasons) if reasons else "run queue has no stuck work or recent failures",
            latency_ms=_elapsed_ms(start),
            details={
                "stuck_after_seconds": stuck_after_seconds,
                "recent_failure_window_minutes": failure_window_minutes,
                "stuck_runs": [_run_row_payload(row, now=now) for row in stuck_runs],
                "recent_failures_count": int(recent_failure_count),
                "recent_failures": [_run_row_payload(row, now=now) for row in recent_failures],
            },
            remediation="Inspect stuck run leases and recent failure errors." if status != "ok" else None,
        )
    except Exception as exc:
        if session is not None:
            await _rollback_health_session(session)
        return HealthCheck(
            name="run",
            status="failed",
            summary="run health check failed",
            latency_ms=_elapsed_ms(start),
            details={"error": str(exc)},
            remediation="Verify agent_runs schema and run migrations.",
        )


async def deep_health_snapshot(
    *,
    consumer_running: bool | None = None,
    session: AsyncSession | None = None,
) -> dict[str, Any]:
    checks = {
        "embedding": await _embedding_health_check(session),
        "providers": _provider_health_check(),
        "scheduler": await _scheduler_health_check(session),
        "run": await _run_health_check(session),
    }
    if consumer_running is not None:
        checks["event_backbone_runtime"] = HealthCheck(
            name="event_backbone_runtime",
            status="ok" if consumer_running else "degraded",
            summary=(
                "run event fanout task is running"
                if consumer_running
                else "run event fanout task is not running"
            ),
            latency_ms=0,
            details={"consumer_running": consumer_running},
            remediation="Check API lifespan startup and CORTEX_EVENT_FANOUT_ENABLED." if not consumer_running else None,
        )
    statuses = {check.status for check in checks.values()}
    overall = "ok"
    if "failed" in statuses:
        overall = "unhealthy"
    elif statuses & {"degraded", "skipped"}:
        overall = "degraded"

    payload = {
        "tier": "deep",
        "status": overall,
        "ok": overall == "ok",
        "generated_at": _iso_now(),
        "version": APP_VERSION,
        "checks": _checks_to_payload(checks),
        "failures": _failures(checks),
    }
    return sanitize_for_health(payload)


async def compatibility_health_snapshot(
    *,
    consumer_running: bool | None = None,
    session: AsyncSession | None = None,
) -> dict[str, Any]:
    try:
        from brain.platform.db.repositories.memories import MemoryRepository
        from brain.platform.db.models.skill import Skill

        if session is None:
            raise RuntimeError("health checks require an explicit database session")
        mem_repo = MemoryRepository(session)
        skill_count = await session.scalar(
            select(func.count(Skill.id)).where(
                or_(Skill.archived == False, Skill.archived.is_(None))  # noqa: E712
            )
        ) or 0
        event_check = await _event_backbone_health_check(
            session,
            consumer_running=consumer_running,
            timeout_ms=_timeout_ms("HEALTH_READY_DB_TIMEOUT_MS", DEFAULT_DB_TIMEOUT_MS),
        )
        return sanitize_for_health({
            "status": "ok",
            "database": "connected",
            "memory_count": await mem_repo.a_count_active(),
            "skill_count": skill_count,
            "run_event_backbone": event_check.details,
            "health_tiers": {
                "live": "/api/health/live",
                "ready": "/api/health/ready",
                "deep": "/api/health/deep",
            },
            "version": APP_VERSION,
        })
    except Exception as exc:
        if session is not None:
            await _rollback_health_session(session)
        return sanitize_for_health({
            "status": "degraded",
            "database": "error",
            "error": str(exc),
            "health_tiers": {
                "live": "/api/health/live",
                "ready": "/api/health/ready",
                "deep": "/api/health/deep",
            },
            "version": APP_VERSION,
        })
