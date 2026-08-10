"""Failure-guard trigger registration and dispatch contract tests."""
from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from brain.systems.failure_guard.core import (
    FailureGuardTriggerKind,
    FailureGuardTriggerMode,
    FailureGuardTriggerRegistration,
    FailureGuardTriggerResult,
    async_evaluate_failure_guard_triggers,
    async_transition_failure_guard_trigger_states,
    require_failure_guard_registrations,
)


_STATELESS_KIND = FailureGuardTriggerKind("stateless_test")
_STATEFUL_KIND = FailureGuardTriggerKind("stateful_test")


@dataclass(frozen=True)
class _StatelessTrigger:
    kind: FailureGuardTriggerKind = field(
        default=_STATELESS_KIND,
        init=False,
    )

    async def evaluate(self, context) -> FailureGuardTriggerResult:
        del context
        return FailureGuardTriggerResult(
            kind=self.kind,
            active=False,
            public_details={"branch": "stateless"},
            alert_title="Stateless test",
            alert_summary="Stateless test",
        )


@dataclass(frozen=True)
class _DualContractTrigger(_StatelessTrigger):
    kind: FailureGuardTriggerKind = field(
        default=_STATEFUL_KIND,
        init=False,
    )

    async def evaluate_with_state(
        self,
        context,
        *,
        state,
    ) -> FailureGuardTriggerResult:
        del context
        return FailureGuardTriggerResult(
            kind=self.kind,
            active=True,
            public_details={"branch": "stateful", "state": dict(state)},
            alert_title="Stateful test",
            alert_summary="Stateful test",
        )

    async def transition_state(self, context, state, *, event):
        del context, state, event
        return None


@dataclass(frozen=True)
class _StatefulTriggerMissingTransition:
    kind: FailureGuardTriggerKind = field(
        default=_STATEFUL_KIND,
        init=False,
    )

    async def evaluate_with_state(self, context, *, state):
        del context, state
        raise AssertionError("registration must fail before evaluation")


@dataclass
class _MemoryStateStore:
    states: dict[FailureGuardTriggerKind, dict[str, object]]
    saved: list[FailureGuardTriggerKind] = field(default_factory=list)
    deleted: list[FailureGuardTriggerKind] = field(default_factory=list)

    async def load_trigger_states(self):
        return self.states

    async def save_trigger_state(self, trigger_kind, state):
        self.states[trigger_kind] = dict(state)
        self.saved.append(trigger_kind)

    async def delete_trigger_state(self, trigger_kind):
        self.states.pop(trigger_kind, None)
        self.deleted.append(trigger_kind)


def test_registration_rejects_stateful_trigger_missing_transition_method():
    with pytest.raises(
        ValueError,
        match=(
            "_StatefulTriggerMissingTransition declared stateful.*"
            "transition_state"
        ),
    ):
        FailureGuardTriggerRegistration(
            mode=FailureGuardTriggerMode.STATEFUL,
            trigger=_StatefulTriggerMissingTransition(),
        )


def test_registration_rejects_stateless_trigger_with_stateful_methods():
    with pytest.raises(
        ValueError,
        match=(
            "_DualContractTrigger declared stateless.*"
            "evaluate_with_state.*transition_state"
        ),
    ):
        FailureGuardTriggerRegistration(
            mode=FailureGuardTriggerMode.STATELESS,
            trigger=_DualContractTrigger(),
        )


@pytest.mark.asyncio
async def test_declared_mode_controls_evaluation_and_persistence():
    stateless_registration = FailureGuardTriggerRegistration(
        mode=FailureGuardTriggerMode.STATELESS,
        trigger=_StatelessTrigger(),
    )
    stateful_registration = FailureGuardTriggerRegistration(
        mode=FailureGuardTriggerMode.STATEFUL,
        trigger=_DualContractTrigger(),
    )
    store = _MemoryStateStore(
        states={
            _STATELESS_KIND: {"orphaned": True},
            _STATEFUL_KIND: {"count": 3},
        }
    )

    results = await async_evaluate_failure_guard_triggers(
        triggers=(stateless_registration, stateful_registration),
        context=object(),
        store=store,
    )

    assert [result.public_details["branch"] for result in results] == [
        "stateless",
        "stateful",
    ]
    assert results[1].public_details["state"] == {"count": 3}

    await async_transition_failure_guard_trigger_states(
        triggers=(stateless_registration, stateful_registration),
        context=object(),
        event="success",
        store=store,
    )

    assert store.states == {_STATELESS_KIND: {"orphaned": True}}
    assert store.saved == []
    assert store.deleted == [_STATEFUL_KIND]


def test_bare_trigger_is_refused_before_it_can_reach_dispatch():
    """A raw trigger has a ``kind``, so a consumer registry's own checks pass it.

    It never runs ``FailureGuardTriggerRegistration.__post_init__``, so its mode
    would only be missed at dispatch, far from the provider that omitted it.
    """

    with pytest.raises(ValueError) as excinfo:
        require_failure_guard_registrations(
            (
                FailureGuardTriggerRegistration(
                    mode=FailureGuardTriggerMode.STATELESS,
                    trigger=_StatelessTrigger(),
                ),
                _StatelessTrigger(),
            ),
            owner="Scheduler",
        )

    message = str(excinfo.value)
    assert "Scheduler" in message
    assert "_StatelessTrigger" in message


def test_registrations_only_registry_is_accepted():
    require_failure_guard_registrations(
        (
            FailureGuardTriggerRegistration(
                mode=FailureGuardTriggerMode.STATELESS,
                trigger=_StatelessTrigger(),
            ),
        ),
        owner="Cycle",
    )
