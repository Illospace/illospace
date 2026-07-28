"""Read-time deploy-state core tests."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from brain.systems.deploy_state import (
    DeployState,
    DeployStateObservation,
    derive_deploy_state,
    observe_deploy_state,
    render_deploy_state,
)
from brain.systems.deploy_state_github import AncestryObservation


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("in_staging", "in_main", "expected"),
    [
        (True, True, DeployState.DEPLOYED),
        (False, True, DeployState.DEPLOYED),
        (True, False, DeployState.STAGING),
        (False, False, DeployState.UNMERGED),
        (True, None, None),
        (None, False, None),
        (None, None, None),
    ],
)
async def test_derive_deploy_state_truth_table(in_staging, in_main, expected):
    async def ancestry(_repo, _sha, branch, *, token=None):
        assert token == "read-token"
        return AncestryObservation(
            branch=branch,
            is_ancestor=(
                in_staging
                if branch == "staging"
                else in_main
            ),
        )

    with patch(
        "brain.systems.deploy_state.observe_ancestry",
        new=AsyncMock(side_effect=ancestry),
    ):
        assert (
            await derive_deploy_state(
                "uwear-ai/uwear-backend",
                "a" * 40,
                tokens=("read-token",),
            )
            is expected
        )


@pytest.mark.asyncio
async def test_indeterminate_identity_falls_through_to_next_token():
    async def ancestry(_repo, _sha, branch, *, token=None):
        if token == "hidden":
            return AncestryObservation(
                branch=branch,
                is_ancestor=None,
                error_category="github_http_404",
                status_code=404,
            )
        return AncestryObservation(
            branch=branch,
            is_ancestor=branch == "main",
        )

    with patch(
        "brain.systems.deploy_state.observe_ancestry",
        new=AsyncMock(side_effect=ancestry),
    ):
        observation = await observe_deploy_state(
            "uwear-ai/uwear-backend",
            "b" * 40,
            tokens=("hidden", "visible"),
        )

    assert observation.state is DeployState.DEPLOYED
    assert observation.in_main is True
    assert observation.display_state == "deployed"
    assert {
        failure.error_category
        for failure in observation.failures
    } == {"github_http_404"}


def test_indeterminate_state_renders_unknown():
    assert render_deploy_state(None) == "unknown"


def test_observation_exposes_latest_branch_ancestry_result_for_callers():
    observation = DeployStateObservation(
        state=DeployState.STAGING,
        in_staging=True,
        in_main=False,
        comparisons=(
            AncestryObservation(
                branch="main",
                is_ancestor=None,
                error_category="github_http_404",
            ),
            AncestryObservation(
                branch="main",
                is_ancestor=False,
                status="diverged",
            ),
        ),
    )

    assert observation.branch_ancestry_result("main") == "diverged (not contained)"
    assert observation.branch_ancestry_result("production") == "unknown"
