"""Cycle adapter for neutral provider subscription-quota probing."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from brain.platform.db.models.cycle import Cycle, CycleRun
from brain.platform.db.models.idea import Idea, IdeaThread
from brain.platform.integrations.provider_quota_preflight import (
    ProviderQuotaPreflightResult,
    probe_provider_quota,
)
from brain.systems.cortex.thought_lifecycle import ThreadMessageCommand, post_thread_message
from brain.systems.cycles.common import (
    SCHEDULED_CYCLE_ORIGIN,
    cycle_run_launch_context,
)

if TYPE_CHECKING:
    from brain.systems.cycles.admission import CycleProviderRoute


def _with_cycle_presentation(
    result: ProviderQuotaPreflightResult,
) -> ProviderQuotaPreflightResult:
    if result.blocked:
        return result.with_presentation(
            visible_message=(
                "Cycle quota blocked: Codex usage is "
                f"{result.used_percent:g}%, at or above the "
                f"{result.thresholds.hard_percent:g}% hard limit. "
                "Illo will admit new runs automatically after usage falls below the limit."
            )
        )
    if result.deferred:
        return result.with_presentation(
            visible_message=(
                "Scheduled Cycle quota deferred: Codex usage is "
                f"{result.used_percent:g}%, at or above the "
                f"{result.thresholds.soft_percent:g}% soft limit. "
                "Illo will try again on a later scheduled run."
            )
        )
    return result


def preflight_cycle_external_quota(
    *,
    route: CycleProviderRoute,
    run: CycleRun,
) -> ProviderQuotaPreflightResult:
    """Probe subscription quota before admitting a Cycle agent run."""

    origin = str(cycle_run_launch_context(run).get("origin") or "")
    result = probe_provider_quota(
        provider=route.provider,
        model=route.model,
        explicit_request=origin != SCHEDULED_CYCLE_ORIGIN,
    )
    return _with_cycle_presentation(result)


async def async_append_cycle_quota_notice(
    session: AsyncSession,
    idea: Idea,
    cycle: Cycle,
    cycle_run: CycleRun,
    preflight: ProviderQuotaPreflightResult,
) -> tuple[dict | None, dict | None]:
    """Persist one visible notice for each quota-restricted episode."""

    await session.scalar(
        select(Idea.id).where(Idea.id == idea.id).with_for_update()
    )
    latest_notice_metadata = await session.scalar(
        select(IdeaThread.metadata_)
        .where(
            IdeaThread.idea_id == idea.id,
            IdeaThread.metadata_.contains({"quota_notice": True}),
        )
        .order_by(IdeaThread.id.desc())
        .limit(1)
    )
    if isinstance(latest_notice_metadata, dict):
        try:
            notice_cycle_run_id = int(latest_notice_metadata["cycle_run_id"])
        except (KeyError, TypeError, ValueError):
            notice_cycle_run_id = None

        if notice_cycle_run_id is not None:
            admitted_since_notice = await session.scalar(
                select(CycleRun.id)
                .where(
                    CycleRun.idea_id == idea.id,
                    CycleRun.id > notice_cycle_run_id,
                    CycleRun.id < cycle_run.id,
                    CycleRun.context_snapshot.contains(
                        {"quota_preflight": {"decision": "admitted"}}
                    ),
                )
                .limit(1)
            )
            if admitted_since_notice is None:
                return None, None

    metadata = {
        "source": "cycle",
        "cycle_id": cycle.id,
        "cycle_run_id": cycle_run.id,
        "quota_notice": True,
        "quota_preflight": preflight.to_dict(),
    }
    result = await post_thread_message(
        session,
        idea=idea,
        command=ThreadMessageCommand(
            idea_id=str(idea.id),
            role="illo",
            content=preflight.visible_message or "Cycle quota admission paused.",
            actor={
                "org_id": str(cycle.org_id) if cycle.org_id else None,
                "name": "Illo",
            },
            attachments=[],
            metadata=metadata,
        ),
        parse_message_type=lambda _content, _role: "agent_response",
        lifecycle_trigger="cycle_quota_preflight_paused",
    )
    return result.message_payload, result.status_change


__all__ = [
    "async_append_cycle_quota_notice",
    "preflight_cycle_external_quota",
]
