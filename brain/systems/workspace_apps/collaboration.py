"""Collaborative event runtime for generated workspace apps."""
from __future__ import annotations

import copy
import re
from collections.abc import Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from brain.platform.db.models.workspace_app import WorkspaceApp, WorkspaceAppEvent
from brain.systems.workspace_apps.service import (
    DEFAULT_STATE_KEY,
    WorkspaceAppConflict,
    WorkspaceAppError,
    a_active_version,
    a_get_app,
    a_get_or_create_state,
    serialize_state,
)

EVENT_TYPE_RE = re.compile(r"^[a-zA-Z][\w.-]{0,119}$")


def _as_mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _collaboration_contract(manifest: Mapping[str, Any] | None) -> dict[str, Any]:
    contract = _as_mapping(_as_mapping(manifest).get("collaboration"))
    if not contract:
        raise WorkspaceAppError(
            "Workspace app collaboration requires manifest.collaboration with declared actions"
        )
    mode = str(contract.get("mode") or "event_sourced").strip()
    if mode != "event_sourced":
        raise WorkspaceAppError("manifest.collaboration.mode must be 'event_sourced'")
    return contract


def _event_action(contract: Mapping[str, Any], event_type: str) -> dict[str, Any]:
    actions = _as_mapping(contract.get("actions"))
    if not actions:
        raise WorkspaceAppError("manifest.collaboration.actions must declare allowed event types")
    raw_action = actions.get(event_type)
    if not isinstance(raw_action, Mapping):
        raise WorkspaceAppError(f"Collaborative event type is not declared: {event_type}")
    return dict(raw_action)


def _collaboration_state_key(
    manifest: Mapping[str, Any] | None,
    contract: Mapping[str, Any],
    explicit_state_key: str | None,
) -> str:
    state_key = (
        str(explicit_state_key or "").strip()
        or str(contract.get("state_key") or "").strip()
        or str(_as_mapping(manifest).get("state_key") or "").strip()
        or DEFAULT_STATE_KEY
    )
    if len(state_key) > 120:
        raise WorkspaceAppError("state_key must be at most 120 characters")
    return state_key


def _thread_id_for_app(app: WorkspaceApp) -> str | None:
    metadata = app.app_metadata or {}
    thread_artifact = _as_mapping(metadata.get("thread_artifact"))
    value = thread_artifact.get("thread_id") or metadata.get("thread_id") or metadata.get("idea_id")
    return str(value) if value else None


def _actor_key(*, actor_user_id: str | None, actor_kind: str) -> str:
    if actor_user_id:
        return f"user:{actor_user_id}"
    return actor_kind or "system"


def _path_parts(path: Any) -> list[str]:
    text = str(path or "").strip()
    if not text:
        raise WorkspaceAppError("collaboration reducer requires state_path")
    return [part for part in text.split(".") if part]


def _read_path(data: Mapping[str, Any], path: list[str], default: Any) -> Any:
    cursor: Any = data
    for part in path:
        if not isinstance(cursor, Mapping) or part not in cursor:
            return default
        cursor = cursor[part]
    return cursor


def _write_path(data: dict[str, Any], path: list[str], value: Any) -> dict[str, Any]:
    next_data = copy.deepcopy(data or {})
    cursor = next_data
    for part in path[:-1]:
        child = cursor.get(part)
        if not isinstance(child, dict):
            child = {}
            cursor[part] = child
        cursor = child
    cursor[path[-1]] = value
    return next_data


def _value_from_payload(payload: Mapping[str, Any], reducer: Mapping[str, Any]) -> Any:
    field = str(reducer.get("value_field") or "").strip()
    if field:
        return payload.get(field)
    return dict(payload)


def _reducer_object(action: Mapping[str, Any]) -> dict[str, Any]:
    reducer = action.get("reducer")
    if isinstance(reducer, str):
        state_path = (
            action.get("state_path")
            or action.get("list_key")
            or action.get("map_key")
            or action.get("state_key")
        )
        normalized = {
            "type": reducer.strip(),
            "state_path": state_path,
        }
        if action.get("value_field") is not None:
            normalized["value_field"] = action.get("value_field")
        return normalized
    return _as_mapping(reducer)


def _top_level_patch(before: Mapping[str, Any], after: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in dict(after).items()
        if dict(before).get(key) != value
    }


def _apply_state_patch(data: dict[str, Any], patch: Mapping[str, Any] | None) -> dict[str, Any]:
    if not patch:
        return dict(data or {})
    return {**(data or {}), **dict(patch)}


def _apply_reducer(
    data: dict[str, Any],
    *,
    action: Mapping[str, Any],
    payload: Mapping[str, Any],
    actor_key: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    reducer = _reducer_object(action)
    if not reducer:
        return data, {}

    reducer_type = str(reducer.get("type") or "").strip()
    path = _path_parts(reducer.get("state_path"))
    before = copy.deepcopy(data or {})

    if reducer_type == "choice_by_actor":
        choices = _read_path(before, path, {})
        if not isinstance(choices, dict):
            choices = {}
        value_field = str(reducer.get("value_field") or "optionId").strip()
        value: Any = payload.get(value_field)
        if value is None:
            value = dict(payload)
        choices[str(actor_key)] = {
            "value": value,
            "payload": dict(payload),
        }
        after = _write_path(before, path, choices)
    elif reducer_type == "append":
        items = _read_path(before, path, [])
        if not isinstance(items, list):
            items = []
        items.append(_value_from_payload(payload, reducer))
        after = _write_path(before, path, items)
    elif reducer_type == "set":
        after = _write_path(before, path, _value_from_payload(payload, reducer))
    else:
        raise WorkspaceAppError(
            "collaboration reducer.type must be one of: choice_by_actor, append, set"
        )

    return after, _top_level_patch(before, after)


def serialize_event(event: WorkspaceAppEvent) -> dict[str, Any]:
    return {
        "id": event.id,
        "org_id": event.org_id,
        "app_id": event.app_id,
        "thread_id": event.thread_id,
        "event_type": event.event_type,
        "idempotency_key": event.idempotency_key,
        "actor_kind": event.actor_kind,
        "actor_user_id": event.actor_user_id,
        "actor_display": event.actor_display or {},
        "payload": event.payload or {},
        "state_key": event.state_key,
        "state_patch": event.state_patch or {},
        "state_version": int(event.state_version or 0),
        "metadata": event.event_metadata or {},
        "created_at": event.created_at,
    }


async def a_list_collaboration_events(
    session: AsyncSession,
    *,
    org_id: str,
    app_id: str,
    after_event_id: int | None = None,
    event_type: str | None = None,
    limit: int = 50,
) -> list[WorkspaceAppEvent]:
    app = await a_get_app(session, org_id=org_id, app_id=app_id)
    safe_limit = max(1, min(int(limit or 50), 200))
    stmt = (
        select(WorkspaceAppEvent)
        .where(WorkspaceAppEvent.org_id == org_id, WorkspaceAppEvent.app_id == app.id)
        .order_by(WorkspaceAppEvent.id.asc())
        .limit(safe_limit)
    )
    if after_event_id is not None:
        stmt = stmt.where(WorkspaceAppEvent.id > int(after_event_id))
    if event_type:
        stmt = stmt.where(WorkspaceAppEvent.event_type == str(event_type).strip())
    return list((await session.scalars(stmt)).all())


async def a_get_collaboration_snapshot(
    session: AsyncSession,
    *,
    org_id: str,
    app_id: str,
    state_key: str | None = None,
    after_event_id: int | None = None,
    limit: int = 50,
    user_id: str | None = None,
) -> dict[str, Any]:
    app = await a_get_app(session, org_id=org_id, app_id=app_id)
    version = await a_active_version(session, app.id)
    manifest = version.manifest if version else {}
    contract = _collaboration_contract(manifest)
    resolved_state_key = _collaboration_state_key(manifest, contract, state_key)
    state = await a_get_or_create_state(
        session,
        org_id=org_id,
        app_id=app.id,
        key=resolved_state_key,
        user_id=user_id,
    )
    await session.refresh(state)
    events = await a_list_collaboration_events(
        session,
        org_id=org_id,
        app_id=app.id,
        after_event_id=after_event_id,
        limit=limit,
    )
    return {
        "app_id": app.id,
        "state": serialize_state(state),
        "events": [serialize_event(event) for event in events],
        "collaboration": dict(contract),
        "duplicate": False,
    }


async def a_append_collaboration_event(
    session: AsyncSession,
    *,
    org_id: str,
    app_id: str,
    event_type: str,
    payload: dict[str, Any] | None = None,
    state_patch: dict[str, Any] | None = None,
    state_key: str | None = None,
    idempotency_key: str | None = None,
    expected_state_version: int | None = None,
    metadata: dict[str, Any] | None = None,
    user_id: str | None = None,
) -> dict[str, Any]:
    normalized_event_type = str(event_type or "").strip()
    if not EVENT_TYPE_RE.match(normalized_event_type):
        raise WorkspaceAppError("event_type must be a stable identifier")

    app = await a_get_app(session, org_id=org_id, app_id=app_id)
    version = await a_active_version(session, app.id)
    manifest = version.manifest if version else {}
    contract = _collaboration_contract(manifest)
    action = _event_action(contract, normalized_event_type)
    resolved_state_key = _collaboration_state_key(manifest, contract, state_key)
    normalized_idempotency_key = str(idempotency_key or "").strip() or None

    if normalized_idempotency_key:
        existing = (
            await session.scalars(
                select(WorkspaceAppEvent).where(
                    WorkspaceAppEvent.app_id == app.id,
                    WorkspaceAppEvent.idempotency_key == normalized_idempotency_key,
                )
            )
        ).first()
        if existing is not None:
            snapshot = await a_get_collaboration_snapshot(
                session,
                org_id=org_id,
                app_id=app.id,
                state_key=existing.state_key,
                after_event_id=max(0, int(existing.id) - 1),
                limit=1,
                user_id=user_id,
            )
            snapshot["duplicate"] = True
            return snapshot

    state = await a_get_or_create_state(
        session,
        org_id=org_id,
        app_id=app.id,
        key=resolved_state_key,
        user_id=user_id,
    )
    current_version = int(state.version or 0)
    if expected_state_version is not None and int(expected_state_version) != current_version:
        raise WorkspaceAppConflict(
            f"State version conflict: expected {expected_state_version}, got {current_version}"
        )

    actor_kind = "user" if user_id else "system"
    actor_key = _actor_key(actor_user_id=user_id, actor_kind=actor_kind)
    payload_data = dict(payload or {})
    next_data = dict(state.data or {})
    applied_patch = dict(state_patch or {})
    if applied_patch:
        next_data = _apply_state_patch(next_data, applied_patch)
    else:
        next_data, applied_patch = _apply_reducer(
            next_data,
            action=action,
            payload=payload_data,
            actor_key=actor_key,
        )

    state_changed = next_data != (state.data or {})
    if state_changed:
        state.data = next_data
        state.version = current_version + 1
        state.updated_by_user_id = user_id

    event = WorkspaceAppEvent(
        org_id=org_id,
        app_id=app.id,
        thread_id=_thread_id_for_app(app),
        event_type=normalized_event_type,
        idempotency_key=normalized_idempotency_key,
        actor_kind=actor_kind,
        actor_user_id=user_id,
        actor_display={},
        payload=payload_data,
        state_key=resolved_state_key,
        state_patch=applied_patch,
        state_version=int(state.version or current_version),
        event_metadata=dict(metadata or {}),
    )
    session.add(event)
    await session.flush()
    await session.refresh(state)
    await session.refresh(event)
    return {
        "app_id": app.id,
        "state": serialize_state(state),
        "events": [serialize_event(event)],
        "collaboration": dict(contract),
        "duplicate": False,
    }


__all__ = [
    "a_append_collaboration_event",
    "a_get_collaboration_snapshot",
    "a_list_collaboration_events",
    "serialize_event",
]
