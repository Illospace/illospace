import pytest

from brain.systems.runs.status import (
    ACTIVE_RUN_STATUSES,
    ALLOWED_RUN_TRANSITIONS,
    RESUMABLE_RUN_STATUSES,
    TERMINAL_RUN_STATUSES,
    RunStatus,
    RunTransitionError,
    coerce_run_status,
    ensure_run_transition,
)


def test_run_state_machine_accepts_only_declared_edges_or_idempotence():
    for current in RunStatus:
        for target in RunStatus:
            if target == current or target in ALLOWED_RUN_TRANSITIONS[current]:
                assert ensure_run_transition(current, target) == (current, target)
            else:
                with pytest.raises(RunTransitionError):
                    ensure_run_transition(current, target)


def test_terminal_run_statuses_are_absorbing_except_for_idempotence():
    for current in TERMINAL_RUN_STATUSES:
        assert ALLOWED_RUN_TRANSITIONS[current] == frozenset()
        assert ensure_run_transition(current, current) == (current, current)

        for target in set(RunStatus) - {current}:
            with pytest.raises(RunTransitionError):
                ensure_run_transition(current, target)


def test_active_and_resumable_status_sets_stay_in_lockstep():
    assert RESUMABLE_RUN_STATUSES == ACTIVE_RUN_STATUSES
    assert not ACTIVE_RUN_STATUSES & TERMINAL_RUN_STATUSES
    assert RunStatus.QUEUED not in ACTIVE_RUN_STATUSES


def test_status_coercion_is_case_insensitive_and_defaults_unknown_values_to_queued():
    assert coerce_run_status(" RUNNING ") == RunStatus.RUNNING
    assert coerce_run_status(RunStatus.PAUSED) == RunStatus.PAUSED
    assert coerce_run_status("mystery") == RunStatus.QUEUED
    assert coerce_run_status(None) == RunStatus.QUEUED

    with pytest.raises(RunTransitionError):
        ensure_run_transition("mystery", RunStatus.COMPLETED)
