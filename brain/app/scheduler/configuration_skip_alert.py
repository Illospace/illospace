"""One durable alert for scheduler jobs blocked by missing configuration."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
import os
from urllib.parse import quote

from brain.app.scheduler.overdue_alert_state import (
    release_scheduler_alert,
    try_claim_scheduler_alert,
)
from brain.systems.cortex.thread_links import public_app_base_url
from brain.systems.failure_guard.slack_delivery import (
    FailureAlertPresentation,
    FailureAlertSubject,
    SlackFailureAlertPolicy,
    async_deliver_failure_alert,
)
from brain.systems.slack.client import slack_web_client_from_runtime


SCHEDULER_CONFIGURATION_SKIP_ALERT_KEY = "scheduler_configuration_skip"

AlertClaim = Callable[..., Awaitable[bool]]
AlertDelivery = Callable[..., Awaitable[None]]
AlertRelease = Callable[[], Awaitable[None]]


async def claim_scheduler_configuration_skip_alert(*, alerted_at: datetime) -> bool:
    """Claim and commit the configuration-skip alert in a short transaction."""
    from brain.platform.db.repositories.unit_of_work import UnitOfWork

    async with UnitOfWork() as uow:
        return await try_claim_scheduler_alert(
            uow.session,
            alert_key=SCHEDULER_CONFIGURATION_SKIP_ALERT_KEY,
            alerted_at=alerted_at,
        )


async def release_scheduler_configuration_skip_alert() -> None:
    """Release a failed delivery claim so a later settlement can retry it."""
    from brain.platform.db.repositories.unit_of_work import UnitOfWork

    async with UnitOfWork() as uow:
        await release_scheduler_alert(
            uow.session,
            alert_key=SCHEDULER_CONFIGURATION_SKIP_ALERT_KEY,
        )


async def async_alert_first_configuration_skip(
    *,
    job_key: str,
    run_id: int,
    reason: str,
    alerted_at: datetime,
    claim_alert: AlertClaim = claim_scheduler_configuration_skip_alert,
    release_alert: AlertRelease = release_scheduler_configuration_skip_alert,
    deliver_alert: AlertDelivery = async_deliver_failure_alert,
) -> bool:
    """Deliver the first configuration-skip alert across replicas and restarts."""
    if not await claim_alert(alerted_at=alerted_at):
        return False

    try:
        await deliver_alert(
            policy=SlackFailureAlertPolicy(
                provide_client=slack_web_client_from_runtime,
                requested_by="scheduler_configuration_skip_alert",
                reason="Report a scheduler job blocked by missing configuration.",
                channel=(
                    os.getenv("ILLO_SCHEDULER_FAILURE_ALERT_CHANNEL", "").strip()
                    or "#alerts"
                ),
                unknown_error_text="Unknown scheduler configuration gap",
            ),
            subject=FailureAlertSubject(
                identity_label="Job key",
                identity=job_key,
                url_label="Job",
                url=(
                    f"{public_app_base_url()}/api/system/scheduler"
                    f"?job_key={quote(job_key, safe='')}&run_id={run_id}"
                ),
                link_label="open scheduler state",
            ),
            presentation=FailureAlertPresentation(
                title="Scheduler job blocked by missing configuration",
                summary=(
                    "The job reported a successful skip, but it cannot succeed "
                    "until its configuration changes."
                ),
            ),
            error_text=reason,
        )
    except Exception:
        await release_alert()
        raise
    return True


__all__ = [
    "SCHEDULER_CONFIGURATION_SKIP_ALERT_KEY",
    "async_alert_first_configuration_skip",
    "claim_scheduler_configuration_skip_alert",
    "release_scheduler_configuration_skip_alert",
]
