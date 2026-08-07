"""Agent-facing Vault access helpers.

This module keeps the public reference path separate from trusted runtime reads.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from brain.platform.events import publish_safe


def _json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    return value


def _grant_prompt_response(
    key_name: str,
    authorization: dict,
    *,
    target_user_id: str,
    org_id: str | None,
    run_id: int | None,
    idea_id: str | None,
    requested_by: str,
    reason: str | None,
) -> dict:
    grant = _json_safe(authorization.get("grant") or {})
    grant_user_id = str(grant.get("requested_by_user_id") or target_user_id).strip() or target_user_id
    if authorization.get("status") != "pending":
        return {"error": authorization.get("reason") or "Vault grant denied"}

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
            "key_name": grant.get("key_name") or key_name,
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
        "key_name": grant.get("key_name") or key_name,
        "reason": grant.get("reason") or reason,
        "requested_by": grant.get("requested_by") or requested_by,
        "run_id": grant.get("run_id") or run_id,
        "status": "pending",
        "target_user_id": grant_user_id,
    }
    if prompt:
        response["prompt"] = prompt
    return response


def _available_reference(key_name: str) -> dict:
    return {
        "key": key_name,
        "status": "available",
        "secret_ref": f"vault:{key_name}",
        "usage": (
            "Raw secret values are not returned to agents. For command/API work, pass this "
            "Vault key through exec_command or run_script secret_env so the trusted runtime "
            "can mount it for one tool call."
        ),
    }


async def request_agent_secret_reference(
    key_name: str,
    *,
    reason: str | None,
    user_id: str | None,
    org_id: str | None,
    run_id: int | None,
    idea_id: str | None,
    requested_by: str,
    project_slug: str | None = None,
    project_slugs: list[str] | tuple[str, ...] | set[str] | None = None,
    target_registry_id: int | None = None,
) -> dict:
    """Return a safe Vault reference without reading the secret value."""
    from brain.systems.vault import authorize_agent_secret_reference

    if not user_id or not str(user_id).strip():
        return {"error": "Vault access requires an authenticated user context"}
    target_user_id = str(user_id).strip()
    authorization = await authorize_agent_secret_reference(
        key_name,
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
        return _grant_prompt_response(
            key_name,
            authorization,
            target_user_id=target_user_id,
            org_id=org_id,
            run_id=run_id,
            idea_id=idea_id,
            requested_by=requested_by,
            reason=reason,
        )
    return _available_reference(key_name)


async def read_agent_secret_for_runtime(
    key_name: str,
    *,
    reason: str,
    user_id: str,
    org_id: str | None,
    run_id: int,
    idea_id: str | None,
    requested_by: str = "secret_env_mount",
    project_slug: str | None = None,
    project_slugs: list[str] | tuple[str, ...] | set[str] | None = None,
    target_registry_id: int | None = None,
) -> dict:
    """Read a Vault secret for a trusted runtime mount."""
    from brain.systems.vault import authorize_agent_secret_read, get_secret

    authorization = await authorize_agent_secret_read(
        key_name,
        actor_user_id=user_id,
        org_id=org_id,
        run_id=run_id,
        reason=reason,
        requested_by=requested_by,
        project_slug=project_slug,
        project_slugs=project_slugs,
        target_registry_id=target_registry_id,
    )
    if not authorization.get("allowed"):
        return _grant_prompt_response(
            key_name,
            authorization,
            target_user_id=user_id,
            org_id=org_id,
            run_id=run_id,
            idea_id=idea_id,
            requested_by=requested_by,
            reason=reason,
        )

    value = await get_secret(
        key_name,
        actor_user_id=user_id,
        org_id=org_id,
        accessed_by="agent",
    )
    if value is None:
        return {"error": f"Secret '{key_name}' not found in vault"}
    return {"key": key_name, "value": value}
