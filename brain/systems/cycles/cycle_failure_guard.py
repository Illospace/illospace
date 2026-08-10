"""Cycle terminal policy, persistence, evaluation, and alert delivery."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
import logging
import os
from types import MappingProxyType
from typing import Protocol, TypeAlias

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from brain.kernel.common.time import ensure_utc
from brain.platform.db.models.cycle import (
    Cycle,
    CycleFailureGuardObservation,
    CycleFailureGuardTriggerState,
)
from brain.systems.cortex.thread_links import public_app_base_url
from brain.systems.failure_guard.core import (
    FailureRecord,
    FailureGuardEvaluation,
    FailureGuardLifecycleEvent,
    FailureGuardStatefulTrigger,
    FailureGuardTrigger,
    FailureGuardTriggerKind,
    FailureGuardTriggerMode,
    FailureGuardTriggerRegistration,
    require_failure_guard_registrations,
    FailureGuardTriggerResult,
    FailureGuardTriggerState,
    async_evaluate_failure_guard_triggers,
    async_evaluate_failure_edges,
    async_transition_failure_guard_trigger_states,
    failure_signature,
)
from brain.systems.failure_guard.cycle_latches import CycleAlertLatchStore
from brain.systems.failure_guard.state_repository import (
    SqlAlchemyFailureGuardStateStore,
)
from brain.systems.failure_guard.slack_delivery import (
    FailureAlertPresentation,
    FailureAlertSubject,
    SlackFailureAlertPolicy,
    async_deliver_failure_alert,
)
from brain.systems.slack.client import slack_web_client_from_runtime


CYCLE_FAILURE_ALERT_THRESHOLD_DEFAULT = 3
CYCLE_REPEATED_FAILURE_ALERT_CLASS = "repeated_failure"
CYCLE_AUTH_BLOCKED_ALERT_CLASS = "auth_blocked"
CYCLE_CONSECUTIVE_TRIGGER_KIND = FailureGuardTriggerKind("consecutive")

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CycleFailureGuardLifecycleContext:
    """Cycle-owned inputs available to trigger evaluation and transitions."""

    cycle: Cycle
    failure: CycleFailureInput | None


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
class CycleFailureInput:
    """Cycle-owned trigger input for one terminal failure."""

    classification: str
    record: FailureRecord
    alert_threshold: int
    alert_class: str
    operator_action: str | None

    def __post_init__(self) -> None:
        if not self.classification.strip():
            raise ValueError("cycle failure classification must not be empty")
        if self.alert_threshold < 1:
            raise ValueError("cycle failure alert threshold must be positive")
        if not self.alert_class.strip():
            raise ValueError("cycle failure alert class must not be empty")
        if self.operator_action is not None and not self.operator_action.strip():
            raise ValueError("cycle failure operator action must not be empty")


@dataclass(frozen=True)
class RecordCycleFailurePolicy:
    """Construct the typed cycle trigger input for one terminal failure."""

    classification: str
    default_error: str
    provide_alert_threshold: Callable[[], int]
    alert_class: str
    operator_action: str | None
    action: CycleTerminalAction = field(
        default=CycleTerminalAction.RECORD_FAILURE,
        init=False,
    )

    def failure_input(self, error_text: str | None) -> CycleFailureInput:
        threshold = self.provide_alert_threshold()
        failure_error = str(error_text or self.default_error).strip()
        return CycleFailureInput(
            classification=self.classification,
            record=FailureRecord(
                signature_input=(
                    f"cycle_status:{self.classification}\n{failure_error}"
                ),
                error_text=failure_error,
            ),
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
        # Quota admission owns its visible, deduplicated notice. It is not a
        # repeated Cycle execution failure and must not trigger Slack alerts.
        "quota_blocked": IgnoreCycleTerminalPolicy(),
    }
)


@dataclass(frozen=True)
class CycleConsecutiveFailuresTrigger:
    """Evaluate cycle streaks from explicit cycle-owned failure input."""

    kind: FailureGuardTriggerKind = field(
        default=CYCLE_CONSECUTIVE_TRIGGER_KIND,
        init=False,
    )

    async def evaluate_with_state(
        self,
        context: CycleFailureGuardLifecycleContext,
        *,
        state: FailureGuardTriggerState,
    ) -> FailureGuardTriggerResult:
        if context.failure is None:
            raise ValueError("Cycle failure trigger evaluation requires a failure")
        return self._result(context.failure, int(state.get("count", 0)))

    def _result(
        self,
        failure: CycleFailureInput,
        count: int,
    ) -> FailureGuardTriggerResult:
        presentation = CYCLE_ALERT_PRESENTATIONS[failure.alert_class]
        summary_lines = [
            f"Failure count: {count}",
            (
                "Window: "
                f"{presentation.describe_window(failure.alert_threshold)}"
            ),
        ]
        if failure.operator_action is not None:
            summary_lines.append(f"Action: {failure.operator_action}")
        return FailureGuardTriggerResult(
            kind=self.kind,
            active=count >= failure.alert_threshold,
            public_details={
                "classification": failure.classification,
                "count": count,
                "threshold": failure.alert_threshold,
                "window_runs": failure.alert_threshold,
            },
            alert_title=presentation.title,
            alert_summary="\n".join(summary_lines),
        )

    async def transition_state(
        self,
        context: CycleFailureGuardLifecycleContext,
        state: FailureGuardTriggerState,
        *,
        event: FailureGuardLifecycleEvent,
    ) -> FailureGuardTriggerState | None:
        if event == "success":
            return None
        count = (
            1
            if event == "new_failure"
            else int(state.get("count", 0)) + 1
        )
        return {"count": count}


class CycleFailureGuardTrigger(
    FailureGuardTrigger[CycleFailureGuardLifecycleContext],
    Protocol,
):
    """One stateless cycle failure trigger."""


class CycleFailureGuardStatefulTrigger(
    FailureGuardStatefulTrigger[CycleFailureGuardLifecycleContext],
    Protocol,
):
    """One state-owning cycle failure trigger."""


CycleFailureGuardTriggerImplementation: TypeAlias = (
    CycleFailureGuardTrigger | CycleFailureGuardStatefulTrigger
)
CycleRegisteredFailureGuardTrigger: TypeAlias = (
    FailureGuardTriggerRegistration[CycleFailureGuardLifecycleContext]
)


@dataclass(frozen=True)
class CycleFailureGuardRegistry:
    """The cycle-owned set of terminal failure triggers."""

    triggers: tuple[CycleRegisteredFailureGuardTrigger, ...]

    def __post_init__(self) -> None:
        require_failure_guard_registrations(self.triggers, owner="Cycle")
        kinds = [str(trigger.kind) for trigger in self.triggers]
        if any(not kind for kind in kinds):
            raise ValueError("Cycle failure-guard trigger kinds must not be empty")
        if len(kinds) != len(set(kinds)):
            raise ValueError("Cycle failure-guard trigger kinds must be unique")


def cycle_failure_guard_registry() -> CycleFailureGuardRegistry:
    """Return every trigger applied to cycle terminal observations."""
    return CycleFailureGuardRegistry(
        triggers=(
            FailureGuardTriggerRegistration(
                mode=FailureGuardTriggerMode.STATEFUL,
                trigger=CycleConsecutiveFailuresTrigger(),
            ),
        )
    )


@dataclass(frozen=True)
class CycleFailureObservationStore:
    """Claim each terminal run once for cycle-failure evaluation."""

    session: AsyncSession

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
            existing_claim = await self.session.get(
                CycleFailureGuardObservation,
                cycle_run_id,
            )
            if existing_claim is not None:
                return False
            raise
        return True


def _cycle_failure_guard_state_store(
    session: AsyncSession,
    cycle_id: int,
) -> SqlAlchemyFailureGuardStateStore[CycleFailureGuardTriggerState]:
    return SqlAlchemyFailureGuardStateStore(
        session=session,
        statement=select(CycleFailureGuardTriggerState).where(
            CycleFailureGuardTriggerState.cycle_id == cycle_id
        ),
        create_record=lambda trigger_kind, trigger_state: (
            CycleFailureGuardTriggerState(
                cycle_id=cycle_id,
                trigger_kind=trigger_kind,
                trigger_state=trigger_state,
            )
        ),
    )


async def async_apply_cycle_terminal_failure_guard(
    session: AsyncSession,
    cycle: Cycle,
    *,
    cycle_run_id: int,
    status: str,
    error_text: str | None,
    latch_store: CycleAlertLatchStore,
    now: datetime | None = None,
) -> FailureGuardEvaluation | None:
    """Claim and apply one canonical terminal cycle outcome exactly once."""
    policy = CYCLE_TERMINAL_POLICIES[status]
    now = ensure_utc(now)
    observation_store = CycleFailureObservationStore(session=session)
    if not await observation_store.claim_observation(cycle_run_id, now):
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
    state_store = _cycle_failure_guard_state_store(session, locked_cycle.id)
    if isinstance(policy, ResetCycleTerminalPolicy):
        latches = dict(await latch_store.load_latches())
        for registration in registry.triggers:
            if registration.kind in latches:
                await latch_store.delete_latch(registration.kind)
        await async_transition_failure_guard_trigger_states(
            triggers=registry.triggers,
            context=CycleFailureGuardLifecycleContext(
                cycle=locked_cycle,
                failure=None,
            ),
            event="success",
            store=state_store,
        )
        locked_cycle.failure_signature = None
        locked_cycle.last_failure_error = None
        await session.flush()
        return None

    failure = policy.failure_input(error_text)
    signature = failure_signature(failure.record.signature_input)
    signature_changed = locked_cycle.failure_signature != signature
    if signature_changed:
        locked_cycle.failure_signature = signature
    await async_transition_failure_guard_trigger_states(
        triggers=registry.triggers,
        context=CycleFailureGuardLifecycleContext(
            cycle=locked_cycle,
            failure=failure,
        ),
        event="new_failure" if signature_changed else "repeated_failure",
        store=state_store,
    )
    if signature_changed:
        latches = dict(await latch_store.load_latches())
        reset_latch = False
        for registration in registry.triggers:
            if registration.kind in latches:
                await latch_store.delete_latch(registration.kind)
                reset_latch = True
        if reset_latch:
            await session.flush()

    locked_cycle.last_failure_error = failure.record.error_text
    results = await async_evaluate_failure_guard_triggers(
        triggers=registry.triggers,
        context=CycleFailureGuardLifecycleContext(
            cycle=locked_cycle,
            failure=failure,
        ),
        store=state_store,
    )
    evaluation = await async_evaluate_failure_edges(
        results=results,
        failure_signature=locked_cycle.failure_signature,
        last_error=locked_cycle.last_failure_error,
        now=now,
        store=latch_store,
        new_edge_mode="latch",
    )
    await session.flush()

    if evaluation.crossed_edges:
        logger.error(
            "%s alert: cycle_id=%s cycle_run_id=%s crossed_triggers=%s "
            "failure_signature=%s error=%s",
            evaluation.crossed_edges[0].alert_title,
            locked_cycle.id,
            cycle_run_id,
            ",".join(str(edge.kind) for edge in evaluation.crossed_edges),
            evaluation.failure_signature,
            failure.record.error_text,
        )
        try:
            crossed_edge = evaluation.crossed_edges[0]
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
                presentation=FailureAlertPresentation(
                    title=crossed_edge.alert_title,
                    summary=crossed_edge.alert_summary,
                ),
                error_text=failure.record.error_text,
            )
        except Exception:
            logger.exception(
                "Cycle failure-guard Slack delivery failed: "
                "cycle_id=%s cycle_run_id=%s",
                locked_cycle.id,
                cycle_run_id,
            )
    return evaluation
