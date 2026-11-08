"""Organization MCP settings. Credentials remain in the vault."""
from __future__ import annotations

import logging
import time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from brain.app.api.auth import get_current_user
from brain.app.api.authorization import PERMISSION_SYSTEM_MANAGE, require_org_context, require_permission
from brain.app.api.deps import get_db, rate_limit
from brain.systems.external_mcp import config, runtime
from brain.systems.external_mcp.client import McpError

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/external-mcp/servers", tags=["external-mcp"], dependencies=[Depends(rate_limit)])
_admin = require_permission(PERMISSION_SYSTEM_MANAGE)


class ServerChanges(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    name: str | None = None
    url: str | None = None
    transport: str | None = None
    auth_mode: str | None = None
    bearer_token_key: str | None = None
    token_url: str | None = None
    client_id_key: str | None = None
    client_secret_key: str | None = None
    scope: str | None = None
    resource: str | None = None
    allowed_tools: list[str] | None = None
    enabled: bool | None = None
    timeout_seconds: int | None = Field(default=None, ge=1, le=120)


async def _server(session, user, server_id):
    server = await config.get_server(session, require_org_context(user), server_id)
    if server is None:
        raise HTTPException(status_code=404, detail="MCP server not found")
    return server


async def _response(session, server):
    return {"id": server.id, **{key: getattr(server, key) for key in config.CONFIG_FIELDS},
            "created_at": server.created_at, "updated_at": server.updated_at,
            "missing_secret_keys": await config.missing_secret_keys(session, server)}


@router.get("/")
async def list_external_mcp_servers(user: dict = Depends(get_current_user), session: AsyncSession = Depends(get_db)):
    servers = await config.list_servers(session, require_org_context(user))
    return [await _response(session, server) for server in servers]


async def _save(session, user, payload, server=None):
    try:
        # A savepoint converts a concurrent duplicate into a safe API error.
        async with session.begin_nested():
            saved = await config.save_server(session, org_id=require_org_context(user), actor_user_id=user["id"],
                                             values=payload.model_dump(exclude_unset=True), server=server)
        return await _response(session, saved)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except IntegrityError:
        raise HTTPException(status_code=409, detail="MCP configuration conflicts with an existing server") from None


@router.post("/", status_code=201)
async def create_external_mcp_server(payload: ServerChanges, user: dict = Depends(_admin), session: AsyncSession = Depends(get_db)):
    return await _save(session, user, payload)


@router.patch("/{server_id}")
async def update_external_mcp_server(server_id: int, payload: ServerChanges, user: dict = Depends(_admin), session: AsyncSession = Depends(get_db)):
    return await _save(session, user, payload, await _server(session, user, server_id))


@router.delete("/{server_id}")
async def delete_external_mcp_server(server_id: int, user: dict = Depends(_admin), session: AsyncSession = Depends(get_db)):
    server = await _server(session, user, server_id)
    await config.delete_server(session, server, actor_user_id=user["id"])
    return {"ok": True}


@router.post("/{server_id}/test")
async def test_external_mcp_server(server_id: int, user: dict = Depends(_admin), session: AsyncSession = Depends(get_db)):
    server = await _server(session, user, server_id)
    started = time.monotonic()
    outcome, error_kind = "ok", None
    try:
        tools = await runtime.discover_tools(server, actor_user_id=user["id"], use_cache=False)
        return {"ok": True, "tools": [{"name": str(tool.get("name", ""))[:128],
                                     "description": str(tool.get("description") or "")[:1000],
                                     "allowed": tool.get("name") in server.allowed_tools} for tool in tools]}
    except McpError as exc:
        outcome, error_kind = exc.outcome, exc.kind
        # The API shows safe status codes, not remote response text.
        return {"ok": False, "tools": [], "error": "Missing vault key" if exc.kind == "missing_secret" else f"MCP connection failed ({exc.kind})"}
    except Exception:
        outcome, error_kind = "unavailable", "unavailable"
        logger.warning("external_mcp_connection_test_failed", extra={"server_id": server.id})
        return {"ok": False, "tools": [], "error": "MCP server is unavailable"}
    finally:
        await config.audit_event(session, server, "connection_tested", actor_user_id=user["id"], outcome=outcome,
                           duration_ms=int((time.monotonic() - started) * 1000), error_kind=error_kind)
