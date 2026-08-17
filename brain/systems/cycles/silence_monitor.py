"""Orchestrate alerts when enabled Cycle schedules miss receipt windows."""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging

from sqlalchemy.ext.asyncio import AsyncSession

from brain.kernel.common.time import assume_utc_optional
from brain.platform.db.repositories.cycle_silence import CycleSilenceRepository
from brain.systems.cycles.silence_alerts import (
    async_deliver_cycle_silence_alert,
)
from brain.systems.cycles.silence_policy import (
    CycleSilenceCandidate,
    CycleSilencePolicy,
    async_cycle_silence_policy,
    evaluate_cycle_silence_candidate,
)
from brain.systems.failure_guard.core import FailureGuardTriggerKind
from brain.systems.failure_guard.cycle_latches import CycleAlertLatchStore


logger = logging.getLogger(__name__)

CYCLE_MISSED_RECEIPT_TRIGGER_KIND = FailureGuardTriggerKind("missed_receipt")


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
AlertDelivery = Callable[[CycleSilenceCandidate], Awaitable[None]]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


async def async_cycle_silence_observation(
    session: AsyncSession,
    *,
    now: datetime,
    grace_margin: timedelta,
) -> CycleSilenceObservation:
    """Evaluate persisted receipt snapshots and read current alert edges."""
    snapshots = await CycleSilenceRepository(session).list_receipt_snapshots()
    candidates: list[CycleSilenceCandidate] = []
    for snapshot in snapshots:
        try:
            candidate = evaluate_cycle_silence_candidate(
                snapshot,
                now=now,
                grace_margin=grace_margin,
            )
        except ValueError:
            logger.exception(
                "Cycle receipt window could not be evaluated: cycle_id=%s",
                snapshot.cycle_id,
            )
            continue
        if candidate is not None:
            candidates.append(candidate)

    latched_cycle_ids = await CycleAlertLatchStore.load_cycle_ids_for_trigger(
        session,
        CYCLE_MISSED_RECEIPT_TRIGGER_KIND,
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
        store = CycleAlertLatchStore(session=uow.session, cycle_id=cycle_id)
        return await store.try_claim_latch(
            CYCLE_MISSED_RECEIPT_TRIGGER_KIND,
            alerted_at,
        )


async def _release_cycle_silence_alert(
    *,
    cycle_id: int,
    alerted_at: datetime | None = None,
) -> None:
    from brain.platform.db.repositories.unit_of_work import UnitOfWork

    async with UnitOfWork() as uow:
        store = CycleAlertLatchStore(session=uow.session, cycle_id=cycle_id)
        await store.release_latch(
            CYCLE_MISSED_RECEIPT_TRIGGER_KIND,
            alerted_at=alerted_at,
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
        deliver_alert: AlertDelivery = async_deliver_cycle_silence_alert,
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
                await self._deliver_alert(candidate)
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


__all__ = [
    "CYCLE_MISSED_RECEIPT_TRIGGER_KIND",
    "CycleSilenceCheck",
    "CycleSilenceMonitor",
    "CycleSilenceObservation",
    "async_cycle_silence_observation",
]
