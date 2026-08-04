"""Briefing-owned packet outcome policy and alert delivery."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
import os

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from brain.platform.db.models.cycle import Cycle
from brain.platform.db.models.launch_handoff import LaunchHandoff
from brain.systems.briefing.outcomes import (
    DEFAULT_OUTCOME_WINDOW_HOURS,
    PacketOutcomeReport,
    load_packet_outcome_report,
)
from brain.systems.cortex.thread_links import public_app_base_url
from brain.systems.failure_guard.core import (
    FailureGuardEvaluation,
    FailureGuardTriggerKind,
    FailureGuardTriggerResult,
    async_evaluate_failure_edges,
)
from brain.systems.failure_guard.cycle_latches import CycleAlertLatchStore
from brain.systems.failure_guard.slack_delivery import (
    FailureAlertPresentation,
    FailureAlertSubject,
    SlackFailureAlertPolicy,
    async_deliver_failure_alert,
)
from brain.systems.slack.client import slack_web_client_from_runtime


# Ten mints catch a real weekly flatline without paging on low-volume noise.
PACKET_FLATLINE_MIN_MINTS = 10
PACKET_FLATLINE_TRIGGER_KIND = FailureGuardTriggerKind(
    "packet_launch_flatline"
)

logger = logging.getLogger(__name__)


def _timestamp(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class PacketFlatlineAssessment:
    """Alert-only packet launch history and flatline policy."""

    report: PacketOutcomeReport
    last_launched_at: datetime | None

    @classmethod
    async def load(
        cls,
        session: AsyncSession,
        *,
        org_id: str,
        now: datetime,
    ) -> PacketFlatlineAssessment:
        report = await load_packet_outcome_report(
            session,
            org_id=org_id,
            now=now,
            since_hours=DEFAULT_OUTCOME_WINDOW_HOURS,
        )
        last_launched_at = await session.scalar(
            select(func.max(LaunchHandoff.last_launched_at)).where(
                LaunchHandoff.org_id == str(org_id),
                LaunchHandoff.source_surface == "inbound_triage",
                LaunchHandoff.launch_count > 0,
            )
        )
        return cls(
            report=report,
            last_launched_at=_timestamp(last_launched_at),
        )

    @property
    def active(self) -> bool:
        return (
            self.report.summary.minted >= PACKET_FLATLINE_MIN_MINTS
            and self.report.summary.launched == 0
        )

    @property
    def days_since_last_launch(self) -> int | None:
        if self.last_launched_at is None:
            return None
        elapsed = max(
            0.0,
            (self.report.now - self.last_launched_at).total_seconds(),
        )
        return int(elapsed // timedelta(days=1).total_seconds())

    def trigger_result(self) -> FailureGuardTriggerResult:
        days = self.days_since_last_launch
        days_line = str(days) if days is not None else "no recorded launch"
        digest_line = self.report.digest_line or "Packets: 0 minted · 0 launched"
        return FailureGuardTriggerResult(
            kind=PACKET_FLATLINE_TRIGGER_KIND,
            active=self.active,
            public_details={
                "minted": self.report.summary.minted,
                "launched": self.report.summary.launched,
                "since_hours": self.report.since_hours,
                "days_since_last_launch": days,
            },
            alert_title="Packet launch flatline",
            alert_summary=(
                f"{digest_line}\n"
                f"Days since last launch: {days_line}"
            ),
        )


async def async_monitor_packet_outcomes(
    session: AsyncSession,
    cycle: Cycle,
    *,
    cycle_run_id: int,
    now: datetime,
    latch_store: CycleAlertLatchStore,
) -> FailureGuardEvaluation | None:
    """Page once for a scheduled digest with a packet launch flatline."""
    if not cycle.org_id:
        return None

    assessment = await PacketFlatlineAssessment.load(
        session,
        org_id=str(cycle.org_id),
        now=now,
    )
    result = assessment.trigger_result()
    latches = dict(await latch_store.load_latches())
    if not result.active and PACKET_FLATLINE_TRIGGER_KIND in latches:
        await latch_store.delete_latch(PACKET_FLATLINE_TRIGGER_KIND)
        await session.flush()

    evaluation = await async_evaluate_failure_edges(
        results=(result,),
        failure_signature=None,
        last_error="No packet minted in the rolling window was launched",
        now=now,
        store=latch_store,
        latch_new_edges=True,
    )
    await session.flush()
    if not evaluation.crossed_edges:
        return evaluation

    days = assessment.days_since_last_launch
    days_line = str(days) if days is not None else "no recorded launch"
    logger.error(
        "Packet launch flatline alert: cycle_id=%s cycle_run_id=%s minted=%s "
        "days_since_last_launch=%s",
        cycle.id,
        cycle_run_id,
        assessment.report.summary.minted,
        days_line,
    )
    try:
        await async_deliver_failure_alert(
            policy=SlackFailureAlertPolicy(
                provide_client=slack_web_client_from_runtime,
                requested_by="packet_outcome_monitor",
                reason="Deliver a packet launch flatline alert to the team.",
                channel=(
                    os.getenv("ILLO_CYCLE_FAILURE_ALERT_CHANNEL", "").strip()
                    or "#alerts"
                ),
                unknown_error_text="Packet launch flatline",
            ),
            subject=FailureAlertSubject(
                identity_label="Cycle",
                identity=f"{cycle.name} (#{cycle.id})",
                url_label="Cycle",
                url=(
                    f"{public_app_base_url()}/cycles"
                    f"?cycle_id={cycle.id}&run_id={cycle_run_id}"
                ),
                link_label="open cycle state",
            ),
            presentation=FailureAlertPresentation(
                title=result.alert_title,
                summary=result.alert_summary,
            ),
            error_text="No packet minted in the rolling window was launched",
        )
    except Exception:
        logger.exception(
            "Packet flatline Slack delivery failed: cycle_id=%s cycle_run_id=%s",
            cycle.id,
            cycle_run_id,
        )
    return evaluation
