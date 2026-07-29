"""Run the hourly Uwear AWS health scan as one headless AgentRun."""
from __future__ import annotations

import asyncio
import sys
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from brain.contracts.scheduler_handoff import (
    emit_detached_agent_run_handoff,
)
from brain.platform.db.enums import SettlementState
from brain.platform.db.models.org import User
from brain.platform.db.models.scheduler import SchedulerJob, SchedulerRun
from brain.platform.db.models.skill_bundle import SkillInstallation
from brain.platform.db.repositories.unit_of_work import UnitOfWork
from brain.systems.runtime_settings import display as runtime_display
from brain.systems.runs.work_intake import WorkIntakeEvent, admit_work

SKILL_NAME = "uwear-aws-health-scan"


def _timestamp_rendering_contract(
    display_timezone: str,
    *,
    scan_started_at: datetime,
) -> str:
    rendered_scan_start = runtime_display.format_display_timestamp(
        scan_started_at,
        display_timezone,
    )
    return (
        f"\ndisplay-timezone: {display_timezone}\n"
        f"scan-started-at: {rendered_scan_start}\n"
        "timestamp-rendering: Render EVERY timestamp in the final alert in "
        f"{display_timezone} alongside its source UTC time, never as UTC-only. "
        "The final Slack posting gate rejects UTC-only timestamp lines."
    )


async def _skill_actor(session, skill_installation_id: int | None) -> User:
    """Resolve the installed skill's user, falling back to the oldest workspace user."""
    installation = None
    if skill_installation_id is not None:
        installation = await session.get(SkillInstallation, int(skill_installation_id))

    if installation is not None:
        actor_id = installation.user_id or installation.installed_by_user_id
        if actor_id:
            actor = await session.get(User, str(actor_id))
            if actor is None:
                raise LookupError(f"Skill installation user '{actor_id}' not found")
            return actor

    stmt = select(User).order_by(User.created_at.asc(), User.id.asc()).limit(1)
    if installation is not None and installation.org_id:
        stmt = stmt.where(User.org_id == str(installation.org_id))
    actor = (await session.scalars(stmt)).first()
    if actor is None:
        raise LookupError("No Illospace user is available to run the AWS health scan")
    return actor


async def spawn_health_scan_run(*, now: datetime | None = None) -> int:
    """Admit exactly one headless run through the canonical work-intake boundary."""
    clock = now or datetime.now(timezone.utc)
    if clock.tzinfo is None:
        clock = clock.replace(tzinfo=timezone.utc)
    else:
        clock = clock.astimezone(timezone.utc)

    invocation_id = str(uuid.uuid4())
    async with UnitOfWork() as uow:
        skill = await uow.skills.get_by_name(SKILL_NAME)
        if skill is None:
            raise LookupError(f"Skill '{SKILL_NAME}' not found")

        actor = await _skill_actor(uow.session, skill.skill_installation_id)
        display_config = await runtime_display.async_get_runtime_display_config(uow.session)
        display_instruction = _timestamp_rendering_contract(
            display_config.display_timezone,
            scan_started_at=clock,
        )
        last_success_started_at = await uow.session.scalar(
            select(SchedulerRun.started_at)
            .join(SchedulerJob, SchedulerRun.job_id == SchedulerJob.id)
            .where(
                SchedulerJob.job_key == "uwear_aws_health_scan",
                SchedulerRun.status == SettlementState.SETTLED_SUCCESS,
                SchedulerRun.started_at.is_not(None),
            )
            .order_by(SchedulerRun.started_at.desc(), SchedulerRun.id.desc())
            .limit(1)
        )
        coverage_line = ""
        if last_success_started_at is not None:
            if last_success_started_at.tzinfo is None:
                last_success_started_at = last_success_started_at.replace(tzinfo=timezone.utc)
            else:
                last_success_started_at = last_success_started_at.astimezone(timezone.utc)
            if last_success_started_at < clock - timedelta(minutes=70):
                coverage_since = max(last_success_started_at, clock - timedelta(hours=6))
                coverage_line = (
                    "\ncoverage-since: "
                    + runtime_display.format_display_timestamp(
                        coverage_since,
                        display_config.display_timezone,
                    )
                )

        result = await admit_work(
            uow.session,
            WorkIntakeEvent(
                source="internal",
                event_type="internal.manual",
                org_id=str(actor.org_id),
                actor={"id": str(actor.id), "org_id": str(actor.org_id)},
                target={
                    "kind": "external_agent_headless_ask",
                    "thread_id": f"headless:{SKILL_NAME}:{invocation_id}",
                    "headless": True,
                    "final_answer_target_surface": "headless",
                },
                payload={
                    "message": (
                        f"/{SKILL_NAME}\n\n"
                        "Execute this skill exactly once. Load and follow its full procedure, "
                        "then return its required health verdict and evidence."
                        f"{display_instruction}{coverage_line}"
                    ),
                    "workspace_ref": {"source": "scheduler", "mode": "headless"},
                    "metadata": {
                        "origin": "scheduler",
                        "originating_surface": "scheduler",
                        "source_surface": "scheduler",
                        "triggering_surface": "scheduler",
                        "final_answer_target_surface": "headless",
                        "headless": True,
                        "execution_profile": "fast",
                        "recipe": "fast",
                        "thinking_tier": skill.thinking_tier,
                        "skill_name": SKILL_NAME,
                        "slash_skill_names": [SKILL_NAME],
                        "display_timezone": display_config.display_timezone,
                        "enforce_display_timezone_on_slack": True,
                    },
                },
                policy={
                    "producer": "scheduler",
                    "idempotency_key": f"uwear_aws_health_scan:{invocation_id}",
                    "run_event": "uwear_aws_health_scan",
                },
            ),
        )
        if not result.ok or result.run_id is None:
            raise RuntimeError(result.skipped_reason or "Failed to admit AWS health scan run")
        return int(result.run_id)


async def async_main() -> int:
    try:
        run_id = await spawn_health_scan_run()
    except Exception as exc:  # noqa: BLE001 - process boundary must fail cleanly
        print(f"AWS health scan spawn failed: {exc}", file=sys.stderr)
        return 1

    print(emit_detached_agent_run_handoff(run_id))
    return 0


def main() -> int:
    return asyncio.run(async_main())


if __name__ == "__main__":
    raise SystemExit(main())
