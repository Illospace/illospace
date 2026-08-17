"""Alert when an enabled Cycle schedule misses its receipt window."""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
import os

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from brain.kernel.common.time import assume_utc_optional
from brain.platform.db.models.cycle import (
    Cycle,
    CycleFailureGuardLatch,
    CycleRun,
)
from brain.systems.cycles.common import cycle_executor_binding
from brain.systems.cycles.schedules import compute_latest_run_at
from brain.systems.cycles.silence_policy import (
    CycleSilencePolicy,
    async_cycle_silence_policy,
)
from brain.systems.cortex.thread_links import public_app_base_url
from brain.systems.failure_guard.core import FailureGuardTriggerKind
from brain.systems.failure_guard.cycle_latches import (
    async_release_cycle_alert_latch,
    async_try_claim_cycle_alert_latch,
)
from brain.systems.failure_guard.slack_delivery import (
    FailureAlertPresentation,
    FailureAlertSubject,
    SlackFailureAlertPolicy,
    async_deliver_failure_alert,
)
from brain.systems.slack.client import slack_web_client_from_runtime


logger = logging.getLogger(__name__)

CYCLE_MISSED_RECEIPT_TRIGGER_KIND = FailureGuardTriggerKind("missed_receipt")


@dataclass(frozen=True, slots=True)
class CycleSilenceCandidate:
    cycle_id: int
    name: str
    binding: str
    expected_at: datetime
    last_receipt_at: datetime | None
    grace_margin: timedelta


@dataclass(frozen=True, slots=True)
class CycleSilenceObservation:
    candidates: tuple[CycleSilenceCandidate, ...]
    latched_cycle_ids: frozenset[int]


@dataclass(frozen=True, slots=True)
class CycleSilenceCheck:
    overdue_cycle_ids: tuple[int, ...]
    alerted_cycle_ids: tuple[int, ...]


PolicyProvider = Callable[[AsyncSession], Awaitable[CycleSilencePolicy]]
CandidateProvider = Callable[..., Awaitable[CycleSilenceObservation]]
AlertClaim = Callable[..., Awaitable[bool]]
AlertRelease = Callable[..., Awaitable[None]]
AlertDelivery = Callable[..., Awaitable[None]]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _latest_receipt_subquery():
    return (
        select(func.max(CycleRun.completed_at))
        .where(CycleRun.cycle_id == Cycle.id)
        .correlate(Cycle)
        .scalar_subquery()
    )


def _missed_receipt_candidate(
    cycle: Cycle,
    *,
    last_receipt_at: datetime | None,
    now: datetime,
    grace_margin: timedelta,
) -> CycleSilenceCandidate | None:
    expected_at = compute_latest_run_at(
        cycle.schedule_expr,
        cycle.timezone,
        at_or_before=now - grace_margin,
    )
    if expected_at is None:
        return None
    monitoring_started_at = assume_utc_optional(
        getattr(cycle, "receipt_monitoring_started_at", None)
        or getattr(cycle, "created_at", None)
    )
    if monitoring_started_at is None or expected_at < monitoring_started_at:
        return None
    normalized_receipt = assume_utc_optional(last_receipt_at)
    if normalized_receipt is not None and normalized_receipt >= expected_at:
        return None
    return CycleSilenceCandidate(
        cycle_id=int(cycle.id),
        name=str(cycle.name),
        binding=cycle_executor_binding(cycle),
        expected_at=expected_at,
        last_receipt_at=normalized_receipt,
        grace_margin=grace_margin,
    )


async def async_cycle_silence_observation(
    session: AsyncSession,
    *,
    now: datetime,
    grace_margin: timedelta,
) -> CycleSilenceObservation:
    """Read every enabled binding and its latest completed CycleRun receipt."""
    latest_receipt = _latest_receipt_subquery()
    rows = (
        await session.execute(
            select(Cycle, latest_receipt.label("last_receipt_at"))
            .where(
                Cycle.enabled.is_(True),
                Cycle.deleted_at.is_(None),
            )
            .order_by(Cycle.id.asc())
        )
    ).all()
    candidates: list[CycleSilenceCandidate] = []
    for cycle, last_receipt_at in rows:
        try:
            candidate = _missed_receipt_candidate(
                cycle,
                last_receipt_at=last_receipt_at,
                now=now,
                grace_margin=grace_margin,
            )
        except ValueError:
            logger.exception(
                "Cycle receipt window could not be evaluated: cycle_id=%s",
                cycle.id,
            )
            continue
        if candidate is not None:
            candidates.append(candidate)

    latched_cycle_ids = frozenset(
        int(cycle_id)
        for cycle_id in (
            await session.scalars(
                select(CycleFailureGuardLatch.cycle_id).where(
                    CycleFailureGuardLatch.trigger_kind
                    == str(CYCLE_MISSED_RECEIPT_TRIGGER_KIND)
                )
            )
        ).all()
    )
    return CycleSilenceObservation(
        candidates=tuple(candidates),
        latched_cycle_ids=latched_cycle_ids,
    )


async def _claim_cycle_silence_alert(
    *,
    cycle_id: int,
    alerted_at: datetime,
) -> bool:
    from brain.platform.db.repositories.unit_of_work import UnitOfWork

    async with UnitOfWork() as uow:
        return await async_try_claim_cycle_alert_latch(
            uow.session,
            cycle_id=cycle_id,
            trigger_kind=CYCLE_MISSED_RECEIPT_TRIGGER_KIND,
            alerted_at=alerted_at,
        )


async def _release_cycle_silence_alert(
    *,
    cycle_id: int,
    alerted_at: datetime | None = None,
) -> None:
    from brain.platform.db.repositories.unit_of_work import UnitOfWork

    async with UnitOfWork() as uow:
        await async_release_cycle_alert_latch(
            uow.session,
            cycle_id=cycle_id,
            trigger_kind=CYCLE_MISSED_RECEIPT_TRIGGER_KIND,
            alerted_at=alerted_at,
        )


def _minutes(duration: timedelta) -> int:
    return int(duration.total_seconds() // 60)


def _candidate_summary(candidate: CycleSilenceCandidate) -> str:
    last_seen = (
        candidate.last_receipt_at.isoformat()
        if candidate.last_receipt_at is not None
        else "never"
    )
    return "\n".join(
        (
            f"Binding: {candidate.binding}",
            f"Expected receipt: {candidate.expected_at.isoformat()}",
            f"Last receipt: {last_seen}",
            f"Grace margin: {_minutes(candidate.grace_margin)}m",
        )
    )


class CycleSilenceMonitor:
    """Check receipt progress on the API loop, independent of executors."""

    name = "cycle_silence_monitor"
    check_interval_seconds = 5 * 60.0

    def __init__(
        self,
        *,
        policy_provider: PolicyProvider = async_cycle_silence_policy,
        candidate_provider: CandidateProvider = async_cycle_silence_observation,
        claim_alert: AlertClaim = _claim_cycle_silence_alert,
        release_alert: AlertRelease = _release_cycle_silence_alert,
        deliver_alert: AlertDelivery = async_deliver_failure_alert,
    ) -> None:
        self._policy_provider = policy_provider
        self._candidate_provider = candidate_provider
        self._claim_alert = claim_alert
        self._release_alert = release_alert
        self._deliver_alert = deliver_alert

    async def run(self) -> None:
        while True:
            await asyncio.sleep(self.check_interval_seconds)
            try:
                await self._run_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the next interval retries
                logger.exception("Cycle receipt-silence check failed safely")

    async def _run_once(
        self,
        *,
        now: datetime | None = None,
    ) -> CycleSilenceCheck:
        from brain.platform.db.repositories.unit_of_work import UnitOfWork

        reference_time = assume_utc_optional(now or _utc_now())
        if reference_time is None:
            raise ValueError("now is required")
        async with UnitOfWork() as uow:
            observation = await self._observe(uow.session, now=reference_time)
        return await self._evaluate(observation, now=reference_time)

    async def _check(
        self,
        session: AsyncSession,
        *,
        now: datetime | None = None,
    ) -> CycleSilenceCheck:
        reference_time = assume_utc_optional(now or _utc_now())
        if reference_time is None:
            raise ValueError("now is required")
        observation = await self._observe(session, now=reference_time)
        return await self._evaluate(observation, now=reference_time)

    async def _observe(
        self,
        session: AsyncSession,
        *,
        now: datetime,
    ) -> CycleSilenceObservation:
        policy = await self._policy_provider(session)
        return await self._candidate_provider(
            session,
            now=now,
            grace_margin=policy.grace_margin,
        )

    async def _evaluate(
        self,
        observation: CycleSilenceObservation,
        *,
        now: datetime,
    ) -> CycleSilenceCheck:
        overdue_ids = tuple(candidate.cycle_id for candidate in observation.candidates)
        overdue_id_set = set(overdue_ids)
        for cycle_id in sorted(observation.latched_cycle_ids - overdue_id_set):
            await self._release_alert(cycle_id=cycle_id)

        alerted_ids: list[int] = []
        for candidate in observation.candidates:
            claimed = await self._claim_alert(
                cycle_id=candidate.cycle_id,
                alerted_at=now,
            )
            if not claimed:
                continue
            try:
                await self._deliver(candidate)
            except Exception:  # noqa: BLE001 - release permits a later retry
                logger.exception(
                    "Cycle receipt-silence Slack delivery failed: cycle_id=%s",
                    candidate.cycle_id,
                )
                await self._release_alert(
                    cycle_id=candidate.cycle_id,
                    alerted_at=now,
                )
                continue
            alerted_ids.append(candidate.cycle_id)
        return CycleSilenceCheck(
            overdue_cycle_ids=overdue_ids,
            alerted_cycle_ids=tuple(alerted_ids),
        )

    async def _deliver(self, candidate: CycleSilenceCandidate) -> None:
        last_seen = (
            candidate.last_receipt_at.isoformat()
            if candidate.last_receipt_at is not None
            else "never"
        )
        await self._deliver_alert(
            policy=SlackFailureAlertPolicy(
                provide_client=slack_web_client_from_runtime,
                requested_by=self.name,
                reason="Deliver a missed Cycle receipt alert to the team.",
                channel=(
                    os.getenv("ILLO_CYCLE_FAILURE_ALERT_CHANNEL", "").strip()
                    or "#alerts"
                ),
                unknown_error_text="Cycle receipt is missing",
            ),
            subject=FailureAlertSubject(
                identity_label="Schedule",
                identity=f"{candidate.name} (#{candidate.cycle_id})",
                url_label="Schedule",
                url=f"{public_app_base_url()}/cycles?cycle_id={candidate.cycle_id}",
                link_label="open schedule state",
            ),
            presentation=FailureAlertPresentation(
                title="Cycle schedule missed receipt",
                summary=_candidate_summary(candidate),
            ),
            error_text=(
                f"Expected a receipt at {candidate.expected_at.isoformat()}; "
                f"last seen {last_seen}."
            ),
        )


__all__ = [
    "CYCLE_MISSED_RECEIPT_TRIGGER_KIND",
    "CycleSilenceCandidate",
    "CycleSilenceCheck",
    "CycleSilenceMonitor",
    "CycleSilenceObservation",
    "async_cycle_silence_observation",
]
