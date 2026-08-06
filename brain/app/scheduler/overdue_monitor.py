"""Alert when scheduler-owned jobs stop advancing.

The freeze latch is scheduler-global and durable. Each API monitor atomically
claims the same stable database key in a short transaction, then delivers only
after that claim commits. API restarts and concurrent replicas therefore share
one alert per freeze without holding a database transaction across Slack. A
healthy observation releases the key so a later freeze can alert again.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
import os

from sqlalchemy.ext.asyncio import AsyncSession

from brain.app.scheduler.cold_start import scheduler_liveness_checkpoint
from brain.app.scheduler.overdue_alert_state import (
    claim_scheduler_overdue_alert,
    release_scheduler_overdue_alert,
)
from brain.app.scheduler.read_models import (
    SchedulerOverdueCandidate,
    async_scheduler_overdue_candidates,
)
from brain.systems.cortex.thread_links import public_app_base_url
from brain.systems.failure_guard.slack_delivery import (
    FailureAlertPresentation,
    FailureAlertSubject,
    SlackFailureAlertPolicy,
    async_deliver_failure_alert,
)
from brain.systems.slack.client import slack_web_client_from_runtime


CandidateProvider = Callable[
    ..., Awaitable[tuple[SchedulerOverdueCandidate, ...]]
]
LivenessCheckpointProvider = Callable[[AsyncSession], Awaitable[datetime | None]]
AlertDelivery = Callable[..., Awaitable[None]]
AlertClaim = Callable[..., Awaitable[bool]]
AlertRelease = Callable[[], Awaitable[None]]
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _SchedulerOverdueObservation:
    candidates: tuple[SchedulerOverdueCandidate, ...]
    last_tick_at: datetime | None


@dataclass(frozen=True, slots=True)
class _SchedulerOverdueCheck:
    overdue_job_keys: tuple[str, ...]
    alert_sent: bool
    last_tick_at: datetime | None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _alert_summary(
    candidates: tuple[SchedulerOverdueCandidate, ...],
    *,
    now: datetime,
    last_tick_at: datetime | None,
) -> str:
    job_lines = [
        f"- {candidate.job_key}: {candidate.lag_seconds_at(now) // 60}m overdue "
        f"(due {candidate.next_run_at.isoformat()})"
        for candidate in candidates
    ]
    tick_text = last_tick_at.isoformat() if last_tick_at else "unknown"
    return "\n".join(("Overdue jobs:", *job_lines, f"Daemon last tick: {tick_text}"))


class SchedulerOverdueMonitor:
    """Monitor scheduler progress through one shared durable freeze latch."""

    name = "scheduler_overdue_monitor"
    overdue_after = timedelta(minutes=15)
    check_interval_seconds = 60.0

    def __init__(
        self,
        *,
        candidate_provider: CandidateProvider = async_scheduler_overdue_candidates,
        liveness_checkpoint: LivenessCheckpointProvider = scheduler_liveness_checkpoint,
        claim_alert: AlertClaim = claim_scheduler_overdue_alert,
        release_alert: AlertRelease = release_scheduler_overdue_alert,
        deliver_alert: AlertDelivery = async_deliver_failure_alert,
    ) -> None:
        self._candidate_provider = candidate_provider
        self._liveness_checkpoint = liveness_checkpoint
        self._claim_alert = claim_alert
        self._release_alert = release_alert
        self._deliver_alert = deliver_alert

    async def run(self) -> None:
        """Check scheduler progress on the independent API event loop."""
        while True:
            await asyncio.sleep(self.check_interval_seconds)
            try:
                await self._run_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the next interval retries
                logger.exception("Scheduler overdue health check failed safely")

    async def _run_once(
        self,
        *,
        now: datetime | None = None,
    ) -> _SchedulerOverdueCheck:
        """Read in one transaction, then evaluate and deliver after it closes."""
        from brain.platform.db.repositories.unit_of_work import UnitOfWork

        reference_time = self._reference_time(now)
        async with UnitOfWork() as uow:
            observation = await self._observe(uow.session, now=reference_time)
        return await self._evaluate(observation, now=reference_time)

    async def _check(
        self,
        session: AsyncSession,
        *,
        now: datetime | None = None,
    ) -> _SchedulerOverdueCheck:
        """Evaluate one check with a caller-owned database session."""
        reference_time = self._reference_time(now)
        observation = await self._observe(session, now=reference_time)
        return await self._evaluate(observation, now=reference_time)

    async def _observe(
        self,
        session: AsyncSession,
        *,
        now: datetime,
    ) -> _SchedulerOverdueObservation:
        candidates = await self._candidate_provider(session, now=now)
        return _SchedulerOverdueObservation(
            candidates=candidates,
            last_tick_at=await self._liveness_checkpoint(session),
        )

    async def _evaluate(
        self,
        observation: _SchedulerOverdueObservation,
        *,
        now: datetime,
    ) -> _SchedulerOverdueCheck:
        overdue_candidates = tuple(
            candidate
            for candidate in observation.candidates
            if candidate.lag_seconds_at(now) > self.overdue_after.total_seconds()
        )
        overdue_job_keys = tuple(
            candidate.job_key for candidate in overdue_candidates
        )
        if not overdue_candidates:
            await self._release_alert()
            return _SchedulerOverdueCheck(
                overdue_job_keys=(),
                alert_sent=False,
                last_tick_at=observation.last_tick_at,
            )
        if not await self._claim_alert(alerted_at=now):
            return _SchedulerOverdueCheck(
                overdue_job_keys=overdue_job_keys,
                alert_sent=False,
                last_tick_at=observation.last_tick_at,
            )

        await self._deliver_alert(
            policy=SlackFailureAlertPolicy(
                provide_client=slack_web_client_from_runtime,
                requested_by=self.name,
                reason="Deliver an overdue scheduler job alert to the team.",
                channel=(
                    os.getenv("ILLO_SCHEDULER_FAILURE_ALERT_CHANNEL", "").strip()
                    or "#alerts"
                ),
                unknown_error_text="Scheduler jobs stopped advancing",
            ),
            subject=FailureAlertSubject(
                identity_label="Job key",
                identity=overdue_candidates[0].job_key,
                url_label="Scheduler",
                url=f"{public_app_base_url()}/api/system/scheduler",
                link_label="open scheduler state",
            ),
            presentation=FailureAlertPresentation(
                title="Scheduler jobs overdue",
                summary=_alert_summary(
                    overdue_candidates,
                    now=now,
                    last_tick_at=observation.last_tick_at,
                ),
            ),
            error_text="Scheduler jobs stopped advancing past next_run_at.",
        )
        return _SchedulerOverdueCheck(
            overdue_job_keys=overdue_job_keys,
            alert_sent=True,
            last_tick_at=observation.last_tick_at,
        )

    @staticmethod
    def _reference_time(now: datetime | None) -> datetime:
        reference_time = now or _utc_now()
        if reference_time.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        return reference_time


__all__ = ["SchedulerOverdueMonitor"]
