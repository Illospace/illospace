"""Vault access for backend-owned Project Context connectors."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from brain.systems.vault import (
    VAULT_UNLOCK_HEADER,
    async_get_secret,
    async_has_pin,
    async_validate_vault_token,
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
) -> str:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(
            async_github_token_from_vault(
                key_name,
                user=user,
                unlock_token=unlock_token,
            )
        )
    raise ProjectContextVaultError(500, "Use async_github_token_from_vault inside async request handlers")


async def async_github_token_from_vault(
    key_name: str,
    *,
    user: dict[str, Any],
    unlock_token: str | None,
) -> str:
    user_id = str(user.get("id") or "")
    org_id = str(user.get("org_id") or "")
    if not user_id or user_id.startswith("service:"):
        raise ProjectContextVaultError(403, "GitHub Vault tokens require a human user")
    if not org_id:
        raise ProjectContextVaultError(403, "GitHub Vault tokens require an org")
    if not await async_has_pin(org_id, user_id):
        raise ProjectContextVaultError(423, "Vault PIN setup required")
    if not await async_validate_vault_token(org_id, user_id, unlock_token or ""):
        raise ProjectContextVaultError(423, "Vault locked")
    try:
        read_kwargs: dict[str, Any] = {
            "actor_user_id": user_id,
            "org_id": org_id,
            "accessed_by": "api",
        }
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
