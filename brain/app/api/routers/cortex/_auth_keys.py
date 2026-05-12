"""Cortex auth status, API key management endpoints."""
from __future__ import annotations

import logging
from typing import Any

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select, text, or_

from brain.app.api.auth import get_current_user
from brain.app.api.routers.cortex._key_utils import (
    VALID_PROVIDERS,
    normalize_provider_api_key as _normalize_provider_api_key,
    verify_provider_api_key as _verify_provider_api_key,
    should_trust_failed_key_verification as _should_trust_failed_key_verification,
    store_org_api_key,
)
from brain.app.api.routers.cortex._router import router
from brain.platform.db.models.org import ApiKeyShare, OrgApiKey, User, UserApiKey
from brain.platform.db.repositories.unit_of_work import UnitOfWork, run_sync_with_unit_of_work

logger = logging.getLogger(__name__)

# ── API Key Management ─────────────────────────────────────────


def _masked_token_info(token: str) -> dict[str, str]:
    return {
        "prefix": token[:18] if token else "",
        "suffix": token[-40:] if token else "",
    }


@router.get("/keys")
async def list_api_keys(request: Request, user: dict[str, Any] = Depends(get_current_user)):
    user_id = user.get("id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    def _list():
        with UnitOfWork() as uow:
            stmt = (
                select(UserApiKey)
                .where(UserApiKey.user_id == user_id)
                .order_by(UserApiKey.created_at.desc())
            )
            keys = []
            for k in uow.session.scalars(stmt).all():
                keys.append({
                    "id": k.id,
                    "provider": k.provider,
                    "label": k.label,
                    "is_active": k.is_active,
                    "created_at": k.created_at,
                    "last_used_at": k.last_used_at,
                    "total_tokens_used": k.total_tokens_used,
                    "estimated_cost_usd": k.estimated_cost_usd,
                })

            stmt = (
                select(
                    UserApiKey.id,
                    UserApiKey.provider,
                    UserApiKey.label,
                    UserApiKey.is_active,
                    UserApiKey.last_used_at,
                    UserApiKey.total_tokens_used,
                    UserApiKey.estimated_cost_usd,
                    User.name.label("shared_by_name"),
                    ApiKeyShare.shared_at,
                )
                .join(ApiKeyShare, ApiKeyShare.api_key_id == UserApiKey.id)
                .join(User, User.id == ApiKeyShare.shared_by_user_id)
                .where(
                    ApiKeyShare.shared_with_user_id == user_id,
                    ApiKeyShare.revoked_at.is_(None),
                )
            )
            shared = [dict(r._mapping) for r in uow.session.execute(stmt).all()]

            org_id = user.get("org_id")
            org_keys = []
            if org_id:
                stmt = (
                    select(OrgApiKey)
                    .where(OrgApiKey.org_id == org_id)
                    .order_by(OrgApiKey.created_at.desc())
                )
                for org_row in uow.session.scalars(stmt).all():
                    try:
                        from brain.systems.vault import _decrypt
                        full_key = _decrypt(bytes(org_row.encrypted_key))
                        prefix = full_key[:12] + "..."
                    except Exception:
                        prefix = "***"
                    org_keys.append({
                        "id": org_row.id,
                        "provider": org_row.provider,
                        "label": org_row.label,
                        "prefix": prefix,
                        "created_at": org_row.created_at,
                        "last_used_at": org_row.last_used_at,
                        "total_tokens_used": org_row.total_tokens_used,
                        "estimated_cost_usd": org_row.estimated_cost_usd,
                    })
        return {
            "own": keys,
            "shared": shared,
            "org_keys": org_keys,
            "org_key": org_keys[0] if org_keys else None,
        }

    return await run_sync_with_unit_of_work(_list)


@router.post("/keys/auto-import")
async def auto_import_keys(request: Request, user: dict[str, Any] = Depends(get_current_user)):
    """Auto-import setup token from ~/.claude/.credentials.json as org + personal key."""
    user_id = user.get("id")
    org_id = user.get("org_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")

    from brain.systems.auth.claude_oauth import get_setup_token
    setup_token = get_setup_token()
    if not setup_token:
        return {"imported": False, "reason": "no_setup_token"}

    def _import():
        imported = []
        with UnitOfWork() as uow:
            stmt = (
                select(UserApiKey.id)
                .where(
                    UserApiKey.user_id == user_id,
                    UserApiKey.provider == "anthropic",
                    UserApiKey.is_active == True,  # noqa: E712
                )
                .limit(1)
            )
            if not uow.session.scalars(stmt).first():
                from brain.systems.vault import _encrypt
                encrypted = _encrypt(setup_token)
                uow.session.execute(
                    text("""
                        INSERT INTO user_api_keys (user_id, provider, encrypted_key, label)
                        VALUES (:user_id, 'anthropic', :encrypted, 'Claude Code')
                        ON CONFLICT (user_id, provider, label) DO UPDATE SET
                            encrypted_key = EXCLUDED.encrypted_key, is_active = TRUE
                        RETURNING id
                    """),
                    {"user_id": user_id, "encrypted": encrypted},
                )
                imported.append("personal")

            if org_id and user.get("role") == "owner":
                stmt = (
                    select(OrgApiKey.id)
                    .where(OrgApiKey.org_id == org_id, OrgApiKey.provider == "anthropic")
                    .limit(1)
                )
                if not uow.session.scalars(stmt).first():
                    from brain.systems.vault import _encrypt
                    encrypted = _encrypt(setup_token)
                    store_org_api_key(org_id, "anthropic", encrypted, label="Claude Code", uow_factory=UnitOfWork)
                    imported.append("org")
        return {"imported": bool(imported), "keys": imported}

    return await run_sync_with_unit_of_work(_import)


@router.post("/keys")
async def add_api_key(request: Request, user: dict[str, Any] = Depends(get_current_user)):
    user_id = user.get("id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    data = await request.json()
    from brain.platform.providers.model_policy import resolve_default_provider

    provider = (
        data.get("provider") or resolve_default_provider(user_id=user_id, org_id=user.get("org_id"))
    ).strip().lower()
    label = data.get("label", "default")
    api_key = _normalize_provider_api_key(data.get("api_key", ""), provider)
    if not api_key:
        raise HTTPException(status_code=400, detail="api_key is required")
    if provider not in VALID_PROVIDERS:
        raise HTTPException(status_code=400, detail=f"Invalid provider")
    verify_error = None
    try:
        _verify_provider_api_key(api_key, provider)
    except Exception as e:
        if not _should_trust_failed_key_verification(provider, api_key):
            raise HTTPException(status_code=400, detail=f"Invalid {provider} key: {e}")
        verify_error = str(e)
        logger.warning(
            "Trusting failed key verification for provider=%s oauth_setup_token=True error=%s",
            provider,
            verify_error,
        )
    from brain.systems.vault import set_api_key
    key_id = await run_sync_with_unit_of_work(set_api_key, user_id, api_key, provider=provider, label=label)
    return {
        "id": key_id,
        "status": "stored",
        "verified": verify_error is None,
        "verify_error": verify_error,
        "stored_token": _masked_token_info(api_key),
    }


@router.put("/keys/default")
async def set_default_key(request: Request, user: dict[str, Any] = Depends(get_current_user)):
    user_id = user.get("id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    data = await request.json()
    api_key_id = data.get("api_key_id")
    def _set_default():
        with UnitOfWork() as uow:
            if api_key_id is not None:
                stmt = (
                    select(UserApiKey.id)
                    .where(
                        UserApiKey.id == api_key_id,
                        UserApiKey.is_active == True,  # noqa: E712
                        or_(
                            UserApiKey.user_id == user_id,
                            UserApiKey.id.in_(
                                select(ApiKeyShare.api_key_id)
                                .where(
                                    ApiKeyShare.shared_with_user_id == user_id,
                                    ApiKeyShare.revoked_at.is_(None),
                                )
                            ),
                        ),
                    )
                )
                if not uow.session.scalars(stmt).first():
                    raise HTTPException(status_code=404, detail="Key not found, not accessible, or inactive")
            u = uow.session.get(User, user_id)
            if u:
                u.default_api_key_id = api_key_id
        return {"status": "default_updated", "api_key_id": api_key_id}

    return await run_sync_with_unit_of_work(_set_default)


@router.post("/keys/org")
async def set_org_main_key(request: Request, user: dict[str, Any] = Depends(get_current_user)):
    role = user.get("role")
    org_id = user.get("org_id")
    if role != "owner":
        raise HTTPException(status_code=403, detail="Only org owners can set the main key")
    data = await request.json()
    from brain.platform.providers.model_policy import resolve_default_provider

    provider = (
        data.get("provider") or resolve_default_provider(user_id=user.get("id"), org_id=org_id)
    ).strip().lower()
    api_key = _normalize_provider_api_key(data.get("api_key", ""), provider)
    if not api_key:
        raise HTTPException(status_code=400, detail="api_key is required")
    if provider not in VALID_PROVIDERS:
        raise HTTPException(status_code=400, detail="Invalid provider")
    verify_error = None
    try:
        _verify_provider_api_key(api_key, provider)
    except Exception as e:
        if not _should_trust_failed_key_verification(provider, api_key):
            raise HTTPException(status_code=400, detail=f"Invalid {provider} key: {e}")
        verify_error = str(e)
        logger.warning(
            "Trusting failed org key verification for provider=%s oauth_setup_token=True error=%s",
            provider,
            verify_error,
        )
    from brain.systems.vault import _encrypt
    encrypted = _encrypt(api_key)
    await run_sync_with_unit_of_work(store_org_api_key, org_id, provider, encrypted, uow_factory=UnitOfWork)
    return {
        "status": "org_key_stored",
        "verified": verify_error is None,
        "verify_error": verify_error,
        "stored_token": _masked_token_info(api_key),
    }


@router.post("/keys/{key_id}/share")
async def share_key(key_id: int, request: Request, user: dict[str, Any] = Depends(get_current_user)):
    user_id = user.get("id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    data = await request.json()
    target_user_id = data.get("shared_with_user_id")
    if not target_user_id:
        raise HTTPException(status_code=400, detail="shared_with_user_id is required")
    from brain.systems.vault import share_api_key
    try:
        share_id = await run_sync_with_unit_of_work(share_api_key, key_id, target_user_id, user_id)
        return {"share_id": share_id, "status": "shared"}
    except ValueError as e:
        raise HTTPException(status_code=403, detail=str(e))


@router.delete("/keys/{key_id}")
async def deactivate_key(key_id: int, user: dict[str, Any] = Depends(get_current_user)):
    user_id = user.get("id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    def _deactivate():
        with UnitOfWork() as uow:
            stmt = (
                select(UserApiKey)
                .where(UserApiKey.id == key_id, UserApiKey.user_id == user_id)
            )
            key = uow.session.scalars(stmt).first()
            if not key:
                raise HTTPException(status_code=404, detail="Key not found or not owned")
            key.is_active = False
        return {"status": "deactivated"}

    return await run_sync_with_unit_of_work(_deactivate)
