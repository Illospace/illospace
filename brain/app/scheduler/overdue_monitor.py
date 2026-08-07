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

from sqlalchemy.ext.asyncio import AsyncSession

from brain.app.scheduler.cold_start import scheduler_liveness_checkpoint
from brain.app.scheduler.overdue_alert_state import (
    SCHEDULER_OVERDUE_FREEZE_ALERT_KEY,
    SchedulerOverdueAlertState,
    SchedulerSelfHealState,
    claim_scheduler_self_heal,
    claim_scheduler_overdue_alert,
    release_scheduler_overdue_alert,
    release_scheduler_self_heal,
    release_scheduler_self_heal_claim,
)
from brain.app.scheduler.read_models import (
    SchedulerOverdueCandidate,
    async_scheduler_overdue_candidates,
)
from brain.kernel.common.env import env_int
from brain.kernel.common.time import assume_utc
from brain.systems.cortex.thread_links import public_app_base_url
from brain.systems.failure_guard.slack_delivery import (
    FailureAlertPresentation,
    FailureAlertSubject,
    SlackFailureAlertPolicy,
    async_deliver_failure_alert,
)
from brain.systems.slack.client import slack_web_client_from_runtime
from brain.systems.runtime_settings.runtime_services import (
    async_try_restart_runtime_services,
)


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
    self_heal_attempt: int | None = None


CandidateProvider = Callable[
    ..., Awaitable[tuple[SchedulerOverdueCandidate, ...]]
]
LivenessCheckpointProvider = Callable[[AsyncSession], Awaitable[datetime | None]]
AlertDelivery = Callable[..., Awaitable[None]]
AlertClaim = Callable[..., Awaitable[bool]]
AlertRelease = Callable[[], Awaitable[SchedulerOverdueAlertState | None]]
SelfHealClaim = Callable[..., Awaitable[SchedulerSelfHealState]]
SelfHealRelease = Callable[[], Awaitable[None]]
SelfHealClaimRelease = Callable[..., Awaitable[None]]
RuntimeServicesRestart = Callable[..., Awaitable[bool]]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _elapsed_text(*, started_at: datetime, ended_at: datetime) -> str:
    elapsed_minutes = max(
        0,
        int(
            (assume_utc(ended_at) - assume_utc(started_at)).total_seconds()
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
    failed_self_heal_attempts: int = 0,
) -> str:
    job_lines = [
        f"- {candidate.job_key}: {candidate.lag_seconds_at(now) // 60}m overdue "
        f"(due {candidate.next_run_at.isoformat()})"
        for candidate in candidates
    ]
    if last_tick_at is None:
        daemon_line = "Daemon last tick and freeze duration are unknown."
    else:
        normalized_tick = assume_utc(last_tick_at)
        daemon_line = (
            "Daemon has not ticked for "
            f"{_elapsed_text(started_at=normalized_tick, ended_at=now)} "
            f"(last tick {normalized_tick.isoformat()})."
        )
    lines = ["Overdue jobs:", *job_lines, daemon_line]
    if failed_self_heal_attempts:
        lines.append(
            f"Self-heal failed after {failed_self_heal_attempts} attempts; "
            "a human is needed."
        )
    return "\n".join(lines)


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
        claim_alert: AlertClaim = claim_scheduler_overdue_alert,
        release_alert: AlertRelease = release_scheduler_overdue_alert,
        claim_self_heal: SelfHealClaim = claim_scheduler_self_heal,
        release_self_heal: SelfHealRelease = release_scheduler_self_heal,
        release_self_heal_claim: SelfHealClaimRelease = (
            release_scheduler_self_heal_claim
        ),
        restart_runtime_services: RuntimeServicesRestart = (
            async_try_restart_runtime_services
        ),
        deliver_alert: AlertDelivery = async_deliver_failure_alert,
        self_heal_after: timedelta | None = None,
        self_heal_max_attempts: int | None = None,
    ) -> None:
        self._candidate_provider = candidate_provider
        self._liveness_checkpoint = liveness_checkpoint
        self._claim_alert = claim_alert
        self._release_alert = release_alert
        self._claim_self_heal = claim_self_heal
        self._release_self_heal = release_self_heal
        self._release_self_heal_claim = release_self_heal_claim
        self._restart_runtime_services = restart_runtime_services
        self._deliver_alert = deliver_alert
        self.self_heal_after = (
            self_heal_after
            if self_heal_after is not None
            else timedelta(
                minutes=env_int(
                    "SCHEDULER_SELF_HEAL_AFTER_MINUTES",
                    10,
                    minimum=1,
                )
            )
        )
        self.self_heal_max_attempts = (
            self_heal_max_attempts
            if self_heal_max_attempts is not None
            else env_int(
                "SCHEDULER_SELF_HEAL_MAX_ATTEMPTS",
                2,
                minimum=1,
            )
        )

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
                await self._release_self_heal()
                await self._deliver_recovery(
                    released,
                    recovered_at=observation.last_tick_at or now,
                    self_heal_attempts=released.self_heal_attempts,
                )
            return _SchedulerOverdueCheck(
                overdue_job_keys=(),
                alert_sent=False,
                last_tick_at=observation.last_tick_at,
            )
        freeze_started_at = min(
            assume_utc(candidate.next_run_at) + self.overdue_after
            for candidate in overdue_candidates
        )
        alert_claimed = await self._claim_alert(
            alerted_at=now,
            freeze_started_at=freeze_started_at,
            next_alert_at=self._next_escalation_at(
                freeze_started_at=freeze_started_at,
                now=now,
            ),
        )
        self_heal = await self._maybe_self_heal(now=now)

        if alert_claimed:
            try:
                await self._deliver_alert(
                    policy=SlackFailureAlertPolicy(
                        provide_client=slack_web_client_from_runtime,
                        requested_by=self.name,
                        reason="Deliver an overdue scheduler job alert to the team.",
                        channel=self._alert_channel(),
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
                            failed_self_heal_attempts=(
                                self_heal.attempts
                                if self_heal.exhausted
                                else 0
                            ),
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
            alert_sent=alert_claimed,
            last_tick_at=observation.last_tick_at,
            self_heal_attempt=self_heal.attempt,
        )

    async def _maybe_self_heal(
        self,
        *,
        now: datetime,
    ) -> SchedulerSelfHealState:
        state = await self._claim_self_heal(
            attempted_at=now,
            heal_after=self.self_heal_after,
            max_attempts=self.self_heal_max_attempts,
        )
        if state.attempt is None or state.freeze_started_at is None:
            return state

        try:
            queued = await self._restart_runtime_services(
                ["scheduler"],
                requested_by="scheduler-self-heal",
            )
        except Exception:  # noqa: BLE001 - alert escalation still proceeds
            logger.exception("Failed to queue automatic scheduler restart")
            return SchedulerSelfHealState(
                attempt=None,
                attempts=state.attempts,
                exhausted=state.attempts >= self.self_heal_max_attempts,
                freeze_started_at=state.freeze_started_at,
            )
        if not queued:
            await self._refund_self_heal_claim(state, attempted_at=now)
            return SchedulerSelfHealState(
                attempt=None,
                attempts=max(0, state.attempts - 1),
                exhausted=False,
                freeze_started_at=state.freeze_started_at,
            )

        try:
            await self._deliver_self_heal_alert(state, attempted_at=now)
        except Exception:  # noqa: BLE001 - the restart is already queued
            logger.exception("Failed to deliver scheduler self-heal alert")
        return state

    async def _refund_self_heal_claim(
        self,
        state: SchedulerSelfHealState,
        *,
        attempted_at: datetime,
    ) -> None:
        try:
            await self._release_self_heal_claim(
                attempted_at=attempted_at,
                freeze_started_at=state.freeze_started_at,
                attempt=state.attempt,
            )
        except Exception:  # noqa: BLE001 - preserve the queue result
            logger.exception("Failed to release unused scheduler self-heal claim")

    async def _deliver_self_heal_alert(
        self,
        state: SchedulerSelfHealState,
        *,
        attempted_at: datetime,
    ) -> None:
        assert state.attempt is not None
        assert state.freeze_started_at is not None
        await self._deliver_alert(
            policy=SlackFailureAlertPolicy(
                provide_client=slack_web_client_from_runtime,
                requested_by=self.name,
                reason="Report an automatic scheduler restart to the team.",
                channel=self._alert_channel(),
                unknown_error_text="Scheduler restart queued automatically",
            ),
            subject=FailureAlertSubject(
                identity_label="Service",
                identity="scheduler",
                url_label="Scheduler",
                url=f"{public_app_base_url()}/api/system/scheduler",
                link_label="open scheduler state",
            ),
            presentation=FailureAlertPresentation(
                title="Scheduler self-heal",
                summary=(
                    "Restarting scheduler automatically, freeze "
                    f"{_elapsed_text(started_at=state.freeze_started_at, ended_at=attempted_at)}, "
                    f"attempt {state.attempt}/{self.self_heal_max_attempts}."
                ),
            ),
            error_text="Scheduler jobs remain overdue.",
        )

    def _next_escalation_at(
        self,
        *,
        freeze_started_at: datetime,
        now: datetime,
    ) -> datetime:
        freeze_started_at = assume_utc(freeze_started_at)
        elapsed = assume_utc(now) - freeze_started_at
        for threshold in self.escalation_thresholds:
            if elapsed < threshold:
                return freeze_started_at + threshold

        repeat_count = elapsed // self.repeat_escalation_interval
        return freeze_started_at + (
            (repeat_count + 1) * self.repeat_escalation_interval
        )

    async def _deliver_recovery(
        self,
        released: SchedulerOverdueAlertState,
        *,
        recovered_at: datetime,
        self_heal_attempts: int,
    ) -> None:
        recovered_at = assume_utc(recovered_at)
        freeze_started_at = assume_utc(
            released.freeze_started_at or released.alerted_at
        )
        try:
            await self._deliver_alert(
                policy=SlackFailureAlertPolicy(
                    provide_client=slack_web_client_from_runtime,
                    requested_by=self.name,
                    reason="Report that overdue scheduler jobs recovered.",
                    channel=self._alert_channel(),
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
                        "Scheduler recovered after automatic restart, freeze "
                        f"{_elapsed_text(started_at=freeze_started_at, ended_at=recovered_at)}, "
                        f"attempts {self_heal_attempts}/{self.self_heal_max_attempts}."
                        if self_heal_attempts
                        else (
                            f"Scheduler jobs advanced at {recovered_at.isoformat()} "
                            "after an overdue freeze of "
                            f"{_elapsed_text(started_at=freeze_started_at, ended_at=recovered_at)}."
                        )
                    ),
                ),
                error_text="Scheduler jobs are advancing again.",
            )
        except Exception:  # noqa: BLE001 - restore so recovery can retry
            try:
                await self._claim_alert(
                    alerted_at=assume_utc(released.alerted_at),
                    freeze_started_at=freeze_started_at,
                    next_alert_at=assume_utc(
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
    def _alert_channel() -> str:
        return (
            os.getenv("ILLO_SCHEDULER_FAILURE_ALERT_CHANNEL", "").strip()
            or "#alerts"
        )

    @staticmethod
    def _reference_time(now: datetime | None) -> datetime:
        reference_time = now or _utc_now()
        if reference_time.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        return reference_time


__all__ = ["SchedulerOverdueMonitor"]
