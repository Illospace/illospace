"""Request-scoped batch deploy-state derivation tests."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator, Mapping
from unittest.mock import AsyncMock, patch

import pytest

from brain.systems.deploy_state import DeployState, derive_deploy_states
from brain.systems.deploy_state_github import AncestryObservation


REPO = "uwear-ai/uwear-backend"


@pytest.mark.asyncio
async def test_batch_deriver_handles_identical_promotion_and_diverged_fix():
    statuses = {
        ("identical", "staging"): True,
        ("identical", "main"): True,
        ("diverged", "staging"): False,
        ("diverged", "main"): False,
    }

    async def ancestry(_repo, sha, branch, *, token=None):
        assert token is None
        await asyncio.sleep(0)
        return AncestryObservation(
            branch=branch,
            is_ancestor=statuses[(sha, branch)],
        )

    with patch(
        "brain.systems.deploy_state.observe_ancestry",
        new=AsyncMock(side_effect=ancestry),
    ):
        states = await derive_deploy_states(
            {
                2474: (REPO, "identical"),
                2475: (REPO, "diverged"),
            },
            tokens=(None,),
        )

    assert states == {
        2474: DeployState.DEPLOYED,
        2475: DeployState.UNMERGED,
    }


@pytest.mark.asyncio
async def test_batch_deriver_preserves_indeterminate_api_as_none():
    async def ancestry(_repo, _sha, branch, *, token=None):
        return AncestryObservation(
            branch=branch,
            is_ancestor=None,
            error_category="github_status_unknown",
        )

    with patch(
        "brain.systems.deploy_state.observe_ancestry",
        new=AsyncMock(side_effect=ancestry),
    ):
        states = await derive_deploy_states(
            {2474: (REPO, "unknown")},
            tokens=(None,),
        )

    assert states == {2474: None}


@pytest.mark.asyncio
async def test_batch_deriver_uses_repo_specific_tokens():
    seen: set[tuple[str, str | None]] = set()

    async def ancestry(repo, _sha, branch, *, token=None):
        seen.add((repo, token))
        return AncestryObservation(
            branch=branch,
            is_ancestor=branch == "staging",
        )

    with patch(
        "brain.systems.deploy_state.observe_ancestry",
        new=AsyncMock(side_effect=ancestry),
    ):
        states = await derive_deploy_states(
            {
                "backend": (REPO, "a"),
                "app": ("uwear-ai/uwearaiapp", "b"),
            },
            tokens={
                REPO: ("backend-token",),
                "uwear-ai/uwearaiapp": ("app-token",),
            },
        )

    assert states == {
        "backend": DeployState.STAGING,
        "app": DeployState.STAGING,
    }
    assert seen == {
        (REPO, "backend-token"),
        ("uwear-ai/uwearaiapp", "app-token"),
    }


@pytest.mark.asyncio
async def test_batch_deriver_deduplicates_shared_fix_refs():
    compare = AsyncMock(return_value="ahead")
    with patch(
        "brain.systems.deploy_state_github.async_compare_commits",
        new=compare,
    ):
        states = await derive_deploy_states(
            {
                2474: (REPO, "same-sha"),
                2475: (REPO, "same-sha"),
                2476: (REPO, "same-sha"),
            },
            tokens=(None,),
        )

    assert states == {
        2474: DeployState.UNMERGED,
        2475: DeployState.UNMERGED,
        2476: DeployState.UNMERGED,
    }
    assert compare.await_count == 2
    assert len(states.observations_by_ref) == 1


@pytest.mark.asyncio
async def test_batch_deriver_bounds_actual_compare_concurrency():
    active = 0
    max_active = 0

    async def compare(_repo, _base, _head, *, token=None):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.01)
        active -= 1
        return "ahead"

    with patch(
        "brain.systems.deploy_state_github.async_compare_commits",
        new=AsyncMock(side_effect=compare),
    ) as compare_mock:
        await derive_deploy_states(
            {
                key: (REPO, f"sha-{key}")
                for key in range(8)
            },
            tokens=(None,),
            concurrency=3,
        )

    assert compare_mock.await_count == 16
    assert max_active == 3


@pytest.mark.asyncio
async def test_batch_deriver_isolates_failure_outside_compare_helper():
    class PoisonedTokens(Mapping[str, tuple[str | None, ...]]):
        def __getitem__(self, key):
            if key == "uwear-ai/poisoned":
                raise RuntimeError("poison")
            return (None,)

        def __iter__(self) -> Iterator[str]:
            return iter(("uwear-ai/poisoned", REPO))

        def __len__(self) -> int:
            return 2

        def get(self, key, default=None):
            return self[key]

    async def ancestry(_repo, _sha, branch, *, token=None):
        return AncestryObservation(
            branch=branch,
            is_ancestor=branch == "main",
        )

    with patch(
        "brain.systems.deploy_state.observe_ancestry",
        new=AsyncMock(side_effect=ancestry),
    ):
        states = await derive_deploy_states(
            {
                "bad-one": ("uwear-ai/poisoned", "bad-sha"),
                "good": (REPO, "good-sha"),
                "bad-two": ("uwear-ai/poisoned", "bad-sha"),
            },
            tokens=PoisonedTokens(),
        )

    assert states == {
        "bad-one": None,
        "good": DeployState.DEPLOYED,
        "bad-two": None,
    }
    failed_ref = ("uwear-ai/poisoned", "bad-sha")
    assert set(states.unavailable_refs) == {failed_ref}
    assert {
        failure.error_category
        for failure in states.unavailable_refs[failed_ref].failures
    } == {"runtime_error"}
