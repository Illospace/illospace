"""Pure deploy-state decisions for alert-linked GitHub tickets.

``deploy_state`` describes the latest fix attempt, independently of the
ticket's workflow status.  This module is the single owner of that axis: Slack
alert parsing, re-fire classification, promotion recommendations, merge-event
classification, and mechanical state derivation all live here.  Database,
GitHub, and Slack wiring supply facts but do not reinterpret them.

Indeterminate infrastructure reads are intentionally represented outside this
module as ``None``.  Callers must degrade open by leaving persisted state
unchanged rather than guessing that a fix did or did not ship.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Mapping

from brain.platform.provider_alerts import AlertSignature, parse_rollbar_alert


class DeployState(StrEnum):
    """Where the latest fix attempt is in the release lifecycle."""

    STAGING = "staging"
    PROD_PENDING = "prod_pending"
    DEPLOYED = "deployed"
    VERIFIED = "verified"


class LadderAction(StrEnum):
    """Action for an alert occurrence matched to an existing ticket."""

    NOTE_OCCURRENCE = "note_occurrence"
    EXPECTED_NOISE = "expected_noise"
    REOPEN_ESCALATE = "reopen_escalate"


class RefireSignal(StrEnum):
    """Additional signal emitted beside the main ladder action."""

    RECOMMEND_PROMOTION = "recommend_promotion"


class MergeKind(StrEnum):
    PROMOTION = "promotion"
    HOTFIX = "hotfix"
    FIX_TO_STAGING = "fix_to_staging"
    OTHER = "other"


def as_utc_datetime(value: datetime | str | None) -> datetime | None:
    """Normalize a datetime/ISO value to aware UTC, or return ``None``."""
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _coerce_deploy_state(value: DeployState | str | None) -> DeployState | None:
    if value is None or value == "":
        return None
    try:
        return DeployState(value)
    except (TypeError, ValueError):
        return None


def classify_refire(
    *,
    deploy_state: DeployState | str | None,
    ticket_status: str | None,
    deployed_at: datetime | None,
    now: datetime,
    settle: timedelta,
) -> LadderAction:
    """Classify an alert re-fire against the latest known fix attempt.

    A merely-staged fix wins over a stale ``Done``: re-firing while the fix
    awaits promotion is expected noise even when the ticket was closed
    prematurely — the caller normalizes the invalid status quietly instead of
    escalating to a builder who has nothing new to act on.
    """
    state = _coerce_deploy_state(deploy_state)
    if state in {DeployState.STAGING, DeployState.PROD_PENDING}:
        return LadderAction.EXPECTED_NOISE
    if str(ticket_status or "").casefold() == "done":
        return LadderAction.REOPEN_ESCALATE
    if state is DeployState.VERIFIED:
        return LadderAction.REOPEN_ESCALATE
    if state is DeployState.DEPLOYED:
        deployed = as_utc_datetime(deployed_at)
        current = as_utc_datetime(now)
        if deployed is not None and current is not None and current < deployed + settle:
            return LadderAction.EXPECTED_NOISE
        return LadderAction.REOPEN_ESCALATE
    return LadderAction.NOTE_OCCURRENCE


def promotion_recommendation_signal(
    *,
    deploy_state: DeployState | str | None,
    occurrence_milestone: int | None,
    promotion_recommended_at: datetime | None,
    now: datetime,
) -> RefireSignal | None:
    """Return the once-per-UTC-day early-promotion signal, when warranted."""
    if _coerce_deploy_state(deploy_state) is not DeployState.PROD_PENDING:
        return None
    if occurrence_milestone is None or occurrence_milestone <= 0:
        return None
    current = as_utc_datetime(now)
    previous = as_utc_datetime(promotion_recommended_at)
    if current is None:
        return None
    if previous is not None and previous.date() == current.date():
        return None
    return RefireSignal.RECOMMEND_PROMOTION


def classify_merge_event(hints: Mapping[str, object] | None) -> MergeKind:
    """Classify a merged pull request from normalized webhook hints."""
    values = hints or {}
    if values.get("merged") is not True:
        return MergeKind.OTHER
    base = str(values.get("base_ref") or "").removeprefix("refs/heads/").casefold()
    head = str(values.get("head_ref") or "").removeprefix("refs/heads/").casefold()
    if base == "main" and head == "staging":
        return MergeKind.PROMOTION
    if base == "main":
        return MergeKind.HOTFIX
    if base == "staging":
        return MergeKind.FIX_TO_STAGING
    return MergeKind.OTHER


def derive_deploy_state(
    *,
    merged: bool | None,
    base_ref: str | None,
    in_staging: bool | None,
    in_main: bool | None,
) -> DeployState | None:
    """Derive the mechanical state shared by the sweep and agent tool."""
    if in_main is True:
        return DeployState.DEPLOYED
    if in_staging is True and in_main is False:
        return DeployState.PROD_PENDING
    if merged is True and str(base_ref or "").casefold() == "staging":
        return DeployState.STAGING
    return None
