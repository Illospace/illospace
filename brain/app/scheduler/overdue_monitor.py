"""Alert while scheduler-owned jobs stop advancing.

The freeze latch is scheduler-global and durable. Each API monitor atomically
claims the same stable database key in a short transaction, then delivers only
after that claim commits. The row also records the freeze start and next alert
time, so API restarts and concurrent replicas share one delivery at each
duration threshold without holding a database transaction across Slack. A
healthy observation releases the key and reports recovery.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
import os

from sqlalchemy import and_, delete, or_
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from brain.app.scheduler.cold_start import scheduler_liveness_checkpoint
from brain.app.scheduler.overdue_alert_state import (
    SCHEDULER_OVERDUE_FREEZE_ALERT_KEY,
)
from brain.app.scheduler.read_models import (
    SchedulerOverdueCandidate,
    async_scheduler_overdue_candidates,
)
from brain.platform.db.models.scheduler import SchedulerAlertLatch
from brain.systems.cortex.thread_links import public_app_base_url
from brain.systems.failure_guard.slack_delivery import (
    FailureAlertPresentation,
    FailureAlertSubject,
    SlackFailureAlertPolicy,
    async_deliver_failure_alert,
)
from brain.systems.slack.client import slack_web_client_from_runtime


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


@dataclass(frozen=True, slots=True)
class _SchedulerOverdueAlertState:
    alerted_at: datetime
    freeze_started_at: datetime | None
    next_alert_at: datetime | None


CandidateProvider = Callable[
    ..., Awaitable[tuple[SchedulerOverdueCandidate, ...]]
]
LivenessCheckpointProvider = Callable[[AsyncSession], Awaitable[datetime | None]]
AlertDelivery = Callable[..., Awaitable[None]]
AlertClaim = Callable[..., Awaitable[bool]]
AlertRelease = Callable[[], Awaitable[_SchedulerOverdueAlertState | None]]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _elapsed_text(*, started_at: datetime, ended_at: datetime) -> str:
    elapsed_minutes = max(
        0,
        int(
            (_aware_utc(ended_at) - _aware_utc(started_at)).total_seconds()
            // 60
        ),
    )
    hours, minutes = divmod(elapsed_minutes, 60)
    if hours and minutes:
        return f"{hours}h {minutes}m"
    if hours:
        return f"{hours}h"
    return f"{minutes}m"


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
    if last_tick_at is None:
        daemon_line = "Daemon last tick and freeze duration are unknown."
    else:
        normalized_tick = _aware_utc(last_tick_at)
        daemon_line = (
            "Daemon has not ticked for "
            f"{_elapsed_text(started_at=normalized_tick, ended_at=now)} "
            f"(last tick {normalized_tick.isoformat()})."
        )
    return "\n".join(("Overdue jobs:", *job_lines, daemon_line))


def _alert_latch_insert(session: AsyncSession):
    if session.get_bind().dialect.name == "sqlite":
        return sqlite_insert(SchedulerAlertLatch)
    return postgresql_insert(SchedulerAlertLatch)


async def _try_claim_scheduler_overdue_alert(
    session: AsyncSession,
    *,
    alerted_at: datetime,
    freeze_started_at: datetime,
    next_alert_at: datetime,
) -> bool:
    """Atomically claim the first alert or one due escalation threshold."""
    claim = (
        _alert_latch_insert(session)
        .values(
            alert_key=SCHEDULER_OVERDUE_FREEZE_ALERT_KEY,
            alerted_at=alerted_at,
            freeze_started_at=freeze_started_at,
            next_alert_at=next_alert_at,
        )
        .on_conflict_do_update(
            index_elements=[SchedulerAlertLatch.alert_key],
            set_={
                "alerted_at": alerted_at,
                "freeze_started_at": freeze_started_at,
                "next_alert_at": next_alert_at,
            },
            where=or_(
                SchedulerAlertLatch.freeze_started_at.is_(None),
                and_(
                    SchedulerAlertLatch.freeze_started_at == freeze_started_at,
                    or_(
                        SchedulerAlertLatch.next_alert_at.is_(None),
                        SchedulerAlertLatch.next_alert_at <= alerted_at,
                    ),
                ),
            ),
        )
        .returning(SchedulerAlertLatch.alert_key)
    )
    result = await session.execute(claim)
    return result.scalar_one_or_none() == SCHEDULER_OVERDUE_FREEZE_ALERT_KEY


async def _claim_scheduler_overdue_alert(
    *,
    alerted_at: datetime,
    freeze_started_at: datetime,
    next_alert_at: datetime,
) -> bool:
    """Claim and commit one overdue alert threshold in a short transaction."""
    from brain.platform.db.repositories.unit_of_work import UnitOfWork

    async with UnitOfWork() as uow:
        return await _try_claim_scheduler_overdue_alert(
            uow.session,
            alerted_at=alerted_at,
            freeze_started_at=freeze_started_at,
            next_alert_at=next_alert_at,
        )


async def _release_scheduler_overdue_alert() -> _SchedulerOverdueAlertState | None:
    """Release and return the overdue latch in one short transaction."""
    from brain.platform.db.repositories.unit_of_work import UnitOfWork

    async with UnitOfWork() as uow:
        released = await uow.session.execute(
            delete(SchedulerAlertLatch)
            .where(
                SchedulerAlertLatch.alert_key
                == SCHEDULER_OVERDUE_FREEZE_ALERT_KEY
            )
            .returning(
                SchedulerAlertLatch.alerted_at,
                SchedulerAlertLatch.freeze_started_at,
                SchedulerAlertLatch.next_alert_at,
            )
        )
        row = released.one_or_none()
        if row is None:
            return None
        return _SchedulerOverdueAlertState(
            alerted_at=row.alerted_at,
            freeze_started_at=row.freeze_started_at,
            next_alert_at=row.next_alert_at,
        )


class SchedulerOverdueMonitor:
    """Monitor progress through one shared, duration-aware freeze latch."""

    name = "scheduler_overdue_monitor"
    overdue_after = timedelta(minutes=15)
    check_interval_seconds = 60.0
    escalation_thresholds = (
        timedelta(hours=1),
        timedelta(hours=4),
        timedelta(hours=12),
    )
    repeat_escalation_interval = timedelta(hours=12)

    def __init__(
        self,
        *,
        candidate_provider: CandidateProvider = async_scheduler_overdue_candidates,
        liveness_checkpoint: LivenessCheckpointProvider = scheduler_liveness_checkpoint,
        claim_alert: AlertClaim = _claim_scheduler_overdue_alert,
        release_alert: AlertRelease = _release_scheduler_overdue_alert,
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
            released = await self._release_alert()
            if released is not None:
                await self._deliver_recovery(
                    released,
                    recovered_at=observation.last_tick_at or now,
                )
            return _SchedulerOverdueCheck(
                overdue_job_keys=(),
                alert_sent=False,
                last_tick_at=observation.last_tick_at,
            )
        freeze_started_at = _aware_utc(observation.last_tick_at or now)
        if not await self._claim_alert(
            alerted_at=now,
            freeze_started_at=freeze_started_at,
            next_alert_at=self._next_escalation_at(
                freeze_started_at=freeze_started_at,
                now=now,
            ),
        ):
            return _SchedulerOverdueCheck(
                overdue_job_keys=overdue_job_keys,
                alert_sent=False,
                last_tick_at=observation.last_tick_at,
            )

        try:
            await self._deliver_alert(
                policy=SlackFailureAlertPolicy(
                    provide_client=slack_web_client_from_runtime,
                    requested_by=self.name,
                    reason="Deliver an overdue scheduler job alert to the team.",
                    channel=(
                        os.getenv(
                            "ILLO_SCHEDULER_FAILURE_ALERT_CHANNEL",
                            "",
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
        except Exception:  # noqa: BLE001 - release lets the next tick retry
            try:
                await self._release_alert()
            except Exception:  # noqa: BLE001 - preserve the delivery error
                logger.exception(
                    "Failed to release scheduler overdue alert after delivery failure"
                )
            raise
        return _SchedulerOverdueCheck(
            overdue_job_keys=overdue_job_keys,
            alert_sent=True,
            last_tick_at=observation.last_tick_at,
        )

    def _next_escalation_at(
        self,
        *,
        freeze_started_at: datetime,
        now: datetime,
    ) -> datetime:
        freeze_started_at = _aware_utc(freeze_started_at)
        elapsed = _aware_utc(now) - freeze_started_at
        for threshold in self.escalation_thresholds:
            if elapsed < threshold:
                return freeze_started_at + threshold

        repeat_count = elapsed // self.repeat_escalation_interval
        return freeze_started_at + (
            (repeat_count + 1) * self.repeat_escalation_interval
        )

    async def _deliver_recovery(
        self,
        released: _SchedulerOverdueAlertState,
        *,
        recovered_at: datetime,
    ) -> None:
        recovered_at = _aware_utc(recovered_at)
        freeze_started_at = _aware_utc(
            released.freeze_started_at or released.alerted_at
        )
        try:
            await self._deliver_alert(
                policy=SlackFailureAlertPolicy(
                    provide_client=slack_web_client_from_runtime,
                    requested_by=self.name,
                    reason="Report that overdue scheduler jobs recovered.",
                    channel=(
                        os.getenv(
                            "ILLO_SCHEDULER_FAILURE_ALERT_CHANNEL",
                            "",
                        ).strip()
                        or "#alerts"
                    ),
                    unknown_error_text="Scheduler jobs recovered",
                ),
                subject=FailureAlertSubject(
                    identity_label="Alert",
                    identity=SCHEDULER_OVERDUE_FREEZE_ALERT_KEY,
                    url_label="Scheduler",
                    url=f"{public_app_base_url()}/api/system/scheduler",
                    link_label="open scheduler state",
                ),
                presentation=FailureAlertPresentation(
                    title="Scheduler jobs recovered",
                    summary=(
                        f"Daemon resumed ticking at {recovered_at.isoformat()} "
                        "after "
                        f"{_elapsed_text(started_at=freeze_started_at, ended_at=recovered_at)} "
                        "without a tick."
                    ),
                ),
                error_text="Scheduler jobs are advancing again.",
            )
        except Exception:  # noqa: BLE001 - restore so recovery can retry
            try:
                await self._claim_alert(
                    alerted_at=_aware_utc(released.alerted_at),
                    freeze_started_at=freeze_started_at,
                    next_alert_at=_aware_utc(
                        released.next_alert_at
                        or self._next_escalation_at(
                            freeze_started_at=freeze_started_at,
                            now=released.alerted_at,
                        )
                    ),
                )
            except Exception:  # noqa: BLE001 - preserve the delivery error
                logger.exception(
                    "Failed to restore scheduler overdue alert after recovery delivery failure"
                )
            raise

    @staticmethod
    def _reference_time(now: datetime | None) -> datetime:
        reference_time = now or _utc_now()
        if reference_time.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        return reference_time


__all__ = ["SchedulerOverdueMonitor"]
