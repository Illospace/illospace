"""Event bus for cortex run.

Compatibility shim for websocket fanout plus durable run-event storage.
"""
from __future__ import annotations

import json
import logging
import asyncio
from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Callable

from sqlalchemy import false, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from brain.platform.db.models.run import CortexEvent
from brain.platform.db.models.idea import Idea
from brain.platform.db.repositories.unit_of_work import UnitOfWork

logger = logging.getLogger(__name__)

# Pluggable publisher — set by the web layer on startup.
_publisher: Callable[[str, dict[str, Any]], None] | None = None
_idea_org_cache: dict[str, str | None] = {}
_run_org_cache: dict[int, str | None] = {}

_run_event_scope: ContextVar[dict[str, Any] | None] = ContextVar(
    "run_event_scope",
    default=None,
)

_LIVE_AFTER_DURABLE_EVENT_TYPES = {
    "browser_session_state",
    "browser_session_frame",
    "browser_session_delta",
    "browser_session_error",
    "browser_session_closed",
    "vault_secret_prompt",
    "vault_agent_grant_prompt",
}


def set_publisher(fn: Callable[[str, dict[str, Any]], None]) -> None:
    """Register the websocket publisher (called once by the web layer)."""
    global _publisher
    _publisher = fn


@contextmanager
def run_event_scope(
    run_id: int | None,
    *,
    idea_id: str | None = None,
    producer: str = "cortex",
    consumer_runtime: str | None = None,
    session: Any | None = None,
):
    """Scope publish() calls so they can also be durably recorded."""
    token = _run_event_scope.set(
        {
            "run_id": run_id,
            "idea_id": idea_id,
            "producer": producer,
            "consumer_runtime": consumer_runtime,
            "session": session,
        }
    )
    try:
        yield
    finally:
        _run_event_scope.reset(token)


def _normalize_payload(payload: dict[str, Any] | None) -> dict[str, Any]:
    if not payload:
        return {}
    return json.loads(json.dumps(payload, default=str))


def _principal_can_replay_all(principal: Mapping[str, Any]) -> bool:
    permissions = set(principal.get("permissions") or [])
    return principal.get("principal_type") == "service" and (
        "internal:api" in permissions or "run:manage" in permissions
    )


def _event_created_at(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _infer_idea_id(payload: Mapping[str, Any]) -> str | None:
    idea_id = payload.get("idea_id")
    if idea_id is None and isinstance(payload.get("idea"), Mapping):
        idea_id = payload["idea"].get("id")
    if idea_id is None:
        idea_id = payload.get("parent_id")
    if idea_id is None:
        return None
    return str(idea_id)


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text_value = str(value).strip()
    return text_value or None


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _infer_run_id(payload: Mapping[str, Any]) -> int | None:
    for key in ("run_id", "owner_run_id", "root_run_id"):
        run_id = _optional_int(payload.get(key))
        if run_id is not None:
            return run_id
    state = payload.get("state")
    if isinstance(state, Mapping):
        for key in ("run_id", "owner_run_id", "root_run_id"):
            run_id = _optional_int(state.get(key))
            if run_id is not None:
                return run_id
    return None


def _lookup_idea_org_id(idea_id: str, *, session: Any | None = None) -> str | None:
    cache_key = str(idea_id)
    if cache_key in _idea_org_cache:
        return _idea_org_cache[cache_key]
    try:
        if session is not None:
            idea = session.get(Idea, cache_key)
            org_id = _optional_text(getattr(idea, "org_id", None)) if idea is not None else None
        else:
            org_id = None
    except Exception as exc:
        logger.debug("event_org_lookup_by_idea_failed idea_id=%s error=%s", cache_key, exc)
        return None
    if org_id:
        _idea_org_cache[cache_key] = org_id
    return org_id


async def _lookup_idea_org_id_async(
    idea_id: str,
    *,
    session: AsyncSession | None = None,
) -> str | None:
    cache_key = str(idea_id)
    if cache_key in _idea_org_cache:
        return _idea_org_cache[cache_key]
    try:
        if session is not None:
            idea = await session.get(Idea, cache_key)
            org_id = _optional_text(getattr(idea, "org_id", None)) if idea is not None else None
        else:
            async with UnitOfWork() as uow:
                idea = await uow.session.get(Idea, cache_key)
                org_id = _optional_text(getattr(idea, "org_id", None)) if idea is not None else None
    except Exception as exc:
        logger.debug("event_org_lookup_by_idea_failed idea_id=%s error=%s", cache_key, exc)
        return None
    if org_id:
        _idea_org_cache[cache_key] = org_id
    return org_id


def _lookup_run_org_id(run_id: int, *, session: Any | None = None) -> str | None:
    cache_key = int(run_id)
    if cache_key in _run_org_cache:
        return _run_org_cache[cache_key]
    try:
        from brain.platform.db.models.agent_run import AgentRunRow

        if session is not None:
            run = session.get(AgentRunRow, cache_key)
            org_id = _optional_text(getattr(run, "org_id", None)) if run is not None else None
        else:
            org_id = None
    except Exception as exc:
        logger.debug("event_org_lookup_by_run_failed run_id=%s error=%s", cache_key, exc)
        return None
    if org_id:
        _run_org_cache[cache_key] = org_id
    return org_id


async def _lookup_run_org_id_async(
    run_id: int,
    *,
    session: AsyncSession | None = None,
) -> str | None:
    cache_key = int(run_id)
    if cache_key in _run_org_cache:
        return _run_org_cache[cache_key]
    try:
        from brain.platform.db.models.agent_run import AgentRunRow

        if session is not None:
            run = await session.get(AgentRunRow, cache_key)
            org_id = _optional_text(getattr(run, "org_id", None)) if run is not None else None
        else:
            async with UnitOfWork() as uow:
                run = await uow.session.get(AgentRunRow, cache_key)
                org_id = _optional_text(getattr(run, "org_id", None)) if run is not None else None
    except Exception as exc:
        logger.debug("event_org_lookup_by_run_failed run_id=%s error=%s", cache_key, exc)
        return None
    if org_id:
        _run_org_cache[cache_key] = org_id
    return org_id


def resolve_event_org_id(
    payload: Mapping[str, Any],
    *,
    session: Any | None = None,
) -> str | None:
    """Resolve the org scope for a live Cortex event payload."""
    org_id = _optional_text(payload.get("org_id"))
    if org_id:
        return org_id
    idea = payload.get("idea")
    if isinstance(idea, Mapping):
        org_id = _optional_text(idea.get("org_id"))
        if org_id:
            return org_id
    idea_id = _infer_idea_id(payload)
    if idea_id:
        org_id = _lookup_idea_org_id(idea_id, session=session)
        if org_id:
            return org_id
    run_id = _infer_run_id(payload)
    if run_id is not None:
        return _lookup_run_org_id(run_id, session=session)
    return None


async def resolve_event_org_id_async(
    payload: Mapping[str, Any],
    *,
    session: AsyncSession | None = None,
) -> str | None:
    """Resolve the org scope for a live Cortex event payload without sync DB access."""
    org_id = _optional_text(payload.get("org_id"))
    if org_id:
        return org_id
    idea = payload.get("idea")
    if isinstance(idea, Mapping):
        org_id = _optional_text(idea.get("org_id"))
        if org_id:
            return org_id
    idea_id = _infer_idea_id(payload)
    if idea_id:
        org_id = await _lookup_idea_org_id_async(idea_id, session=session)
        if org_id:
            return org_id
    run_id = _infer_run_id(payload)
    if run_id is not None:
        return await _lookup_run_org_id_async(run_id, session=session)
    return None


def _new_cortex_event(
    event_type: str,
    payload: dict[str, Any] | None = None,
) -> CortexEvent:
    normalized_payload = _normalize_payload(payload)
    return CortexEvent(
        event_type=str(event_type),
        idea_id=_infer_idea_id(normalized_payload),
        target_id=_optional_text(normalized_payload.get("target_id")),
        session_id=_optional_text(normalized_payload.get("session_id")),
        duration_ms=_optional_int(normalized_payload.get("duration_ms")),
        metadata_=normalized_payload,
    )


async def _write_cortex_event_async(
    session: AsyncSession,
    *,
    event_type: str,
    payload: dict[str, Any] | None = None,
) -> CortexEvent:
    event = _new_cortex_event(event_type, payload)
    session.add(event)
    await session.flush()
    return event


async def record_cortex_event_async(
    event_type: str,
    payload: dict[str, Any] | None = None,
    *,
    session: AsyncSession | None = None,
) -> CortexEvent:
    """Persist a generic Cortex websocket event using the async DB runtime."""
    if session is not None:
        return await _write_cortex_event_async(session, event_type=event_type, payload=payload)

    async with UnitOfWork() as uow:
        return await _write_cortex_event_async(uow.session, event_type=event_type, payload=payload)


def _active_async_loop() -> asyncio.AbstractEventLoop | None:
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


def _log_async_event_write_failure(
    event_type: str,
    task: asyncio.Task,
    *,
    log_name: str = "cortex_event_write_failed",
) -> None:
    try:
        task.result()
    except Exception as exc:
        logger.warning(
            "%s event_type=%s error=%s",
            log_name,
            event_type,
            exc,
        )


def cortex_event_to_message(
    event: CortexEvent,
    *,
    replayed: bool = False,
) -> dict[str, Any]:
    """Translate a durable Cortex event row into the websocket payload shape."""
    payload = _normalize_payload(event.metadata_)
    message = {
        **payload,
        "type": event.event_type,
        "event_channel": "cortex",
        "event_cursor": int(event.id),
        "cortex_event_id": int(event.id),
    }
    if event.idea_id is not None:
        message.setdefault("idea_id", str(event.idea_id))
    if event.target_id is not None:
        message.setdefault("target_id", str(event.target_id))
    if event.session_id is not None:
        message.setdefault("session_id", str(event.session_id))
    if event.duration_ms is not None:
        message.setdefault("duration_ms", int(event.duration_ms))
    created_at = _event_created_at(getattr(event, "created_at", None))
    if created_at is not None:
        message["event_created_at"] = created_at
    if replayed:
        message["replayed"] = True
    return message


def _cortex_event_replay_stmt(
    principal: Mapping[str, Any],
    *,
    last_event_id: int,
    limit: int,
):
    stmt = (
        select(CortexEvent)
        .where(CortexEvent.id > int(last_event_id))
        .order_by(CortexEvent.id.asc())
        .limit(limit)
    )
    if not _principal_can_replay_all(principal):
        org_id = str(principal.get("org_id") or "").strip()
        user_id = str(principal.get("id") or principal.get("user_id") or "").strip()
        if not org_id:
            stmt = stmt.where(false())
        else:
            stmt = stmt.join(Idea, Idea.id == CortexEvent.idea_id).where(
                Idea.org_id == org_id
            )
            target_user_id = CortexEvent.metadata_["target_user_id"].as_string()
            stmt = stmt.where(or_(target_user_id.is_(None), target_user_id == "", target_user_id == user_id))
    return stmt


async def list_cortex_events_after_for_principal_async(
    session: AsyncSession,
    principal: Mapping[str, Any],
    *,
    last_event_id: int = 0,
    limit: int = 100,
) -> list[CortexEvent]:
    """Return Cortex events visible to a replay principal after a cursor."""
    stmt = _cortex_event_replay_stmt(principal, last_event_id=last_event_id, limit=limit)
    result = await session.scalars(stmt)
    return list(result.all())


def publish(event_type: str, data: dict[str, Any]) -> None:
    """Publish an event and, when scoped, persist it durably first."""
    scope = _run_event_scope.get()
    recorded_durable = False
    if scope and scope.get("run_id") is not None:
        try:
            from brain.systems.runs.event_log import async_record_run_event

            write = async_record_run_event(
                int(scope["run_id"]),
                event_type,
                data,
                idea_id=scope.get("idea_id") or data.get("idea_id"),
                producer=scope.get("producer") or "cortex",
                consumer_runtime=scope.get("consumer_runtime"),
                session=scope.get("session"),
            )
            loop = _active_async_loop()
            if loop is not None:
                task = loop.create_task(write)
                task.add_done_callback(
                    lambda done_task, event_type=event_type: _log_async_event_write_failure(
                        event_type,
                        done_task,
                        log_name="run_event_write_failed",
                    )
                )
            else:
                asyncio.run(write)
        except Exception as exc:
            logger.warning(
                "run_event_write_failed event_type=%s error=%s",
                event_type,
                exc,
            )
        else:
            recorded_durable = True

    if recorded_durable and event_type not in _LIVE_AFTER_DURABLE_EVENT_TYPES:
        # Durable run-scoped events are replayed by the API consumer.
        # Skip the live publisher here to avoid double fanout.
        return

    if not recorded_durable and _infer_idea_id(data) is not None:
        loop = _active_async_loop()
        if loop is not None:
            task = loop.create_task(record_cortex_event_async(event_type, data))
            task.add_done_callback(
                lambda done_task, event_type=event_type: _log_async_event_write_failure(
                    event_type,
                    done_task,
                )
            )
        else:
            try:
                asyncio.run(record_cortex_event_async(event_type, data))
            except Exception as exc:
                logger.warning(
                    "cortex_event_write_failed event_type=%s error=%s",
                    event_type,
                    exc,
                )

    if _publisher:
        try:
            _publisher(event_type, data)
        except Exception as exc:
            logger.warning("Event publish failed: %s", exc)


def publish_safe(event_type: str, data: dict[str, Any]) -> None:
    """Same as publish but never raises."""
    try:
        publish(event_type, data)
    except Exception:
        pass


def publish_live(event_type: str, data: dict[str, Any]) -> None:
    """Publish a non-durable live event.

    Use this for high-frequency UI-only signals such as token deltas. Durable
    run replay stores settled activity; it should not persist every chunk.
    """
    if not _publisher:
        return
    payload = _normalize_payload(data)
    try:
        _publisher(event_type, payload)
    except Exception as exc:
        logger.warning("Live event publish failed: %s", exc)


def publish_live_safe(event_type: str, data: dict[str, Any]) -> None:
    """Same as publish_live but never raises."""
    try:
        publish_live(event_type, data)
    except Exception:
        pass
