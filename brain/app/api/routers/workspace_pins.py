"""Workspace pins router."""
from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from brain.app.api.auth import get_current_user
from brain.app.api.authorization import require_org_context
from brain.app.api.deps import get_db, rate_limit
from brain.app.api.routers.ws import ws_manager
from brain.app.api.schemas.workspace_pins import (
    WorkspacePinCreate,
    WorkspacePinRead,
    WorkspacePinUpdate,
)
from brain.platform.db.models.workspace_pin import WorkspacePin

DEFAULT_PIN_COLOR = "#d5a14d"
HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")

router = APIRouter(
    prefix="/api/workspace-pins",
    tags=["workspace-pins"],
    dependencies=[Depends(rate_limit)],
)


def _normalize_pin_color(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    color = value.strip()
    if HEX_COLOR_RE.fullmatch(color):
        return color
    return None


def _require_pin_author(pin: WorkspacePin, user: dict[str, Any]) -> None:
    creator_id = str(pin.created_by_user_id) if pin.created_by_user_id else None
    user_id = str(user.get("id")) if user.get("id") else None
    if creator_id and creator_id != user_id:
        raise HTTPException(status_code=403, detail="Only the pin creator can edit this pin")


def _serialize_pin(pin: WorkspacePin) -> WorkspacePinRead:
    return WorkspacePinRead.model_validate(
        {
            "id": str(pin.id),
            "org_id": str(pin.org_id),
            "label": pin.label,
            "color": pin.color,
            "position_x": pin.position_x,
            "position_y": pin.position_y,
            "metadata": pin.pin_metadata or {},
            "created_by_user_id": str(pin.created_by_user_id) if pin.created_by_user_id else None,
            "archived_at": pin.archived_at,
            "created_at": pin.created_at,
            "updated_at": pin.updated_at,
        }
    )


def _get_pin_for_org(db: Session, org_id: str, pin_id: str) -> WorkspacePin:
    pin = db.scalar(
        select(WorkspacePin).where(
            WorkspacePin.id == pin_id,
            WorkspacePin.org_id == org_id,
        )
    )
    if pin is None or pin.archived_at is not None:
        raise HTTPException(status_code=404, detail="Workspace pin not found")
    return pin


@router.get("", response_model=list[WorkspacePinRead], include_in_schema=False)
@router.get("/", response_model=list[WorkspacePinRead])
def list_workspace_pins(
    db: Session = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    org_id = require_org_context(user)
    pins = db.scalars(
        select(WorkspacePin)
        .where(WorkspacePin.org_id == org_id, WorkspacePin.archived_at.is_(None))
        .order_by(WorkspacePin.created_at.asc(), WorkspacePin.id.asc())
    ).all()
    return [_serialize_pin(pin) for pin in pins]


@router.post("", response_model=WorkspacePinRead, status_code=201, include_in_schema=False)
@router.post("/", response_model=WorkspacePinRead, status_code=201)
async def create_workspace_pin(
    body: WorkspacePinCreate,
    db: Session = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    org_id = require_org_context(user)
    color = (
        _normalize_pin_color(body.color)
        or _normalize_pin_color(user.get("color"))
        or DEFAULT_PIN_COLOR
    )
    pin = WorkspacePin(
        org_id=org_id,
        label=body.label.strip(),
        color=color,
        position_x=body.position_x,
        position_y=body.position_y,
        pin_metadata=body.metadata,
        created_by_user_id=str(user.get("id")) if user.get("id") else None,
    )
    db.add(pin)
    db.flush()
    payload = _serialize_pin(pin)
    await ws_manager.broadcast_to_org(org_id, "workspace_pin_created", {"pin": payload.model_dump(mode="json")})
    return payload


@router.patch("/{pin_id}", response_model=WorkspacePinRead)
async def update_workspace_pin(
    pin_id: str,
    body: WorkspacePinUpdate,
    db: Session = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
    ):
    org_id = require_org_context(user)
    pin = _get_pin_for_org(db, org_id, pin_id)
    _require_pin_author(pin, user)
    updates = body.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")

    if "label" in updates and updates["label"] is not None:
        pin.label = str(updates["label"]).strip()
    if "color" in updates and updates["color"] is not None:
        pin.color = str(updates["color"])
    if "position_x" in updates and updates["position_x"] is not None:
        pin.position_x = float(updates["position_x"])
    if "position_y" in updates and updates["position_y"] is not None:
        pin.position_y = float(updates["position_y"])
    if "metadata" in updates and updates["metadata"] is not None:
        pin.pin_metadata = dict(updates["metadata"])

    db.flush()
    payload = _serialize_pin(pin)
    await ws_manager.broadcast_to_org(org_id, "workspace_pin_updated", {"pin": payload.model_dump(mode="json")})
    return payload


@router.delete("/{pin_id}")
async def delete_workspace_pin(
    pin_id: str,
    db: Session = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
    ):
    org_id = require_org_context(user)
    pin = _get_pin_for_org(db, org_id, pin_id)
    _require_pin_author(pin, user)
    db.delete(pin)
    db.flush()
    await ws_manager.broadcast_to_org(org_id, "workspace_pin_deleted", {"pin_id": pin_id})
    return {"deleted": {"id": pin_id}}
