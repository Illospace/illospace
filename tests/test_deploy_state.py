"""Pure deploy-state core tests."""

from datetime import datetime, timedelta, timezone
from itertools import product

import pytest

from brain.systems.deploy_state import (
    DeployState,
    LadderAction,
    MergeKind,
    RefireSignal,
    classify_merge_event,
    classify_refire,
    derive_deploy_state,
    parse_rollbar_alert,
    promotion_recommendation_signal,
)


NOW = datetime(2026, 7, 10, 12, tzinfo=timezone.utc)
SETTLE = timedelta(minutes=30)


@pytest.mark.parametrize(
    ("state", "status", "deployed_at", "expected"),
    [
        (None, "Todo", None, LadderAction.NOTE_OCCURRENCE),
        (DeployState.STAGING, "Todo", None, LadderAction.EXPECTED_NOISE),
        (DeployState.PROD_PENDING, "In Progress", None, LadderAction.EXPECTED_NOISE),
        (DeployState.DEPLOYED, "Todo", NOW - timedelta(minutes=29), LadderAction.EXPECTED_NOISE),
        (DeployState.DEPLOYED, "Todo", NOW - SETTLE, LadderAction.REOPEN_ESCALATE),
        (DeployState.DEPLOYED, "Todo", None, LadderAction.REOPEN_ESCALATE),
        (DeployState.VERIFIED, "Todo", NOW - timedelta(days=2), LadderAction.REOPEN_ESCALATE),
        (None, "Done", None, LadderAction.REOPEN_ESCALATE),
        # A merely-staged fix wins over a stale Done: re-firing before
        # promotion is expected noise even on a prematurely closed ticket
        # (the caller normalizes the status quietly; no builder escalation).
        (DeployState.STAGING, "Done", None, LadderAction.EXPECTED_NOISE),
        (DeployState.PROD_PENDING, "Done", None, LadderAction.EXPECTED_NOISE),
        (DeployState.DEPLOYED, "Done", NOW - timedelta(minutes=1), LadderAction.REOPEN_ESCALATE),
        (DeployState.VERIFIED, "Done", NOW, LadderAction.REOPEN_ESCALATE),
    ],
)
def test_refire_ladder_truth_table(state, status, deployed_at, expected):
    assert classify_refire(
        deploy_state=state,
        ticket_status=status,
        deployed_at=deployed_at,
        now=NOW,
        settle=SETTLE,
    ) is expected


@pytest.mark.parametrize(
    ("state", "inside_settle", "milestone", "status"),
    list(
        product(
            [None, *list(DeployState)],
            [False, True],
            [None, 100],
            ["Todo", "Done"],
        )
    ),
)
def test_full_state_settle_milestone_status_matrix(state, inside_settle, milestone, status):
    deployed_at = NOW - (timedelta(minutes=1) if inside_settle else timedelta(hours=2))
    action = classify_refire(
        deploy_state=state,
        ticket_status=status,
        deployed_at=deployed_at,
        now=NOW,
        settle=SETTLE,
    )
    if state in {DeployState.STAGING, DeployState.PROD_PENDING}:
        # Pre-promotion fixes are expected noise even on a stale Done ticket.
        expected = LadderAction.EXPECTED_NOISE
    elif status == "Done" or state is DeployState.VERIFIED:
        expected = LadderAction.REOPEN_ESCALATE
    elif state is DeployState.DEPLOYED:
        expected = (
            LadderAction.EXPECTED_NOISE
            if inside_settle
            else LadderAction.REOPEN_ESCALATE
        )
    else:
        expected = LadderAction.NOTE_OCCURRENCE
    assert action is expected
    signal = promotion_recommendation_signal(
        deploy_state=state,
        occurrence_milestone=milestone,
        promotion_recommended_at=None,
        now=NOW,
    )
    assert (signal is RefireSignal.RECOMMEND_PROMOTION) is (
        state is DeployState.PROD_PENDING and milestone is not None
    )


def test_milestone_does_not_change_primary_ladder_action():
    assert classify_refire(
        deploy_state=DeployState.PROD_PENDING,
        ticket_status="Todo",
        deployed_at=None,
        now=NOW,
        settle=SETTLE,
    ) is LadderAction.EXPECTED_NOISE
    assert promotion_recommendation_signal(
        deploy_state=DeployState.PROD_PENDING,
        occurrence_milestone=100,
        promotion_recommended_at=None,
        now=NOW,
    ) is RefireSignal.RECOMMEND_PROMOTION


def test_promotion_recommendation_is_deduped_per_utc_day():
    assert promotion_recommendation_signal(
        deploy_state="prod_pending",
        occurrence_milestone=100,
        promotion_recommended_at=NOW - timedelta(hours=1),
        now=NOW,
    ) is None
    assert promotion_recommendation_signal(
        deploy_state="prod_pending",
        occurrence_milestone=200,
        promotion_recommended_at=NOW - timedelta(days=1),
        now=NOW,
    ) is RefireSignal.RECOMMEND_PROMOTION
    assert promotion_recommendation_signal(
        deploy_state="staging",
        occurrence_milestone=100,
        promotion_recommended_at=None,
        now=NOW,
    ) is None


def test_parse_real_rollbar_fallback():
    parsed = parse_rollbar_alert(
        "<https://app.rollbar.com/a/uwear/fix/item/Uwear-API/2206|"
        "#2206 100th error: ClientError: 400 INVALID_ARGUMENT. {'err...>"
    )
    assert parsed is not None
    assert parsed.signature == "Uwear-API#2206"
    assert parsed.milestone == 100
    assert parsed.title.startswith("ClientError: 400 INVALID_ARGUMENT")


@pytest.mark.parametrize(
    ("label", "milestone", "title"),
    [
        ("#17 New item: RuntimeError: exploded", None, "RuntimeError: exploded"),
        ("#17 New item RuntimeError: exploded", None, "RuntimeError: exploded"),
        ("#17 21st error: RuntimeError: exploded", 21, "RuntimeError: exploded"),
        ("#17 2nd occurrence: RuntimeError: exploded", 2, "RuntimeError: exploded"),
    ],
)
def test_parse_rollbar_forms(label, milestone, title):
    parsed = parse_rollbar_alert(
        f"<https://app.rollbar.com/a/acme/fix/item/worker/17|{label}>"
    )
    assert parsed is not None
    assert parsed.signature == "worker#17"
    assert parsed.milestone == milestone
    assert parsed.title == title


@pytest.mark.parametrize("text", [None, "", "#2206 100th error: nope", "Sentry item #2206"])
def test_non_rollbar_text_returns_none(text):
    assert parse_rollbar_alert(text) is None


@pytest.mark.parametrize(
    ("hints", "expected"),
    [
        ({"merged": True, "base_ref": "main", "head_ref": "staging"}, MergeKind.PROMOTION),
        ({"merged": True, "base_ref": "main", "head_ref": "fix/urgent"}, MergeKind.HOTFIX),
        ({"merged": True, "base_ref": "staging", "head_ref": "fix/bug"}, MergeKind.FIX_TO_STAGING),
        ({"merged": False, "base_ref": "main", "head_ref": "staging"}, MergeKind.OTHER),
        ({"merged": True, "base_ref": "develop", "head_ref": "fix/bug"}, MergeKind.OTHER),
    ],
)
def test_classify_merge_event(hints, expected):
    assert classify_merge_event(hints) is expected


def test_derive_deploy_state_is_shared_mechanical_axis():
    assert derive_deploy_state(merged=True, base_ref="staging", in_staging=True, in_main=True) is DeployState.DEPLOYED
    assert derive_deploy_state(merged=True, base_ref="staging", in_staging=True, in_main=False) is DeployState.PROD_PENDING
    assert derive_deploy_state(merged=True, base_ref="staging", in_staging=None, in_main=None) is DeployState.STAGING
    assert derive_deploy_state(merged=None, base_ref=None, in_staging=None, in_main=None) is None
