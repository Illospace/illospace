"""Cortex auth status, API key management endpoints."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select, text, or_
from sqlalchemy.ext.asyncio import AsyncSession

from brain.app.api.auth import get_current_user
from brain.app.api.deps import get_db
from brain.app.api.routers.cortex._key_utils import (
    VALID_PROVIDERS,
    normalize_provider_api_key as _normalize_provider_api_key,
    verify_provider_api_key as _verify_provider_api_key,
    should_trust_failed_key_verification as _should_trust_failed_key_verification,
)
from brain.app.api.routers.cortex._router import router
from brain.platform.db.models.org import ApiKeyShare, Org, OrgApiKey, User, UserApiKey

logger = logging.getLogger(__name__)

# ── API Key Management ─────────────────────────────────────────


def _masked_token_info(token: str) -> dict[str, str]:
    return {
        "prefix": token[:18] if token else "",
        "suffix": token[-40:] if token else "",
    }


async def _resolve_default_provider(db: AsyncSession, user: dict[str, Any]) -> str:
    """Resolve key-provider defaults without crossing the sync model-policy boundary."""
    user_id = user.get("id")
    org_id = user.get("org_id")
    if user_id:
        row = (
            await db.execute(
                select(User.default_provider, User.org_id, UserApiKey.provider.label("key_provider"))
                .outerjoin(UserApiKey, UserApiKey.id == User.default_api_key_id)
                .where(User.id == user_id)
                .limit(1)
            )
        ).mappings().first()
        if row:
            explicit_provider = str(row.get("default_provider") or "").strip().lower()
            if explicit_provider in VALID_PROVIDERS:
                return explicit_provider
            key_provider = str(row.get("key_provider") or "").strip().lower()
            if key_provider in VALID_PROVIDERS:
                return key_provider
            org_id = org_id or row.get("org_id")

    if org_id:
        config = await db.scalar(select(Org.memory_model_config).where(Org.id == org_id).limit(1))
        if isinstance(config, dict):
            org_provider = str(config.get("default_provider") or "").strip().lower()
            if org_provider in VALID_PROVIDERS:
                return org_provider
    return "openai"


async def _upsert_org_api_key(
    db: AsyncSession,
    *,
    org_id: str,
    provider: str,
    encrypted_key: bytes,
    label: str | None = None,
) -> None:
    values = {"org_id": org_id, "provider": provider, "encrypted": encrypted_key}
    if label is None:
        await db.execute(
            text("""
                INSERT INTO org_api_keys (org_id, provider, encrypted_key)
                VALUES (:org_id, :provider, :encrypted)
                ON CONFLICT (org_id, provider) DO UPDATE SET
                    encrypted_key = EXCLUDED.encrypted_key
            """),
            values,
        )
        return

    await db.execute(
        text("""
            INSERT INTO org_api_keys (org_id, provider, encrypted_key, label)
            VALUES (:org_id, :provider, :encrypted, :label)
            ON CONFLICT (org_id, provider) DO UPDATE SET
                encrypted_key = EXCLUDED.encrypted_key,
                label = EXCLUDED.label
        """),
        {**values, "label": label},
    )


@router.get("/keys")
async def list_api_keys(
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    user_id = user.get("id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")

    stmt = (
        select(UserApiKey)
        .where(UserApiKey.user_id == user_id)
        .order_by(UserApiKey.created_at.desc())
    )
    keys = [
        {
            "id": key.id,
            "provider": key.provider,
            "label": key.label,
            "is_active": key.is_active,
            "created_at": key.created_at,
            "last_used_at": key.last_used_at,
            "total_tokens_used": key.total_tokens_used,
            "estimated_cost_usd": key.estimated_cost_usd,
        }
        for key in (await db.scalars(stmt)).all()
    ]

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
    shared = [dict(row._mapping) for row in (await db.execute(stmt)).all()]

    org_keys = []
    if org_id := user.get("org_id"):
        stmt = (
            select(OrgApiKey)
            .where(OrgApiKey.org_id == org_id)
            .order_by(OrgApiKey.created_at.desc())
        )
        for org_row in (await db.scalars(stmt)).all():
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


@router.post("/keys/auto-import")
async def auto_import_keys(
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Auto-import setup token from ~/.claude/.credentials.json as org + personal key."""
    user_id = user.get("id")
    org_id = user.get("org_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")

    from brain.systems.auth.claude_oauth import get_setup_token
    setup_token = get_setup_token()
    if not setup_token:
        return {"imported": False, "reason": "no_setup_token"}

    imported = []
    stmt = (
        select(UserApiKey.id)
        .where(
            UserApiKey.user_id == user_id,
            UserApiKey.provider == "anthropic",
            UserApiKey.is_active == True,  # noqa: E712
        )
        .limit(1)
    )
    if not (await db.scalars(stmt)).first():
        from brain.systems.vault import _encrypt
        encrypted = _encrypt(setup_token)
        await db.execute(
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
        if not (await db.scalars(stmt)).first():
            from brain.systems.vault import _encrypt
            encrypted = _encrypt(setup_token)
            await _upsert_org_api_key(
                db,
                org_id=org_id,
                provider="anthropic",
                encrypted_key=encrypted,
                label="Claude Code",
            )
            imported.append("org")
    return {"imported": bool(imported), "keys": imported}


@router.post("/keys")
async def add_api_key(
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    user_id = user.get("id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    data = await request.json()

    provider = (data.get("provider") or await _resolve_default_provider(db, user)).strip().lower()
    label = data.get("label", "default")
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
            "Trusting failed key verification for provider=%s oauth_setup_token=True error=%s",
            provider,
            verify_error,
        )

    from brain.systems.vault import _encrypt
    encrypted = _encrypt(api_key)
    existing = (
        await db.scalars(
            select(UserApiKey).where(
                UserApiKey.user_id == user_id,
                UserApiKey.provider == provider,
                UserApiKey.label == label,
            )
        )
    ).first()
    if existing:
        existing.encrypted_key = encrypted
        existing.is_active = True
        await db.flush()
        key_id = existing.id
    else:
        key = UserApiKey(
            user_id=user_id,
            provider=provider,
            encrypted_key=encrypted,
            label=label,
        )
        db.add(key)
        await db.flush()
        key_id = key.id
    return {
        "id": key_id,
        "status": "stored",
        "verified": verify_error is None,
        "verify_error": verify_error,
        "stored_token": _masked_token_info(api_key),
    }


@router.put("/keys/default")
async def set_default_key(
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    user_id = user.get("id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    data = await request.json()
    api_key_id = data.get("api_key_id")
    if api_key_id is not None:
        stmt = (
            select(UserApiKey.id)
            .where(
                UserApiKey.id == api_key_id,
                UserApiKey.is_active == True,  # noqa: E712
                or_(
                    UserApiKey.user_id == user_id,
                    UserApiKey.id.in_(
                        select(ApiKeyShare.api_key_id).where(
                            ApiKeyShare.shared_with_user_id == user_id,
                            ApiKeyShare.revoked_at.is_(None),
                        )
                    ),
                ),
            )
        )
        if not (await db.scalars(stmt)).first():
            raise HTTPException(status_code=404, detail="Key not found, not accessible, or inactive")
    db_user = await db.get(User, user_id)
    if db_user:
        db_user.default_api_key_id = api_key_id
    return {"status": "default_updated", "api_key_id": api_key_id}


@router.post("/keys/org")
async def set_org_main_key(
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    role = user.get("role")
    org_id = user.get("org_id")
    if role != "owner":
        raise HTTPException(status_code=403, detail="Only org owners can set the main key")
    data = await request.json()

    provider = (data.get("provider") or await _resolve_default_provider(db, user)).strip().lower()
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
    await _upsert_org_api_key(db, org_id=org_id, provider=provider, encrypted_key=encrypted)
    return {
        "status": "org_key_stored",
        "verified": verify_error is None,
        "verify_error": verify_error,
        "stored_token": _masked_token_info(api_key),
    }


@router.post("/keys/{key_id}/share")
async def share_key(
    key_id: int,
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    user_id = user.get("id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    data = await request.json()
    target_user_id = data.get("shared_with_user_id")
    if not target_user_id:
        raise HTTPException(status_code=400, detail="shared_with_user_id is required")

    key_row = (
        await db.scalars(
            select(UserApiKey).where(
                UserApiKey.id == key_id,
                UserApiKey.user_id == user_id,
            )
        )
    ).first()
    if not key_row:
        raise HTTPException(status_code=403, detail=f"API key {key_id} not found or not owned by {user_id}")

    sharer = await db.get(User, user_id)
    recipient = await db.get(User, target_user_id)
    if (
        not sharer
        or not recipient
        or not getattr(sharer, "org_id", None)
        or str(sharer.org_id) != str(recipient.org_id)
    ):
        raise HTTPException(status_code=403, detail="API keys can only be shared with users in the same org")

    existing = (
        await db.scalars(
            select(ApiKeyShare).where(
                ApiKeyShare.api_key_id == key_id,
                ApiKeyShare.shared_with_user_id == target_user_id,
            )
        )
    ).first()
    now = datetime.now(timezone.utc)
    if existing:
        existing.revoked_at = None
        existing.shared_at = now
        await db.flush()
        share_id = existing.id
    else:
        share = ApiKeyShare(
            api_key_id=key_id,
            shared_with_user_id=target_user_id,
            shared_by_user_id=user_id,
            shared_at=now,
        )
        db.add(share)
        await db.flush()
        share_id = share.id
    return {"share_id": share_id, "status": "shared"}


@router.delete("/keys/{key_id}")
async def deactivate_key(
    key_id: int,
    user: dict[str, Any] = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    user_id = user.get("id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    key = (
        await db.scalars(
            select(UserApiKey).where(UserApiKey.id == key_id, UserApiKey.user_id == user_id)
        )
    ).first()
    if not key:
        raise HTTPException(status_code=404, detail="Key not found or not owned")
    key.is_active = False
    return {"status": "deactivated"}
