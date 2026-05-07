"""Append-only AgentRun event helpers."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy import false, select

from brain.systems.runs.events import run_event
from brain.systems.runs.store import AgentRunStore
from brain.systems.runs.ui_events import run_event_to_ui_message
from brain.platform.db.models.agent_run import AgentRunEventRow, AgentRunRow
from brain.platform.db.repositories.unit_of_work import UnitOfWork


def record_run_event(
    run_id: int,
    event_type: str,
    payload: dict[str, Any] | None = None,
    *,
    producer: str = "agent_runtime",
    session=None,
    **_: Any,
) -> AgentRunEventRow:
    def _write(active_session):
        store = AgentRunStore(active_session)
        row = store.require_run(int(run_id))
        return store.append_event(
            run_event(
                int(run_id),
                str(event_type),
                dict(payload or {}),
                root_run_id=row.root_run_id,
                producer=producer,
            )
        )

    if session is not None:
        return _write(session)
    with UnitOfWork() as uow:
        return _write(uow.session)


def record_run_degraded_event(run_id: int, event_type: str, payload: dict[str, Any] | None = None, **kwargs: Any) -> AgentRunEventRow:
    data = dict(payload or {})
    data.setdefault("degraded", True)
    return record_run_event(run_id, event_type, data, **kwargs)


def run_event_to_message(event: AgentRunEventRow, *, replayed: bool = False) -> dict[str, Any] | None:
    return run_event_to_ui_message(event, replayed=replayed)


def run_event_backbone_status(
    session,
    consumer_name: str = "api.websocket_fanout",
    *,
    consumer_running: bool | None = None,
    stale_after_seconds: int = 120,
) -> dict[str, Any]:
    from brain.app.api.ws.run_events import run_event_backbone_status as _status

    return _status(
        session,
        consumer_name,
        consumer_running=consumer_running,
        stale_after_seconds=stale_after_seconds,
    )


def _principal_can_replay_all(principal: Mapping[str, Any]) -> bool:
    permissions = set(principal.get("permissions") or [])
    return principal.get("principal_type") == "service" and (
        "internal:api" in permissions or "run:manage" in permissions
    )


def list_run_events_after_for_principal(
    session,
    principal: Mapping[str, Any],
    *,
    last_event_id: int = 0,
    limit: int = 100,
) -> list[AgentRunEventRow]:
    stmt = (
        select(AgentRunEventRow, AgentRunRow.thread_id, AgentRunRow.profile, AgentRunRow.org_id)
        .join(AgentRunRow, AgentRunRow.id == AgentRunEventRow.run_id)
        .where(AgentRunEventRow.id > int(last_event_id))
        .order_by(AgentRunEventRow.id.asc())
        .limit(limit)
    )
    if not _principal_can_replay_all(principal):
        org_id = str(principal.get("org_id") or "").strip()
        if not org_id:
            stmt = stmt.where(false())
        else:
            stmt = stmt.where(AgentRunRow.org_id == org_id)
    rows = []
    for event, thread_id, profile, row_org_id in session.execute(stmt).all():
        setattr(event, "_agent_run_thread_id", thread_id)
        setattr(event, "_agent_run_profile", profile)
        setattr(event, "_agent_run_org_id", row_org_id)
        rows.append(event)
    return rows


__all__ = [
    "list_run_events_after_for_principal",
    "record_run_degraded_event",
    "record_run_event",
    "run_event_backbone_status",
    "run_event_to_message",
]
