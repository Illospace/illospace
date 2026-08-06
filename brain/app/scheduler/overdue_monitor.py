"""Alert when scheduler-owned jobs are late or keep failing.

The health latches are process-local and belong to the one monitor hosted by the
API lifespan. An API restart during an active condition can therefore repeat an
alert. Restart-safe or multi-replica deduplication needs a scheduler-global state
table and a database migration.
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
from brain.app.scheduler.read_models import (
    SchedulerFailureCandidate,
    SchedulerOverdueCandidate,
    async_scheduler_failure_candidates,
    async_scheduler_overdue_candidates,
)
from brain.app.scheduler.scheduler_failure_guard import (
    ConsecutiveFailuresTrigger,
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
FailureCandidateProvider = Callable[
    ..., Awaitable[tuple[SchedulerFailureCandidate, ...]]
]
LivenessCheckpointProvider = Callable[[AsyncSession], Awaitable[datetime | None]]
AlertDelivery = Callable[..., Awaitable[None]]
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _SchedulerHealthObservation:
    candidates: tuple[SchedulerOverdueCandidate, ...]
    failure_candidates: tuple[SchedulerFailureCandidate, ...]
    last_tick_at: datetime | None


@dataclass(frozen=True, slots=True)
class _SchedulerHealthCheck:
    overdue_job_keys: tuple[str, ...]
    failure_job_keys: tuple[str, ...]
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


def _failure_alert_summary(
    candidates: tuple[SchedulerFailureCandidate, ...],
) -> str:
    job_lines = []
    for candidate in candidates:
        history = (
            "zero lifetime successes"
            if candidate.lifetime_success_count == 0
            else f"{candidate.failure_count} consecutive failures"
        )
        signature = candidate.failure_signature or "unknown"
        job_lines.append(
            f"- {candidate.job_key}: {history}; "
            f"failure signature {signature}"
        )
    return "\n".join(("Failing jobs:", *job_lines))


class SchedulerOverdueMonitor:
    """Own API-process scheduler overdue and failure health latches."""

    name = "scheduler_overdue_monitor"
    overdue_after = timedelta(minutes=15)
    check_interval_seconds = 60.0

    def __init__(
        self,
        *,
        candidate_provider: CandidateProvider = async_scheduler_overdue_candidates,
        failure_candidate_provider: FailureCandidateProvider = (
            async_scheduler_failure_candidates
        ),
        liveness_checkpoint: LivenessCheckpointProvider = scheduler_liveness_checkpoint,
        deliver_alert: AlertDelivery = async_deliver_failure_alert,
    ) -> None:
        self._candidate_provider = candidate_provider
        self._failure_candidate_provider = failure_candidate_provider
        self._liveness_checkpoint = liveness_checkpoint
        self._deliver_alert = deliver_alert
        self._alerted_for_current_freeze = False
        self._alerted_failure_job_keys: set[str] = set()

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
    ) -> _SchedulerHealthCheck:
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
    ) -> _SchedulerHealthCheck:
        """Evaluate one check with a caller-owned database session."""
        reference_time = self._reference_time(now)
        observation = await self._observe(session, now=reference_time)
        return await self._evaluate(observation, now=reference_time)

    async def _observe(
        self,
        session: AsyncSession,
        *,
        now: datetime,
    ) -> _SchedulerHealthObservation:
        candidates = await self._candidate_provider(session, now=now)
        return _SchedulerHealthObservation(
            candidates=candidates,
            failure_candidates=await self._failure_candidate_provider(
                session,
                failure_streak_threshold=(
                    ConsecutiveFailuresTrigger.from_settings().threshold
                ),
            ),
            last_tick_at=await self._liveness_checkpoint(session),
        )

    async def _evaluate(
        self,
        observation: _SchedulerHealthObservation,
        *,
        now: datetime,
    ) -> _SchedulerHealthCheck:
        overdue_candidates = tuple(
            candidate
            for candidate in observation.candidates
            if candidate.lag_seconds_at(now) > self.overdue_after.total_seconds()
        )
        overdue_job_keys = tuple(
            candidate.job_key for candidate in overdue_candidates
        )
        alert_sent = False
        if not overdue_candidates:
            self._alerted_for_current_freeze = False
        elif not self._alerted_for_current_freeze:
            await self._deliver_alert(
                policy=SlackFailureAlertPolicy(
                    provide_client=slack_web_client_from_runtime,
                    requested_by=self.name,
                    reason="Deliver an overdue scheduler job alert to the team.",
                    channel=(
                        os.getenv(
                            "ILLO_SCHEDULER_FAILURE_ALERT_CHANNEL", ""
                        ).strip()
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
            self._alerted_for_current_freeze = True
            alert_sent = True

        current_failure_job_keys = {
            candidate.job_key for candidate in observation.failure_candidates
        }
        self._alerted_failure_job_keys.intersection_update(
            current_failure_job_keys
        )
        new_failure_candidates = tuple(
            candidate
            for candidate in observation.failure_candidates
            if candidate.job_key not in self._alerted_failure_job_keys
        )
        if new_failure_candidates:
            await self._deliver_alert(
                policy=SlackFailureAlertPolicy(
                    provide_client=slack_web_client_from_runtime,
                    requested_by=self.name,
                    reason="Deliver a failing scheduler job alert to the team.",
                    channel=(
                        os.getenv(
                            "ILLO_SCHEDULER_FAILURE_ALERT_CHANNEL", ""
                        ).strip()
                        or "#alerts"
                    ),
                    unknown_error_text="Scheduler jobs keep failing",
                ),
                subject=FailureAlertSubject(
                    identity_label="Job key",
                    identity=new_failure_candidates[0].job_key,
                    url_label="Scheduler",
                    url=f"{public_app_base_url()}/api/system/scheduler",
                    link_label="open scheduler state",
                ),
                presentation=FailureAlertPresentation(
                    title="Scheduler jobs failing",
                    summary=_failure_alert_summary(new_failure_candidates),
                ),
                error_text=(
                    new_failure_candidates[0].last_failure_error
                    or "Scheduler job settled as failure"
                ),
            )
            self._alerted_failure_job_keys.update(
                candidate.job_key for candidate in new_failure_candidates
            )
            alert_sent = True

        return _SchedulerHealthCheck(
            overdue_job_keys=overdue_job_keys,
            failure_job_keys=tuple(
                candidate.job_key
                for candidate in observation.failure_candidates
            ),
            alert_sent=alert_sent,
            last_tick_at=observation.last_tick_at,
        )

    @staticmethod
    def _reference_time(now: datetime | None) -> datetime:
        reference_time = now or _utc_now()
        if reference_time.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        return reference_time


__all__ = ["SchedulerOverdueMonitor"]
