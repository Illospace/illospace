"""Human-facing external personal-agent connection management."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from brain.app.api.auth import get_current_user
from brain.app.api.authorization import require_org_context
from brain.app.api.db_utils import run_db
from brain.app.api.deps import get_db, rate_limit
from brain.app.api.schemas.external_agents import (
    ExternalAgentConnectionCreate,
    ExternalAgentConnectionRead,
    ExternalAgentTokenCreate,
    ExternalAgentTokenRead,
)
from brain.systems.external_agents import service as external_agents


router = APIRouter(
    prefix="/api/agent-connections",
    tags=["agent-connections"],
    dependencies=[Depends(rate_limit)],
)


def _raise_external_agent_error(exc: Exception) -> None:
    if isinstance(exc, external_agents.ExternalAgentNotFound):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, external_agents.ExternalAgentPermissionError):
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    if isinstance(exc, external_agents.ExternalAgentError):
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    raise exc


@router.get("", response_model=list[ExternalAgentConnectionRead])
async def list_agent_connections(
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    org_id = require_org_context(user)

    def _list(sync_db):
        return [
            external_agents.serialize_connection(row)
            for row in external_agents.list_connections(sync_db, org_id=org_id)
        ]

    return await run_db(db, _list)


@router.post("", response_model=ExternalAgentConnectionRead, status_code=201)
async def create_agent_connection(
    payload: ExternalAgentConnectionCreate,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    org_id = require_org_context(user)
    user_id = str(user.get("id"))

    def _create(sync_db):
        try:
            row = external_agents.create_connection(
                sync_db,
                org_id=org_id,
                owner_user_id=user_id,
                display_name=payload.display_name,
                agent_kind=payload.agent_kind,
                transport=payload.transport,
                endpoint_url=payload.endpoint_url,
                remote_agent_id=payload.remote_agent_id,
                remote_agent_card=payload.remote_agent_card,
                capabilities=payload.capabilities,
                metadata=payload.metadata,
            )
            return external_agents.serialize_connection(row)
        except Exception as exc:
            _raise_external_agent_error(exc)

    return await run_db(db, _create)


@router.post("/{connection_id}/tokens", response_model=ExternalAgentTokenRead, status_code=201)
async def mint_agent_connection_token(
    connection_id: str,
    payload: ExternalAgentTokenCreate,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    org_id = require_org_context(user)

    def _mint(sync_db):
        try:
            raw_token, row = external_agents.mint_connection_token(
                sync_db,
                connection_id=connection_id,
                org_id=org_id,
                name=payload.name,
                scopes=payload.scopes,
                expires_at=payload.expires_at,
            )
            data = external_agents.serialize_token(row)
            data["token"] = raw_token
            return data
        except Exception as exc:
            _raise_external_agent_error(exc)

    return await run_db(db, _mint)


@router.post("/{connection_id}/test")
async def mark_agent_connection_tested(
    connection_id: str,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    org_id = require_org_context(user)

    def _test(sync_db):
        try:
            connection = external_agents._require_connection(  # intentional internal service helper
                sync_db,
                connection_id=connection_id,
                org_id=org_id,
            )
            connection.last_tested_at = external_agents.utcnow()
            connection.status = "configured"
            sync_db.flush()
            return external_agents.serialize_connection(connection)
        except Exception as exc:
            _raise_external_agent_error(exc)

    return await run_db(db, _test)
