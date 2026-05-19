"""Human-facing external personal-agent connection management."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from brain.app.api.auth import get_current_user
from brain.app.api.authorization import require_org_context
from brain.app.api.deps import get_db, rate_limit
from brain.app.api.routers.external_agent_errors import raise_external_agent_http_error
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


def _is_connection_admin(user: dict[str, Any]) -> bool:
    return str(user.get("role") or "").lower() in external_agents.CONNECTION_ADMIN_ROLES


@router.get("", response_model=list[ExternalAgentConnectionRead])
async def list_agent_connections(
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    org_id = require_org_context(user)
    owner_user_id = None if _is_connection_admin(user) else str(user.get("id") or "")

    rows = await external_agents.list_connections(
        db,
        org_id=org_id,
        owner_user_id=owner_user_id,
    )
    return [external_agents.serialize_connection(row) for row in rows]


@router.post("", response_model=ExternalAgentConnectionRead, status_code=201)
async def create_agent_connection(
    payload: ExternalAgentConnectionCreate,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    org_id = require_org_context(user)
    user_id = str(user.get("id"))

    try:
        row = await external_agents.create_connection(
            db,
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
        raise_external_agent_http_error(exc)


@router.post("/{connection_id}/tokens", response_model=ExternalAgentTokenRead, status_code=201)
async def mint_agent_connection_token(
    connection_id: str,
    payload: ExternalAgentTokenCreate,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    org_id = require_org_context(user)
    user_id = str(user.get("id") or "")
    role = str(user.get("role") or "")

    try:
        await external_agents.require_connection_for_user(
            db,
            connection_id=connection_id,
            org_id=org_id,
            user_id=user_id,
            role=role,
            require_manage=True,
        )
        raw_token, row = await external_agents.mint_connection_token(
            db,
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
        raise_external_agent_http_error(exc)


@router.get("/{connection_id}/tokens", response_model=list[ExternalAgentTokenRead])
async def list_agent_connection_tokens(
    connection_id: str,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    org_id = require_org_context(user)
    user_id = str(user.get("id") or "")
    role = str(user.get("role") or "")

    try:
        await external_agents.require_connection_for_user(
            db,
            connection_id=connection_id,
            org_id=org_id,
            user_id=user_id,
            role=role,
            require_manage=True,
        )
        rows = await external_agents.list_connection_tokens(
            db,
            connection_id=connection_id,
            org_id=org_id,
        )
        return [external_agents.serialize_token(row) for row in rows]
    except Exception as exc:
        raise_external_agent_http_error(exc)


@router.delete("/{connection_id}/tokens/{token_id}", response_model=ExternalAgentTokenRead)
async def revoke_agent_connection_token(
    connection_id: str,
    token_id: str,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    org_id = require_org_context(user)
    user_id = str(user.get("id") or "")
    role = str(user.get("role") or "")

    try:
        await external_agents.require_connection_for_user(
            db,
            connection_id=connection_id,
            org_id=org_id,
            user_id=user_id,
            role=role,
            require_manage=True,
        )
        row = await external_agents.revoke_connection_token(
            db,
            connection_id=connection_id,
            token_id=token_id,
            org_id=org_id,
        )
        return external_agents.serialize_token(row)
    except Exception as exc:
        raise_external_agent_http_error(exc)


@router.delete("/{connection_id}", response_model=ExternalAgentConnectionRead)
async def delete_agent_connection(
    connection_id: str,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    org_id = require_org_context(user)
    user_id = str(user.get("id") or "")
    role = str(user.get("role") or "")

    try:
        await external_agents.require_connection_for_user(
            db,
            connection_id=connection_id,
            org_id=org_id,
            user_id=user_id,
            role=role,
            require_manage=True,
        )
        connection = await external_agents.disable_connection(
            db,
            connection_id=connection_id,
            org_id=org_id,
        )
        return external_agents.serialize_connection(connection)
    except Exception as exc:
        raise_external_agent_http_error(exc)


@router.post("/{connection_id}/test")
async def mark_agent_connection_tested(
    connection_id: str,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    org_id = require_org_context(user)
    user_id = str(user.get("id") or "")
    role = str(user.get("role") or "")

    try:
        connection = await external_agents.require_connection_for_user(
            db,
            connection_id=connection_id,
            org_id=org_id,
            user_id=user_id,
            role=role,
            require_manage=True,
        )
        connection.last_tested_at = external_agents.utcnow()
        connection.status = "configured"
        await db.flush()
        return external_agents.serialize_connection(connection)
    except Exception as exc:
        raise_external_agent_http_error(exc)
