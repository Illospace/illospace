"""Vault router — scoped secret management, sharing, lock state, and audit."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from brain.app.api.auth import get_current_user
from brain.app.api.authorization import can_audit_vault, can_share_vault
from brain.app.api.deps import get_db, rate_limit
from brain.app.api.db_utils import run_db
from brain.app.api.schemas.vault import (
    SecretCreate,
    SecretRead,
    SecretReveal,
    VaultProjectBindingCreate,
    VaultProjectBindingRead,
    VaultShareCreate,
)


class PinSetup(BaseModel):
    new_pin: str = Field(min_length=4, max_length=128)
    current_pin: str | None = Field(default=None, max_length=128)


class PinUnlock(BaseModel):
    pin: str = Field(min_length=1, max_length=128)


class SecretUpdate(BaseModel):
    value: str | None = Field(default=None, min_length=1)
    description: str | None = None
    category: str | None = None
    agent_access_level: str | None = Field(default=None, pattern="^(available|ask|manual)$")


class AgentGrantApproval(BaseModel):
    ttl_minutes: int = Field(default=15, ge=1, le=60)
    max_reads: int = Field(default=1, ge=1, le=25)


router = APIRouter(
    prefix="/api/vault",
    tags=["vault"],
    dependencies=[Depends(rate_limit)],
)


def _require_user_id(user: dict[str, Any]) -> str:
    user_id = str(user.get("id") or "")
    if not user_id or user_id.startswith("service:"):
        raise HTTPException(status_code=403, detail="Vault access requires a human user")
    return user_id


def _org_id(user: dict[str, Any]) -> str | None:
    value = user.get("org_id")
    return str(value) if value else None


def _require_vault_share(user: dict[str, Any]) -> None:
    if not can_share_vault(user):
        raise HTTPException(status_code=403, detail="Permission denied")


def _require_vault_audit(user: dict[str, Any]) -> None:
    if not can_audit_vault(user):
        raise HTTPException(status_code=403, detail="Permission denied")


def _vault_token(request: Request) -> str:
    from brain.systems.vault import VAULT_UNLOCK_HEADER

    return request.headers.get(VAULT_UNLOCK_HEADER, "")


def _raise_if_vault_not_configured(exc: RuntimeError) -> None:
    if "VAULT_MASTER_KEY is required" not in str(exc):
        return
    raise HTTPException(
        status_code=503,
        detail="Vault master key is not configured. Set VAULT_MASTER_KEY before saving or revealing secrets.",
    ) from exc


def _require_unlocked(request: Request, user: dict[str, Any]) -> None:
    from brain.systems.vault import has_pin, validate_vault_token

    user_id = _require_user_id(user)
    if not has_pin(user_id):
        return
    if not validate_vault_token(user_id, _vault_token(request)):
        raise HTTPException(status_code=423, detail="Vault locked")


async def _async_require_unlocked(request: Request, user: dict[str, Any]) -> None:
    from brain.systems.vault import async_has_pin, async_validate_vault_token

    user_id = _require_user_id(user)
    if not await async_has_pin(user_id):
        return
    if not await async_validate_vault_token(user_id, _vault_token(request)):
        raise HTTPException(status_code=423, detail="Vault locked")


@router.get("/pin-status")
async def get_pin_status(user: dict[str, Any] = Depends(get_current_user)):
    from brain.systems.vault import async_get_pin_status as _status

    return await _status(_require_user_id(user))


@router.post("/setup-pin")
async def setup_pin(body: PinSetup, user: dict[str, Any] = Depends(get_current_user)):
    from brain.systems.vault import async_set_pin

    if not await async_set_pin(_require_user_id(user), body.new_pin, body.current_pin):
        raise HTTPException(status_code=403, detail="Current PIN is incorrect")
    return {"success": True}


@router.post("/unlock")
async def unlock_vault(body: PinUnlock, user: dict[str, Any] = Depends(get_current_user)):
    from brain.systems.vault import async_unlock_vault as _unlock

    unlocked = await _unlock(_require_user_id(user), body.pin)
    if not unlocked:
        raise HTTPException(status_code=403, detail="Incorrect PIN or vault locked")
    token, expires = unlocked
    return {"unlocked": True, "token": token, "expires_at": expires}


@router.post("/lock")
async def lock_vault(request: Request, user: dict[str, Any] = Depends(get_current_user)):
    from brain.systems.vault import async_revoke_vault_token

    await async_revoke_vault_token(_require_user_id(user), _vault_token(request))
    return {"locked": True}


@router.get("/org-users")
async def list_org_users(
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
):
    await _async_require_unlocked(request, user)
    _require_vault_share(user)
    org_id = _org_id(user)
    if not org_id:
        return []
    from brain.systems.vault import async_get_org_users

    user_id = _require_user_id(user)
    return [row for row in await async_get_org_users(org_id) if str(row.get("id")) != user_id]


@router.delete("/shares/{share_id}")
async def revoke_share(
    share_id: int,
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
):
    await _async_require_unlocked(request, user)
    _require_vault_share(user)
    from brain.systems.vault import async_revoke_share as _revoke

    if not await _revoke(share_id, _require_user_id(user)):
        raise HTTPException(status_code=404, detail="Share not found or not owned by you")
    return {"deleted": True}


@router.put("/{key_name}")
async def update_secret(
    key_name: str,
    body: SecretUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    await _async_require_unlocked(request, user)
    from brain.systems.vault import async_set_secret, normalize_agent_access_level

    user_id = _require_user_id(user)
    from brain.platform.db.repositories.vault import VaultRepository

    secret = await run_db(db, lambda sync_db: VaultRepository(sync_db).get_by_key(user_id, key_name))
    if not secret:
        raise HTTPException(status_code=404, detail="Secret not found")
    updates = body.model_dump(exclude_unset=True)
    if "value" in updates:
        try:
            await async_set_secret(
                key_name=key_name,
                value=updates.pop("value"),
                user_id=user_id,
                org_id=_org_id(user),
                description=updates.get("description", secret.description),
                category=updates.get("category", secret.category),
                agent_access_level=updates.get("agent_access_level"),
            )
        except RuntimeError as exc:
            _raise_if_vault_not_configured(exc)
            raise
    else:
        def _update(sync_db: Session) -> None:
            scoped_secret = VaultRepository(sync_db).get_by_key(user_id, key_name)
            if scoped_secret is None:
                raise HTTPException(status_code=404, detail="Secret not found")
            for k, v in updates.items():
                if k == "agent_access_level":
                    v = normalize_agent_access_level(v)
                setattr(scoped_secret, k, v)
            sync_db.flush()

        await run_db(db, _update)
    return {"updated": True}


@router.get("/missing")
async def list_missing(
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
):
    await _async_require_unlocked(request, user)
    _require_vault_audit(user)
    from brain.systems.vault import async_get_missing_requests

    return await async_get_missing_requests(user_id=_require_user_id(user), org_id=_org_id(user))


@router.get("/log", response_model=list)
async def get_access_log(
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
):
    await _async_require_unlocked(request, user)
    _require_vault_audit(user)
    from brain.systems.vault import async_get_vault_access_log

    return await async_get_vault_access_log(_require_user_id(user), org_id=_org_id(user), limit=100)


@router.get("/agent-grants")
async def list_agent_grants(
    request: Request,
    status: str | None = None,
    user: dict[str, Any] = Depends(get_current_user),
):
    await _async_require_unlocked(request, user)
    from brain.systems.vault import async_list_agent_grants as _list_grants

    statuses = [part.strip() for part in status.split(",") if part.strip()] if status else None
    return await _list_grants(_require_user_id(user), org_id=_org_id(user), statuses=statuses)


@router.post("/agent-grants/{grant_id}/approve")
async def approve_agent_grant(
    grant_id: int,
    body: AgentGrantApproval,
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
):
    await _async_require_unlocked(request, user)
    from brain.systems.vault import async_approve_agent_grant as _approve

    result = await _approve(
        grant_id,
        approved_by_user_id=_require_user_id(user),
        org_id=_org_id(user),
        ttl_minutes=body.ttl_minutes,
        max_reads=body.max_reads,
    )
    if result is None:
        raise HTTPException(status_code=404, detail="Grant not found")
    return result


@router.post("/agent-grants/{grant_id}/deny")
async def deny_agent_grant(
    grant_id: int,
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
):
    await _async_require_unlocked(request, user)
    from brain.systems.vault import async_deny_agent_grant as _deny

    result = await _deny(grant_id, denied_by_user_id=_require_user_id(user), org_id=_org_id(user))
    if result is None:
        raise HTTPException(status_code=404, detail="Grant not found")
    return result


@router.get("/project-bindings", response_model=list[VaultProjectBindingRead])
async def list_project_bindings(
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
):
    await _async_require_unlocked(request, user)
    from brain.systems.vault import async_list_project_bindings as _list_bindings

    return await _list_bindings(user_id=_require_user_id(user), org_id=_org_id(user))


@router.post("/{secret_id}/project-bindings", response_model=VaultProjectBindingRead, status_code=201)
async def bind_project_secret(
    secret_id: int,
    body: VaultProjectBindingCreate,
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
):
    await _async_require_unlocked(request, user)
    from brain.systems.vault import async_bind_project_secret as _bind_project_secret

    try:
        result = await _bind_project_secret(
            secret_id,
            user_id=_require_user_id(user),
            org_id=_org_id(user),
            project_slug=body.project_slug,
            env_name=body.env_name,
            target_registry_id=body.target_registry_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="Secret not found or not owned by you")
    return result


@router.delete("/project-bindings/{binding_id}")
async def delete_project_binding(
    binding_id: int,
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
):
    await _async_require_unlocked(request, user)
    from brain.systems.vault import async_delete_project_binding as _delete_project_binding

    deleted = await _delete_project_binding(binding_id, user_id=_require_user_id(user), org_id=_org_id(user))
    if not deleted:
        raise HTTPException(status_code=404, detail="Project binding not found")
    return {"deleted": True}


@router.get("/", response_model=list[SecretRead])
async def list_secrets(
    request: Request,
    category: str | None = None,
    user: dict[str, Any] = Depends(get_current_user),
):
    await _async_require_unlocked(request, user)
    from brain.systems.vault import async_list_secrets as _list

    return await _list(_require_user_id(user), category=category, org_id=_org_id(user))


@router.get("/{key_name}", response_model=SecretReveal)
async def reveal_secret(
    key_name: str,
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
):
    await _async_require_unlocked(request, user)
    from brain.systems.vault import async_reveal_secret as _reveal

    try:
        value = await _reveal(key_name, user_id=_require_user_id(user), org_id=_org_id(user))
    except RuntimeError as exc:
        _raise_if_vault_not_configured(exc)
        raise
    if value is None:
        raise HTTPException(status_code=404, detail="Secret not found")
    return SecretReveal(key_name=key_name, value=value)


@router.post("/", response_model=SecretRead, status_code=201)
async def create_secret(
    body: SecretCreate,
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
):
    await _async_require_unlocked(request, user)
    from brain.systems.vault import async_get_secret_record, async_set_secret

    user_id = _require_user_id(user)
    try:
        await async_set_secret(
            key_name=body.key_name,
            value=body.value,
            user_id=user_id,
            org_id=_org_id(user),
            description=body.description,
            category=body.category,
            agent_access_level=body.agent_access_level,
        )
    except RuntimeError as exc:
        _raise_if_vault_not_configured(exc)
        raise
    secret = await async_get_secret_record(body.key_name, user_id)
    if not secret:
        raise HTTPException(status_code=500, detail="Failed to create secret")
    if not isinstance(getattr(secret, "agent_access_level", None), str):
        secret.agent_access_level = body.agent_access_level
    return secret


@router.delete("/{key_name}")
async def delete_secret(
    key_name: str,
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
):
    await _async_require_unlocked(request, user)
    from brain.systems.vault import async_delete_secret as _delete

    deleted = await _delete(key_name, user_id=_require_user_id(user))
    if not deleted:
        raise HTTPException(status_code=404, detail="Secret not found")
    return {"deleted": True}


@router.post("/{secret_id}/share", response_model=dict)
async def share_secret(
    secret_id: int,
    body: VaultShareCreate,
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
):
    await _async_require_unlocked(request, user)
    _require_vault_share(user)
    from brain.systems.vault import async_share_secret as _share

    result = await _share(
        secret_id,
        body.shared_with_user_id,
        _require_user_id(user),
        org_id=_org_id(user),
    )
    if result is None:
        raise HTTPException(status_code=404, detail="Secret not found, not owned by you, or recipient outside org")
    return result
