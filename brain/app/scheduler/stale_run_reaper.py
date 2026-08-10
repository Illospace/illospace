"""Maintain agent-run deadlines and reap stale runs from the API process.

The scheduler daemon is one of the processes this loop must survive. The loop
therefore runs on the API event loop and uses only bounded, idempotent database
operations. A durable scheduler alert latch prevents duplicate Slack delivery
when more than one API replica observes the same overdue runs.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import logging
import os
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from brain.app.scheduler.overdue_alert_state import (
    release_scheduler_alert,
    try_claim_scheduler_alert,
)
from brain.contracts.statuses import PROCESSING_RUN_STATUS_VALUES
from brain.kernel.common.time import assume_utc
from brain.platform.db.models.agent_run import AgentRunRow
from brain.systems.cortex.thread_links import public_app_base_url
from brain.systems.cycles.service import async_finalize_cycle_run_from_run
from brain.systems.failure_guard.slack_delivery import (
    FailureAlertPresentation,
    FailureAlertSubject,
    SlackFailureAlertPolicy,
    async_deliver_failure_alert,
)
from brain.systems.runs.cortex.runner import (
    reap_stale_active_runs,
    settle_terminal_root_run_async,
)
from brain.systems.runs.deadlines import DeadlineSweepResult, sweep_agent_run_deadlines
from brain.systems.slack.client import slack_web_client_from_runtime


AGENT_RUN_DEADLINE_OVERDUE_ALERT_KEY_PREFIX = "agent_run_deadline_overdue"
_DEADLINE_ALERT_AFTER = timedelta(hours=1)
_MAINTENANCE_LIMIT = 25
_OPERATION_TIMEOUT_SECONDS = 10.0

CandidateProvider = Callable[..., Awaitable[tuple["OverdueAgentRun", ...]]]
ReapRuns = Callable[..., Awaitable[int]]
DeadlineSweep = Callable[..., Awaitable[DeadlineSweepResult]]
SettleRun = Callable[[AsyncSession, int], Awaitable[object]]
FinalizeRun = Callable[..., Awaitable[object]]
AlertDelivery = Callable[..., Awaitable[None]]
AlertClaim = Callable[..., Awaitable[bool]]
AlertRelease = Callable[..., Awaitable[None]]

logger = logging.getLogger(__name__)
_latest_reaper_check: _StaleRunReaperCheck | None = None


def _emit(payload: dict[str, Any]) -> None:
    """Write compact runtime evidence to the container's stdout stream."""
    print(json.dumps(payload, separators=(",", ":"), default=str), flush=True)


def _error_text(exc: BaseException) -> str:
    detail = str(exc).strip()
    label = type(exc).__name__
    return f"{label}: {detail}" if detail else label


def _duration_label(seconds: float) -> str:
    total_minutes = max(0, int(seconds)) // 60
    hours, minutes = divmod(total_minutes, 60)
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _run_origin(row: AgentRunRow) -> str:
    metadata = row.metadata_ if isinstance(row.metadata_, dict) else {}
    containers = [
        metadata,
        metadata.get("launch_envelope"),
        metadata.get("runtime_envelope"),
    ]
    for container in containers:
        if not isinstance(container, dict):
            continue
        for key in ("origin", "originating_surface", "source"):
            value = str(container.get(key) or "").strip()
            if value:
                return value
    return str(row.source_idempotency_scope or "").strip() or "unknown"


@dataclass(frozen=True, slots=True)
class OverdueAgentRun:
    run_id: int
    started_at: datetime
    deadline_at: datetime
    origin: str

    def age_seconds_at(self, now: datetime) -> float:
        return max(
            0.0,
            (assume_utc(now) - assume_utc(self.started_at)).total_seconds(),
        )

    def deadline_lag_seconds_at(self, now: datetime) -> float:
        return max(
            0.0,
            (assume_utc(now) - assume_utc(self.deadline_at)).total_seconds(),
        )


@dataclass(frozen=True, slots=True)
class _StaleRunReaperCheck:
    checked_at: datetime
    reaped: int
    closeout_requested: int
    expired: int
    overdue_run_ids: tuple[int, ...]
    alert_sent: bool
    errors: tuple[str, ...] = ()


def agent_run_maintenance_snapshot(
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return process-local evidence from the latest maintenance interval."""
    reference_time = assume_utc(now or datetime.now(timezone.utc))
    check = _latest_reaper_check
    if check is None:
        return {
            "state": "stalled",
            "last_interval_at": None,
            "interval_age_seconds": None,
            "reaped": 0,
            "closeout_requested": 0,
            "expired": 0,
            "overdue_run_ids": [],
            "alert_sent": False,
            "errors": [],
            "reason": "No agent-run maintenance interval has been recorded.",
        }

    age_seconds = max(
        0,
        int((reference_time - assume_utc(check.checked_at)).total_seconds()),
    )
    if check.errors:
        state = "stalled"
        reason = "The latest agent-run maintenance interval had an error."
    elif age_seconds > int(StaleRunReaper.check_interval_seconds * 5):
        state = "stalled"
        reason = "Agent-run maintenance has not reported for five intervals."
    elif age_seconds > int(StaleRunReaper.check_interval_seconds * 2):
        state = "late"
        reason = "The latest agent-run maintenance interval is late."
    else:
        state = "good"
        reason = "Agent-run maintenance is reporting on time."
    return {
        "state": state,
        "last_interval_at": assume_utc(check.checked_at).isoformat(),
        "interval_age_seconds": age_seconds,
        "reaped": check.reaped,
        "closeout_requested": check.closeout_requested,
        "expired": check.expired,
        "overdue_run_ids": list(check.overdue_run_ids),
        "alert_sent": check.alert_sent,
        "errors": list(check.errors),
        "reason": reason,
    }


async def async_overdue_agent_runs(
    session: AsyncSession,
    *,
    now: datetime,
    overdue_after: timedelta,
    limit: int,
) -> tuple[OverdueAgentRun, ...]:
    """Return a bounded snapshot of processing runs far past their deadline."""
    cutoff = assume_utc(now) - overdue_after
    rows = list(
        (
            await session.scalars(
                select(AgentRunRow)
                .where(
                    AgentRunRow.status.in_(PROCESSING_RUN_STATUS_VALUES),
                    AgentRunRow.deadline_at.is_not(None),
                    AgentRunRow.deadline_at < cutoff,
                )
                .order_by(AgentRunRow.deadline_at.asc(), AgentRunRow.id.asc())
                .limit(max(1, int(limit)))
            )
        ).all()
    )
    return tuple(
        OverdueAgentRun(
            run_id=int(row.id),
            started_at=assume_utc(row.started_at or row.created_at),
            deadline_at=assume_utc(row.deadline_at),
            origin=_run_origin(row),
        )
        for row in rows
        if row.deadline_at is not None
    )


def _deadline_overdue_alert_key(run_id: int) -> str:
    return f"{AGENT_RUN_DEADLINE_OVERDUE_ALERT_KEY_PREFIX}:{int(run_id)}"


async def _claim_deadline_overdue_alert(
    *,
    run_id: int,
    alerted_at: datetime,
) -> bool:
    from brain.platform.db.repositories.unit_of_work import UnitOfWork

    async with UnitOfWork() as uow:
        return await try_claim_scheduler_alert(
            uow.session,
            alert_key=_deadline_overdue_alert_key(run_id),
            alerted_at=alerted_at,
        )


async def _release_deadline_overdue_alert(*, run_id: int) -> None:
    from brain.platform.db.repositories.unit_of_work import UnitOfWork

    async with UnitOfWork() as uow:
        await release_scheduler_alert(
            uow.session,
            alert_key=_deadline_overdue_alert_key(run_id),
        )


def _alert_summary(candidates: tuple[OverdueAgentRun, ...], *, now: datetime) -> str:
    return "\n".join(
        (
            f"- Run {candidate.run_id}: age "
            f"{_duration_label(candidate.age_seconds_at(now))}; "
            f"origin: {candidate.origin}; deadline overdue by "
            f"{_duration_label(candidate.deadline_lag_seconds_at(now))}"
        )
        for candidate in candidates
    )


class StaleRunReaper:
    """Run bounded agent-run maintenance outside the scheduler daemon."""

    name = "stale_run_reaper"
    check_interval_seconds = 60.0
    reap_limit = _MAINTENANCE_LIMIT
    deadline_sweep_limit = _MAINTENANCE_LIMIT
    deadline_alert_after = _DEADLINE_ALERT_AFTER
    operation_timeout_seconds = _OPERATION_TIMEOUT_SECONDS

    def __init__(
        self,
        *,
        candidate_provider: CandidateProvider = async_overdue_agent_runs,
        reap_runs: ReapRuns = reap_stale_active_runs,
        deadline_sweep: DeadlineSweep = sweep_agent_run_deadlines,
        settle_run: SettleRun = settle_terminal_root_run_async,
        finalize_run: FinalizeRun = async_finalize_cycle_run_from_run,
        claim_alert: AlertClaim = _claim_deadline_overdue_alert,
        release_alert: AlertRelease = _release_deadline_overdue_alert,
        deliver_alert: AlertDelivery = async_deliver_failure_alert,
    ) -> None:
        self._candidate_provider = candidate_provider
        self._reap_runs = reap_runs
        self._deadline_sweep = deadline_sweep
        self._settle_run = settle_run
        self._finalize_run = finalize_run
        self._claim_alert = claim_alert
        self._release_alert = release_alert
        self._deliver_alert = deliver_alert

    async def run(self) -> None:
        """Run maintenance on the API event loop at a fixed cadence."""
        while True:
            interval_started_at = asyncio.get_running_loop().time()
            try:
                await self._run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - the next interval retries
                logger.exception("Agent-run stale reaper failed safely")
                self._record_interval(
                    _StaleRunReaperCheck(
                        checked_at=datetime.now(timezone.utc),
                        reaped=0,
                        closeout_requested=0,
                        expired=0,
                        overdue_run_ids=(),
                        alert_sent=False,
                        errors=(_error_text(exc),),
                    )
                )
                _emit(
                    {
                        "event": "stale_run_reaper_interval_failed",
                        "host": "api",
                        "ok": False,
                        "error": _error_text(exc),
                    }
                )
            elapsed = asyncio.get_running_loop().time() - interval_started_at
            await asyncio.sleep(max(0.0, self.check_interval_seconds - elapsed))

    async def _run_once(
        self,
        *,
        now: datetime | None = None,
    ) -> _StaleRunReaperCheck:
        reference_time = self._reference_time(now)
        errors: list[str] = []
        candidates: tuple[OverdueAgentRun, ...] | None = None
        try:
            candidates = await self._bounded(
                self._overdue_candidates(now=reference_time)
            )
        except Exception as exc:  # noqa: BLE001 - maintenance must still run
            logger.exception("agent_run_deadline_overdue_observation_failed")
            errors.append(f"overdue_observation={_error_text(exc)}")
            self._emit_failure(
                "agent_run_deadline_overdue_observation_failed",
                exc,
                checked_at=reference_time,
            )

        deadline_result = DeadlineSweepResult()
        try:
            deadline_result = await self._bounded(
                self._enforce_deadlines(now=reference_time)
            )
        except Exception as exc:  # noqa: BLE001 - stale reaping must still run
            logger.exception("agent_run_deadline_sweep_failed")
            errors.append(f"deadline_sweep={_error_text(exc)}")
            self._emit_failure(
                "agent_run_deadline_sweep_failed",
                exc,
                checked_at=reference_time,
            )

        reaped = 0
        try:
            reaped = await self._bounded(
                self._reap_runs(
                    now=reference_time,
                    limit=self.reap_limit,
                )
            )
        except Exception as exc:  # noqa: BLE001 - the next interval retries
            logger.exception("agent_run_stale_reap_failed")
            errors.append(f"stale_reap={_error_text(exc)}")
            self._emit_failure(
                "agent_run_stale_reap_failed",
                exc,
                checked_at=reference_time,
                reaped=0,
            )

        alert_sent = False
        if candidates is not None:
            try:
                alert_sent = await self._bounded(
                    self._evaluate_alert(candidates, now=reference_time)
                )
            except Exception as exc:  # noqa: BLE001 - the next interval retries
                logger.exception("agent_run_deadline_overdue_alert_failed")
                errors.append(f"overdue_alert={_error_text(exc)}")
                self._emit_failure(
                    "agent_run_deadline_overdue_alert_failed",
                    exc,
                    checked_at=reference_time,
                )
        check = _StaleRunReaperCheck(
            checked_at=reference_time,
            reaped=reaped,
            closeout_requested=deadline_result.closeout_requested,
            expired=deadline_result.expired,
            overdue_run_ids=tuple(candidate.run_id for candidate in candidates or ()),
            alert_sent=alert_sent,
            errors=tuple(errors),
        )
        self._record_interval(check)
        return check

    async def _bounded(self, operation: Awaitable[Any]) -> Any:
        return await asyncio.wait_for(
            operation,
            timeout=max(0.01, float(self.operation_timeout_seconds)),
        )

    @staticmethod
    def _emit_failure(
        event: str,
        exc: BaseException,
        *,
        checked_at: datetime,
        **counters: Any,
    ) -> None:
        _emit(
            {
                "event": event,
                "host": "api",
                "ok": False,
                "checked_at": assume_utc(checked_at).isoformat(),
                **counters,
                "error": _error_text(exc),
            }
        )

    @staticmethod
    def _record_interval(check: _StaleRunReaperCheck) -> None:
        global _latest_reaper_check
        _latest_reaper_check = check
        _emit(
            {
                "event": "agent_run_stale_reap",
                "host": "api",
                "ok": not check.errors,
                "checked_at": assume_utc(check.checked_at).isoformat(),
                "reaped": check.reaped,
                "expired": check.expired,
                "closeout_requested": check.closeout_requested,
                "overdue_run_ids": list(check.overdue_run_ids),
                "alert_sent": check.alert_sent,
                "errors": list(check.errors),
            }
        )

    async def _overdue_candidates(
        self,
        *,
        now: datetime,
    ) -> tuple[OverdueAgentRun, ...]:
        from brain.platform.db.repositories.unit_of_work import UnitOfWork

        async with UnitOfWork() as uow:
            return await self._candidate_provider(
                uow.session,
                now=now,
                overdue_after=self.deadline_alert_after,
                limit=self.deadline_sweep_limit,
            )

    async def _enforce_deadlines(self, *, now: datetime) -> DeadlineSweepResult:
        from brain.platform.db.repositories.unit_of_work import UnitOfWork

        async with UnitOfWork() as uow:
            result = await self._deadline_sweep(
                uow.session,
                now=now,
                limit=self.deadline_sweep_limit,
            )
        for run_id in result.expired_run_ids:
            async with UnitOfWork() as uow:
                await self._settle_run(uow.session, int(run_id))
            await self._finalize_run(
                int(run_id),
                status="expired",
                error="Agent run deadline elapsed",
            )
        return result

    async def _evaluate_alert(
        self,
        candidates: tuple[OverdueAgentRun, ...],
        *,
        now: datetime,
    ) -> bool:
        if not candidates:
            return False
        claimed: list[OverdueAgentRun] = []
        for candidate in candidates:
            if await self._claim_alert(run_id=candidate.run_id, alerted_at=now):
                claimed.append(candidate)
        claimed_candidates = tuple(claimed)
        if not claimed_candidates:
            return False

        try:
            await self._deliver_alert(
                policy=SlackFailureAlertPolicy(
                    provide_client=slack_web_client_from_runtime,
                    requested_by=self.name,
                    reason="Deliver an overdue agent-run alert to the team.",
                    channel=(
                        os.getenv("ILLO_SCHEDULER_FAILURE_ALERT_CHANNEL", "").strip()
                        or "#alerts"
                    ),
                    unknown_error_text="Agent run exceeded its deadline",
                ),
                subject=FailureAlertSubject(
                    identity_label="Run id",
                    identity=str(claimed_candidates[0].run_id),
                    url_label="Illo",
                    url=public_app_base_url(),
                    link_label="open Illo",
                ),
                presentation=FailureAlertPresentation(
                    title="Agent run overdue",
                    summary=_alert_summary(claimed_candidates, now=now),
                ),
                error_text="Agent run remained active more than one hour past its deadline.",
            )
        except Exception:  # noqa: BLE001 - release lets the next tick retry
            for candidate in claimed_candidates:
                try:
                    await self._release_alert(run_id=candidate.run_id)
                except Exception:  # noqa: BLE001 - preserve the delivery error
                    logger.exception(
                        "Failed to release agent-run deadline alert after delivery failure",
                        extra={"run_id": candidate.run_id},
                    )
            raise
        return True

    @staticmethod
    def _reference_time(now: datetime | None) -> datetime:
        reference_time = now or datetime.now(timezone.utc)
        if reference_time.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        return reference_time.astimezone(timezone.utc)


__all__ = [
    "AGENT_RUN_DEADLINE_OVERDUE_ALERT_KEY_PREFIX",
    "OverdueAgentRun",
    "StaleRunReaper",
    "agent_run_maintenance_snapshot",
    "async_overdue_agent_runs",
]
