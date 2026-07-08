"""Vault router — org secret management, lock state, and audit."""
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from brain.app.api.auth import get_current_user
from brain.app.api.authorization import can_audit_vault, require_org_context
from brain.app.api.deps import get_db, rate_limit
from brain.app.api.schemas.vault import (
    SecretCreate,
    SecretRead,
    SecretReveal,
    VaultProjectBindingCreate,
    VaultProjectBindingRead,
    validate_github_app_secret_value,
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
    model_config = {"hide_input_in_errors": True}


class AgentGrantApproval(BaseModel):
    ttl_minutes: int = Field(default=15, ge=1, le=60)
    max_reads: int = Field(default=1, ge=1, le=25)


router = APIRouter(
    prefix="/api/vault",
    tags=["vault"],
    dependencies=[Depends(rate_limit)],
)


def _require_actor_user_id(user: dict[str, Any]) -> str:
    user_id = str(user.get("id") or "")
    if not user_id or user_id.startswith("service:"):
        raise HTTPException(status_code=403, detail="Vault access requires a human user")
    return user_id


def _require_org_id(user: dict[str, Any]) -> str:
    return require_org_context(user)


def _vault_identity(user: dict[str, Any]) -> tuple[str, str]:
    return _require_org_id(user), _require_actor_user_id(user)


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


def _masked_github_app_reveal(value: str) -> str:
    try:
        payload = json.loads(value)
        if not isinstance(payload, dict):
            raise ValueError
        app_id = str(int(str(payload.get("app_id") or "").strip()))
        installation_id = str(int(str(payload.get("installation_id") or "").strip()))
    except (TypeError, ValueError, json.JSONDecodeError):
        raise HTTPException(
            status_code=422,
            detail="GitHub App credential cannot be revealed because the stored value is invalid.",
        ) from None

    masked = {
        "app_id": app_id,
        "installation_id": installation_id,
    }
    client_id = payload.get("client_id")
    if isinstance(client_id, str):
        clean_client_id = client_id.strip()
        if clean_client_id and "\n" not in clean_client_id and len(clean_client_id) <= 128:
            masked["client_id"] = clean_client_id
    return json.dumps(masked, sort_keys=True)


def _github_app_update_422(detail: str) -> HTTPException:
    return HTTPException(status_code=422, detail=detail)


async def _async_require_unlocked(request: Request, user: dict[str, Any]) -> None:
    from brain.systems.vault import async_has_pin, async_validate_vault_token

    org_id, actor_user_id = _vault_identity(user)
    if not await async_has_pin(org_id, actor_user_id):
        raise HTTPException(status_code=423, detail="Vault PIN setup required")
    if not await async_validate_vault_token(org_id, actor_user_id, _vault_token(request)):
        raise HTTPException(status_code=423, detail="Vault locked")


@router.get("/pin-status")
async def get_pin_status(user: dict[str, Any] = Depends(get_current_user)):
    from brain.systems.vault import async_get_pin_status as _status

    org_id, actor_user_id = _vault_identity(user)
    return await _status(org_id, actor_user_id)


@router.post("/setup-pin")
async def setup_pin(body: PinSetup, user: dict[str, Any] = Depends(get_current_user)):
    from brain.systems.vault import async_set_pin

    org_id, actor_user_id = _vault_identity(user)
    if not await async_set_pin(org_id, actor_user_id, body.new_pin, body.current_pin):
        raise HTTPException(status_code=403, detail="Current PIN is incorrect")
    return {"success": True}


@router.post("/unlock")
async def unlock_vault(body: PinUnlock, user: dict[str, Any] = Depends(get_current_user)):
    from brain.systems.vault import async_unlock_vault as _unlock

    org_id, actor_user_id = _vault_identity(user)
    unlocked = await _unlock(org_id, actor_user_id, body.pin)
    if not unlocked:
        raise HTTPException(status_code=403, detail="Incorrect PIN or vault locked")
    token, expires = unlocked
    return {"unlocked": True, "token": token, "expires_at": expires}


@router.post("/lock")
async def lock_vault(request: Request, user: dict[str, Any] = Depends(get_current_user)):
    from brain.systems.vault import async_revoke_vault_token

    org_id, actor_user_id = _vault_identity(user)
    await async_revoke_vault_token(org_id, actor_user_id, _vault_token(request))
    return {"locked": True}


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

    org_id, actor_user_id = _vault_identity(user)
    from brain.platform.db.repositories.vault import VaultRepository

    repo = VaultRepository(db)
    secret = await repo.a_get_by_key(org_id, key_name)
    if not secret:
        raise HTTPException(status_code=404, detail="Secret not found")
    updates = body.model_dump(exclude_unset=True)
    effective_category = updates.get("category", getattr(secret, "category", None))
    effective_level = updates.get("agent_access_level", getattr(secret, "agent_access_level", None))
    if effective_category == "github_app":
        try:
            normalized_level = normalize_agent_access_level(effective_level)
        except ValueError as exc:
            raise _github_app_update_422(str(exc)) from exc
        if normalized_level != "manual":
            raise _github_app_update_422("github_app secrets must be stored with agent_access_level 'manual'")
        if "value" in updates:
            try:
                validate_github_app_secret_value(updates["value"])
            except ValueError as exc:
                raise _github_app_update_422(str(exc)) from exc
    if "value" in updates:
        try:
            await async_set_secret(
                key_name=key_name,
                value=updates.pop("value"),
                actor_user_id=actor_user_id,
                org_id=org_id,
                description=updates.get("description", secret.description),
                category=updates.get("category", secret.category),
                agent_access_level=updates.get("agent_access_level"),
            )
        except RuntimeError as exc:
            _raise_if_vault_not_configured(exc)
            raise
    else:
        for k, v in updates.items():
            if k == "agent_access_level":
                v = normalize_agent_access_level(v)
            setattr(secret, k, v)
        await db.flush()
    return {"updated": True}


@router.get("/missing")
async def list_missing(
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
):
    await _async_require_unlocked(request, user)
    _require_vault_audit(user)
    from brain.systems.vault import async_get_missing_requests

    org_id, actor_user_id = _vault_identity(user)
    return await async_get_missing_requests(actor_user_id=actor_user_id, org_id=org_id)


@router.get("/log", response_model=list)
async def get_access_log(
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
):
    await _async_require_unlocked(request, user)
    _require_vault_audit(user)
    from brain.systems.vault import async_get_vault_access_log

    org_id, actor_user_id = _vault_identity(user)
    return await async_get_vault_access_log(actor_user_id, org_id=org_id, limit=100)


@router.get("/agent-grants")
async def list_agent_grants(
    request: Request,
    status: str | None = None,
    user: dict[str, Any] = Depends(get_current_user),
):
    await _async_require_unlocked(request, user)
    from brain.systems.vault import async_list_agent_grants as _list_grants

    statuses = [part.strip() for part in status.split(",") if part.strip()] if status else None
    org_id, actor_user_id = _vault_identity(user)
    return await _list_grants(actor_user_id, org_id=org_id, statuses=statuses)


@router.post("/agent-grants/{grant_id}/approve")
async def approve_agent_grant(
    grant_id: int,
    body: AgentGrantApproval,
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
):
    await _async_require_unlocked(request, user)
    from brain.systems.vault import async_approve_agent_grant as _approve

    org_id, actor_user_id = _vault_identity(user)
    result = await _approve(
        grant_id,
        approved_by_user_id=actor_user_id,
        org_id=org_id,
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

    org_id, actor_user_id = _vault_identity(user)
    result = await _deny(grant_id, denied_by_user_id=actor_user_id, org_id=org_id)
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

    org_id, actor_user_id = _vault_identity(user)
    return await _list_bindings(actor_user_id=actor_user_id, org_id=org_id)


@router.post("/{secret_id}/project-bindings", response_model=VaultProjectBindingRead, status_code=201)
async def bind_project_secret(
    secret_id: int,
    body: VaultProjectBindingCreate,
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
):
    await _async_require_unlocked(request, user)
    from brain.systems.vault import async_bind_project_secret as _bind_project_secret

    org_id, actor_user_id = _vault_identity(user)
    try:
        result = await _bind_project_secret(
            secret_id,
            actor_user_id=actor_user_id,
            org_id=org_id,
            project_slug=body.project_slug,
            env_name=body.env_name,
            target_registry_id=body.target_registry_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="Secret not found in org vault")
    return result


@router.delete("/project-bindings/{binding_id}")
async def delete_project_binding(
    binding_id: int,
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
):
    await _async_require_unlocked(request, user)
    from brain.systems.vault import async_delete_project_binding as _delete_project_binding

    org_id, actor_user_id = _vault_identity(user)
    deleted = await _delete_project_binding(
        binding_id,
        actor_user_id=actor_user_id,
        org_id=org_id,
    )
    if not deleted:
        raise HTTPException(status_code=404, detail="Project binding not found")
    return {"deleted": True}


@router.get("/", response_model=list[SecretRead])
async def list_secrets(
    category: str | None = None,
    user: dict[str, Any] = Depends(get_current_user),
):
    from brain.systems.vault import async_list_secrets as _list

    org_id, actor_user_id = _vault_identity(user)
    return await _list(actor_user_id, category=category, org_id=org_id)


@router.get("/{key_name}", response_model=SecretReveal)
async def reveal_secret(
    key_name: str,
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
):
    await _async_require_unlocked(request, user)
    from brain.systems.vault import async_get_secret_record, async_reveal_secret as _reveal

    org_id, actor_user_id = _vault_identity(user)
    try:
        secret = await async_get_secret_record(key_name, actor_user_id, org_id=org_id)
        value = await _reveal(key_name, actor_user_id=actor_user_id, org_id=org_id)
    except RuntimeError as exc:
        _raise_if_vault_not_configured(exc)
        raise
    if value is None:
        raise HTTPException(status_code=404, detail="Secret not found")
    if getattr(secret, "category", None) == "github_app":
        value = _masked_github_app_reveal(value)
    return SecretReveal(key_name=key_name, value=value)


@router.post("/", response_model=SecretRead, status_code=201)
async def create_secret(
    body: SecretCreate,
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
):
    await _async_require_unlocked(request, user)
    from brain.systems.vault import async_get_secret_record, async_set_secret

    org_id, actor_user_id = _vault_identity(user)
    try:
        await async_set_secret(
            key_name=body.key_name,
            value=body.value,
            actor_user_id=actor_user_id,
            org_id=org_id,
            description=body.description,
            category=body.category,
            agent_access_level=body.agent_access_level,
        )
    except RuntimeError as exc:
        _raise_if_vault_not_configured(exc)
        raise
    secret = await async_get_secret_record(body.key_name, actor_user_id, org_id=org_id)
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

    org_id, actor_user_id = _vault_identity(user)
    deleted = await _delete(key_name, actor_user_id=actor_user_id, org_id=org_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Secret not found")
    return {"deleted": True}
