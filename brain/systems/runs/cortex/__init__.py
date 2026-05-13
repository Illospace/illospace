"""Cortex projection and admission shell for AgentRun."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from brain.systems.runs.domain import AgentRunRequest
from brain.systems.runs.events import run_event
from brain.systems.runs.skill_commands import iter_slash_skill_commands, parse_slash_skill_names
from brain.systems.runs.store import AgentRunStore, AsyncAgentRunStore
from brain.systems.runs.cortex.runner import queue_status, start_runner, stop_runner
from brain.systems.runs.cortex.thread_binding import a_build_run_request, build_run_request
from brain.platform.db.models.idea import Idea, IdeaStateLog
from brain.platform.db.repositories.unit_of_work import UnitOfWork, open_unit_of_work


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


def _skill_exists(name: str) -> bool:
    try:
        from brain.platform.db.repositories.unit_of_work import UnitOfWork, open_unit_of_work

        with open_unit_of_work(UnitOfWork) as uow:
            return bool(uow.skills.get_by_name(name))
    except Exception:
        return False


def _parse_skill_mentions(message: str) -> list[str]:
    return parse_slash_skill_names(message)


def _parse_skill_override(message: str) -> tuple[str | None, str]:
    raw = message or ""
    for command in iter_slash_skill_commands(raw):
        name = command["name"]
        if not _skill_exists(name):
            return None, raw
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


def _mark_idea_working_for_run_admission(session, idea_id: str, run_id: int) -> dict[str, Any] | None:
    idea = session.get(Idea, str(idea_id))
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
    session.flush()
    return {
        "idea_id": str(idea_id),
        "old_status": previous_status,
        "new_status": "working",
        "run_id": int(run_id),
        "changed": True,
    }


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


def _record_adaptation(run_id: int, adaptation: dict[str, Any] | str, *, session=None) -> None:
    """Record an adaptation as AgentRun metadata and as an append-only event."""

    def _write(active_session) -> None:
        row = AgentRunStore(active_session).require_run(int(run_id))
        payload = adaptation if isinstance(adaptation, dict) else {"message": str(adaptation)}
        current = row.adaptations
        current.append(dict(payload))
        row.adaptations = current
        AgentRunStore(active_session).append_event(
            run_event(
                int(run_id),
                "run.adaptation_recorded",
                dict(payload),
                root_run_id=row.root_run_id,
            )
        )

    if session is not None:
        _write(session)
        return
    with open_unit_of_work(UnitOfWork) as uow:
        _write(uow.session)


def _get_adaptation_history(run_id: int, *, session=None) -> list[dict[str, Any]]:
    def _read(active_session) -> list[dict[str, Any]]:
        row = AgentRunStore(active_session).get_run(int(run_id))
        return row.adaptations if row else []

    if session is not None:
        return _read(session)
    with open_unit_of_work(UnitOfWork) as uow:
        return _read(uow.session)


def admit_run(request: RunAdmissionRequest, *, session=None) -> RunAdmissionResult:
    def _admit(active_session) -> RunAdmissionResult:
        run_request: AgentRunRequest = build_run_request(
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
        run = AgentRunStore(active_session).create_run(run_request)
        _mark_idea_working_for_run_admission(active_session, request.idea_id, int(run.id))
        return RunAdmissionResult(ok=True, run_id=run.id)
    if session is not None:
        return _admit(session)
    with open_unit_of_work(UnitOfWork) as uow:
        return _admit(uow.session)


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
    async with UnitOfWork() as uow:
        return await _admit(uow.session)


def idea_run_history(idea_id: str) -> list[dict[str, Any]]:
    from brain.systems.runs.cortex.read_models import serialize_run_history

    return serialize_run_history(idea_id)


def cancel_runs_for_idea(idea_id: str) -> int:
    from brain.systems.runs.status import RunStatus
    from brain.platform.db.models.agent_run import AgentRunRow

    count = 0
    with open_unit_of_work(UnitOfWork) as uow:
        store = AgentRunStore(uow.session)
        rows = (
            uow.session.query(AgentRunRow)
            .filter(
                AgentRunRow.thread_id == idea_id,
                AgentRunRow.status.in_(["queued", "starting", "running", "paused", "verifying"]),
            )
            .all()
        )
        for row in rows:
            store.append_event(
                run_event(
                    int(row.id),
                    "run.canceled",
                    {"reason": "canceled_for_thread"},
                    root_run_id=row.root_run_id,
                )
            )
            store.set_status(row.id, RunStatus.CANCELED, reason="canceled_for_thread")
            count += 1
    return count


cancel_idea_runs = cancel_runs_for_idea
supersede_runs_for_idea = cancel_runs_for_idea


__all__ = [
    "RunAdmissionRequest",
    "RunAdmissionResult",
    "_get_adaptation_history",
    "_a_mark_idea_working_for_run_admission",
    "_mark_idea_working_for_run_admission",
    "_parse_skill_mentions",
    "_parse_skill_override",
    "_record_adaptation",
    "admit_run",
    "async_admit_run",
    "cancel_idea_runs",
    "cancel_runs_for_idea",
    "idea_run_history",
    "queue_status",
    "ensure_schema",
    "start_runner",
    "stop_runner",
    "supersede_runs_for_idea",
]
