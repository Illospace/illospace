"""Workspace data navigation tool handlers."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
import json
import uuid
from typing import Any, Mapping

from sqlalchemy import String, cast, func, or_, select

from brain.systems.runs.tool_catalog.handlers.common import _agent_context, logger

_ZERO_UUID = "00000000-0000-0000-0000-000000000000"


@dataclass(frozen=True)
class WorkspaceDataQueryContext:
    start: datetime | None
    end: datetime | None
    org_id: str | None
    user_id: str | None
    person_ids: list[str]
    idea_id: str | None
    run_id: int | None
    domain_id: int | None
    object_key: str | None
    query: str | None
    search: str | None
    include_archived: bool
    limit: int


@dataclass(frozen=True)
class WorkspaceDataSource:
    name: str
    description: str
    groups: tuple[str, ...]
    handler: Callable[[Any, dict[str, Any], WorkspaceDataQueryContext], None]
    db_backed: bool = True


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _time_bounds(
    time_window: str | None,
    *,
    start_at: str | None = None,
    end_at: str | None = None,
) -> tuple[datetime | None, datetime | None, str]:
    now = _now_utc()
    window = (time_window or "last_7d").strip().lower()
    end = _parse_datetime(end_at) or now
    start = _parse_datetime(start_at)
    if window == "custom":
        return start, end, window
    if window == "today":
        return datetime.combine(now.date(), time.min, tzinfo=timezone.utc), end, window
    if window == "yesterday":
        day = now.date() - timedelta(days=1)
        return (
            datetime.combine(day, time.min, tzinfo=timezone.utc),
            datetime.combine(day, time.max, tzinfo=timezone.utc),
            window,
        )
    if window == "last_24h":
        return now - timedelta(hours=24), end, window
    if window == "this_week":
        start_of_week = now.date() - timedelta(days=now.weekday())
        return datetime.combine(start_of_week, time.min, tzinfo=timezone.utc), end, window
    if window == "this_month":
        return datetime(now.year, now.month, 1, tzinfo=timezone.utc), end, window
    if window == "last_30d":
        return now - timedelta(days=30), end, window
    return now - timedelta(days=7), end, "last_7d"


def _normalize_sources(sources: list[str] | None) -> list[str]:
    adapters = _source_adapters()
    aliases = _source_aliases()
    default_sources = list(_default_source_names())
    if not sources:
        return default_sources
    normalized: list[str] = []
    for source in sources:
        key = str(source or "").strip().lower()
        expanded = aliases.get(key, (key,))
        for item in expanded:
            if item in adapters:
                if item not in normalized:
                    normalized.append(item)
    return normalized or default_sources


def _serialize_dt(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value) if value is not None else None


def _snippet(value: Any, limit: int = 360) -> str | None:
    text = " ".join(str(value or "").split())
    if not text:
        return None
    return text[:limit]


def _jsonable(value: Any) -> Any:
    try:
        json.dumps(value, default=str)
        return value
    except Exception:
        return str(value)


def _text_filter(search: str | None, *columns: Any) -> Any | None:
    needle = " ".join(str(search or "").strip().lower().split())
    if not needle:
        return None
    pattern = f"%{needle}%"
    return or_(*[func.lower(column).like(pattern) for column in columns])


def _apply_date_bounds(stmt: Any, column: Any, start: datetime | None, end: datetime | None) -> Any:
    if start is not None:
        stmt = stmt.where(column >= start)
    if end is not None:
        stmt = stmt.where(column <= end)
    return stmt


def _uuid_text_equals(uuid_column: Any, text_column: Any) -> Any:
    """Compare a UUID column with a legacy text UUID column without aborting Postgres."""
    return cast(uuid_column, String) == text_column


def _session_dialect_name(session: Any) -> str | None:
    try:
        return session.get_bind().dialect.name
    except Exception:
        return None


def _valid_uuid(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return str(uuid.UUID(text))
    except (TypeError, ValueError, AttributeError):
        return None


def _normalize_postgres_uuid_filter(
    payload: dict[str, Any],
    *,
    field: str,
    value: str | None,
    fallback: str = _ZERO_UUID,
) -> str | None:
    if value is None:
        return None
    normalized = _valid_uuid(value)
    if normalized:
        return normalized
    payload["warnings"].append({
        "source": "scope",
        "error": f"Invalid {field} UUID filter; using an empty-result sentinel instead.",
    })
    return fallback


def _rollback_after_source_error(session: Any, source: str) -> None:
    try:
        session.rollback()
    except Exception:
        logger.exception("query_workspace_data rollback failed after source=%s", source)


def _resolve_people(session: Any, *, org_id: str | None, person: str | None) -> list[dict[str, Any]]:
    if not person:
        return []
    from brain.platform.db.models.org import User

    needle = " ".join(str(person).strip().lower().split())
    if not needle:
        return []
    stmt = select(User).where(
        or_(
            func.lower(User.name).like(f"%{needle}%"),
            func.lower(User.email).like(f"%{needle}%"),
        )
    )
    if org_id:
        stmt = stmt.where(User.org_id == org_id)
    rows = session.scalars(stmt.limit(10)).all()
    return [
        {
            "id": str(user.id),
            "name": user.name,
            "email": user.email,
            "org_id": str(user.org_id) if user.org_id is not None else None,
        }
        for user in rows
    ]


def _scope_idea(stmt: Any, Idea: Any, *, org_id: str | None, user_id: str | None) -> Any:
    if org_id:
        return stmt.where(Idea.org_id == org_id)
    if user_id:
        return stmt.where(Idea.user_id == user_id)
    return stmt


def _scope_run(stmt: Any, AgentRun: Any, *, org_id: str | None, user_id: str | None) -> Any:
    if org_id:
        return stmt.where(AgentRun.org_id == org_id)
    if user_id:
        return stmt.where(AgentRun.user_id == user_id)
    return stmt


def _idea_title(idea: Any) -> str | None:
    if not idea:
        return None
    return idea.display_title or idea.title


def _add_error(payload: dict[str, Any], source: str, exc: Exception) -> None:
    payload["warnings"].append({
        "source": source,
        "error": f"{type(exc).__name__}: {str(exc)[:300]}",
    })


def _latest_run_final_answers(session: Any, run_ids: list[int]) -> dict[int, str]:
    if not run_ids:
        return {}
    from brain.platform.db.models.agent_run import AgentRunArtifactRow

    stmt = (
        select(AgentRunArtifactRow.run_id, AgentRunArtifactRow.text)
        .where(
            AgentRunArtifactRow.run_id.in_(run_ids),
            AgentRunArtifactRow.artifact_type == "final_answer",
        )
        .order_by(
            AgentRunArtifactRow.run_id.asc(),
            AgentRunArtifactRow.created_at.desc().nullslast(),
            AgentRunArtifactRow.id.desc(),
        )
    )
    answers: dict[int, str] = {}
    for run_id, text in session.execute(stmt).all():
        run_id_int = int(run_id)
        if run_id_int not in answers:
            answer = _snippet(text, 520)
            if answer:
                answers[run_id_int] = answer
    return answers


def _query_runs(
    session: Any,
    payload: dict[str, Any],
    *,
    start: datetime | None,
    end: datetime | None,
    org_id: str | None,
    user_id: str | None,
    person_ids: list[str],
    idea_id: str | None,
    run_id: int | None,
    search: str | None,
    limit: int,
) -> None:
    from brain.systems.runs.cortex.read_models import project_run_status
    from brain.platform.db.models.run import AgentRun
    from brain.platform.db.models.idea import Idea
    from brain.platform.db.models.org import User

    stmt = (
        select(AgentRun, Idea, User)
        .outerjoin(Idea, _uuid_text_equals(Idea.id, AgentRun.thread_id))
        .outerjoin(User, User.id == AgentRun.user_id)
        .order_by(AgentRun.created_at.desc().nullslast(), AgentRun.id.desc())
        .limit(limit)
    )
    stmt = _scope_run(stmt, AgentRun, org_id=org_id, user_id=user_id)
    stmt = _apply_date_bounds(stmt, AgentRun.created_at, start, end)
    if run_id is not None:
        stmt = stmt.where(AgentRun.id != run_id)
    if idea_id:
        stmt = stmt.where(AgentRun.thread_id == idea_id)
    if person_ids:
        stmt = stmt.where(AgentRun.user_id.in_(person_ids))
    text_match = _text_filter(
        search,
        AgentRun.input_message,
        AgentRun.context_summary,
        Idea.title,
        Idea.display_title,
    )
    if text_match is not None:
        stmt = stmt.where(text_match)

    rows = session.execute(stmt).all()
    final_answers = _latest_run_final_answers(
        session,
        [int(run.id) for run, _idea, _user in rows],
    )
    payload["sources"]["runs"] = [
        {
            "id": int(run.id),
            "type": "run",
            "created_at": _serialize_dt(run.created_at),
            "started_at": _serialize_dt(run.started_at),
            "completed_at": _serialize_dt(run.completed_at),
            "status": project_run_status(run.status),
            "run_status": run.status,
            "settlement_state": None,
            "idea_id": str(run.thread_id),
            "thread_id": str(run.thread_id),
            "idea_title": _idea_title(idea),
            "user_id": str(run.user_id) if run.user_id is not None else None,
            "user_name": user.name if user else None,
            "skill": (((run.metadata_ or {}).get("routing") or {}).get("selected_skill"))
            if isinstance(run.metadata_, dict)
            else None,
            "model": (((run.metadata_ or {}).get("routing") or {}).get("model"))
            if isinstance(run.metadata_, dict)
            else (run.model_policy or {}).get("model"),
            "message": _snippet(run.input_message),
            "last_activity": _snippet(run.context_summary, 240),
            "output": final_answers.get(int(run.id))
            or _snippet(
                ((run.metadata_ or {}).get("final_summary"))
                if isinstance(run.metadata_, dict)
                else None,
                520,
            ),
            "tool_summary": {
                "workers_used": ((run.metadata_ or {}).get("usage") or {}).get("workers_used")
                if isinstance(run.metadata_, dict)
                else None,
                "tokens_total": ((run.metadata_ or {}).get("usage") or {}).get("tokens_total")
                if isinstance(run.metadata_, dict)
                else None,
                "estimated_cost": ((run.metadata_ or {}).get("usage") or {}).get("estimated_cost")
                if isinstance(run.metadata_, dict)
                else None,
            },
            "provenance": {"table": "agent_runs", "id": int(run.id)},
        }
        for run, idea, user in rows
    ]


def _query_threads(
    session: Any,
    payload: dict[str, Any],
    *,
    start: datetime | None,
    end: datetime | None,
    org_id: str | None,
    user_id: str | None,
    person_ids: list[str],
    idea_id: str | None,
    search: str | None,
    limit: int,
) -> None:
    from brain.platform.db.models.idea import Idea, IdeaThread
    from brain.platform.db.models.org import User

    stmt = (
        select(IdeaThread, Idea, User)
        .outerjoin(Idea, Idea.id == IdeaThread.idea_id)
        .outerjoin(User, User.id == IdeaThread.user_id)
        .order_by(IdeaThread.created_at.desc().nullslast(), IdeaThread.id.desc())
        .limit(limit)
    )
    stmt = _scope_idea(stmt, Idea, org_id=org_id, user_id=user_id)
    stmt = _apply_date_bounds(stmt, IdeaThread.created_at, start, end)
    if idea_id:
        stmt = stmt.where(IdeaThread.idea_id == idea_id)
    if person_ids:
        stmt = stmt.where(IdeaThread.user_id.in_(person_ids))
    text_match = _text_filter(search, IdeaThread.content, Idea.title, Idea.display_title)
    if text_match is not None:
        stmt = stmt.where(text_match)

    rows = session.execute(stmt).all()
    payload["sources"]["threads"] = [
        {
            "id": int(thread.id),
            "type": "thread_message",
            "created_at": _serialize_dt(thread.created_at),
            "idea_id": str(thread.idea_id) if thread.idea_id is not None else None,
            "idea_title": _idea_title(idea),
            "role": thread.role,
            "message_type": thread.message_type,
            "user_id": str(thread.user_id) if thread.user_id is not None else None,
            "user_name": user.name if user else None,
            "content": _snippet(thread.content, 520),
            "provenance": {"table": "idea_threads", "id": int(thread.id)},
        }
        for thread, idea, user in rows
    ]


def _query_ideas(
    session: Any,
    payload: dict[str, Any],
    *,
    start: datetime | None,
    end: datetime | None,
    org_id: str | None,
    user_id: str | None,
    person_ids: list[str],
    idea_id: str | None,
    search: str | None,
    include_archived: bool,
    limit: int,
) -> None:
    from brain.platform.db.models.idea import Idea

    stmt = select(Idea).order_by(Idea.updated_at.desc().nullslast(), Idea.created_at.desc()).limit(limit)
    stmt = _scope_idea(stmt, Idea, org_id=org_id, user_id=user_id)
    stmt = _apply_date_bounds(stmt, Idea.updated_at, start, end)
    if idea_id:
        stmt = stmt.where(Idea.id == idea_id)
    if person_ids:
        stmt = stmt.where(Idea.user_id.in_(person_ids))
    if not include_archived:
        stmt = stmt.where(Idea.archived_at.is_(None))
    text_match = _text_filter(search, Idea.title, Idea.display_title, Idea.description, Idea.working_memory)
    if text_match is not None:
        stmt = stmt.where(text_match)

    rows = session.scalars(stmt).all()
    payload["sources"]["ideas"] = [
        {
            "id": str(idea.id),
            "type": "idea",
            "created_at": _serialize_dt(idea.created_at),
            "updated_at": _serialize_dt(idea.updated_at),
            "status": idea.status,
            "title": idea.display_title or idea.title,
            "description": _snippet(idea.description),
            "user_id": str(idea.user_id) if idea.user_id is not None else None,
            "org_id": str(idea.org_id) if idea.org_id is not None else None,
            "active_agents": idea.active_agents,
            "working_memory": _snippet(idea.working_memory, 360),
            "provenance": {"table": "ideas", "id": str(idea.id)},
        }
        for idea in rows
    ]


def _query_tool_calls(
    session: Any,
    payload: dict[str, Any],
    *,
    start: datetime | None,
    end: datetime | None,
    org_id: str | None,
    user_id: str | None,
    person_ids: list[str],
    idea_id: str | None,
    search: str | None,
    limit: int,
) -> None:
    from brain.platform.db.models.agent_run import AgentRunEventRow
    from brain.platform.db.models.run import AgentRun
    from brain.platform.db.models.idea import Idea

    stmt = (
        select(AgentRunEventRow, AgentRun, Idea)
        .join(AgentRun, AgentRun.id == AgentRunEventRow.run_id)
        .outerjoin(Idea, _uuid_text_equals(Idea.id, AgentRun.thread_id))
        .where(AgentRunEventRow.event_type == "run.tool_completed")
        .order_by(AgentRunEventRow.created_at.desc().nullslast(), AgentRunEventRow.id.desc())
        .limit(limit)
    )
    stmt = _scope_run(stmt, AgentRun, org_id=org_id, user_id=user_id)
    stmt = _apply_date_bounds(stmt, AgentRunEventRow.created_at, start, end)
    if idea_id:
        stmt = stmt.where(AgentRun.thread_id == idea_id)
    if person_ids:
        stmt = stmt.where(AgentRun.user_id.in_(person_ids))
    text_match = _text_filter(
        search,
        AgentRunEventRow.event_type,
        Idea.title,
        Idea.display_title,
    )
    if text_match is not None:
        stmt = stmt.where(text_match)

    rows = session.execute(stmt).all()
    payload["sources"]["tool_calls"] = [
        {
            "id": int(event.id),
            "type": "tool_call",
            "called_at": _serialize_dt(event.created_at),
            "run_id": int(event.run_id),
            "idea_id": str(run.thread_id),
            "idea_title": _idea_title(idea),
            "run_status": run.status if run else None,
            "tool_name": (event.payload or {}).get("tool_name"),
            "source": (event.payload or {}).get("source"),
            "args": _snippet((event.payload or {}).get("args"), 420),
            "result": _snippet((event.payload or {}).get("result"), 420),
            "provenance": {"table": "agent_run_events", "id": int(event.id)},
        }
        for event, run, idea in rows
    ]


def _query_domains(
    session: Any,
    payload: dict[str, Any],
    *,
    start: datetime | None,
    end: datetime | None,
    org_id: str | None,
    person_ids: list[str],
    domain_id: int | None,
    search: str | None,
    include_archived: bool,
    limit: int,
) -> None:
    if not org_id:
        payload["warnings"].append({"source": "domains", "error": "org_id required"})
        payload["sources"]["domains"] = []
        return
    from brain.platform.db.models.domain import Domain

    stmt = select(Domain).where(Domain.org_id == org_id).order_by(Domain.updated_at.desc()).limit(limit)
    stmt = _apply_date_bounds(stmt, Domain.updated_at, start, end)
    if domain_id is not None:
        stmt = stmt.where(Domain.id == domain_id)
    if person_ids:
        stmt = stmt.where(or_(Domain.created_by_user_id.in_(person_ids), Domain.updated_by_user_id.in_(person_ids)))
    if not include_archived:
        stmt = stmt.where(Domain.archived_at.is_(None))
    text_match = _text_filter(search, Domain.name, Domain.slug, Domain.description)
    if text_match is not None:
        stmt = stmt.where(text_match)

    rows = session.scalars(stmt).all()
    payload["sources"]["domains"] = [
        {
            "id": int(domain.id),
            "type": "domain",
            "created_at": _serialize_dt(domain.created_at),
            "updated_at": _serialize_dt(domain.updated_at),
            "slug": domain.slug,
            "name": domain.name,
            "description": _snippet(domain.description),
            "created_by_user_id": str(domain.created_by_user_id) if domain.created_by_user_id else None,
            "updated_by_user_id": str(domain.updated_by_user_id) if domain.updated_by_user_id else None,
            "archived_at": _serialize_dt(domain.archived_at),
            "provenance": {"table": "domains", "id": int(domain.id)},
        }
        for domain in rows
    ]


def _query_domain_records(
    session: Any,
    payload: dict[str, Any],
    *,
    start: datetime | None,
    end: datetime | None,
    org_id: str | None,
    person_ids: list[str],
    domain_id: int | None,
    object_key: str | None,
    search: str | None,
    include_archived: bool,
    limit: int,
) -> None:
    if not org_id:
        payload["warnings"].append({"source": "domain_records", "error": "org_id required"})
        payload["sources"]["domain_records"] = []
        return
    from brain.platform.db.models.domain import Domain, DomainObjectType, DomainRecord

    stmt = (
        select(DomainRecord, Domain, DomainObjectType)
        .join(Domain, Domain.id == DomainRecord.domain_id)
        .join(DomainObjectType, DomainObjectType.id == DomainRecord.object_type_id)
        .where(DomainRecord.org_id == org_id)
        .order_by(DomainRecord.updated_at.desc(), DomainRecord.id.desc())
        .limit(limit)
    )
    stmt = _apply_date_bounds(stmt, DomainRecord.updated_at, start, end)
    if domain_id is not None:
        stmt = stmt.where(DomainRecord.domain_id == domain_id)
    if object_key:
        stmt = stmt.where(DomainObjectType.key == object_key)
    if person_ids:
        stmt = stmt.where(or_(DomainRecord.created_by_user_id.in_(person_ids), DomainRecord.updated_by_user_id.in_(person_ids)))
    if not include_archived:
        stmt = stmt.where(DomainRecord.archived_at.is_(None), Domain.archived_at.is_(None))
    text_match = _text_filter(search, DomainRecord.title, DomainRecord.search_text, Domain.name, DomainObjectType.name)
    if text_match is not None:
        stmt = stmt.where(text_match)

    rows = session.execute(stmt).all()
    payload["sources"]["domain_records"] = [
        {
            "id": int(record.id),
            "type": "domain_record",
            "created_at": _serialize_dt(record.created_at),
            "updated_at": _serialize_dt(record.updated_at),
            "domain_id": int(record.domain_id),
            "domain_name": domain.name,
            "object_key": obj.key,
            "object_name": obj.name,
            "title": record.title,
            "data": _jsonable(record.data),
            "version": record.version,
            "provenance": {"table": "domain_records", "id": int(record.id)},
        }
        for record, domain, obj in rows
    ]


def _query_domain_events(
    session: Any,
    payload: dict[str, Any],
    *,
    start: datetime | None,
    end: datetime | None,
    org_id: str | None,
    person_ids: list[str],
    domain_id: int | None,
    search: str | None,
    limit: int,
) -> None:
    if not org_id:
        payload["warnings"].append({"source": "domain_events", "error": "org_id required"})
        payload["sources"]["domain_events"] = []
        return
    from brain.platform.db.models.domain import Domain, DomainEvent

    stmt = (
        select(DomainEvent, Domain)
        .join(Domain, Domain.id == DomainEvent.domain_id)
        .where(DomainEvent.org_id == org_id)
        .order_by(DomainEvent.created_at.desc(), DomainEvent.id.desc())
        .limit(limit)
    )
    stmt = _apply_date_bounds(stmt, DomainEvent.created_at, start, end)
    if domain_id is not None:
        stmt = stmt.where(DomainEvent.domain_id == domain_id)
    if person_ids:
        stmt = stmt.where(DomainEvent.actor_id.in_(person_ids))
    text_match = _text_filter(search, DomainEvent.event_type, DomainEvent.reason, Domain.name)
    if text_match is not None:
        stmt = stmt.where(text_match)

    rows = session.execute(stmt).all()
    payload["sources"]["domain_events"] = [
        {
            "id": int(event.id),
            "type": "domain_event",
            "created_at": _serialize_dt(event.created_at),
            "event_type": event.event_type,
            "domain_id": int(event.domain_id),
            "domain_name": domain.name,
            "record_id": event.record_id,
            "relation_id": event.relation_id,
            "actor_kind": event.actor_kind,
            "actor_id": event.actor_id,
            "run_id": event.run_id,
            "idea_id": str(event.idea_id) if event.idea_id else None,
            "reason": _snippet(event.reason),
            "provenance": {"table": "domain_events", "id": int(event.id)},
        }
        for event, domain in rows
    ]


def _query_workspace_apps(
    session: Any,
    payload: dict[str, Any],
    *,
    start: datetime | None,
    end: datetime | None,
    org_id: str | None,
    person_ids: list[str],
    search: str | None,
    include_archived: bool,
    limit: int,
) -> None:
    if not org_id:
        payload["warnings"].append({"source": "workspace_apps", "error": "org_id required"})
        payload["sources"]["workspace_apps"] = []
        return
    from brain.platform.db.models.workspace_app import WorkspaceApp

    stmt = select(WorkspaceApp).where(WorkspaceApp.org_id == org_id).order_by(WorkspaceApp.updated_at.desc()).limit(limit)
    stmt = _apply_date_bounds(stmt, WorkspaceApp.updated_at, start, end)
    if person_ids:
        stmt = stmt.where(or_(WorkspaceApp.created_by_user_id.in_(person_ids), WorkspaceApp.anchor_user_id.in_(person_ids)))
    if not include_archived:
        stmt = stmt.where(WorkspaceApp.archived_at.is_(None))
    text_match = _text_filter(search, WorkspaceApp.key, WorkspaceApp.name, WorkspaceApp.description, WorkspaceApp.renderer_key)
    if text_match is not None:
        stmt = stmt.where(text_match)

    rows = session.scalars(stmt).all()
    payload["sources"]["workspace_apps"] = [
        {
            "id": str(app.id),
            "type": "workspace_app",
            "created_at": _serialize_dt(app.created_at),
            "updated_at": _serialize_dt(app.updated_at),
            "key": app.key,
            "name": app.name,
            "description": _snippet(app.description),
            "renderer_key": app.renderer_key,
            "created_by_user_id": str(app.created_by_user_id) if app.created_by_user_id else None,
            "anchor_user_id": str(app.anchor_user_id) if app.anchor_user_id else None,
            "archived_at": _serialize_dt(app.archived_at),
            "provenance": {"table": "workspace_apps", "id": str(app.id)},
        }
        for app in rows
    ]


def _query_app_state(
    session: Any,
    payload: dict[str, Any],
    *,
    start: datetime | None,
    end: datetime | None,
    org_id: str | None,
    person_ids: list[str],
    search: str | None,
    limit: int,
) -> None:
    if not org_id:
        payload["warnings"].append({"source": "app_state", "error": "org_id required"})
        payload["sources"]["app_state"] = []
        return
    from brain.platform.db.models.workspace_app import WorkspaceApp, WorkspaceAppState

    stmt = (
        select(WorkspaceAppState, WorkspaceApp)
        .join(WorkspaceApp, WorkspaceApp.id == WorkspaceAppState.app_id)
        .where(WorkspaceAppState.org_id == org_id)
        .order_by(WorkspaceAppState.updated_at.desc(), WorkspaceAppState.id.desc())
        .limit(limit)
    )
    stmt = _apply_date_bounds(stmt, WorkspaceAppState.updated_at, start, end)
    if person_ids:
        stmt = stmt.where(WorkspaceAppState.updated_by_user_id.in_(person_ids))
    text_match = _text_filter(search, WorkspaceAppState.key, WorkspaceAppState.scope, WorkspaceApp.key, WorkspaceApp.name)
    if text_match is not None:
        stmt = stmt.where(text_match)

    rows = session.execute(stmt).all()
    payload["sources"]["app_state"] = [
        {
            "id": int(state.id),
            "type": "workspace_app_state",
            "created_at": _serialize_dt(state.created_at),
            "updated_at": _serialize_dt(state.updated_at),
            "app_id": str(state.app_id),
            "app_name": app.name,
            "scope": state.scope,
            "key": state.key,
            "data": _jsonable(state.data),
            "updated_by_user_id": str(state.updated_by_user_id) if state.updated_by_user_id else None,
            "provenance": {"table": "workspace_app_states", "id": int(state.id)},
        }
        for state, app in rows
    ]


def _query_memories(
    payload: dict[str, Any],
    *,
    query: str | None,
    search: str | None,
    user_id: str | None,
    org_id: str | None,
    limit: int,
) -> None:
    recall_query = (search or query or "").strip()
    if not recall_query:
        payload["sources"]["memories"] = []
        payload["warnings"].append({"source": "memories", "error": "query or search required for memory recall"})
        return
    from brain.app.mcp.server import tool_brain_recall

    result = tool_brain_recall(
        query=recall_query,
        limit=min(max(limit, 1), 10),
        user_id=user_id,
        org_id=org_id,
        expand_lazy_load=True,
    )
    memories = result.get("memories") if isinstance(result, Mapping) else []
    payload["sources"]["memories"] = [
        {
            "id": memory.get("id"),
            "type": "memory",
            "memory_type": memory.get("type"),
            "created_at": memory.get("created_at"),
            "content": _snippet(memory.get("content"), 520),
            "score": memory.get("similarity") or memory.get("score"),
            "provenance": {"table": "memories", "id": memory.get("id")},
        }
        for memory in memories
        if isinstance(memory, Mapping)
    ]


def _activity_timestamp(record: Mapping[str, Any]) -> str | None:
    for key in ("completed_at", "updated_at", "called_at", "created_at", "started_at"):
        value = record.get(key)
        if value:
            return str(value)
    return None


def _activity_sort_key(item: Mapping[str, Any]) -> datetime:
    parsed = _parse_datetime(str(item.get("timestamp") or ""))
    return parsed or datetime.min.replace(tzinfo=timezone.utc)


def _activity_title(record: Mapping[str, Any]) -> str | None:
    for key in ("idea_title", "title", "name", "app_name", "domain_name", "key", "slug"):
        value = record.get(key)
        if value:
            return _snippet(value, 160)
    return None


def _activity_summary(source: str, record: Mapping[str, Any]) -> str | None:
    if source == "runs":
        return _snippet(
            record.get("output") or record.get("message") or record.get("last_activity"),
            280,
        )
    if source == "threads":
        return _snippet(record.get("content"), 280)
    if source == "tool_calls":
        tool_name = record.get("tool_name")
        result = _snippet(record.get("result") or record.get("args"), 220)
        return _snippet(
            f"{tool_name}: {result}" if tool_name and result else tool_name or result,
            280,
        )
    if source in {"ideas", "workspace_apps"}:
        return _snippet(record.get("working_memory") or record.get("description"), 280)
    if source == "app_state":
        return _snippet(record.get("data"), 280)
    if source in {"domain_records", "domain_events", "domains"}:
        return _snippet(
            record.get("reason") or record.get("data") or record.get("description"),
            280,
        )
    return _snippet(record, 280)


def _activity_user_id(record: Mapping[str, Any]) -> str | None:
    for key in (
        "user_id",
        "created_by_user_id",
        "updated_by_user_id",
        "anchor_user_id",
        "actor_id",
    ):
        value = record.get(key)
        if value:
            return str(value)
    return None


def _build_activity_items(payload: Mapping[str, Any], *, limit: int = 30) -> list[dict[str, Any]]:
    sources = payload.get("sources")
    if not isinstance(sources, Mapping):
        return []
    items: list[dict[str, Any]] = []
    for source, rows in sources.items():
        if not isinstance(rows, list):
            continue
        for record in rows:
            if not isinstance(record, Mapping):
                continue
            timestamp = _activity_timestamp(record)
            if not timestamp:
                continue
            items.append({
                "timestamp": timestamp,
                "source": str(source),
                "type": record.get("type") or str(source),
                "title": _activity_title(record),
                "summary": _activity_summary(str(source), record),
                "status": record.get("status") or record.get("run_status"),
                "user_id": _activity_user_id(record),
                "user_name": record.get("user_name"),
                "idea_id": record.get("idea_id"),
                "idea_title": record.get("idea_title"),
                "provenance": record.get("provenance"),
            })
    items.sort(key=_activity_sort_key, reverse=True)
    return items[: max(1, int(limit or 30))]


def _run_runs(session: Any, payload: dict[str, Any], ctx: WorkspaceDataQueryContext) -> None:
    _query_runs(
        session,
        payload,
        start=ctx.start,
        end=ctx.end,
        org_id=ctx.org_id,
        user_id=ctx.user_id,
        person_ids=ctx.person_ids,
        idea_id=ctx.idea_id,
        run_id=ctx.run_id,
        search=ctx.search,
        limit=ctx.limit,
    )


def _run_threads(session: Any, payload: dict[str, Any], ctx: WorkspaceDataQueryContext) -> None:
    _query_threads(
        session,
        payload,
        start=ctx.start,
        end=ctx.end,
        org_id=ctx.org_id,
        user_id=ctx.user_id,
        person_ids=ctx.person_ids,
        idea_id=ctx.idea_id,
        search=ctx.search,
        limit=ctx.limit,
    )


def _run_ideas(session: Any, payload: dict[str, Any], ctx: WorkspaceDataQueryContext) -> None:
    _query_ideas(
        session,
        payload,
        start=ctx.start,
        end=ctx.end,
        org_id=ctx.org_id,
        user_id=ctx.user_id,
        person_ids=ctx.person_ids,
        idea_id=ctx.idea_id,
        search=ctx.search,
        include_archived=ctx.include_archived,
        limit=ctx.limit,
    )


def _run_tool_calls(session: Any, payload: dict[str, Any], ctx: WorkspaceDataQueryContext) -> None:
    _query_tool_calls(
        session,
        payload,
        start=ctx.start,
        end=ctx.end,
        org_id=ctx.org_id,
        user_id=ctx.user_id,
        person_ids=ctx.person_ids,
        idea_id=ctx.idea_id,
        search=ctx.search,
        limit=ctx.limit,
    )


def _run_domains(session: Any, payload: dict[str, Any], ctx: WorkspaceDataQueryContext) -> None:
    _query_domains(
        session,
        payload,
        start=ctx.start,
        end=ctx.end,
        org_id=ctx.org_id,
        person_ids=ctx.person_ids,
        domain_id=ctx.domain_id,
        search=ctx.search,
        include_archived=ctx.include_archived,
        limit=ctx.limit,
    )


def _run_domain_records(session: Any, payload: dict[str, Any], ctx: WorkspaceDataQueryContext) -> None:
    _query_domain_records(
        session,
        payload,
        start=ctx.start,
        end=ctx.end,
        org_id=ctx.org_id,
        person_ids=ctx.person_ids,
        domain_id=ctx.domain_id,
        object_key=ctx.object_key,
        search=ctx.search,
        include_archived=ctx.include_archived,
        limit=ctx.limit,
    )


def _run_domain_events(session: Any, payload: dict[str, Any], ctx: WorkspaceDataQueryContext) -> None:
    _query_domain_events(
        session,
        payload,
        start=ctx.start,
        end=ctx.end,
        org_id=ctx.org_id,
        person_ids=ctx.person_ids,
        domain_id=ctx.domain_id,
        search=ctx.search,
        limit=ctx.limit,
    )


def _run_workspace_apps(session: Any, payload: dict[str, Any], ctx: WorkspaceDataQueryContext) -> None:
    _query_workspace_apps(
        session,
        payload,
        start=ctx.start,
        end=ctx.end,
        org_id=ctx.org_id,
        person_ids=ctx.person_ids,
        search=ctx.search,
        include_archived=ctx.include_archived,
        limit=ctx.limit,
    )


def _run_app_state(session: Any, payload: dict[str, Any], ctx: WorkspaceDataQueryContext) -> None:
    _query_app_state(
        session,
        payload,
        start=ctx.start,
        end=ctx.end,
        org_id=ctx.org_id,
        person_ids=ctx.person_ids,
        search=ctx.search,
        limit=ctx.limit,
    )


def _run_memories(_session: Any, payload: dict[str, Any], ctx: WorkspaceDataQueryContext) -> None:
    _query_memories(
        payload,
        query=ctx.query,
        search=ctx.search,
        user_id=ctx.user_id,
        org_id=ctx.org_id,
        limit=ctx.limit,
    )


_SOURCE_ADAPTERS: dict[str, WorkspaceDataSource] = {
    "runs": WorkspaceDataSource(
        name="runs",
        description="Cortex run/run records with statuses, messages, output summaries, and cost/tool metadata.",
        groups=("all", "activity"),
        handler=_run_runs,
    ),
    "threads": WorkspaceDataSource(
        name="threads",
        description="Cortex idea thread messages and author metadata.",
        groups=("all", "activity"),
        handler=_run_threads,
    ),
    "ideas": WorkspaceDataSource(
        name="ideas",
        description="Thought/idea records, titles, statuses, owners, and working memory snippets.",
        groups=("all", "activity"),
        handler=_run_ideas,
    ),
    "tool_calls": WorkspaceDataSource(
        name="tool_calls",
        description="Persisted Cortex tool-call records with args and result snippets.",
        groups=("all", "activity"),
        handler=_run_tool_calls,
    ),
    "domains": WorkspaceDataSource(
        name="domains",
        description="Domain schema records.",
        groups=("all", "domain"),
        handler=_run_domains,
    ),
    "domain_records": WorkspaceDataSource(
        name="domain_records",
        description="Domain object records and structured data.",
        groups=("all", "domain"),
        handler=_run_domain_records,
    ),
    "domain_events": WorkspaceDataSource(
        name="domain_events",
        description="Domain audit/event stream records.",
        groups=("all", "activity", "domain"),
        handler=_run_domain_events,
    ),
    "workspace_apps": WorkspaceDataSource(
        name="workspace_apps",
        description="Generated workspace apps and app metadata.",
        groups=("all", "activity", "apps"),
        handler=_run_workspace_apps,
    ),
    "app_state": WorkspaceDataSource(
        name="app_state",
        description="Generated workspace app state records.",
        groups=("all", "apps"),
        handler=_run_app_state,
    ),
    "memories": WorkspaceDataSource(
        name="memories",
        description="Optional semantic memory recall, included only when explicitly requested.",
        groups=("memory",),
        handler=_run_memories,
        db_backed=False,
    ),
}


def _source_adapters() -> dict[str, WorkspaceDataSource]:
    return _SOURCE_ADAPTERS


def _default_source_names() -> tuple[str, ...]:
    return tuple(name for name, source in _SOURCE_ADAPTERS.items() if source.db_backed)


def _source_aliases() -> dict[str, tuple[str, ...]]:
    aliases: dict[str, list[str]] = {}
    for source in _SOURCE_ADAPTERS.values():
        aliases.setdefault(source.name, []).append(source.name)
        for group in source.groups:
            aliases.setdefault(group, []).append(source.name)
    return {key: tuple(dict.fromkeys(values)) for key, values in aliases.items()}


def query_workspace_data(
    *,
    sources: list[str] | None = None,
    query: str | None = None,
    search: str | None = None,
    person: str | None = None,
    time_window: str = "last_7d",
    start_at: str | None = None,
    end_at: str | None = None,
    limit: int = 20,
    idea_id: str | None = None,
    domain_id: int | None = None,
    object_key: str | None = None,
    include_archived: bool = False,
    user_id: str | None = None,
    org_id: str | None = None,
    run_id: int | None = None,
) -> dict[str, Any]:
    """Query typed workspace data with source-level failure isolation."""
    from brain.platform.db.repositories.unit_of_work import UnitOfWork

    source_names = _normalize_sources(sources)
    start, end, resolved_window = _time_bounds(time_window, start_at=start_at, end_at=end_at)
    per_source_limit = min(max(int(limit or 20), 1), 100)
    payload: dict[str, Any] = {
        "query": query,
        "search": search,
        "sources_requested": list(sources or ["all"]),
        "checked_sources": source_names,
        "source_catalog": {
            name: {
                "description": adapter.description,
                "groups": list(adapter.groups),
                "db_backed": adapter.db_backed,
            }
            for name, adapter in _source_adapters().items()
        },
        "time_window": {
            "name": resolved_window,
            "start_at": _serialize_dt(start),
            "end_at": _serialize_dt(end),
        },
        "scope": {
            "org_id": org_id,
            "user_id": user_id,
            "person": person,
            "idea_id": idea_id,
            "domain_id": domain_id,
            "object_key": object_key,
        },
        "people": [],
        "activity_items": [],
        "sources": {},
        "counts": {},
        "warnings": [],
    }

    with UnitOfWork() as uow:
        session = uow.session
        if _session_dialect_name(session) == "postgresql":
            org_id = _normalize_postgres_uuid_filter(payload, field="org_id", value=org_id)
            idea_id = _normalize_postgres_uuid_filter(payload, field="idea_id", value=idea_id)
            if not org_id:
                user_id = _normalize_postgres_uuid_filter(payload, field="user_id", value=user_id)
            payload["scope"]["org_id"] = org_id
            payload["scope"]["user_id"] = user_id
            payload["scope"]["idea_id"] = idea_id
        if not org_id and user_id:
            try:
                from brain.platform.db.models.org import User

                current_user = session.get(User, user_id)
                if current_user and current_user.org_id:
                    org_id = str(current_user.org_id)
                    payload["scope"]["org_id"] = org_id
            except Exception as exc:
                _rollback_after_source_error(session, "scope")
                _add_error(payload, "scope", exc)
        if not org_id and not user_id:
            payload["warnings"].append({
                "source": "scope",
                "error": "query_workspace_data requires user_id or org_id for DB-backed workspace sources",
            })
            adapters = _source_adapters()
            source_names = [source for source in source_names if not adapters[source].db_backed]
            payload["checked_sources"] = source_names
        try:
            people = _resolve_people(session, org_id=org_id, person=person)
        except Exception as exc:
            _rollback_after_source_error(session, "people")
            _add_error(payload, "people", exc)
            people = []
        person_ids = [person_row["id"] for person_row in people]
        payload["people"] = people
        if person and not person_ids:
            payload["warnings"].append({
                "source": "people",
                "error": f"No user matched person filter: {person}",
            })
            person_ids = ["00000000-0000-0000-0000-000000000000"]

        adapters = _source_adapters()
        ctx = WorkspaceDataQueryContext(
            start=start,
            end=end,
            org_id=org_id,
            user_id=user_id,
            person_ids=person_ids,
            idea_id=idea_id,
            run_id=run_id,
            domain_id=domain_id,
            object_key=object_key,
            query=query,
            search=search,
            include_archived=include_archived,
            limit=per_source_limit,
        )

        for source in source_names:
            try:
                adapters[source].handler(session, payload, ctx)
            except Exception as exc:
                logger.exception("query_workspace_data source failed source=%s", source)
                _rollback_after_source_error(session, source)
                payload["sources"][source] = []
                _add_error(payload, source, exc)

    payload["counts"] = {
        source: len(rows) if isinstance(rows, list) else 0
        for source, rows in payload["sources"].items()
    }
    payload["total_count"] = sum(payload["counts"].values())
    payload["activity_items"] = _build_activity_items(payload, limit=per_source_limit)
    return payload


def _handle_query_workspace_data(
    sources: list[str] | None = None,
    query: str | None = None,
    search: str | None = None,
    person: str | None = None,
    time_window: str = "last_7d",
    start_at: str | None = None,
    end_at: str | None = None,
    limit: int = 20,
    idea_id: str | None = None,
    domain_id: int | None = None,
    object_key: str | None = None,
    include_archived: bool = False,
) -> str:
    run = getattr(_agent_context, "run", None)
    execution_metadata = getattr(_agent_context, "execution_metadata", {}) or {}
    payload = query_workspace_data(
        sources=sources,
        query=query,
        search=search,
        person=person,
        time_window=time_window,
        start_at=start_at,
        end_at=end_at,
        limit=limit,
        idea_id=idea_id or getattr(_agent_context, "idea_id", None) or execution_metadata.get("idea_id"),
        domain_id=domain_id,
        object_key=object_key,
        include_archived=include_archived,
        user_id=getattr(_agent_context, "user_id", None) or execution_metadata.get("user_id"),
        org_id=getattr(_agent_context, "org_id", None) or execution_metadata.get("org_id"),
        run_id=getattr(run, "run_id", None) or execution_metadata.get("run_id"),
    )
    return json.dumps(payload, default=str)
