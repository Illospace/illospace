"""Aggregate Cortex bootstrap endpoint.

The route is additive: existing read endpoints keep their exact response
shapes, while the frontend can fetch the same payloads with fewer round trips.
"""
from __future__ import annotations

from typing import Any

from fastapi import Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from brain.app.api.auth import get_current_user
from brain.app.api.authorization import require_org_context
from brain.app.api.deps import get_db
from brain.app.api.routers.cortex._helpers import _require_idea_for_user
from brain.app.api.routers.cortex._ideas import _idea_read_with_author, list_ideas_payload
from brain.app.api.routers.cortex._idea_ops import unified_stream_payload
from brain.app.api.routers.cortex._misc import list_connections_payload
from brain.app.api.routers.cortex._router import router
from brain.app.api.routers.team import list_members_payload
from brain.app.api.routers.workspace_pins import _serialize_pin
from brain.app.api.schemas.team import TeamMemberRead
from brain.platform.db.models.workspace_pin import WorkspacePin
from brain.systems.services.runtime_introspection import get_provider_auth_status
from brain.systems.workspace_apps.service import list_apps, serialize_apps


CORE_INCLUDE = {"ideas", "connections", "team_members"}
WORKSPACE_INCLUDE = {"workspace_apps", "workspace_pins"}


def _parse_include(include: str | None) -> set[str]:
    tokens = {part.strip().lower() for part in (include or "core").split(",") if part.strip()}
    if not tokens:
        tokens = {"core"}

    resolved: set[str] = set()
    for token in tokens:
        if token == "core":
            resolved.update(CORE_INCLUDE)
        elif token == "workspace":
            resolved.update(WORKSPACE_INCLUDE)
        elif token in CORE_INCLUDE or token in WORKSPACE_INCLUDE or token in {"auth_status", "direct_thread", "selected_idea"}:
            resolved.add(token)
        else:
            raise HTTPException(status_code=400, detail=f"Unsupported cortex bootstrap include: {token}")
    return resolved


def _dump_payload(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, list):
        return [_dump_payload(item) for item in value]
    return value


@router.get("/bootstrap")
def cortex_bootstrap(
    include: str | None = "core",
    idea_id: str | None = None,
    include_debug: bool = False,
    provider: str | None = None,
    db: Session = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    include_set = _parse_include(include)
    payload: dict[str, Any] = {
        "ideas": None,
        "connections": None,
        "team_members": None,
        "workspace_apps": None,
        "workspace_pins": None,
        "selected_idea": None,
        "auth_status": None,
        "meta": {
            "org_id": user.get("org_id"),
            "include": sorted(include_set),
        },
    }

    if "ideas" in include_set:
        payload["ideas"] = _dump_payload(list_ideas_payload(db=db, user=user))
    if "connections" in include_set:
        payload["connections"] = list_connections_payload(db=db, user=user)
    if "team_members" in include_set:
        payload["team_members"] = _dump_payload([
            TeamMemberRead.model_validate(member)
            for member in list_members_payload(db=db, user=user)
        ])
    if "workspace_apps" in include_set:
        org_id = require_org_context(user)
        payload["workspace_apps"] = _dump_payload(serialize_apps(db, list_apps(db, org_id)))
    if "workspace_pins" in include_set:
        org_id = require_org_context(user)
        pins = db.scalars(
            select(WorkspacePin)
            .where(WorkspacePin.org_id == org_id, WorkspacePin.archived_at.is_(None))
            .order_by(WorkspacePin.created_at.asc(), WorkspacePin.id.asc())
        ).all()
        payload["workspace_pins"] = _dump_payload([_serialize_pin(pin) for pin in pins])
    if "selected_idea" in include_set:
        if not idea_id:
            raise HTTPException(status_code=400, detail="idea_id is required when include contains selected_idea")
        idea = _require_idea_for_user(db, idea_id, user)
        payload["selected_idea"] = _dump_payload(_idea_read_with_author(idea, db))
    if "direct_thread" in include_set:
        if not idea_id:
            raise HTTPException(status_code=400, detail="idea_id is required when include contains direct_thread")
        payload["direct_thread"] = {
            "idea_id": idea_id,
            "stream": unified_stream_payload(
                idea_id=idea_id,
                include_debug=include_debug,
                user=user,
            ),
        }
    if "auth_status" in include_set:
        try:
            payload["auth_status"] = get_provider_auth_status(
                user_id=user.get("id"),
                org_id=user.get("org_id"),
                provider=provider,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    return payload
