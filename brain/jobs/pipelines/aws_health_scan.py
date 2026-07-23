"""Run the hourly Uwear AWS health scan as one headless AgentRun."""
from __future__ import annotations

import asyncio
import json
import sys
import uuid

from sqlalchemy import select

from brain.platform.db.models.agent_run import AgentRunRow
from brain.platform.db.models.org import User
from brain.platform.db.models.skill_bundle import SkillInstallation
from brain.platform.db.repositories.unit_of_work import UnitOfWork
from brain.systems.runs.status import RunStatus, TERMINAL_RUN_STATUSES, coerce_run_status
from brain.systems.runs.work_intake import WorkIntakeEvent, admit_work

SKILL_NAME = "uwear-aws-health-scan"
RUN_TIMEOUT_SECONDS = 840
POLL_INTERVAL_SECONDS = 2.0


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


async def spawn_health_scan_run() -> int:
    """Admit exactly one headless run through the canonical work-intake boundary."""
    invocation_id = str(uuid.uuid4())
    async with UnitOfWork() as uow:
        skill = await uow.skills.get_by_name(SKILL_NAME)
        if skill is None:
            raise LookupError(f"Skill '{SKILL_NAME}' not found")

        actor = await _skill_actor(uow.session, skill.skill_installation_id)
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


async def _read_run_status(run_id: int) -> RunStatus:
    async with UnitOfWork() as uow:
        run = await uow.session.get(AgentRunRow, int(run_id))
        if run is None:
            raise LookupError(f"Agent run {run_id} not found")
        return coerce_run_status(run.status)


async def wait_for_terminal_run(
    run_id: int,
    *,
    timeout_seconds: float = RUN_TIMEOUT_SECONDS,
    poll_interval_seconds: float = POLL_INTERVAL_SECONDS,
) -> RunStatus:
    """Poll the admitted run until it settles or the pipeline wait budget expires."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.0, float(timeout_seconds))
    while True:
        status = await _read_run_status(run_id)
        if status in TERMINAL_RUN_STATUSES:
            return status

        remaining = deadline - loop.time()
        if remaining <= 0:
            raise TimeoutError(
                f"Agent run {run_id} did not reach a terminal state within {timeout_seconds:g}s"
            )
        await asyncio.sleep(min(max(0.01, poll_interval_seconds), remaining))


async def async_main(*, timeout_seconds: float = RUN_TIMEOUT_SECONDS) -> int:
    try:
        run_id = await spawn_health_scan_run()
    except Exception as exc:  # noqa: BLE001 - process boundary must fail cleanly
        print(f"AWS health scan spawn failed: {exc}", file=sys.stderr)
        return 1

    try:
        status = await wait_for_terminal_run(run_id, timeout_seconds=timeout_seconds)
    except Exception as exc:  # noqa: BLE001 - process boundary must fail cleanly
        print(f"AWS health scan run {run_id} wait failed: {exc}", file=sys.stderr)
        return 1

    print(json.dumps({"run_id": run_id, "status": status.value}, sort_keys=True))
    if status != RunStatus.COMPLETED:
        print(f"AWS health scan run {run_id} ended with status {status.value}", file=sys.stderr)
        return 1
    return 0


def main() -> int:
    return asyncio.run(async_main())


if __name__ == "__main__":
    raise SystemExit(main())
