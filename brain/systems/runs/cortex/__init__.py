"""Cortex projection and admission shell for AgentRun."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from brain.systems.runs.domain import AgentRunRequest
from brain.systems.runs.events import run_event
from brain.systems.runs.skill_commands import iter_slash_skill_commands, parse_slash_skill_names
from brain.systems.runs.store import AsyncAgentRunStore
from brain.systems.runs.cortex.runner import queue_status, queue_status_async, start_runner, stop_runner
from brain.systems.runs.cortex.thread_binding import a_build_run_request
from brain.platform.db.models.idea import Idea, IdeaStateLog

UnitOfWork = None


def _unit_of_work_factory():
    global UnitOfWork
    if UnitOfWork is None:
        from brain.platform.db.repositories.unit_of_work import UnitOfWork as _UnitOfWork

        UnitOfWork = _UnitOfWork
    return UnitOfWork



@dataclass(frozen=True)
class RunAdmissionRequest:
    idea_id: str
    event: str
    message: str
    priority: int = 0
    user_id: str | None = None
    metadata: dict[str, Any] | None = None
    source: str | None = None
    producer: str | None = None
    idempotency_key: str | None = None


@dataclass(frozen=True)
class RunAdmissionResult:
    ok: bool
    run_id: int | None = None
    skipped_reason: str | None = None



def _parse_skill_mentions(message: str) -> list[str]:
    return parse_slash_skill_names(message)


def _parse_skill_override(message: str) -> tuple[str | None, str]:
    raw = message or ""
    for command in iter_slash_skill_commands(raw):
        name = command["name"]
        prefix = raw[: command["start"]]
        suffix = raw[command["end"] :]
        inline = bool(prefix.rsplit("\n", 1)[-1].strip())
        if inline:
            return name, f"{prefix}{name}{suffix}"
        return name, f"{prefix}{suffix.lstrip()}"
    return None, raw


def ensure_schema() -> None:
    """Import-time schema hook retained as an explicit no-op.

    AgentRun schema ownership now belongs to Alembic and SQLAlchemy models; the
    runtime does not perform production DDL.
    """
    return None



async def _a_mark_idea_working_for_run_admission(session, idea_id: str, run_id: int) -> dict[str, Any] | None:
    idea = await session.get(Idea, str(idea_id))
    if idea is None:
        return None

    previous_status = str(getattr(idea, "status", "") or "")
    if previous_status in {"archived", "resolved"}:
        return None
    if previous_status == "working":
        return {
            "idea_id": str(idea_id),
            "old_status": previous_status,
            "new_status": "working",
            "run_id": int(run_id),
            "changed": False,
        }

    now = datetime.now(timezone.utc)
    idea.status = "working"
    idea.updated_at = now
    session.add(
        IdeaStateLog(
            idea_id=str(idea_id),
            from_state=previous_status or None,
            to_state="working",
            changed_at=now,
            trigger="agent_run_admitted",
        )
    )
    await session.flush()
    return {
        "idea_id": str(idea_id),
        "old_status": previous_status,
        "new_status": "working",
        "run_id": int(run_id),
        "changed": True,
    }


async def _record_adaptation(run_id: int, adaptation: dict[str, Any] | str, *, session=None) -> None:
    """Record an adaptation as AgentRun metadata and as an append-only event."""

    async def _write(active_session) -> None:
        store = AsyncAgentRunStore(active_session)
        row = await store.require_run(int(run_id))
        payload = adaptation if isinstance(adaptation, dict) else {"message": str(adaptation)}
        current = row.adaptations
        current.append(dict(payload))
        row.adaptations = current
        await store.append_event(
            run_event(
                int(run_id),
                "run.adaptation_recorded",
                dict(payload),
                root_run_id=row.root_run_id,
            )
        )

    if session is not None:
        await _write(session)
        return
    async with _unit_of_work_factory()() as uow:
        await _write(uow.session)


async def _get_adaptation_history(run_id: int, *, session=None) -> list[dict[str, Any]]:
    async def _read(active_session) -> list[dict[str, Any]]:
        row = await AsyncAgentRunStore(active_session).get_run(int(run_id))
        return row.adaptations if row else []

    if session is not None:
        return await _read(session)
    async with _unit_of_work_factory()() as uow:
        return await _read(uow.session)


async def async_admit_run(request: RunAdmissionRequest, *, session=None) -> RunAdmissionResult:
    async def _admit(active_session) -> RunAdmissionResult:
        run_request: AgentRunRequest = await a_build_run_request(
            active_session,
            idea_id=request.idea_id,
            event=request.event,
            message=request.message,
            user_id=request.user_id,
            metadata=request.metadata or {},
            priority=request.priority,
            source=request.source,
            producer=request.producer,
            idempotency_key=request.idempotency_key,
        )
        run = await AsyncAgentRunStore(active_session).create_run(run_request)
        await _a_mark_idea_working_for_run_admission(active_session, request.idea_id, int(run.id))
        return RunAdmissionResult(ok=True, run_id=run.id)

    if session is not None:
        return await _admit(session)
    async with _unit_of_work_factory()() as uow:
        return await _admit(uow.session)


async def async_cancel_runs_for_idea(idea_id: str) -> int:
    from sqlalchemy import select
    from brain.systems.runs.status import RunStatus
    from brain.platform.db.models.agent_run import AgentRunRow

    count = 0
    async with _unit_of_work_factory()() as uow:
        store = AsyncAgentRunStore(uow.session)
        result = await uow.session.scalars(
            select(AgentRunRow).where(
                AgentRunRow.thread_id == idea_id,
                AgentRunRow.status.in_(["queued", "starting", "running", "paused", "verifying"]),
            )
        )
        for row in result.all():
            await store.append_event(
                run_event(
                    int(row.id),
                    "run.canceled",
                    {"reason": "canceled_for_thread"},
                    root_run_id=row.root_run_id,
                )
            )
            await store.set_status(row.id, RunStatus.CANCELED, reason="canceled_for_thread")
            count += 1
    return count


async def async_idea_run_history(idea_id: str) -> list[dict[str, Any]]:
    from brain.systems.runs.cortex.read_models import serialize_run_history_async

    return await serialize_run_history_async(idea_id)


cancel_idea_runs = async_cancel_runs_for_idea
supersede_runs_for_idea = async_cancel_runs_for_idea


__all__ = [
    "RunAdmissionRequest",
    "RunAdmissionResult",
    "_get_adaptation_history",
    "_a_mark_idea_working_for_run_admission",
    "_parse_skill_mentions",
    "_parse_skill_override",
    "_record_adaptation",
    "async_admit_run",
    "cancel_idea_runs",
    "async_cancel_runs_for_idea",
    "async_idea_run_history",
    "queue_status",
    "queue_status_async",
    "ensure_schema",
    "start_runner",
    "stop_runner",
    "supersede_runs_for_idea",
]
