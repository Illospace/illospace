"""Cycle terminal policy, persistence, evaluation, and alert delivery."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
import logging
import os
from types import MappingProxyType

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from brain.kernel.common.time import ensure_utc
from brain.platform.db.models.cycle import (
    Cycle,
    CycleFailureGuardLatch,
    CycleFailureGuardObservation,
)
from brain.systems.cortex.thread_links import public_app_base_url
from brain.systems.failure_guard.core import (
    CONSECUTIVE_TRIGGER_KIND,
    FailureGuardEvaluation,
    FailureGuardLatch,
    FailureGuardRegistry,
    FailureGuardResetEvent,
    FailureGuardSubject,
    FailureGuardTriggerKind,
    FailureGuardTriggerResult,
    FailureObservation,
    async_record_failure,
    async_reset_failure_guard,
)
from brain.systems.failure_guard.slack_delivery import (
    FailureAlertSubject,
    SlackFailureAlertPolicy,
    async_deliver_failure_alert,
)
from brain.systems.slack.client import slack_web_client_from_runtime


CYCLE_FAILURE_ALERT_THRESHOLD_DEFAULT = 3
CYCLE_REPEATED_FAILURE_ALERT_CLASS = "repeated_failure"
CYCLE_AUTH_BLOCKED_ALERT_CLASS = "auth_blocked"

logger = logging.getLogger(__name__)


def _positive_int_setting(name: str, default: int) -> int:
    try:
        configured = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        configured = default
    return max(1, configured)


def _cycle_failure_alert_threshold() -> int:
    return _positive_int_setting(
        "CYCLE_FAILURE_ALERT_THRESHOLD",
        CYCLE_FAILURE_ALERT_THRESHOLD_DEFAULT,
    )


def _immediate_alert_threshold() -> int:
    return 1


class CycleTerminalAction(Enum):
    """The complete set of guard effects for a terminal cycle outcome."""

    RESET = "reset"
    RECORD_FAILURE = "record_failure"
    IGNORE = "ignore"


@dataclass(frozen=True)
class ResetCycleTerminalPolicy:
    action: CycleTerminalAction = field(
        default=CycleTerminalAction.RESET,
        init=False,
    )


@dataclass(frozen=True)
class IgnoreCycleTerminalPolicy:
    action: CycleTerminalAction = field(
        default=CycleTerminalAction.IGNORE,
        init=False,
    )


@dataclass(frozen=True)
class RecordCycleFailurePolicy:
    """Construct the typed observation for one terminal failure class."""

    classification: str
    default_error: str
    provide_alert_threshold: Callable[[], int]
    alert_class: str
    operator_action: str | None
    action: CycleTerminalAction = field(
        default=CycleTerminalAction.RECORD_FAILURE,
        init=False,
    )

    def observation(self, error_text: str | None) -> FailureObservation:
        threshold = self.provide_alert_threshold()
        failure_error = str(error_text or self.default_error).strip()
        return FailureObservation(
            classification=self.classification,
            signature_input=(
                f"cycle_status:{self.classification}\n{failure_error}"
            ),
            error_text=failure_error,
            alert_threshold=threshold,
            alert_class=self.alert_class,
            operator_action=self.operator_action,
        )


CycleTerminalPolicy = (
    ResetCycleTerminalPolicy
    | IgnoreCycleTerminalPolicy
    | RecordCycleFailurePolicy
)


def _consecutive_window(threshold: int) -> str:
    return f"{threshold} consecutive scheduled runs"


def _scheduled_interval_window(threshold: int) -> str:
    del threshold
    return "1 scheduled interval"


@dataclass(frozen=True)
class CycleAlertPresentation:
    """Caller-owned copy for one typed cycle alert class."""

    title: str
    describe_window: Callable[[int], str]


CYCLE_ALERT_PRESENTATIONS: Mapping[str, CycleAlertPresentation] = MappingProxyType(
    {
        CYCLE_REPEATED_FAILURE_ALERT_CLASS: CycleAlertPresentation(
            title="Cycle repeated failure",
            describe_window=_consecutive_window,
        ),
        CYCLE_AUTH_BLOCKED_ALERT_CLASS: CycleAlertPresentation(
            title="Cycle authentication blocked",
            describe_window=_scheduled_interval_window,
        ),
    }
)


CYCLE_TERMINAL_POLICIES: Mapping[str, CycleTerminalPolicy] = MappingProxyType(
    {
        "completed": ResetCycleTerminalPolicy(),
        "failed": RecordCycleFailurePolicy(
            classification="failed",
            default_error="Cycle ended with failed status",
            provide_alert_threshold=_cycle_failure_alert_threshold,
            alert_class=CYCLE_REPEATED_FAILURE_ALERT_CLASS,
            operator_action=None,
        ),
        "skipped": IgnoreCycleTerminalPolicy(),
        "degraded": RecordCycleFailurePolicy(
            classification="degraded",
            default_error="Cycle ended with degraded status",
            provide_alert_threshold=_cycle_failure_alert_threshold,
            alert_class=CYCLE_REPEATED_FAILURE_ALERT_CLASS,
            operator_action=None,
        ),
        "auth_blocked": RecordCycleFailurePolicy(
            classification="auth_blocked",
            default_error="Cycle authentication is blocked",
            provide_alert_threshold=_immediate_alert_threshold,
            alert_class=CYCLE_AUTH_BLOCKED_ALERT_CLASS,
            operator_action="reconnect OpenAI in Settings > Access",
        ),
    }
)


@dataclass(frozen=True)
class CycleConsecutiveFailuresTrigger:
    """Present cycle streaks from explicit failure-observation data."""

    default_threshold: int
    kind: FailureGuardTriggerKind = field(
        default=CONSECUTIVE_TRIGGER_KIND,
        init=False,
    )

    @classmethod
    def from_settings(cls) -> CycleConsecutiveFailuresTrigger:
        return cls(default_threshold=_cycle_failure_alert_threshold())

    async def evaluate(
        self,
        session: AsyncSession,
        subject: FailureGuardSubject,
        now: datetime,
        *,
        observation: FailureObservation | None = None,
    ) -> FailureGuardTriggerResult:
        del session, now
        count = int(subject.consecutive_failure_count or 0)
        if observation is None:
            classification = "failure"
            threshold = self.default_threshold
            alert_class = CYCLE_REPEATED_FAILURE_ALERT_CLASS
            operator_action = None
        else:
            classification = observation.classification
            threshold = observation.alert_threshold
            alert_class = observation.alert_class
            operator_action = observation.operator_action

        presentation = CYCLE_ALERT_PRESENTATIONS[alert_class]
        summary_lines = [
            f"Failure count: {count}",
            f"Window: {presentation.describe_window(threshold)}",
        ]
        if operator_action is not None:
            summary_lines.append(f"Action: {operator_action}")
        return FailureGuardTriggerResult(
            active=count >= threshold,
            public_details={
                "classification": classification,
                "count": count,
                "threshold": threshold,
                "window_runs": threshold,
            },
            alert_title=presentation.title,
            alert_summary="\n".join(summary_lines),
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
    """Return every trigger applied to cycle terminal observations."""
    return FailureGuardRegistry(
        triggers=(CycleConsecutiveFailuresTrigger.from_settings(),)
    )


@dataclass(frozen=True)
class CycleFailureGuardStore:
    """Persist cycle latches and unique run-observation claims."""

    session: AsyncSession
    cycle_id: int

    async def load_latches(
        self,
    ) -> dict[FailureGuardTriggerKind, CycleFailureGuardLatch]:
        result = await self.session.scalars(
            select(CycleFailureGuardLatch).where(
                CycleFailureGuardLatch.cycle_id == self.cycle_id
            )
        )
        return {
            FailureGuardTriggerKind(latch.trigger_kind): latch
            for latch in result.all()
        }

    async def create_latch(
        self,
        trigger_kind: FailureGuardTriggerKind,
        alerted_at: datetime,
    ) -> FailureGuardLatch:
        latch = CycleFailureGuardLatch(
            cycle_id=self.cycle_id,
            trigger_kind=str(trigger_kind),
            alerted_at=alerted_at,
        )
        self.session.add(latch)
        return latch

    async def delete_latch(
        self,
        trigger_kind: FailureGuardTriggerKind,
    ) -> None:
        await self.session.execute(
            delete(CycleFailureGuardLatch).where(
                CycleFailureGuardLatch.cycle_id == self.cycle_id,
                CycleFailureGuardLatch.trigger_kind == str(trigger_kind),
            )
        )

    async def claim_observation(
        self,
        cycle_run_id: int,
        observed_at: datetime,
    ) -> bool:
        """Claim one terminal run without invalidating the outer transaction."""
        try:
            async with self.session.begin_nested():
                self.session.add(
                    CycleFailureGuardObservation(
                        cycle_run_id=cycle_run_id,
                        observed_at=observed_at,
                    )
                )
                await self.session.flush()
        except IntegrityError:
            return False
        return True


async def async_apply_cycle_terminal_failure_guard(
    session: AsyncSession,
    cycle: Cycle,
    *,
    cycle_run_id: int,
    status: str,
    error_text: str | None,
    now: datetime | None = None,
) -> FailureGuardEvaluation | None:
    """Claim and apply one canonical terminal cycle outcome exactly once."""
    policy = CYCLE_TERMINAL_POLICIES[status]
    now = ensure_utc(now)
    store = CycleFailureGuardStore(session=session, cycle_id=cycle.id)
    if not await store.claim_observation(cycle_run_id, now):
        return None
    if isinstance(policy, IgnoreCycleTerminalPolicy):
        return None

    locked_cycle = await session.scalar(
        select(Cycle)
        .where(Cycle.id == cycle.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if locked_cycle is None:
        raise ValueError(f"Cycle {cycle.id} not found")

    registry = cycle_failure_guard_registry()
    if isinstance(policy, ResetCycleTerminalPolicy):
        await async_reset_failure_guard(
            session,
            locked_cycle,
            now=now,
            registry=registry,
            store=store,
        )
        return None

    observation = policy.observation(error_text)
    evaluation = await async_record_failure(
        session,
        locked_cycle,
        observation=observation,
        now=now,
        registry=registry,
        store=store,
    )

    if evaluation.crossed_edges:
        logger.error(
            "%s alert: cycle_id=%s cycle_run_id=%s crossed_triggers=%s "
            "failure_signature=%s error=%s",
            evaluation.crossed_edges[0].alert_title,
            locked_cycle.id,
            cycle_run_id,
            ",".join(str(edge.kind) for edge in evaluation.crossed_edges),
            evaluation.failure_signature,
            observation.error_text,
        )
        try:
            await async_deliver_failure_alert(
                policy=SlackFailureAlertPolicy(
                    provide_client=slack_web_client_from_runtime,
                    requested_by="cycle_failure_alert",
                    reason="Deliver a repeated cycle failure alert to the team.",
                    channel=(
                        os.getenv(
                            "ILLO_CYCLE_FAILURE_ALERT_CHANNEL",
                            "",
                        ).strip()
                        or "#alerts"
                    ),
                    unknown_error_text="Unknown cycle failure",
                    combined_alert_title="Cycle failure guard alert",
                ),
                subject=FailureAlertSubject(
                    identity_label="Cycle",
                    identity=f"{locked_cycle.name} (#{locked_cycle.id})",
                    url_label="Cycle",
                    url=(
                        f"{public_app_base_url()}/cycles"
                        f"?cycle_id={locked_cycle.id}&run_id={cycle_run_id}"
                    ),
                    link_label="open cycle state",
                ),
                evaluation=evaluation,
                error_text=observation.error_text,
            )
        except Exception:
            logger.exception(
                "Cycle failure-guard Slack delivery failed: "
                "cycle_id=%s cycle_run_id=%s",
                locked_cycle.id,
                cycle_run_id,
            )
    return evaluation
