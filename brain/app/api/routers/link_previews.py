"""Shared link-preview resolver for product object references."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from brain.app.api.auth import get_current_user
from brain.app.api.authorization import require_org_context
from brain.app.api.deps import get_db, rate_limit
from brain.app.api.schemas.object_references import (
    LinkPreviewResolveRequest,
    LinkPreviewResolveResponse,
)
from brain.systems.cortex.object_references import resolve_object_reference_values

router = APIRouter(
    prefix="/api/link-previews",
    tags=["link-previews"],
    dependencies=[Depends(rate_limit)],
)


@router.post("/resolve", response_model=LinkPreviewResolveResponse)
async def resolve_link_previews(
    body: LinkPreviewResolveRequest,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(get_current_user),
) -> LinkPreviewResolveResponse:
    org_id = require_org_context(user)
    previews = await resolve_object_reference_values(
        db,
        body.urls,
        org_id=org_id,
        user_id=str(user.get("id")) if user.get("id") else None,
        include_handoff=True,
    )
    await db.commit()
    return LinkPreviewResolveResponse(previews=previews)
