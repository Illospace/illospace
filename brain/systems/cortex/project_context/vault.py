"""Vault access for backend-owned Project Context connectors."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from brain.systems.vault import (
    VAULT_UNLOCK_HEADER,
    async_get_secret,
    async_has_pin,
    async_validate_vault_token,
    get_secret,
    has_pin,
    validate_vault_token,
)


@dataclass
class ProjectContextVaultError(Exception):
    status_code: int
    detail: str


def github_token_from_vault(
    key_name: str,
    *,
    user: dict[str, Any],
    unlock_token: str | None,
    allow_shared: bool = True,
) -> str:
    user_id = str(user.get("id") or "")
    if not user_id or user_id.startswith("service:"):
        raise ProjectContextVaultError(403, "GitHub Vault tokens require a human user")
    if has_pin(user_id) and not validate_vault_token(user_id, unlock_token or ""):
        raise ProjectContextVaultError(423, "Vault locked")
    try:
        read_kwargs: dict[str, Any] = {
            "user_id": user_id,
            "org_id": str(user.get("org_id")) if user.get("org_id") else None,
            "accessed_by": "api",
        }
        if not allow_shared:
            read_kwargs["allow_shared"] = False
        token = get_secret(key_name.strip(), **read_kwargs)
    except RuntimeError as exc:
        if "VAULT_MASTER_KEY is required" in str(exc):
            raise ProjectContextVaultError(
                503,
                "Vault master key is not configured. Set VAULT_MASTER_KEY before using GitHub tokens.",
            ) from exc
        raise
    if not token:
        raise ProjectContextVaultError(404, "GitHub token not found in Vault")
    return token.strip()


async def async_github_token_from_vault(
    key_name: str,
    *,
    user: dict[str, Any],
    unlock_token: str | None,
    allow_shared: bool = True,
) -> str:
    user_id = str(user.get("id") or "")
    if not user_id or user_id.startswith("service:"):
        raise ProjectContextVaultError(403, "GitHub Vault tokens require a human user")
    if await async_has_pin(user_id) and not await async_validate_vault_token(user_id, unlock_token or ""):
        raise ProjectContextVaultError(423, "Vault locked")
    try:
        read_kwargs: dict[str, Any] = {
            "user_id": user_id,
            "org_id": str(user.get("org_id")) if user.get("org_id") else None,
            "accessed_by": "api",
        }
        if not allow_shared:
            read_kwargs["allow_shared"] = False
        token = await async_get_secret(key_name.strip(), **read_kwargs)
    except RuntimeError as exc:
        if "VAULT_MASTER_KEY is required" in str(exc):
            raise ProjectContextVaultError(
                503,
                "Vault master key is not configured. Set VAULT_MASTER_KEY before using GitHub tokens.",
            ) from exc
        raise
    if not token:
        raise ProjectContextVaultError(404, "GitHub token not found in Vault")
    return token.strip()
