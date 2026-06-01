"""Central runtime secret resolution for trusted Illospace tool execution."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal


RuntimeSecretAccess = Literal["agent_tool", "service"]


class RuntimeSecretUnavailable(PermissionError):
    """Raised when a runtime tool cannot resolve a required secret."""


@dataclass(frozen=True)
class RuntimeSecretContext:
    actor_user_id: str | None
    org_id: str | None
    run_id: int | None = None
    idea_id: str | None = None
    project_slug: str | None = None
    project_slugs: list[str] | tuple[str, ...] | set[str] | None = None
    target_registry_id: int | None = None


def _clean_key_name(key_name: str) -> str:
    key = str(key_name or "").strip()
    if not key:
        raise RuntimeSecretUnavailable("Vault secret key name is required")
    return key


def _vault_key_candidates(key_name: str) -> tuple[str, ...]:
    """Return Vault lookup candidates, preserving exact keys before legacy uppercase aliases."""

    key = _clean_key_name(key_name)
    candidates = [key]
    upper_key = key.upper()
    if upper_key != key:
        candidates.append(upper_key)
    return tuple(dict.fromkeys(candidates))


def _clean_env_names(key_name: str, env_names: tuple[str, ...] | list[str] | None) -> tuple[str, ...]:
    raw_names = tuple(str(name or "").strip() for name in (env_names or (key_name,)))
    names: list[str] = []
    for name in raw_names:
        if not name:
            continue
        names.append(name)
        upper_name = name.upper()
        if upper_name != name:
            names.append(upper_name)
    return tuple(dict.fromkeys(names))


def _env_secret(env_names: tuple[str, ...]) -> str | None:
    for env_name in env_names:
        value = os.environ.get(env_name, "").strip()
        if value:
            return value
    return None


async def _select_existing_vault_key_candidate(
    candidates: tuple[str, ...],
    *,
    actor_user_id: str,
    org_id: str,
) -> str:
    """Choose an existing Vault key without recording false missing-key requests."""

    if len(candidates) <= 1:
        return candidates[0]

    from brain.systems.vault import async_get_secret_record

    for candidate in candidates:
        secret = await async_get_secret_record(
            candidate,
            actor_user_id=actor_user_id,
            org_id=org_id,
        )
        if secret is not None:
            return candidate
    return candidates[0]


async def read_runtime_secret(
    key_name: str,
    *,
    context: RuntimeSecretContext,
    reason: str,
    requested_by: str,
    access: RuntimeSecretAccess = "agent_tool",
    allow_env_fallback: bool = False,
    env_names: tuple[str, ...] | list[str] | None = None,
) -> str:
    """Resolve a secret for trusted runtime code without exposing it to the agent."""

    key = _clean_key_name(key_name)
    key_candidates = _vault_key_candidates(key)
    env_candidates = _clean_env_names(key, env_names)

    actor_user_id = str(context.actor_user_id or "").strip()
    org_id = str(context.org_id or "").strip()

    if access == "service":
        if actor_user_id and org_id:
            from brain.systems.vault import get_secret

            candidate = await _select_existing_vault_key_candidate(
                key_candidates,
                actor_user_id=actor_user_id,
                org_id=org_id,
            )
            value = await get_secret(
                candidate,
                actor_user_id=actor_user_id,
                org_id=org_id,
                accessed_by=requested_by,
            )
            if value:
                return str(value)
        if allow_env_fallback:
            value = _env_secret(env_candidates)
            if value:
                return value
        raise RuntimeSecretUnavailable(f"Vault secret '{key}' is not available to the runtime")

    if access != "agent_tool":
        raise RuntimeSecretUnavailable(f"Unknown runtime secret access mode: {access}")

    if not actor_user_id:
        raise RuntimeSecretUnavailable("Runtime secret reads require an authenticated user context")
    if not org_id:
        raise RuntimeSecretUnavailable("Runtime secret reads require a workspace org context")
    if context.run_id is None:
        raise RuntimeSecretUnavailable("Runtime secret reads require an AgentRun id")

    from brain.systems.vault.agent_access import read_agent_secret_for_runtime

    candidate = await _select_existing_vault_key_candidate(
        key_candidates,
        actor_user_id=actor_user_id,
        org_id=org_id,
    )
    response = await read_agent_secret_for_runtime(
        candidate,
        reason=reason,
        user_id=actor_user_id,
        org_id=org_id,
        run_id=int(context.run_id),
        idea_id=context.idea_id,
        requested_by=requested_by,
        project_slug=context.project_slug,
        project_slugs=context.project_slugs,
        target_registry_id=context.target_registry_id,
    )
    if not isinstance(response, dict):
        raise RuntimeSecretUnavailable(f"Vault secret '{candidate}' could not be read")
    if response.get("error"):
        raise RuntimeSecretUnavailable(str(response.get("error")))
    value = response.get("value")
    if value is None:
        raise RuntimeSecretUnavailable(f"Vault secret '{candidate}' did not return a value")
    return str(value)


async def runtime_secret_env(
    mounts: dict[str, tuple[str, str]],
    *,
    context: RuntimeSecretContext,
    requested_by: str,
) -> dict[str, str]:
    """Resolve env-name -> (vault-key, reason) mounts through the central resolver."""

    resolved: dict[str, str] = {}
    for env_name, (vault_key, reason) in mounts.items():
        resolved[env_name] = await read_runtime_secret(
            vault_key,
            context=context,
            reason=reason,
            requested_by=requested_by,
            access="agent_tool",
            allow_env_fallback=False,
        )
    return resolved
