"""Authentication dependency for FastAPI routes."""
from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request

from brain.app.api.deps import _is_internal
from brain.app.api.authorization import human_identity, service_principal_context
from brain.app.api.config import (
    AUTH_DEV_FALLBACK_ENABLED,
    INTERNAL_BEARER_TOKEN_SOURCES,
    INTERNAL_BEARER_TOKENS,
)


_LOCALHOST_NAMES = {"localhost", "127.0.0.1", "::1", "[::1]"}


async def _get_localhost_user() -> dict[str, Any] | None:
    """Find the first approved user (owner preferred) for explicit dev fallback.

    Returns a full user context dict with real UUID, or None if no users exist.
    This prevents the "system" string from leaking into UUID columns.
    """
    try:
        from brain.systems.auth.users import safe_user_context
        from sqlalchemy import text as sa_text
        from brain.platform.db.repositories.unit_of_work import UnitOfWork
        async with UnitOfWork() as uow:
            result = await uow.session.execute(sa_text(
                "SELECT id FROM users WHERE approved = TRUE "
                "ORDER BY CASE WHEN role = 'owner' THEN 0 ELSE 1 END, created_at "
                "LIMIT 1"
            ))
            row = result.mappings().first()
            if not row:
                return None
            from brain.systems.auth.users import async_get_user_by_id
            db_user = await async_get_user_by_id(str(row["id"]))
            if not db_user:
                return None
            ctx = safe_user_context(db_user)
            identity = human_identity(ctx).to_user_context()
            return {
                **identity,
                "color": ctx.get("color", "#6366f1"),
                "attribution_enabled": ctx.get("attribution_enabled", True),
                "default_provider": ctx.get("default_provider"),
                "internal": True,
                "audit": {
                    **identity["audit"],
                    "auth_source": "dev_localhost_user_fallback",
                },
            }
    except Exception:
        return None


def _service_principal_for_token(token: str) -> dict[str, Any]:
    token_source = INTERNAL_BEARER_TOKEN_SOURCES.get(token, "internal_token")
    return service_principal_context("internal-api", token_source=token_source)


def _is_local_dev_request(request: Request) -> bool:
    client_addr = request.client.host if request.client else ""
    if not _is_internal(client_addr):
        return False
    url = getattr(request, "url", None)
    host = (getattr(url, "hostname", None) or request.headers.get("host", "").split(":", 1)[0] or client_addr).strip().lower()
    return host in _LOCALHOST_NAMES


async def get_current_user(request: Request) -> dict[str, Any]:
    """Extract and validate current user from session or bearer token.

    Always reads fresh user data from DB so code changes never require re-login.
    """
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]
        if token in INTERNAL_BEARER_TOKENS:
            return _service_principal_for_token(token)

    user_id = request.session.get("user_id") if hasattr(request, "session") else None

    # Localhost user fallback is dev/test-only and must be explicitly enabled.
    if not user_id:
        if AUTH_DEV_FALLBACK_ENABLED and _is_local_dev_request(request):
            fallback = await _get_localhost_user()
            if fallback:
                return fallback
            return service_principal_context("dev-localhost", token_source="localhost")
    if not user_id:
        raise HTTPException(status_code=401, detail="Authentication required")

    # Always read fresh from DB — no stale session fields
    from brain.systems.auth.users import async_get_user_by_id, safe_user_context
    db_user = await async_get_user_by_id(user_id)
    if not db_user:
        request.session.clear()
        raise HTTPException(status_code=401, detail="User not found")

    if not db_user.get("approved", False):
        raise HTTPException(status_code=403, detail="Account pending approval")

    ctx = safe_user_context(db_user)
    identity = human_identity(ctx).to_user_context()
    return {
        **identity,
        "color": ctx.get("color", "#6366f1"),
        "attribution_enabled": ctx.get("attribution_enabled", True),
        "default_provider": ctx.get("default_provider"),
    }


async def get_optional_user(request: Request) -> dict[str, Any] | None:
    try:
        return await get_current_user(request)
    except HTTPException:
        return None
