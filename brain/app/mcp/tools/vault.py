"""Vault MCP tool implementations."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode


VAULT_PROMPT_CATEGORIES = {
    "general",
    "api",
    "aws",
    "auth",
    "analytics",
    "database",
    "messaging",
    "monitoring",
    "payments",
    "service",
}


def clean_vault_prompt_text(value: str | None, *, max_chars: int = 240) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "..."


def normalize_vault_key_name(key_name: str) -> str:
    cleaned = str(key_name or "").strip()
    if not cleaned:
        raise ValueError("key_name is required")
    return cleaned.upper()


def safe_vault_secret_summary(secret: dict[str, Any]) -> dict[str, Any]:
    return {
        "key_name": str(secret.get("key_name") or ""),
        "description": str(secret.get("description") or ""),
        "category": str(secret.get("category") or "general"),
        "agent_access_level": str(secret.get("agent_access_level") or "ask"),
    }


def vault_prompt_url(
    *,
    key_name: str,
    description: str,
    category: str,
) -> str:
    return "/vault?" + urlencode({
        "add_secret": key_name,
        "description": description,
        "category": category,
    })


async def brain_vault_tool(
    key: str,
    reason: str | None = None,
    user_id: str | None = None,
    org_id: str | None = None,
    run_id: int | None = None,
    idea_id: str | None = None,
    requested_by: str = "agent",
    project_slug: str | None = None,
    project_slugs: list[str] | tuple[str, ...] | set[str] | None = None,
    target_registry_id: int | None = None,
    *,
    json_safe: Any,
) -> dict:
    """Retrieve a secret from the vault."""
    from brain.systems.cortex.events import publish_safe
    from brain.systems.vault import authorize_agent_secret_read, get_secret

    if not user_id:
        return {"error": "Vault access requires an authenticated user context"}
    target_user_id = str(user_id).strip()
    if not target_user_id:
        return {"error": "Vault access requires an authenticated user context"}
    authorization = await authorize_agent_secret_read(
        key,
        actor_user_id=target_user_id,
        org_id=org_id,
        run_id=run_id,
        reason=reason,
        requested_by=requested_by,
        project_slug=project_slug,
        project_slugs=project_slugs,
        target_registry_id=target_registry_id,
    )
    if not authorization.get("allowed"):
        grant = json_safe(authorization.get("grant") or {})
        grant_user_id = str(grant.get("requested_by_user_id") or target_user_id).strip() or target_user_id
        if authorization.get("status") == "pending":
            normalized_idea_id = (str(idea_id).strip() if idea_id else "") or None
            prompt = None
            if normalized_idea_id:
                prompt = {
                    "id": f"vault-grant-{grant.get('id') or run_id or 'thread'}",
                    "idea_id": normalized_idea_id,
                    "org_id": org_id,
                    "target_user_id": grant_user_id,
                    "run_id": grant.get("run_id") or run_id,
                    "grant_id": grant.get("id"),
                    "key_name": grant.get("key_name") or key,
                    "requested_by": grant.get("requested_by") or requested_by,
                    "reason": grant.get("reason") or reason,
                    "requested_at": grant.get("requested_at"),
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
                publish_safe("vault_agent_grant_prompt", {
                    "idea_id": normalized_idea_id,
                    "org_id": org_id,
                    "target_user_id": grant_user_id,
                    "run_id": grant.get("run_id") or run_id,
                    "grant": grant,
                    "prompt": prompt,
                })
            response = {
                "error": "Vault grant required before this agent can read the secret",
                "grant_id": grant.get("id"),
                "key_name": grant.get("key_name") or key,
                "reason": grant.get("reason") or reason,
                "requested_by": grant.get("requested_by") or requested_by,
                "run_id": grant.get("run_id") or run_id,
                "status": "pending",
                "target_user_id": grant_user_id,
            }
            if prompt:
                response["prompt"] = prompt
            return response
        return {"error": authorization.get("reason") or "Vault grant denied"}
    value = await get_secret(
        key,
        actor_user_id=target_user_id,
        org_id=org_id,
        accessed_by="agent",
    )
    if value is None:
        return {"error": f"Secret '{key}' not found in vault"}
    return {"key": key, "value": value}


async def vault_inventory_tool(
    category: str | None = None,
    access_level: str | None = None,
    user_id: str | None = None,
    org_id: str | None = None,
) -> dict:
    """List safe Vault metadata so the agent can choose an existing key."""
    from brain.systems.vault import async_list_secrets

    if not user_id:
        return {"error": "Vault inventory requires an authenticated user context"}
    normalized_user_id = str(user_id).strip()
    if not normalized_user_id:
        return {"error": "Vault inventory requires an authenticated user context"}
    normalized_org_id = (str(org_id).strip() if org_id else "") or None
    normalized_category = str(category or "").strip().lower() or None
    normalized_access_level = str(access_level or "").strip().lower() or None

    secrets = [
        safe_vault_secret_summary(secret)
        for secret in await async_list_secrets(
            actor_user_id=normalized_user_id,
            org_id=normalized_org_id,
            category=normalized_category,
        )
    ]
    if normalized_access_level:
        secrets = [
            secret
            for secret in secrets
            if secret["agent_access_level"].strip().lower() == normalized_access_level
        ]
    secrets.sort(key=lambda secret: (secret["category"], secret["key_name"]))
    return {
        "secrets": secrets,
        "count": len(secrets),
        "metadata_only": True,
        "guidance": (
            "Use these names/descriptions/categories to decide which exact key to request with brain_vault. "
            "If no suitable key exists, ask the user or call vault_secret_prompt."
        ),
    }


async def vault_secret_prompt_tool(
    key_name: str,
    description: str | None = None,
    category: str = "api",
    reason: str | None = None,
    user_id: str | None = None,
    org_id: str | None = None,
    run_id: int | None = None,
    idea_id: str | None = None,
    requested_by: str = "agent",
) -> dict:
    """Open a guided Vault form for a user-supplied secret value."""
    from brain.systems.cortex.events import publish_safe
    from brain.systems.vault import record_missing_request

    if not user_id:
        return {"error": "Vault secret prompts require an authenticated user context"}

    normalized_user_id = str(user_id).strip()
    if not normalized_user_id:
        return {"error": "Vault secret prompts require an authenticated user context"}
    normalized_org_id = (str(org_id).strip() if org_id else "") or None
    normalized_idea_id = (str(idea_id).strip() if idea_id else "") or None

    try:
        normalized_key = normalize_vault_key_name(key_name)
    except ValueError as exc:
        return {"error": str(exc)}

    normalized_category = str(category or "api").strip().lower() or "api"
    if normalized_category not in VAULT_PROMPT_CATEGORIES:
        normalized_category = "general"
    clean_description = clean_vault_prompt_text(
        description or f"Credential requested by Illo for {normalized_key}.",
    )
    clean_reason = clean_vault_prompt_text(reason or clean_description, max_chars=360)
    clean_requested_by = clean_vault_prompt_text(requested_by or "agent", max_chars=80) or "agent"

    prompt = {
        "id": f"vault-secret-{run_id or 'thread'}-{uuid.uuid4().hex[:10]}",
        "idea_id": normalized_idea_id,
        "org_id": normalized_org_id,
        "run_id": run_id,
        "key_name": normalized_key,
        "description": clean_description,
        "category": normalized_category,
        "reason": clean_reason,
        "requested_by": clean_requested_by,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    await record_missing_request(normalized_key, actor_user_id=normalized_user_id, org_id=normalized_org_id)

    if normalized_idea_id:
        publish_safe("vault_secret_prompt", {
            "idea_id": normalized_idea_id,
            "org_id": normalized_org_id,
            "run_id": run_id,
            "prompt": prompt,
            "key_name": normalized_key,
            "description": clean_description,
            "category": normalized_category,
            "reason": clean_reason,
            "requested_by": clean_requested_by,
        })

    response = {
        "prompted": bool(normalized_idea_id),
        "status": "opened" if normalized_idea_id else "recorded",
        "key_name": normalized_key,
        "description": clean_description,
        "category": normalized_category,
        "prompt": prompt,
        "vault_url": vault_prompt_url(
            key_name=normalized_key,
            description=clean_description,
            category=normalized_category,
        ),
    }
    if not normalized_idea_id:
        response["warning"] = (
            "No current Cortex thread was bound, so the missing key was recorded for Vault."
        )
    return response
