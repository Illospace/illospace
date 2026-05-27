"""Cycle execution target resolution."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import and_, or_, select

from brain.platform.db.models.cycle import Cycle, CycleRun
from brain.platform.db.models.idea import Idea
from brain.platform.db.models.org import User
from brain.platform.db.models.run import AgentRun
from brain.systems.cortex.thought_lifecycle import (
    ThoughtStatusCommand,
    transition_thought_status,
)
from brain.systems.cycles.status import CYCLE_RUN_ACTIVE_STATUSES


@dataclass(frozen=True)
class CycleExecutionTarget:
    idea: Idea
    should_publish_idea: bool
    output_target_ephemeral: bool


async def async_resolve_cycle_execution_target(
    session,
    *,
    cycle: Cycle,
    run: CycleRun,
) -> CycleExecutionTarget:
    idea = await _async_load_target_idea(session, cycle)
    should_publish_idea = False
    output_target_ephemeral = False

    if idea and idea.archived_at is not None:
        await transition_thought_status(
            session,
            idea=idea,
            command=ThoughtStatusCommand(
                to_status="needs_input",
                trigger="cycle_reopen",
                actor={
                    "user_id": str(cycle.user_id) if cycle.user_id else None,
                    "org_id": str(cycle.org_id) if cycle.org_id else None,
                },
            ),
        )
        should_publish_idea = True

    if idea and await _async_idea_has_active_run(session, idea.id):
        idea = _new_cycle_run_idea(cycle, run, per_run=True)
        session.add(idea)
        await session.flush()
        should_publish_idea = True
        output_target_ephemeral = True

    if idea is None:
        idea = _new_cycle_run_idea(cycle, run, per_run=False)
        session.add(idea)
        await session.flush()
        cycle.target_idea_id = idea.id
        should_publish_idea = True

    return CycleExecutionTarget(
        idea=idea,
        should_publish_idea=should_publish_idea,
        output_target_ephemeral=output_target_ephemeral,
    )


def serialize_execution_idea(idea: Idea) -> dict:
    return {
        "id": idea.id,
        "title": idea.title,
        "display_title": idea.display_title,
        "description": idea.description,
        "status": idea.status,
        "origin": idea.origin,
        "origin_ref": idea.origin_ref,
        "salience_score": idea.salience_score,
        "position_x": idea.position_x,
        "position_y": idea.position_y,
        "created_at": idea.created_at.isoformat() if idea.created_at else None,
        "updated_at": idea.updated_at.isoformat() if idea.updated_at else None,
        "user_id": idea.user_id,
        "archived_at": idea.archived_at.isoformat() if idea.archived_at else None,
        "active_agents": idea.active_agents,
        "attachments": idea.attachments or [],
    }


async def _async_load_target_idea(session, cycle: Cycle) -> Idea | None:
    if not cycle.target_idea_id:
        return None
    result = await session.scalars(
        select(Idea)
        .where(
            Idea.id == cycle.target_idea_id,
            _cycle_target_idea_scope_condition(cycle),
        )
        .with_for_update()
    )
    return result.first()


async def _async_idea_has_active_run(session, idea_id: str) -> bool:
    stmt = (
        select(AgentRun.id)
        .where(
            AgentRun.thread_id == idea_id,
            AgentRun.status.in_(CYCLE_RUN_ACTIVE_STATUSES),
        )
        .limit(1)
    )
    result = await session.execute(stmt)
    return result.first() is not None


def _cycle_target_idea_scope_condition(cycle: Cycle):
    if cycle.org_id:
        org_user_ids = select(User.id).where(User.org_id == cycle.org_id)
        return or_(
            Idea.org_id == cycle.org_id,
            and_(Idea.org_id.is_(None), Idea.user_id.in_(org_user_ids)),
        )
    return Idea.user_id == cycle.user_id


def _new_cycle_run_idea(cycle: Cycle, run: CycleRun, *, per_run: bool) -> Idea:
    return Idea(
        title=_cycle_idea_title(cycle, run.scheduled_for, per_run=per_run),
        description=cycle.prompt[:2000],
        status="emerged",
        origin="cycle_run" if per_run else "cycle",
        origin_ref=f"cycle:{cycle.id}:run:{run.id}" if per_run else f"cycle:{cycle.id}",
        user_id=cycle.user_id,
        org_id=cycle.org_id,
    )


def _cycle_idea_title(cycle: Cycle, scheduled_for: datetime, *, per_run: bool) -> str:
    if not per_run:
        return cycle.name
    local_time = scheduled_for.astimezone(ZoneInfo(cycle.timezone))
    return f"{cycle.name} - {local_time.strftime('%b %d %I:%M %p')}"
