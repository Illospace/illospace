"""Run-scoped secret environment mounts for trusted tool execution."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any


SECRET_ENV_MOUNT_TOOLS = frozenset({"exec_command", "run_script", "test_runner"})
RESOLVED_SECRET_ENV_ARG = "_resolved_secret_env"
SECRET_ENV_SCHEMA = {
    "type": "object",
    "description": (
        "Optional run-scoped Vault secret mounts. Map environment variable names to "
        "{vault_key, reason}. The trusted runtime resolves the secret value, injects it only "
        "for this tool call, and redacts it from stdout/stderr/artifacts. Never put raw "
        "secret values here."
    ),
    "additionalProperties": {
        "type": "object",
        "properties": {
            "vault_key": {"type": "string", "description": "Vault key name to mount"},
            "reason": {"type": "string", "description": "Why this tool call needs the secret"},
        },
        "required": ["vault_key"],
        "additionalProperties": False,
    },
}

_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_MAX_SECRET_ENV_MOUNTS = 20


@dataclass(frozen=True)
class SecretEnvMount:
    env_name: str
    vault_key: str
    reason: str


def _default_mount_reason(env_name: str, vault_key: str) -> str:
    return f"Mount Vault key {vault_key} as environment variable {env_name} for this tool call."


def normalize_secret_env_mounts(raw: Any) -> dict[str, SecretEnvMount]:
    """Validate agent-authored mount specs without accepting shorthand shapes."""
    if raw in (None, {}, []):
        return {}
    if not isinstance(raw, dict):
        raise ValueError("secret_env must be an object mapping environment variable names to mount specs")
    if len(raw) > _MAX_SECRET_ENV_MOUNTS:
        raise ValueError(f"secret_env supports at most {_MAX_SECRET_ENV_MOUNTS} mounts")

    mounts: dict[str, SecretEnvMount] = {}
    for raw_env_name, raw_spec in raw.items():
        env_name = str(raw_env_name or "").strip()
        if not _ENV_NAME_RE.fullmatch(env_name):
            raise ValueError(f"Invalid secret_env variable name: {env_name or '<empty>'}")
        if not isinstance(raw_spec, dict):
            raise ValueError(f"secret_env.{env_name} must be an object with vault_key")
        vault_key = str(raw_spec.get("vault_key") or "").strip()
        if not vault_key:
            raise ValueError(f"secret_env.{env_name} requires vault_key")
        reason = str(raw_spec.get("reason") or "").strip() or _default_mount_reason(env_name, vault_key)
        mounts[env_name] = SecretEnvMount(env_name=env_name, vault_key=vault_key, reason=reason)
    return mounts


async def resolve_secret_env_mounts(
    tool_name: str,
    raw_mounts: Any,
    *,
    run_id: int,
    context: dict[str, Any],
) -> dict[str, str]:
    """Resolve a tool call's Vault mount specs into environment variables."""
    if tool_name not in SECRET_ENV_MOUNT_TOOLS:
        if raw_mounts not in (None, {}, []):
            raise ValueError(f"{tool_name} does not support secret_env mounts")
        return {}

    mounts = normalize_secret_env_mounts(raw_mounts)
    if not mounts:
        return {}

    from brain.systems.vault.agent_access import read_agent_secret_for_runtime

    user_id = str(context.get("actor_id") or "").strip()
    org_id = str(context.get("org_id") or "").strip() or None
    if not user_id:
        raise PermissionError("secret_env mounts require an authenticated user context")

    resolved: dict[str, str] = {}
    for env_name, mount in mounts.items():
        response = await read_agent_secret_for_runtime(
            mount.vault_key,
            reason=mount.reason,
            user_id=user_id,
            org_id=org_id,
            run_id=run_id,
            idea_id=str(context.get("idea_id") or "").strip() or None,
            requested_by="secret_env_mount",
            project_slug=context.get("project_slug"),
            project_slugs=context.get("project_slugs"),
            target_registry_id=context.get("target_registry_id"),
        )
        if not isinstance(response, dict):
            raise PermissionError(f"Vault secret '{mount.vault_key}' could not be read")
        if response.get("error"):
            raise PermissionError(str(response.get("error")))
        value = response.get("value")
        if value is None:
            raise PermissionError(f"Vault secret '{mount.vault_key}' did not return a value")
        resolved[env_name] = str(value)
    return resolved


def handler_args_with_resolved_secret_env(
    tool_name: str,
    args: dict[str, Any],
    resolved_secret_env: dict[str, str],
) -> dict[str, Any]:
    """Remove public mount specs and pass resolved values on the internal channel."""
    handler_args = dict(args)
    handler_args.pop("secret_env", None)
    if tool_name in SECRET_ENV_MOUNT_TOOLS and resolved_secret_env:
        handler_args[RESOLVED_SECRET_ENV_ARG] = dict(resolved_secret_env)
    return handler_args
