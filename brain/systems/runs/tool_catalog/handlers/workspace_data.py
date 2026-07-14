"""Workspace data navigation tool handlers."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
import inspect
import json
import uuid
from typing import Any, Mapping

from sqlalchemy import String, and_, cast, func, or_, select

from brain.kernel.common.pagination import next_offset_token, page_offset
from brain.systems.cortex.thread_links import thread_id_from_reference, thread_link_payload
from brain.systems.runs.tool_definitions import WORKSPACE_OVERVIEW_SPARSE_GUIDANCE
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
    cycle_id: int | None
    object_key: str | None
    query: str | None
    search: str | None
    include_archived: bool
    limit: int
    offset: int


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
    if window in {"all", "any", "none"}:
        return None, None, "all"
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


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def _result_all(result: Any) -> list[Any]:
    result = await _maybe_await(result)
    return list(await _maybe_await(result.all()))


async def _session_execute_all(session: Any, stmt: Any) -> list[Any]:
    return await _result_all(session.execute(stmt))


async def _session_scalars_all(session: Any, stmt: Any) -> list[Any]:
    return await _result_all(session.scalars(stmt))


async def _session_get(session: Any, model: Any, identifier: Any) -> Any:
    return await _maybe_await(session.get(model, identifier))


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


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _workspace_page_kind(source_names: list[str]) -> str:
    return f"workspace_data:{','.join(source_names)}"


def _page_rows(
    payload: dict[str, Any],
    source: str,
    rows: list[Any],
    *,
    limit: int,
) -> list[Any]:
    if len(rows) > limit:
        payload.setdefault("_more_sources", []).append(source)
    return rows[:limit]


def _thread_id_from_first_reference(*values: Any) -> str | None:
    for value in values:
        text = _optional_text(value)
        if not text:
            continue
        parsed = thread_id_from_reference(text, allow_raw_id=True)
        if parsed:
            return parsed
    return None


def _normalize_postgres_uuid_filter(
    payload: dict[str, Any],
    *,
    field: str,
    value: str | None,
    fallback: str = _ZERO_UUID,
) -> str | None:
    text = _optional_text(value)
    if text is None:
        return None
    normalized = _valid_uuid(text)
    if normalized:
        return normalized
    payload["warnings"].append({
        "source": "scope",
        "error": f"Invalid {field} UUID filter; using an empty-result sentinel instead.",
    })
    return fallback


async def _rollback_after_source_error(session: Any, source: str) -> None:
    try:
        await _maybe_await(session.rollback())
    except Exception:
        logger.exception("query_workspace_data rollback failed after source=%s", source)


async def _resolve_people(session: Any, *, org_id: str | None, person: str | None) -> list[dict[str, Any]]:
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
    rows = await _session_scalars_all(session, stmt.limit(10))
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


def _thread_reference_for_idea(idea: Any, idea_id: Any = None) -> dict[str, Any] | None:
    resolved_id = str(idea_id or getattr(idea, "id", "") or "").strip()
    if not resolved_id:
        return None
    return {
        "type": "thread_reference",
        "object_type": "thread",
        "object_id": resolved_id,
        "thread_id": resolved_id,
        "status": "available",
        "title": _idea_title(idea) or "Untitled thread",
        "preview_summary": getattr(idea, "preview_summary", None),
        "preview_source": getattr(idea, "preview_source", None),
        "preview_updated_at": _serialize_dt(getattr(idea, "preview_updated_at", None)),
        **thread_link_payload(resolved_id),
    }


def _thread_link_fields(idea: Any, idea_id: Any = None) -> dict[str, Any]:
    reference = _thread_reference_for_idea(idea, idea_id)
    if not reference:
        return {}
    return {
        **thread_link_payload(reference["thread_id"]),
        "thread_reference": reference,
    }


def _add_error(payload: dict[str, Any], source: str, exc: Exception) -> None:
    payload["warnings"].append({
        "source": source,
        "error": f"{type(exc).__name__}: {str(exc)[:300]}",
    })


async def _latest_run_final_answers(session: Any, run_ids: list[int]) -> dict[int, str]:
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
    for run_id, text in await _session_execute_all(session, stmt):
        run_id_int = int(run_id)
        if run_id_int not in answers:
            answer = _snippet(text, 520)
            if answer:
                answers[run_id_int] = answer
    return answers


def _project_context_resource_summary(context: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(context, Mapping):
        return {"count": 0, "items": []}
    resources = [item for item in (context.get("resources") or []) if isinstance(item, Mapping)]
    return {
        "count": len(resources),
        "items": [
            {
                "id": item.get("id"),
                "kind": item.get("kind") or item.get("type"),
                "label": item.get("label") or item.get("name"),
                "path": item.get("path"),
                "repo": item.get("repo"),
                "uri": item.get("uri"),
            }
            for item in resources[:10]
        ],
    }


async def _query_team_members(
    session: Any,
    payload: dict[str, Any],
    *,
    start: datetime | None,
    end: datetime | None,
    org_id: str | None,
    user_id: str | None,
    person_ids: list[str],
    search: str | None,
    limit: int,
) -> None:
    from brain.platform.db.models.org import User

    stmt = select(User).order_by(User.created_at.desc()).limit(limit)
    if org_id:
        stmt = stmt.where(User.org_id == org_id)
    elif user_id:
        stmt = stmt.where(User.id == user_id)
    else:
        payload["warnings"].append({"source": "team_members", "error": "org_id or user_id required"})
        payload["sources"]["team_members"] = []
        return
    if person_ids:
        stmt = stmt.where(User.id.in_(person_ids))
    text_match = _text_filter(search, User.name, User.email, User.role)
    if text_match is not None:
        stmt = stmt.where(text_match)

    rows = await _session_scalars_all(session, stmt)
    payload["sources"]["team_members"] = [
        {
            "id": str(user.id),
            "type": "team_member",
            "created_at": _serialize_dt(user.created_at),
            "user_id": str(user.id),
            "org_id": str(user.org_id) if user.org_id is not None else None,
            "name": user.name,
            "email": user.email,
            "role": user.role,
            "approved": bool(user.approved),
            "attribution_enabled": bool(user.attribution_enabled),
            "provenance": {"table": "users", "id": str(user.id)},
        }
        for user in rows
    ]


async def _query_runs(
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
    if idea_id and run_id is not None:
        stmt = stmt.where(
            or_(AgentRun.thread_id == idea_id, AgentRun.parent_run_id == run_id)
        )
    elif idea_id:
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

    rows = await _session_execute_all(session, stmt)
    final_answers = await _latest_run_final_answers(
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
            **_thread_link_fields(idea, run.thread_id),
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


async def _query_threads(
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

    rows = await _session_execute_all(session, stmt)
    thread_rows: list[dict[str, Any]] = []
    for thread, idea, user in rows:
        links = thread_link_payload(thread.idea_id) if thread.idea_id is not None else {}
        thread_rows.append({
            "id": int(thread.id),
            "type": "thread_message",
            "created_at": _serialize_dt(thread.created_at),
            "idea_id": str(thread.idea_id) if thread.idea_id is not None else None,
            "thread_id": str(thread.idea_id) if thread.idea_id is not None else None,
            "idea_title": _idea_title(idea),
            **links,
            "thread_reference": _thread_reference_for_idea(idea, thread.idea_id),
            "role": thread.role,
            "message_type": thread.message_type,
            "user_id": str(thread.user_id) if thread.user_id is not None else None,
            "user_name": user.name if user else None,
            "content": _snippet(thread.content, 520),
            "provenance": {"table": "idea_threads", "id": int(thread.id)},
        })
    payload["sources"]["threads"] = thread_rows


async def _query_ideas(
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

    rows = await _session_scalars_all(session, stmt)
    idea_rows: list[dict[str, Any]] = []
    for idea in rows:
        links = thread_link_payload(idea.id)
        idea_rows.append({
            "id": str(idea.id),
            "type": "idea",
            "created_at": _serialize_dt(idea.created_at),
            "updated_at": _serialize_dt(idea.updated_at),
            "status": idea.status,
            "title": idea.display_title or idea.title,
            "thread_id": str(idea.id),
            **links,
            "thread_reference": _thread_reference_for_idea(idea),
            "description": _snippet(idea.description),
            "preview_summary": getattr(idea, "preview_summary", None),
            "preview_source": getattr(idea, "preview_source", None),
            "preview_updated_at": _serialize_dt(getattr(idea, "preview_updated_at", None)),
            "user_id": str(idea.user_id) if idea.user_id is not None else None,
            "org_id": str(idea.org_id) if idea.org_id is not None else None,
            "active_agents": idea.active_agents,
            "working_memory": _snippet(idea.working_memory, 360),
            "provenance": {"table": "ideas", "id": str(idea.id)},
        })
    payload["sources"]["ideas"] = idea_rows


async def _query_tool_calls(
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

    rows = await _session_execute_all(session, stmt)
    payload["sources"]["tool_calls"] = [
        {
            "id": int(event.id),
            "type": "tool_call",
            "called_at": _serialize_dt(event.created_at),
            "run_id": int(event.run_id),
            "idea_id": str(run.thread_id),
            **_thread_link_fields(idea, run.thread_id),
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


async def _query_project_profiles(
    session: Any,
    payload: dict[str, Any],
    *,
    start: datetime | None,
    end: datetime | None,
    org_id: str | None,
    user_id: str | None,
    person_ids: list[str],
    search: str | None,
    include_archived: bool,
    limit: int,
) -> None:
    if not org_id:
        payload["warnings"].append({"source": "project_profiles", "error": "org_id required"})
        payload["sources"]["project_profiles"] = []
        return
    from brain.platform.db.models.idea import ProjectProfile, ProjectProfileAccess
    from brain.platform.db.models.org import User
    from brain.systems.cortex.project_context.access import project_profile_visible_predicate

    stmt = (
        select(ProjectProfile, User)
        .outerjoin(User, User.id == ProjectProfile.user_id)
        .where(ProjectProfile.org_id == org_id)
        .where(project_profile_visible_predicate(ProjectProfile, ProjectProfileAccess, user_id))
        .order_by(ProjectProfile.created_at.desc())
        .limit(limit)
    )
    stmt = _apply_date_bounds(stmt, ProjectProfile.created_at, start, end)
    if person_ids:
        stmt = stmt.where(ProjectProfile.user_id.in_(person_ids))
    elif user_id and not org_id:
        stmt = stmt.where(ProjectProfile.user_id == user_id)
    if not include_archived:
        stmt = stmt.where(ProjectProfile.active.is_(True))
    text_match = _text_filter(
        search,
        ProjectProfile.slug,
        ProjectProfile.name,
        ProjectProfile.description,
    )
    if text_match is not None:
        stmt = stmt.where(text_match)

    rows = await _session_execute_all(session, stmt)
    payload["sources"]["project_profiles"] = [
        {
            "id": str(profile.id),
            "type": "project_profile",
            "created_at": _serialize_dt(profile.created_at),
            "slug": profile.slug,
            "name": profile.name,
            "description": _snippet(profile.description),
            "active": bool(profile.active),
            "visibility": getattr(profile, "visibility", "public") or "public",
            "user_id": str(profile.user_id) if profile.user_id is not None else None,
            "user_name": user.name if user else None,
            "org_id": str(profile.org_id) if profile.org_id is not None else None,
            "default_environment_binding_id": profile.default_environment_binding_id,
            "resources": _project_context_resource_summary(profile.project_context),
            "metadata": _jsonable(profile.metadata_ or {}),
            "provenance": {"table": "project_profiles", "id": str(profile.id)},
        }
        for profile, user in rows
    ]


async def _query_project_attachments(
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
    from brain.platform.db.models.idea import Idea, IdeaProjectAttachment, ProjectProfile
    from brain.platform.db.models.org import User

    stmt = (
        select(IdeaProjectAttachment, Idea, ProjectProfile, User)
        .join(Idea, Idea.id == IdeaProjectAttachment.idea_id)
        .outerjoin(ProjectProfile, ProjectProfile.id == IdeaProjectAttachment.project_profile_id)
        .outerjoin(User, User.id == IdeaProjectAttachment.attached_by)
        .order_by(IdeaProjectAttachment.created_at.desc(), IdeaProjectAttachment.id.desc())
        .limit(limit)
    )
    stmt = _scope_idea(stmt, Idea, org_id=org_id, user_id=user_id)
    stmt = _apply_date_bounds(stmt, IdeaProjectAttachment.created_at, start, end)
    if idea_id:
        stmt = stmt.where(IdeaProjectAttachment.idea_id == idea_id)
    if person_ids:
        stmt = stmt.where(or_(IdeaProjectAttachment.attached_by.in_(person_ids), Idea.user_id.in_(person_ids)))
    if not include_archived:
        stmt = stmt.where(IdeaProjectAttachment.status != "invalid")
    text_match = _text_filter(
        search,
        Idea.title,
        Idea.display_title,
        ProjectProfile.slug,
        ProjectProfile.name,
        IdeaProjectAttachment.status,
    )
    if text_match is not None:
        stmt = stmt.where(text_match)

    rows = await _session_execute_all(session, stmt)
    payload["sources"]["project_attachments"] = [
        {
            "id": int(attachment.id),
            "type": "project_attachment",
            "created_at": _serialize_dt(attachment.created_at),
            "idea_id": str(attachment.idea_id),
            **_thread_link_fields(idea, attachment.idea_id),
            "idea_title": _idea_title(idea),
            "project_profile_id": str(attachment.project_profile_id) if attachment.project_profile_id else None,
            "project_name": profile.name if profile else None,
            "project_slug": profile.slug if profile else None,
            "attached_by": str(attachment.attached_by) if attachment.attached_by else None,
            "user_id": str(attachment.attached_by) if attachment.attached_by else None,
            "user_name": user.name if user else None,
            "status": attachment.status,
            "validation_errors": _jsonable(attachment.validation_errors or []),
            "resources": _project_context_resource_summary(attachment.snapshot),
            "permission_scope": _jsonable(attachment.permission_scope or {}),
            "environment_binding_id": attachment.environment_binding_id,
            "provenance": {"table": "idea_project_attachments", "id": int(attachment.id)},
        }
        for attachment, idea, profile, user in rows
    ]


async def _query_domains(
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
    offset: int = 0,
) -> None:
    if not org_id:
        payload["warnings"].append({"source": "domains", "error": "org_id required"})
        payload["sources"]["domains"] = []
        return
    from brain.platform.db.models.domain import Domain

    stmt = (
        select(Domain)
        .where(Domain.org_id == org_id)
        .order_by(Domain.updated_at.desc(), Domain.id.desc())
        .offset(offset)
        .limit(limit + 1)
    )
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

    rows = _page_rows(
        payload,
        "domains",
        await _session_scalars_all(session, stmt),
        limit=limit,
    )
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


async def _query_domain_records(
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
    offset: int = 0,
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
        .offset(offset)
        .limit(limit + 1)
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

    rows = _page_rows(
        payload,
        "domain_records",
        await _session_execute_all(session, stmt),
        limit=limit,
    )
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


async def _query_domain_events(
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
    offset: int = 0,
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
        .offset(offset)
        .limit(limit + 1)
    )
    stmt = _apply_date_bounds(stmt, DomainEvent.created_at, start, end)
    if domain_id is not None:
        stmt = stmt.where(DomainEvent.domain_id == domain_id)
    if person_ids:
        stmt = stmt.where(DomainEvent.actor_id.in_(person_ids))
    text_match = _text_filter(search, DomainEvent.event_type, DomainEvent.reason, Domain.name)
    if text_match is not None:
        stmt = stmt.where(text_match)

    rows = _page_rows(
        payload,
        "domain_events",
        await _session_execute_all(session, stmt),
        limit=limit,
    )
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
            **_thread_link_fields(None, event.idea_id),
            "reason": _snippet(event.reason),
            "provenance": {"table": "domain_events", "id": int(event.id)},
        }
        for event, domain in rows
    ]


async def _query_workspace_apps(
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

    rows = await _session_scalars_all(session, stmt)
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


async def _query_app_state(
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

    rows = await _session_execute_all(session, stmt)
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


def _scope_cycle(
    stmt: Any,
    Cycle: Any,
    User: Any,
    *,
    org_id: str | None,
    user_id: str | None,
) -> Any:
    if org_id:
        org_user_ids = select(User.id).where(User.org_id == org_id)
        return stmt.where(or_(Cycle.org_id == org_id, and_(Cycle.org_id.is_(None), Cycle.user_id.in_(org_user_ids))))
    if user_id:
        return stmt.where(Cycle.user_id == user_id)
    return stmt


async def _query_cycles(
    session: Any,
    payload: dict[str, Any],
    *,
    start: datetime | None,
    end: datetime | None,
    org_id: str | None,
    user_id: str | None,
    person_ids: list[str],
    cycle_id: int | None,
    search: str | None,
    include_archived: bool,
    limit: int,
    offset: int = 0,
) -> None:
    from brain.platform.db.models.cycle import Cycle
    from brain.platform.db.models.idea import Idea
    from brain.platform.db.models.org import User

    stmt = (
        select(Cycle, User, Idea)
        .outerjoin(User, User.id == Cycle.user_id)
        .outerjoin(Idea, Idea.id == Cycle.target_idea_id)
        .order_by(Cycle.updated_at.desc().nullslast(), Cycle.created_at.desc(), Cycle.id.desc())
        .offset(offset)
        .limit(limit + 1)
    )
    stmt = _scope_cycle(stmt, Cycle, User, org_id=org_id, user_id=user_id)
    stmt = _apply_date_bounds(stmt, Cycle.updated_at, start, end)
    if person_ids:
        stmt = stmt.where(Cycle.user_id.in_(person_ids))
    if cycle_id is not None:
        stmt = stmt.where(Cycle.id == cycle_id)
    if not include_archived:
        stmt = stmt.where(Cycle.deleted_at.is_(None))
    text_match = _text_filter(
        search,
        Cycle.name,
        Cycle.prompt,
        Cycle.schedule_expr,
        Cycle.last_status,
        Cycle.last_error,
        Idea.title,
        Idea.display_title,
    )
    if text_match is not None:
        stmt = stmt.where(text_match)

    rows = _page_rows(
        payload,
        "cycles",
        await _session_execute_all(session, stmt),
        limit=limit,
    )
    payload["sources"]["cycles"] = [
        {
            "id": int(cycle.id),
            "type": "cycle",
            "created_at": _serialize_dt(cycle.created_at),
            "updated_at": _serialize_dt(cycle.updated_at),
            "user_id": str(cycle.user_id) if cycle.user_id is not None else None,
            "user_name": user.name if user else None,
            "org_id": str(cycle.org_id) if cycle.org_id is not None else None,
            "workspace_id": str(cycle.org_id) if cycle.org_id is not None else None,
            "creator_type": getattr(cycle, "creator_type", None),
            "creator_id": getattr(cycle, "creator_id", None),
            "maintainer_type": getattr(cycle, "maintainer_type", None),
            "maintainer_id": getattr(cycle, "maintainer_id", None),
            "name": cycle.name,
            "prompt": _snippet(cycle.prompt, 520),
            "schedule_expr": cycle.schedule_expr,
            "timezone": cycle.timezone,
            "enabled": bool(cycle.enabled),
            "target_idea_id": str(cycle.target_idea_id) if cycle.target_idea_id else None,
            "idea_id": str(cycle.target_idea_id) if cycle.target_idea_id else None,
            **_thread_link_fields(idea, cycle.target_idea_id),
            "idea_title": _idea_title(idea),
            "next_run_at": _serialize_dt(cycle.next_run_at),
            "last_run_at": _serialize_dt(cycle.last_run_at),
            "last_status": cycle.last_status,
            "last_error": _snippet(cycle.last_error),
            "deleted_at": _serialize_dt(cycle.deleted_at),
            "provenance": {"table": "cycles", "id": int(cycle.id)},
        }
        for cycle, user, idea in rows
    ]


async def _query_cycle_runs(
    session: Any,
    payload: dict[str, Any],
    *,
    start: datetime | None,
    end: datetime | None,
    org_id: str | None,
    user_id: str | None,
    person_ids: list[str],
    idea_id: str | None,
    cycle_id: int | None,
    search: str | None,
    include_archived: bool,
    limit: int,
    offset: int = 0,
) -> None:
    from brain.platform.db.models.cycle import Cycle, CycleRun
    from brain.platform.db.models.idea import Idea
    from brain.platform.db.models.org import User

    stmt = (
        select(CycleRun, Cycle, User, Idea)
        .join(Cycle, Cycle.id == CycleRun.cycle_id)
        .outerjoin(User, User.id == Cycle.user_id)
        .outerjoin(Idea, Idea.id == CycleRun.idea_id)
        .order_by(CycleRun.created_at.desc(), CycleRun.id.desc())
        .offset(offset)
        .limit(limit + 1)
    )
    stmt = _scope_cycle(stmt, Cycle, User, org_id=org_id, user_id=user_id)
    stmt = _apply_date_bounds(stmt, CycleRun.created_at, start, end)
    if idea_id:
        stmt = stmt.where(CycleRun.idea_id == idea_id)
    if cycle_id is not None:
        stmt = stmt.where(CycleRun.cycle_id == cycle_id)
    if person_ids:
        stmt = stmt.where(Cycle.user_id.in_(person_ids))
    if not include_archived:
        stmt = stmt.where(Cycle.deleted_at.is_(None))
    text_match = _text_filter(
        search,
        Cycle.name,
        CycleRun.status,
        CycleRun.error,
        CycleRun.skip_reason,
        CycleRun.prompt_snapshot,
        Idea.title,
        Idea.display_title,
    )
    if text_match is not None:
        stmt = stmt.where(text_match)

    rows = _page_rows(
        payload,
        "cycle_runs",
        await _session_execute_all(session, stmt),
        limit=limit,
    )
    payload["sources"]["cycle_runs"] = [
        {
            "id": int(run.id),
            "type": "cycle_run",
            "created_at": _serialize_dt(run.created_at),
            "scheduled_for": _serialize_dt(run.scheduled_for),
            "started_at": _serialize_dt(run.started_at),
            "completed_at": _serialize_dt(run.completed_at),
            "cycle_id": int(run.cycle_id),
            "revision_id": int(run.revision_id) if getattr(run, "revision_id", None) is not None else None,
            "cycle_name": cycle.name,
            "status": run.status,
            "error": _snippet(run.error),
            "skip_reason": run.skip_reason,
            "idea_id": str(run.idea_id) if run.idea_id else None,
            **_thread_link_fields(idea, run.idea_id),
            "idea_title": _idea_title(idea),
            "run_id": int(run.run_id) if run.run_id is not None else None,
            "prompt_snapshot": _snippet(run.prompt_snapshot, 520),
            "guidance_snapshot": _jsonable(getattr(run, "guidance_snapshot", []) or []),
            "output_targets_snapshot": _jsonable(getattr(run, "output_targets_snapshot", []) or []),
            "context_snapshot": _jsonable(getattr(run, "context_snapshot", {}) or {}),
            "self_review_summary": _snippet(getattr(run, "self_review_summary", None), 520),
            "user_id": str(cycle.user_id) if cycle.user_id else None,
            "user_name": user.name if user else None,
            "provenance": {"table": "cycle_runs", "id": int(run.id)},
        }
        for run, cycle, user, idea in rows
    ]


async def _query_last_completed_cycle_run(
    *,
    cycle_id: int,
    org_id: str | None,
    user_id: str | None,
    include_deleted: bool,
) -> dict[str, Any]:
    """Read one cycle watermark without scanning the broad run-history surface."""
    from brain.platform.db.models.cycle import Cycle, CycleRun
    from brain.platform.db.models.org import User
    from brain.platform.db.repositories.unit_of_work import UnitOfWork

    if not org_id and not user_id:
        return {
            "view": "cycle_last_completed_run",
            "cycle_id": cycle_id,
            "last_completed_run": None,
            "error": "read_cycles could not access this workspace or user context",
            "evidence_health": {"status": "degraded", "completeness": "unavailable"},
        }

    async with UnitOfWork() as uow:
        stmt = (
            select(CycleRun, Cycle)
            .join(Cycle, Cycle.id == CycleRun.cycle_id)
            .where(
                CycleRun.cycle_id == cycle_id,
                CycleRun.completed_at.is_not(None),
            )
            .order_by(CycleRun.completed_at.desc(), CycleRun.id.desc())
            .limit(1)
        )
        stmt = _scope_cycle(stmt, Cycle, User, org_id=org_id, user_id=user_id)
        if not include_deleted:
            stmt = stmt.where(Cycle.deleted_at.is_(None))
        rows = await _session_execute_all(uow.session, stmt)

    if not rows:
        watermark = None
    else:
        run, cycle = rows[0]
        watermark = {
            "cycle_id": int(cycle.id),
            "cycle_name": cycle.name,
            "cycle_run_id": int(run.id),
            "completed_at": _serialize_dt(run.completed_at),
            "status": run.status,
            "run_id": int(run.run_id) if run.run_id is not None else None,
        }
    return {
        "view": "cycle_last_completed_run",
        "cycle_id": cycle_id,
        "last_completed_run": watermark,
        "evidence_health": {"status": "ok", "completeness": "complete"},
    }


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
    for key in (
        "idea_title",
        "title",
        "name",
        "project_name",
        "cycle_name",
        "app_name",
        "domain_name",
        "key",
        "slug",
    ):
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
    if source == "project_profiles":
        resources = record.get("resources") if isinstance(record.get("resources"), Mapping) else {}
        return _snippet(
            record.get("description") or f"{resources.get('count', 0)} project resources",
            280,
        )
    if source == "project_attachments":
        resources = record.get("resources") if isinstance(record.get("resources"), Mapping) else {}
        return _snippet(
            f"Attached {resources.get('count', 0)} project resources; status {record.get('status')}",
            280,
        )
    if source == "cycles":
        return _snippet(
            f"{record.get('schedule_expr')} {record.get('timezone')} next={record.get('next_run_at')} last={record.get('last_status')}",
            280,
        )
    if source == "cycle_runs":
        return _snippet(record.get("error") or record.get("prompt_snapshot"), 280)
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
        "attached_by",
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
                "thread_id": record.get("thread_id") or record.get("idea_id"),
                "thread_route": record.get("thread_route"),
                "thread_url": record.get("thread_url") or record.get("url"),
                "url": record.get("url") or record.get("thread_url"),
                "thread_reference": record.get("thread_reference"),
                "provenance": record.get("provenance"),
            })
    items.sort(key=_activity_sort_key, reverse=True)
    return items[: max(1, int(limit or 30))]


async def _run_team_members(session: Any, payload: dict[str, Any], ctx: WorkspaceDataQueryContext) -> None:
    await _query_team_members(
        session,
        payload,
        start=ctx.start,
        end=ctx.end,
        org_id=ctx.org_id,
        user_id=ctx.user_id,
        person_ids=ctx.person_ids,
        search=ctx.search,
        limit=ctx.limit,
    )


async def _run_runs(session: Any, payload: dict[str, Any], ctx: WorkspaceDataQueryContext) -> None:
    await _query_runs(
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


async def _run_threads(session: Any, payload: dict[str, Any], ctx: WorkspaceDataQueryContext) -> None:
    await _query_threads(
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


async def _run_ideas(session: Any, payload: dict[str, Any], ctx: WorkspaceDataQueryContext) -> None:
    await _query_ideas(
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


async def _run_tool_calls(session: Any, payload: dict[str, Any], ctx: WorkspaceDataQueryContext) -> None:
    await _query_tool_calls(
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


async def _run_project_profiles(session: Any, payload: dict[str, Any], ctx: WorkspaceDataQueryContext) -> None:
    await _query_project_profiles(
        session,
        payload,
        start=ctx.start,
        end=ctx.end,
        org_id=ctx.org_id,
        user_id=ctx.user_id,
        person_ids=ctx.person_ids,
        search=ctx.search,
        include_archived=ctx.include_archived,
        limit=ctx.limit,
    )


async def _run_project_attachments(session: Any, payload: dict[str, Any], ctx: WorkspaceDataQueryContext) -> None:
    await _query_project_attachments(
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


async def _run_domains(session: Any, payload: dict[str, Any], ctx: WorkspaceDataQueryContext) -> None:
    await _query_domains(
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
        offset=ctx.offset,
    )


async def _run_domain_records(session: Any, payload: dict[str, Any], ctx: WorkspaceDataQueryContext) -> None:
    await _query_domain_records(
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
        offset=ctx.offset,
    )


async def _run_domain_events(session: Any, payload: dict[str, Any], ctx: WorkspaceDataQueryContext) -> None:
    await _query_domain_events(
        session,
        payload,
        start=ctx.start,
        end=ctx.end,
        org_id=ctx.org_id,
        person_ids=ctx.person_ids,
        domain_id=ctx.domain_id,
        search=ctx.search,
        limit=ctx.limit,
        offset=ctx.offset,
    )


async def _run_workspace_apps(session: Any, payload: dict[str, Any], ctx: WorkspaceDataQueryContext) -> None:
    await _query_workspace_apps(
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


async def _run_app_state(session: Any, payload: dict[str, Any], ctx: WorkspaceDataQueryContext) -> None:
    await _query_app_state(
        session,
        payload,
        start=ctx.start,
        end=ctx.end,
        org_id=ctx.org_id,
        person_ids=ctx.person_ids,
        search=ctx.search,
        limit=ctx.limit,
    )


async def _run_cycles(session: Any, payload: dict[str, Any], ctx: WorkspaceDataQueryContext) -> None:
    await _query_cycles(
        session,
        payload,
        start=ctx.start,
        end=ctx.end,
        org_id=ctx.org_id,
        user_id=ctx.user_id,
        person_ids=ctx.person_ids,
        cycle_id=ctx.cycle_id,
        search=ctx.search,
        include_archived=ctx.include_archived,
        limit=ctx.limit,
        offset=ctx.offset,
    )


async def _run_cycle_runs(session: Any, payload: dict[str, Any], ctx: WorkspaceDataQueryContext) -> None:
    await _query_cycle_runs(
        session,
        payload,
        start=ctx.start,
        end=ctx.end,
        org_id=ctx.org_id,
        user_id=ctx.user_id,
        person_ids=ctx.person_ids,
        idea_id=ctx.idea_id,
        cycle_id=ctx.cycle_id,
        search=ctx.search,
        include_archived=ctx.include_archived,
        limit=ctx.limit,
        offset=ctx.offset,
    )


_SOURCE_ADAPTERS: dict[str, WorkspaceDataSource] = {
    "team_members": WorkspaceDataSource(
        name="team_members",
        description="Workspace roster: users, roles, emails, approval state, and attribution settings.",
        groups=("all", "team", "people"),
        handler=_run_team_members,
    ),
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
    "project_profiles": WorkspaceDataSource(
        name="project_profiles",
        description="Reusable Project Context profiles with resources such as repos, files, folders, docs, and metadata.",
        groups=("all", "projects", "project_contexts"),
        handler=_run_project_profiles,
    ),
    "project_attachments": WorkspaceDataSource(
        name="project_attachments",
        description="Project Context snapshots attached to Cortex thoughts, including validation and resource summaries.",
        groups=("all", "activity", "projects", "project_contexts"),
        handler=_run_project_attachments,
    ),
    "domains": WorkspaceDataSource(
        name="domains",
        description="Domain schema records for user-created structured workspace databases.",
        groups=("all", "domain", "records"),
        handler=_run_domains,
    ),
    "domain_records": WorkspaceDataSource(
        name="domain_records",
        description="Domain object records and structured data.",
        groups=("all", "domain", "records"),
        handler=_run_domain_records,
    ),
    "domain_events": WorkspaceDataSource(
        name="domain_events",
        description="Domain audit/event stream records.",
        groups=("all", "activity", "domain", "records"),
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
    "cycles": WorkspaceDataSource(
        name="cycles",
        description="Workspace Cycles: recurring Illo prompts, schedules, enabled state, and last/next run status.",
        groups=("all", "cycles"),
        handler=_run_cycles,
    ),
    "cycle_runs": WorkspaceDataSource(
        name="cycle_runs",
        description="Individual Cycle run history with status, linked thought, run id, and prompt snapshot.",
        groups=("all", "activity", "cycles"),
        handler=_run_cycle_runs,
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


async def query_workspace_data(
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
    cycle_id: int | None = None,
    object_key: str | None = None,
    include_archived: bool = False,
    cursor: str | None = None,
    user_id: str | None = None,
    org_id: str | None = None,
    run_id: int | None = None,
) -> dict[str, Any]:
    """Query typed workspace data with source-level failure isolation."""
    from brain.platform.db.repositories.unit_of_work import UnitOfWork

    org_id = _optional_text(org_id)
    user_id = _optional_text(user_id)
    idea_id = _thread_id_from_first_reference(idea_id)
    object_key = _optional_text(object_key)
    source_names = _normalize_sources(sources)
    start, end, resolved_window = _time_bounds(time_window, start_at=start_at, end_at=end_at)
    per_source_limit = min(max(int(limit or 20), 1), 100)
    page_kind = _workspace_page_kind(source_names)
    offset = page_offset(cursor, kind=page_kind)
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
            "cycle_id": cycle_id,
            "object_key": object_key,
        },
        "people": [],
        "activity_items": [],
        "sources": {},
        "counts": {},
        "warnings": [],
    }

    async with UnitOfWork() as uow:
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

                current_user = await _session_get(session, User, user_id)
                if current_user and current_user.org_id:
                    org_id = str(current_user.org_id)
                    payload["scope"]["org_id"] = org_id
            except Exception as exc:
                await _rollback_after_source_error(session, "scope")
                _add_error(payload, "scope", exc)
        if not org_id and not user_id:
            payload["warnings"].append({
                "source": "scope",
                "error": "query_workspace_data could not access this workspace or user context",
            })
            adapters = _source_adapters()
            source_names = [source for source in source_names if not adapters[source].db_backed]
            payload["checked_sources"] = source_names
        try:
            people = await _resolve_people(session, org_id=org_id, person=person)
        except Exception as exc:
            await _rollback_after_source_error(session, "people")
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
            cycle_id=cycle_id,
            object_key=object_key,
            query=query,
            search=search,
            include_archived=include_archived,
            limit=per_source_limit,
            offset=offset,
        )

        for source in source_names:
            try:
                await _maybe_await(adapters[source].handler(session, payload, ctx))
            except Exception as exc:
                logger.exception("query_workspace_data source failed source=%s", source)
                await _rollback_after_source_error(session, source)
                payload["sources"][source] = []
                _add_error(payload, source, exc)

    more_sources = list(dict.fromkeys(payload.pop("_more_sources", [])))
    has_more = bool(more_sources)
    payload["counts"] = {
        source: len(rows) if isinstance(rows, list) else 0
        for source, rows in payload["sources"].items()
    }
    payload["total_count"] = sum(payload["counts"].values())
    payload["activity_items"] = _build_activity_items(payload, limit=per_source_limit)
    payload["pagination"] = {
        "offset": offset,
        "limit": per_source_limit,
        "has_more": has_more,
        "more_sources": more_sources,
    }
    payload["truncated"] = has_more
    payload["next_page"] = (
        next_offset_token(kind=page_kind, offset=offset, returned=per_source_limit)
        if has_more
        else None
    )
    payload["evidence_health"] = {
        "status": "degraded" if payload["warnings"] else "ok",
        "completeness": "more_available" if has_more else "complete",
    }
    return payload


def _workspace_query_scope(
    *,
    idea_id: str | None = None,
    thread_url: str | None = None,
    domain_id: int | None = None,
    cycle_id: int | None = None,
    object_key: str | None = None,
    include_archived: bool = False,
    default_current_idea: bool = False,
) -> dict[str, Any]:
    run = getattr(_agent_context, "run", None)
    execution_metadata = getattr(_agent_context, "execution_metadata", {}) or {}
    explicit_thread_scope = thread_url is not None or idea_id is not None
    if not explicit_thread_scope and default_current_idea:
        scoped_idea_id = _thread_id_from_first_reference(
            getattr(_agent_context, "idea_id", None) or execution_metadata.get("idea_id")
        )
    else:
        scoped_idea_id = _thread_id_from_first_reference(thread_url, idea_id)
    return {
        "idea_id": scoped_idea_id,
        "domain_id": domain_id,
        "cycle_id": cycle_id,
        "object_key": _optional_text(object_key),
        "include_archived": include_archived,
        "user_id": _optional_text(getattr(_agent_context, "user_id", None) or execution_metadata.get("user_id")),
        "org_id": _optional_text(getattr(_agent_context, "org_id", None) or execution_metadata.get("org_id")),
        "run_id": getattr(run, "run_id", None) or execution_metadata.get("run_id"),
    }


async def _query_workspace_data_for_agent(**kwargs: Any) -> dict[str, Any]:
    scope_kwargs = _workspace_query_scope(
        idea_id=kwargs.pop("idea_id", None),
        thread_url=kwargs.pop("thread_url", None),
        domain_id=kwargs.pop("domain_id", None),
        cycle_id=kwargs.pop("cycle_id", None),
        object_key=kwargs.pop("object_key", None),
        include_archived=bool(kwargs.pop("include_archived", False)),
        default_current_idea=bool(kwargs.pop("_default_current_idea", False)),
    )
    return await query_workspace_data(**kwargs, **scope_kwargs)


def _workspace_view_payload(view: str, payload: dict[str, Any]) -> dict[str, Any]:
    payload["view"] = view
    payload.setdefault("answering_guidance", [])
    return payload


def _build_workspace_overview(payload: Mapping[str, Any]) -> dict[str, Any]:
    sources = payload.get("sources") if isinstance(payload.get("sources"), Mapping) else {}
    counts = payload.get("counts") if isinstance(payload.get("counts"), Mapping) else {}
    gaps: list[str] = []
    if int(counts.get("project_profiles") or 0) == 0 and int(counts.get("project_attachments") or 0) == 0:
        gaps.append("No reusable Project Context profiles or thread attachments were found.")
    if int(counts.get("domains") or 0) == 0:
        gaps.append("No user-created Domains were found for structured workspace records.")
    if int(counts.get("workspace_apps") or 0) == 0:
        gaps.append("No generated workspace apps or dashboards were found.")
    if int(counts.get("cycles") or 0) == 0:
        gaps.append("No recurring Cycles were found.")

    return {
        "team_members": (sources.get("team_members") or [])[:10],
        "active_or_recent_thoughts": (sources.get("ideas") or [])[:10],
        "recent_activity": (payload.get("activity_items") or [])[:10],
        "project_contexts": {
            "profiles": (sources.get("project_profiles") or [])[:10],
            "attachments": (sources.get("project_attachments") or [])[:10],
        },
        "structured_records": {
            "domains": (sources.get("domains") or [])[:10],
            "recent_records": (sources.get("domain_records") or [])[:10],
        },
        "workspace_apps": (sources.get("workspace_apps") or [])[:10],
        "cycles": (sources.get("cycles") or [])[:10],
        "setup_gaps": gaps,
    }


def _add_team_member_activity_summary(payload: dict[str, Any]) -> None:
    sources = payload.get("sources") if isinstance(payload.get("sources"), Mapping) else {}
    members = sources.get("team_members") if isinstance(sources.get("team_members"), list) else []
    activity = payload.get("activity_items") if isinstance(payload.get("activity_items"), list) else []
    by_user: dict[str, dict[str, Any]] = {}
    for item in activity:
        if not isinstance(item, Mapping):
            continue
        user_id = str(item.get("user_id") or "")
        if not user_id:
            continue
        summary = by_user.setdefault(user_id, {"count": 0, "latest": []})
        summary["count"] += 1
        if len(summary["latest"]) < 5:
            summary["latest"].append(item)
    payload["member_activity"] = [
        {
            "user_id": member.get("user_id") or member.get("id"),
            "name": member.get("name"),
            "email": member.get("email"),
            **by_user.get(str(member.get("user_id") or member.get("id") or ""), {"count": 0, "latest": []}),
        }
        for member in members
        if isinstance(member, Mapping)
    ]


async def _handle_query_workspace_data(
    sources: list[str] | None = None,
    query: str | None = None,
    search: str | None = None,
    person: str | None = None,
    time_window: str = "last_7d",
    start_at: str | None = None,
    end_at: str | None = None,
    limit: int = 20,
    idea_id: str | None = None,
    thread_url: str | None = None,
    domain_id: int | None = None,
    cycle_id: int | None = None,
    object_key: str | None = None,
    include_archived: bool = False,
    cursor: str | None = None,
) -> str:
    payload = await _query_workspace_data_for_agent(
        sources=sources,
        query=query,
        search=search,
        person=person,
        time_window=time_window,
        start_at=start_at,
        end_at=end_at,
        limit=limit,
        idea_id=idea_id,
        thread_url=thread_url,
        domain_id=domain_id,
        cycle_id=cycle_id,
        object_key=object_key,
        include_archived=include_archived,
        cursor=cursor,
        _default_current_idea=True,
    )
    return json.dumps(payload, default=str)


async def _handle_read_workspace_overview(
    query: str | None = None,
    time_window: str = "all",
    limit: int = 10,
    include_archived: bool = False,
) -> str:
    payload = await _query_workspace_data_for_agent(
        sources=[
            "team_members",
            "ideas",
            "threads",
            "runs",
            "project_profiles",
            "project_attachments",
            "domains",
            "domain_records",
            "workspace_apps",
            "cycles",
        ],
        query=query or "workspace overview",
        time_window=time_window,
        limit=limit,
        include_archived=include_archived,
    )
    payload = _workspace_view_payload("workspace_overview", payload)
    payload["overview"] = _build_workspace_overview(payload)
    payload["answering_guidance"] = [
        "Distinguish what already exists in this workspace from what Illo can help set up.",
        "Use setup_gaps to avoid overclaiming configured project context, Domains, apps, or Cycles.",
        WORKSPACE_OVERVIEW_SPARSE_GUIDANCE,
    ]
    return json.dumps(payload, default=str)


async def _handle_read_team_activity(
    query: str | None = None,
    search: str | None = None,
    person: str | None = None,
    time_window: str = "last_7d",
    start_at: str | None = None,
    end_at: str | None = None,
    limit: int = 20,
    idea_id: str | None = None,
    thread_url: str | None = None,
    cursor: str | None = None,
) -> str:
    payload = await _query_workspace_data_for_agent(
        sources=["activity"],
        query=query or "team activity",
        search=search,
        person=person,
        time_window=time_window,
        start_at=start_at,
        end_at=end_at,
        limit=limit,
        idea_id=idea_id,
        thread_url=thread_url,
        cursor=cursor,
    )
    payload = _workspace_view_payload("team_activity", payload)
    payload["answering_guidance"] = [
        "Base recaps on activity_items first, then use per-source rows for detail.",
        "When next_page is present, follow it before treating the activity window as complete.",
        "When pointing someone to existing work in a Cortex Thread, include the returned thread_url with the Thread title.",
        "When results are empty, say what was checked instead of guessing from memory.",
    ]
    return json.dumps(payload, default=str)


async def _handle_read_project_contexts(
    query: str | None = None,
    search: str | None = None,
    idea_id: str | None = None,
    time_window: str = "all",
    start_at: str | None = None,
    end_at: str | None = None,
    limit: int = 20,
    include_inactive: bool = False,
) -> str:
    payload = await _query_workspace_data_for_agent(
        sources=["project_contexts"],
        query=query or "project contexts",
        search=search,
        time_window=time_window,
        start_at=start_at,
        end_at=end_at,
        limit=limit,
        idea_id=idea_id,
        include_archived=include_inactive,
    )
    payload = _workspace_view_payload("project_contexts", payload)
    payload["answering_guidance"] = [
        "Use profiles for reusable workspace-level context and attachments for context bound to a Cortex thought.",
        "If no resources are present, explain that Illo can help create or attach Project Context.",
    ]
    return json.dumps(payload, default=str)


async def _handle_read_team_members(
    query: str | None = None,
    search: str | None = None,
    person: str | None = None,
    time_window: str = "all",
    limit: int = 20,
    include_activity: bool = True,
) -> str:
    sources = ["team_members", "runs", "threads", "ideas"] if include_activity else ["team_members"]
    payload = await _query_workspace_data_for_agent(
        sources=sources,
        query=query or "team members",
        search=search,
        person=person,
        time_window=time_window,
        limit=limit,
    )
    payload = _workspace_view_payload("team_members", payload)
    if include_activity:
        _add_team_member_activity_summary(payload)
    payload["answering_guidance"] = [
        "Use roster rows for identity/role facts and member_activity for recent work signals.",
        "Avoid inferring availability, intent, or ownership beyond the records returned.",
    ]
    return json.dumps(payload, default=str)


async def _handle_read_workspace_records(
    query: str | None = None,
    search: str | None = None,
    domain_id: int | None = None,
    object_key: str | None = None,
    time_window: str = "all",
    start_at: str | None = None,
    end_at: str | None = None,
    limit: int = 20,
    include_archived: bool = False,
    cursor: str | None = None,
) -> str:
    payload = await _query_workspace_data_for_agent(
        sources=["records"],
        query=query or "workspace records",
        search=search,
        domain_id=domain_id,
        object_key=object_key,
        time_window=time_window,
        start_at=start_at,
        end_at=end_at,
        limit=limit,
        include_archived=include_archived,
        cursor=cursor,
    )
    payload = _workspace_view_payload("workspace_records", payload)
    payload["answering_guidance"] = [
        "Domains are user-created structured databases, not the system's raw database tables.",
        "When next_page is present, follow it before treating records or Domain events as complete.",
        "Use Domain schemas to explain what each record type is for before summarizing records.",
    ]
    return json.dumps(payload, default=str)


async def _handle_read_cycles(
    query: str | None = None,
    search: str | None = None,
    person: str | None = None,
    time_window: str = "all",
    start_at: str | None = None,
    end_at: str | None = None,
    limit: int = 20,
    include_deleted: bool = False,
    cycle_id: int | None = None,
    last_completed_run: bool = False,
    cursor: str | None = None,
) -> str:
    if last_completed_run:
        if cycle_id is None or cycle_id < 1:
            return json.dumps({"error": "last_completed_run requires a positive cycle_id"})
        scope = _workspace_query_scope(
            cycle_id=cycle_id,
            include_archived=include_deleted,
        )
        payload = await _query_last_completed_cycle_run(
            cycle_id=cycle_id,
            org_id=scope["org_id"],
            user_id=scope["user_id"],
            include_deleted=include_deleted,
        )
        return json.dumps(payload, default=str)
    payload = await _query_workspace_data_for_agent(
        sources=["cycles"],
        query=query or "workspace cycles",
        search=search,
        person=person,
        time_window=time_window,
        start_at=start_at,
        end_at=end_at,
        limit=limit,
        include_archived=include_deleted,
        cycle_id=cycle_id,
        cursor=cursor,
    )
    payload = _workspace_view_payload("cycles", payload)
    payload["answering_guidance"] = [
        "Use cycles for recurring configuration and cycle_runs for actual execution history.",
        "When next_page is present, follow it before treating Cycle run history as complete.",
        "Distinguish enabled/disabled/deleted Cycles when summarizing scheduled work.",
    ]
    return json.dumps(payload, default=str)


async def _handle_read_workspace_apps(
    query: str | None = None,
    search: str | None = None,
    time_window: str = "all",
    start_at: str | None = None,
    end_at: str | None = None,
    limit: int = 20,
    include_archived: bool = False,
    confirm_include_archived: bool = False,
    include_state: bool = True,
) -> str:
    if include_archived and not confirm_include_archived:
        return json.dumps(
            {
                "error": (
                    "include_archived=true requires confirm_include_archived=true. "
                    "Archived apps are only for explicit archived-app inspection; "
                    "new app builds should search active apps or create a fresh app."
                )
            },
            default=str,
        )
    payload = await _query_workspace_data_for_agent(
        sources=["apps"] if include_state else ["workspace_apps"],
        query=query or "workspace apps",
        search=search,
        time_window=time_window,
        start_at=start_at,
        end_at=end_at,
        limit=limit,
        include_archived=include_archived,
    )
    payload = _workspace_view_payload("workspace_apps", payload)
    payload["answering_guidance"] = [
        "Use workspace_apps for app identity/metadata and app_state only for app-local UI state.",
        "Recordful apps should be backed by Domains; app-local state is not the workspace database.",
    ]
    return json.dumps(payload, default=str)
