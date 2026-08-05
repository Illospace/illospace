"""Acceptance coverage for durable Cycle execution-policy resolution."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from brain.platform.db.models.cycle import Cycle, CycleRun
from brain.systems.cycles import execution_policy_registry as policy_registry_module
from brain.systems.cycles.access import CycleActor
from brain.systems.cycles.commands import async_create_cycle, async_update_cycle
from brain.systems.cycles.execution_policy_registry import (
    CycleExecutionPolicyRegistration,
    CycleExecutionPolicyRegistry,
    async_apply_cycle_execution_policy,
    cycle_execution_policy_registry,
    validate_cycle_execution_policy_key,
)
from brain.systems.cycles.promotion_readiness import (
    PROMOTION_READINESS_POLICY,
    async_apply_promotion_readiness_gate,
)


def test_promotion_readiness_gate_is_the_first_registered_policy():
    assert cycle_execution_policy_registry().resolve(
        PROMOTION_READINESS_POLICY.execution_policy_key
    ) is async_apply_promotion_readiness_gate
    assert validate_cycle_execution_policy_key(None) is None


def test_duplicate_registry_key_fails_loudly_at_construction():
    with pytest.raises(
        ValueError,
        match=(
            "Ambiguous Cycle execution policy key: 'duplicate_policy' "
            "resolves to 2 gates"
        ),
    ):
        CycleExecutionPolicyRegistry(
            registrations=(
                CycleExecutionPolicyRegistration(
                    "duplicate_policy",
                    AsyncMock(),
                ),
                CycleExecutionPolicyRegistration(
                    "duplicate_policy",
                    AsyncMock(),
                ),
            )
        )


def test_registry_rejects_an_empty_policy_key_at_construction():
    with pytest.raises(
        ValueError,
        match="Cycle execution policy registration requires a key",
    ):
        CycleExecutionPolicyRegistry(
            registrations=(
                CycleExecutionPolicyRegistration("  ", AsyncMock()),
            )
        )


@pytest.mark.asyncio
async def test_renamed_cycle_reaches_gate_through_real_execution_policy_dispatcher(
    monkeypatch,
):
    cycle = Cycle()
    cycle.id = 9
    cycle.name = "Renamed promotion readiness cycle"
    cycle.execution_policy_key = PROMOTION_READINESS_POLICY.execution_policy_key
    run = CycleRun()
    run.id = 2507
    run.context_snapshot = {}
    session = object()
    applied = []

    async def gate(active_session, *, cycle, run):
        applied.append((active_session, cycle.name, run.id))
        return None

    isolated_registry = CycleExecutionPolicyRegistry(
        registrations=(
            CycleExecutionPolicyRegistration(
                PROMOTION_READINESS_POLICY.execution_policy_key,
                gate,
            ),
        )
    )
    monkeypatch.setattr(
        policy_registry_module,
        "cycle_execution_policy_registry",
        lambda: isolated_registry,
    )

    effect = await async_apply_cycle_execution_policy(
        session,
        cycle=cycle,
        run=run,
    )

    assert effect is None
    assert applied == [(session, "Renamed promotion readiness cycle", run.id)]


@pytest.mark.asyncio
async def test_create_rejects_an_unknown_policy_key_before_saving():
    with pytest.raises(
        ValueError,
        match="Unknown Cycle execution policy key: 'unknown_policy'",
    ):
        await async_create_cycle(
            object(),
            actor=CycleActor(user_id="user-1", org_id="org-1"),
            name="Unsafe cycle",
            prompt="Do expensive work.",
            timezone_name="UTC",
            schedule_expr="0 9 * * *",
            execution_policy_key="unknown_policy",
        )


@pytest.mark.asyncio
async def test_update_rejects_an_unknown_policy_key_without_mutating_the_cycle():
    cycle = Cycle()
    cycle.name = "Original name"
    cycle.execution_policy_key = "unknown_policy"

    with pytest.raises(
        ValueError,
        match="Unknown Cycle execution policy key: 'unknown_policy'",
    ):
        await async_update_cycle(
            object(),
            cycle,
            actor=CycleActor(user_id="user-1", org_id="org-1"),
            name="Changed name",
        )

    assert cycle.name == "Original name"
    assert cycle.execution_policy_key == "unknown_policy"
