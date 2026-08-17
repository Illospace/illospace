"""Slack adapter for Cycle receipt-silence alerts."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
import os

from brain.systems.cortex.thread_links import public_app_base_url
from brain.systems.cycles.silence_policy import CycleSilenceCandidate
from brain.systems.failure_guard.slack_delivery import (
    FailureAlertPresentation,
    FailureAlertSubject,
    SlackFailureAlertPolicy,
    async_deliver_failure_alert,
)
from brain.systems.slack.client import slack_web_client_from_runtime


FailureAlertDelivery = Callable[..., Awaitable[None]]


def _minutes(candidate: CycleSilenceCandidate) -> int:
    return int(candidate.grace_margin.total_seconds() // 60)


def _candidate_summary(candidate: CycleSilenceCandidate) -> str:
    last_seen = (
        candidate.last_receipt_at.isoformat()
        if candidate.last_receipt_at is not None
        else "never"
    )
    return "\n".join(
        (
            f"Binding: {candidate.binding}",
            f"Expected receipt: {candidate.expected_at.isoformat()}",
            f"Last receipt: {last_seen}",
            f"Grace margin: {_minutes(candidate)}m",
        )
    )


async def async_deliver_cycle_silence_alert(
    candidate: CycleSilenceCandidate,
    *,
    deliver_alert: FailureAlertDelivery = async_deliver_failure_alert,
) -> None:
    """Compose and deliver one missed-receipt Slack alert."""
    last_seen = (
        candidate.last_receipt_at.isoformat()
        if candidate.last_receipt_at is not None
        else "never"
    )
    await deliver_alert(
        policy=SlackFailureAlertPolicy(
            provide_client=slack_web_client_from_runtime,
            requested_by="cycle_silence_monitor",
            reason="Deliver a missed Cycle receipt alert to the team.",
            channel=(
                os.getenv("ILLO_CYCLE_FAILURE_ALERT_CHANNEL", "").strip()
                or "#alerts"
            ),
            unknown_error_text="Cycle receipt is missing",
        ),
        subject=FailureAlertSubject(
            identity_label="Schedule",
            identity=f"{candidate.name} (#{candidate.cycle_id})",
            url_label="Schedule",
            url=f"{public_app_base_url()}/cycles?cycle_id={candidate.cycle_id}",
            link_label="open schedule state",
        ),
        presentation=FailureAlertPresentation(
            title="Cycle schedule missed receipt",
            summary=_candidate_summary(candidate),
        ),
        error_text=(
            f"Expected a receipt at {candidate.expected_at.isoformat()}; "
            f"last seen {last_seen}."
        ),
    )


__all__ = ["async_deliver_cycle_silence_alert"]
