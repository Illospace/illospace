"""Auth router — login, logout, register, current user."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.responses import JSONResponse

from brain.app.api.auth import get_current_user
from brain.app.api.ws.auth import WS_TOKEN_TTL_SECONDS, create_ws_token, ensure_ws_session_id
from brain.systems.auth.users import (
    async_authenticate,
    async_create_first_user,
    async_create_workspace_owner,
    async_create_user,
    async_get_default_org_summary,
    async_get_org_summary_by_slug,
    async_get_user_by_id,
    async_has_any_users,
    async_get_user_by_email,
    safe_user_context,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api")


def _populate_session(request: Request, ctx: dict):
    """Store user context in session after login/register."""
    request.session["user_id"] = ctx["id"]
    request.session["email"] = ctx["email"]
    request.session["user_email"] = ctx.get("email", "")
    request.session["user_name"] = ctx.get("name", "")
    request.session["user_role"] = ctx.get("role", "")
    request.session["user_color"] = ctx.get("color", "#6366f1")
    request.session["org_id"] = ctx.get("org_id") or ""
    request.session["org_name"] = ctx.get("org_name", "")
    request.session["attribution_enabled"] = ctx.get("attribution_enabled", True)
    request.session["approved"] = ctx.get("approved", False)


def _current_user_response(ctx: dict) -> dict:
    return {
        "id": ctx.get("id"),
        "name": ctx.get("name", ""),
        "email": ctx.get("email", ""),
        "role": ctx.get("role", "member"),
        "color": ctx.get("color", "#6366f1"),
        "org_id": ctx.get("org_id"),
        "org_name": ctx.get("org_name", ""),
        "attribution_enabled": ctx.get("attribution_enabled", True),
        "approved": ctx.get("approved", True),
    }


@router.post("/login")
async def login(request: Request):
    body = await request.json()
    email = body.get("email", "")
    password = body.get("password", "")
    user = await async_authenticate(email, password)
    if user is None:
        return JSONResponse(status_code=401, content={"error": "Invalid credentials"})
    ctx = safe_user_context(user)
    _populate_session(request, ctx)
    return {"ok": True, "user": ctx}


@router.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return {"ok": True}


@router.get("/me")
async def me(request: Request):
    session_user_id = request.session.get("user_id")
    if session_user_id:
        user = await async_get_user_by_id(session_user_id)
        if not user:
            request.session.clear()
            return JSONResponse(status_code=200, content=None)
        return safe_user_context(user)

    if not request.headers.get("Authorization", "").startswith("Bearer "):
        return JSONResponse(status_code=200, content=None)

    try:
        current_user = await get_current_user(request)
    except HTTPException:
        return JSONResponse(status_code=200, content=None)
    if current_user and current_user.get("principal_type") == "human":
        return _current_user_response(current_user)
    return JSONResponse(status_code=200, content=None)


@router.get("/auth/setup-check")
async def setup_check(workspace: str | None = None):
    """Return whether the instance needs first-time setup (no users exist)."""
    requested_org = await async_get_org_summary_by_slug(workspace) if workspace else None
    return {
        "setup_required": not await async_has_any_users(),
        "default_org": await async_get_default_org_summary(),
        "requested_org": requested_org,
    }


@router.post("/auth/ws-token")
async def issue_ws_token(request: Request, user: dict = Depends(get_current_user)):
    """Mint a short-lived token for the current browser session's WS handshake."""
    if user.get("principal_type") != "human":
        return JSONResponse(
            status_code=403,
            content={"error": "WebSocket tokens require a human session"},
        )
    if not user.get("org_id"):
        return JSONResponse(
            status_code=403,
            content={"error": "Organization context required"},
        )

    try:
        body = await request.json()
    except Exception:
        body = {}
    tab_id = body.get("tab_id") if isinstance(body, dict) else None
    token, claims = create_ws_token(
        user,
        session_id=ensure_ws_session_id(request.session),
        tab_id=tab_id,
    )
    return {
        "token": token,
        "expires_at": claims.expires_at.isoformat(),
        "ttl_seconds": WS_TOKEN_TTL_SECONDS,
        "session_id": claims.session_id,
        "tab_id": claims.tab_id,
    }


@router.post("/register")
async def register(request: Request):
    body = await request.json()
    name = (body.get("name") or "").strip()
    email = (body.get("email") or "").strip()
    password = body.get("password", "")
    org_name = (body.get("org_name") or "").strip()
    workspace_mode = (body.get("workspace_mode") or "").strip().lower()
    workspace_slug = (body.get("workspace_slug") or "").strip().lower()
    if org_name:
        workspace_mode = "create"
    elif workspace_mode not in {"create", "join"}:
        workspace_mode = "join"

    # Validation
    if not name or not email or not password:
        return JSONResponse(status_code=400, content={"error": "All fields are required"})
    if len(password) < 8:
        return JSONResponse(status_code=400, content={"error": "Password must be at least 8 characters"})

    # Check duplicate email
    existing = await async_get_user_by_email(email)
    if existing:
        return JSONResponse(status_code=400, content={"error": "Email already in use"})

    users_exist = await async_has_any_users()

    # First user setup: create org + owner
    if not users_exist:
        if not org_name:
            return JSONResponse(status_code=400, content={"error": "Workspace name is required for setup"})
        try:
            user = await async_create_first_user(name, email, password, org_name)
        except Exception:
            logger.exception("Failed to create first user")
            return JSONResponse(status_code=400, content={"error": "Failed to create workspace"})
        ctx = safe_user_context(user)
        _populate_session(request, ctx)
        return ctx

    if workspace_mode == "create":
        if not org_name:
            return JSONResponse(status_code=400, content={"error": "Workspace name is required"})
        try:
            user = await async_create_workspace_owner(name, email, password, org_name)
        except Exception:
            logger.exception("Failed to create workspace")
            return JSONResponse(status_code=400, content={"error": "Failed to create workspace"})
        ctx = safe_user_context(user)
        _populate_session(request, ctx)
        return ctx

    # Existing org: joining requires an invite/workspace slug. Do not silently
    # attach public signups to the first org.
    if not workspace_slug:
        return JSONResponse(status_code=400, content={"error": "Invite link required to join a workspace"})
    target_org = await async_get_org_summary_by_slug(workspace_slug) if workspace_slug else None
    if not target_org:
        return JSONResponse(status_code=404, content={"error": "Workspace invite not found"})
    try:
        user = await async_create_user(name, email, password, target_org["id"])
    except Exception:
        logger.exception("Failed to create user")
        return JSONResponse(status_code=400, content={"error": "Email already in use"})

    ctx = safe_user_context(user)
    _populate_session(request, ctx)
    return ctx
