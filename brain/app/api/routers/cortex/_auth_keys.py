"""Cortex auth status and org-owned provider key endpoints."""
from __future__ import annotations

import logging
from typing import Any

from fastapi import Depends, HTTPException, Request
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from brain.app.api.auth import get_current_user
from brain.app.api.deps import get_db
from brain.app.api.routers.cortex._key_utils import (
    VALID_PROVIDERS,
    normalize_provider_api_key as _normalize_provider_api_key,
    should_trust_failed_key_verification as _should_trust_failed_key_verification,
    verify_provider_api_key as _verify_provider_api_key,
)
from brain.app.api.routers.cortex._router import router
from brain.platform.db.models.org import Org, OrgApiKey
from brain.systems.vault import async_set_org_api_key

logger = logging.getLogger(__name__)


def _masked_token_info(token: str) -> dict[str, str]:
    return {
        "prefix": token[:18] if token else "",
        "suffix": token[-40:] if token else "",
    }


def _require_org_id(user: dict[str, Any]) -> str:
    org_id = str(user.get("org_id") or "").strip()
    if not org_id:
        raise HTTPException(status_code=403, detail="Workspace context is required")
    return org_id


def _require_org_key_manager(user: dict[str, Any]) -> str:
    if user.get("role") not in {"owner", "admin"}:
        raise HTTPException(status_code=403, detail="Only workspace owners and admins can manage provider keys")
    return _require_org_id(user)


async def _resolve_default_provider(db: AsyncSession, user: dict[str, Any]) -> str:
    """Resolve the org default provider without user-owned key preferences."""
    org_id = user.get("org_id")
    if org_id:
        config = await db.scalar(select(Org.memory_model_config).where(Org.id == org_id).limit(1))
        if isinstance(config, dict):
            org_provider = str(config.get("default_provider") or "").strip().lower()
            if org_provider in VALID_PROVIDERS:
                return org_provider
    return "openai"


async def _org_key_payloads(db: AsyncSession, org_id: str) -> list[dict[str, Any]]:
    rows = (
        await db.scalars(
            select(OrgApiKey)
            .where(OrgApiKey.org_id == org_id)
            .order_by(OrgApiKey.created_at.desc())
        )
    ).all()
    payloads: list[dict[str, Any]] = []
    for row in rows:
        try:
            from brain.systems.vault import _decrypt

            full_key = _decrypt(bytes(row.encrypted_key))
            prefix = full_key[:12] + "..."
        except Exception:
            prefix = "***"
        payloads.append({
            "id": row.id,
            "provider": row.provider,
            "label": row.label,
            "prefix": prefix,
            "created_at": row.created_at,
            "last_used_at": row.last_used_at,
            "total_tokens_used": row.total_tokens_used,
            "estimated_cost_usd": row.estimated_cost_usd,
        })
    return payloads


async def _store_org_key_from_request(
    request: Request,
    user: dict[str, Any],
    db: AsyncSession,
) -> dict[str, Any]:
    org_id = _require_org_key_manager(user)
    data = await request.json()

    provider = (data.get("provider") or await _resolve_default_provider(db, user)).strip().lower()
    label = str(data.get("label") or "main").strip() or "main"
    api_key = _normalize_provider_api_key(data.get("api_key", ""), provider)
    if not api_key:
        raise HTTPException(status_code=400, detail="api_key is required")
    if provider not in VALID_PROVIDERS:
        raise HTTPException(status_code=400, detail="Invalid provider")

    verify_error = None
    try:
        _verify_provider_api_key(api_key, provider)
    except Exception as exc:
        if not _should_trust_failed_key_verification(provider, api_key):
            raise HTTPException(status_code=400, detail=f"Invalid {provider} key: {exc}") from exc
        verify_error = str(exc)
        logger.warning(
            "Trusting failed org key verification for provider=%s oauth_setup_token=True error=%s",
            provider,
            verify_error,
        )

    key_id = await async_set_org_api_key(
        org_id,
        api_key,
        provider=provider,
        label=label,
        session=db,
    )
    return {
        "id": key_id,
        "status": "org_key_stored",
        "verified": verify_error is None,
        "verify_error": verify_error,
        "stored_token": _masked_token_info(api_key),
    }


@router.get("/keys")
async def list_api_keys(
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    del request
    org_id = _require_org_id(user)
    org_keys = await _org_key_payloads(db, org_id)
    return {
        "org_keys": org_keys,
        "org_key": org_keys[0] if org_keys else None,
    }


@router.post("/keys/auto-import")
async def auto_import_keys(
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Auto-import setup token from ~/.claude/.credentials.json as an org key."""
    del request
    org_id = _require_org_key_manager(user)

    from brain.systems.auth.claude_oauth import get_setup_token

    setup_token = get_setup_token()
    if not setup_token:
        return {"imported": False, "reason": "no_setup_token"}

    key_id = await async_set_org_api_key(
        org_id,
        setup_token,
        provider="anthropic",
        label="Claude Code",
        session=db,
    )
    return {"imported": True, "keys": ["org"], "id": key_id}


@router.post("/keys")
async def add_api_key(
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await _store_org_key_from_request(request, user, db)


@router.post("/keys/org")
async def set_org_main_key(
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await _store_org_key_from_request(request, user, db)


@router.delete("/keys/{key_id}")
async def delete_key(
    key_id: int,
    user: dict[str, Any] = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    org_id = _require_org_key_manager(user)
    result = await db.execute(
        delete(OrgApiKey).where(
            OrgApiKey.id == key_id,
            OrgApiKey.org_id == org_id,
        )
    )
    deleted = int(getattr(result, "rowcount", 0) or 0)
    if deleted <= 0:
        raise HTTPException(status_code=404, detail="Org provider key not found")
    return {"status": "deleted"}
