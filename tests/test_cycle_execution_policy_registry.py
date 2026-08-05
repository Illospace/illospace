"""Acceptance coverage for durable Cycle execution-policy resolution."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from brain.platform.db.models.cycle import Cycle
from brain.systems.cycles.access import CycleActor
from brain.systems.cycles.commands import async_create_cycle, async_update_cycle
from brain.systems.cycles.execution_policy_registry import (
    CycleExecutionPolicyRegistry,
    cycle_execution_policy_registry,
    validate_cycle_execution_policy_key,
)
from brain.systems.cycles.promotion_readiness import (
    PROMOTION_READINESS_POLICY,
    async_apply_promotion_readiness_gate,
)


def test_promotion_readiness_gate_is_the_first_registered_policy():
    assert cycle_execution_policy_registry.resolve(
        PROMOTION_READINESS_POLICY.execution_policy_key
    ) is async_apply_promotion_readiness_gate
    assert validate_cycle_execution_policy_key(None) is None


def test_ambiguous_registry_resolution_fails_loudly():
    registry = CycleExecutionPolicyRegistry()
    registry.register("duplicate_policy", AsyncMock())
    registry.register("duplicate_policy", AsyncMock())

    with pytest.raises(
        ValueError,
        match=(
            "Ambiguous Cycle execution policy key: 'duplicate_policy' "
            "resolves to 2 gates"
        ),
    ):
        registry.resolve("duplicate_policy")


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
