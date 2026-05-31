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
    key = str(key_name or "").strip().upper()
    if not key:
        raise RuntimeSecretUnavailable("Vault secret key name is required")
    return key


def _clean_env_names(key_name: str, env_names: tuple[str, ...] | list[str] | None) -> tuple[str, ...]:
    names = tuple(str(name or "").strip() for name in (env_names or (key_name,)))
    return tuple(name for name in names if name)


def _env_secret(env_names: tuple[str, ...]) -> str | None:
    for env_name in env_names:
        value = os.environ.get(env_name, "").strip()
        if value:
            return value
    return None


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
    env_candidates = _clean_env_names(key, env_names)

    actor_user_id = str(context.actor_user_id or "").strip()
    org_id = str(context.org_id or "").strip()

    if access == "service":
        if actor_user_id and org_id:
            from brain.systems.vault import get_secret

            value = await get_secret(
                key,
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

    response = await read_agent_secret_for_runtime(
        key,
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
        raise RuntimeSecretUnavailable(f"Vault secret '{key}' could not be read")
    if response.get("error"):
        raise RuntimeSecretUnavailable(str(response.get("error")))
    value = response.get("value")
    if value is None:
        raise RuntimeSecretUnavailable(f"Vault secret '{key}' did not return a value")
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
