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
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Mapping


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


@dataclass(frozen=True, slots=True)
class AlertSignature:
    """Structured identity and occurrence information from a Rollbar alert."""

    project: str
    item_number: int
    title: str
    occurrence_milestone: int | None = None

    @property
    def signature(self) -> str:
        return f"{self.project}#{self.item_number}"

    @property
    def milestone(self) -> int | None:
        """Short alias used by triage callers."""
        return self.occurrence_milestone


_ROLLBAR_ITEM_RE = re.compile(
    r"https?://app\.rollbar\.com/[^\s|>]+/item/"
    r"(?P<project>[^/\s|>]+)/(?P<item>\d+)"
    r"(?:[^|>]*\|(?P<label>[^>]*))?",
    re.IGNORECASE,
)
_MILESTONE_RE = re.compile(
    r"\b(?P<count>\d+)(?:st|nd|rd|th)\s+(?:error|occurrence)\s*:\s*",
    re.IGNORECASE,
)
_NEW_ITEM_RE = re.compile(r"\bnew\s+item\b\s*:?\s*", re.IGNORECASE)
_ITEM_PREFIX_RE = re.compile(r"^\s*#?\d+\s*")


def parse_rollbar_alert(text: str | None) -> AlertSignature | None:
    """Parse Rollbar's Slack attachment fallback into a stable signature.

    The caller assembles the text (including attachment fallback/title text).
    Messages without a Rollbar item URL return ``None`` even if they contain an
    issue-like ``#123`` fragment, avoiding false matches on ordinary Slack text.
    """
    if not text:
        return None
    match = _ROLLBAR_ITEM_RE.search(str(text))
    if not match:
        return None

    label = (match.group("label") or "").strip()
    label = _ITEM_PREFIX_RE.sub("", label, count=1)
    milestone_match = _MILESTONE_RE.search(label)
    milestone = int(milestone_match.group("count")) if milestone_match else None
    if milestone_match:
        title = label[milestone_match.end():]
    else:
        title = _NEW_ITEM_RE.sub("", label, count=1)
    title = title.strip().rstrip("> ")

    return AlertSignature(
        project=match.group("project"),
        item_number=int(match.group("item")),
        title=title,
        occurrence_milestone=milestone,
    )


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
