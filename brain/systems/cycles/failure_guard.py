"""Cycle adapter for the shared failure-guard engine and delivery path."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from brain.kernel.common.time import ensure_utc
from brain.platform.db.models.cycle import Cycle
from brain.systems.cortex.thread_links import public_app_base_url
from brain.systems.failure_guard import (
    CONSECUTIVE_TRIGGER_KIND,
    FailureAlertSubject,
    FailureGuardEvaluation,
    FailureGuardLatch,
    FailureGuardRegistry,
    FailureGuardResetEvent,
    FailureGuardSubject,
    FailureGuardTriggerKind,
    FailureGuardTriggerResult,
    async_deliver_failure_alert,
    async_record_failure,
    async_reset_failure_guard,
    positive_int_setting,
)


CYCLE_FAILURE_ALERT_THRESHOLD_DEFAULT = 3
CYCLE_FAILURE_STATUSES = frozenset({"failed", "auth_blocked", "degraded"})
CYCLE_SUCCESS_STATUS = "completed"

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CycleConsecutiveFailuresTrigger:
    """Present cycle streaks and immediate operator-actionable auth blocks."""

    threshold: int
    kind: FailureGuardTriggerKind = field(
        default=CONSECUTIVE_TRIGGER_KIND,
        init=False,
    )

    @classmethod
    def from_settings(cls) -> CycleConsecutiveFailuresTrigger:
        return cls(
            threshold=positive_int_setting(
                "SCHEDULER_FAILURE_ALERT_THRESHOLD",
                CYCLE_FAILURE_ALERT_THRESHOLD_DEFAULT,
            ),
        )

    async def evaluate(
        self,
        session: AsyncSession,
        subject: FailureGuardSubject,
        now: datetime,
    ) -> FailureGuardTriggerResult:
        del session, now
        count = int(subject.consecutive_failure_count or 0)
        auth_blocked = (
            getattr(subject, "_failure_guard_status", None) == "auth_blocked"
        )
        threshold = 1 if auth_blocked else self.threshold
        if auth_blocked:
            alert_title = "Cycle authentication blocked"
            alert_summary = (
                f"Failure count: {count}\n"
                "Window: 1 scheduled interval\n"
                "Action: reconnect OpenAI in Settings > Access"
            )
        else:
            alert_title = "Cycle repeated failure"
            alert_summary = (
                f"Failure count: {count}\n"
                f"Window: {threshold} consecutive scheduled runs"
            )
        return FailureGuardTriggerResult(
            active=count >= threshold,
            public_details={
                "count": count,
                "threshold": threshold,
                "window_runs": threshold,
                "auth_blocked": auth_blocked,
            },
            alert_title=alert_title,
            alert_summary=alert_summary,
        )

    async def should_reset(
        self,
        session: AsyncSession,
        subject: FailureGuardSubject,
        now: datetime,
        *,
        event: FailureGuardResetEvent,
    ) -> bool:
        del session, subject, now, event
        return True


def cycle_failure_guard_registry() -> FailureGuardRegistry:
    """Use the shared engine with the single latch cycles can persist."""
    return FailureGuardRegistry(
        triggers=(CycleConsecutiveFailuresTrigger.from_settings(),)
    )


@dataclass
class _InlineFailureGuardLatch:
    """Adapt a cycle-owned alert timestamp to the generic latch contract."""

    alerted_at: datetime


async def async_apply_cycle_terminal_failure_guard(
    session: AsyncSession,
    cycle: Cycle,
    *,
    cycle_run_id: int,
    status: str,
    error_text: str | None,
    now: datetime | None = None,
) -> FailureGuardEvaluation | None:
    """Record one cycle terminal outcome and deliver any crossed edge."""
    if status not in CYCLE_FAILURE_STATUSES | {CYCLE_SUCCESS_STATUS}:
        return None

    now = ensure_utc(now)
    locked_cycle = await session.scalar(
        select(Cycle)
        .where(Cycle.id == cycle.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if locked_cycle is None:
        raise ValueError(f"Cycle {cycle.id} not found")

    registry = cycle_failure_guard_registry()
    latches: dict[FailureGuardTriggerKind, FailureGuardLatch] = {}
    if locked_cycle.failure_alerted_at is not None:
        latches[CONSECUTIVE_TRIGGER_KIND] = _InlineFailureGuardLatch(
            alerted_at=locked_cycle.failure_alerted_at
        )

    async def delete_latch(
        trigger_kind: FailureGuardTriggerKind,
        latch: FailureGuardLatch,
    ) -> None:
        del trigger_kind, latch
        locked_cycle.failure_alerted_at = None

    if status == CYCLE_SUCCESS_STATUS:
        await async_reset_failure_guard(
            session,
            locked_cycle,
            now=now,
            registry=registry,
            latches=latches,
            delete_latch=delete_latch,
        )
        return None

    async def create_latch(
        trigger_kind: FailureGuardTriggerKind,
        alerted_at: datetime,
    ) -> FailureGuardLatch:
        if trigger_kind != CONSECUTIVE_TRIGGER_KIND:
            raise ValueError(
                f"Cycles cannot persist failure-guard trigger {trigger_kind}"
            )
        locked_cycle.failure_alerted_at = alerted_at
        return _InlineFailureGuardLatch(alerted_at=alerted_at)

    failure_error = str(error_text or f"Cycle ended with {status} status").strip()
    setattr(locked_cycle, "_failure_guard_status", status)
    try:
        evaluation = await async_record_failure(
            session,
            locked_cycle,
            failure_identity=f"cycle_status:{status}\n{failure_error}",
            error_text=failure_error,
            now=now,
            registry=registry,
            latches=latches,
            create_latch=create_latch,
            delete_latch=delete_latch,
        )
    finally:
        delattr(locked_cycle, "_failure_guard_status")

    if evaluation.crossed_edges:
        logger.error(
            "%s alert: cycle_id=%s cycle_run_id=%s crossed_triggers=%s "
            "failure_signature=%s error=%s",
            evaluation.crossed_edges[0].alert_title,
            locked_cycle.id,
            cycle_run_id,
            ",".join(str(edge.kind) for edge in evaluation.crossed_edges),
            evaluation.failure_signature,
            failure_error,
        )
        try:
            await async_deliver_failure_alert(
                subject=FailureAlertSubject(
                    identity_label="Cycle",
                    identity=f"{locked_cycle.name} (#{locked_cycle.id})",
                    url_label="Cycle",
                    url=(
                        f"{public_app_base_url()}/cycles"
                        f"?cycle_id={locked_cycle.id}&run_id={cycle_run_id}"
                    ),
                    link_label="open cycle state",
                    combined_alert_title="Cycle failure guard alert",
                ),
                evaluation=evaluation,
                error_text=failure_error,
            )
        except Exception:
            logger.exception(
                "Cycle failure-guard Slack delivery failed: "
                "cycle_id=%s cycle_run_id=%s",
                locked_cycle.id,
                cycle_run_id,
            )
    return evaluation
