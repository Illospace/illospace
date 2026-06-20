"""Run-scoped credential materialization for managed workspace tools."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import shutil
import shlex
import tempfile
from typing import Any

from brain.platform.integrations.openai_codex_auth import (
    encode_codex_auth_payload,
    parse_codex_auth_payload,
)


WORKSPACE_TOOL_AUTH_TOOLS = frozenset({"exec_command", "run_script", "test_runner"})
RESOLVED_WORKSPACE_TOOL_ENV_ARG = "_resolved_workspace_tool_env"
RESOLVED_WORKSPACE_TOOL_SENSITIVE_VALUES_ARG = "_resolved_workspace_tool_sensitive_values"
WORKSPACE_TOOL_AUTH_SCHEMA = {
    "type": "array",
    "items": {"type": "string"},
    "description": (
        "Optional workspace tool bundle ids or runtime auth profile ids whose declared credentials "
        "should be materialized for this call. exec_command also auto-detects installed workspace "
        "tool profiles from command names. This is a hint/reference only; never include raw secrets."
    ),
}


@dataclass
class WorkspaceToolRuntimeMaterialization:
    env: dict[str, str] = field(default_factory=dict)
    sensitive_values: list[str] = field(default_factory=list)
    activated_profiles: list[dict[str, str]] = field(default_factory=list)
    cleanup_paths: list[Path] = field(default_factory=list)

    def cleanup(self) -> None:
        for path in self.cleanup_paths:
            shutil.rmtree(path, ignore_errors=True)


def normalize_workspace_tool_auth(raw: Any) -> set[str]:
    if raw in (None, {}, []):
        return set()
    if isinstance(raw, str):
        return {_normalize_ref(raw)}
    if isinstance(raw, (list, tuple, set)):
        return {_normalize_ref(item) for item in raw if str(item or "").strip()}
    if isinstance(raw, dict):
        values: list[Any] = []
        for key in ("bundle_id", "bundle", "profile_id", "profile", "tool"):
            if raw.get(key):
                values.append(raw[key])
        for key in ("bundle_ids", "bundles", "profile_ids", "profiles", "tools"):
            if isinstance(raw.get(key), (list, tuple, set)):
                values.extend(raw[key])
        return {_normalize_ref(item) for item in values if str(item or "").strip()}
    raise ValueError("workspace_tool_auth must be a string, array, or object of bundle/profile references")


async def resolve_workspace_tool_runtime(
    tool_name: str,
    args: dict[str, Any],
    *,
    run_id: int,
    context: dict[str, Any],
) -> WorkspaceToolRuntimeMaterialization:
    raw_auth = args.get("workspace_tool_auth")
    if tool_name not in WORKSPACE_TOOL_AUTH_TOOLS:
        if raw_auth not in (None, {}, []):
            raise ValueError(f"{tool_name} does not support workspace_tool_auth")
        return WorkspaceToolRuntimeMaterialization()

    explicit_refs = normalize_workspace_tool_auth(raw_auth)
    command_names = _command_names_for_tool_call(tool_name, args)
    if not explicit_refs and not command_names:
        return WorkspaceToolRuntimeMaterialization()

    profiles = _matching_auth_profiles(
        explicit_refs=explicit_refs,
        command_names=command_names,
        org_id=str(context.get("org_id") or "").strip() or None,
    )
    if not profiles:
        return WorkspaceToolRuntimeMaterialization()

    materialized = WorkspaceToolRuntimeMaterialization()
    try:
        for bundle_id, profile in profiles:
            credential = await _resolve_credential(profile, context=context, run_id=run_id)
            _materialize_profile(bundle_id, profile, credential, materialized)
            materialized.activated_profiles.append({
                "bundle_id": bundle_id,
                "profile_id": str(profile.get("id") or "default"),
            })
        return materialized
    except Exception:
        materialized.cleanup()
        raise


def handler_args_with_resolved_workspace_tool_runtime(
    tool_name: str,
    args: dict[str, Any],
    materialized: WorkspaceToolRuntimeMaterialization,
) -> dict[str, Any]:
    handler_args = dict(args)
    handler_args.pop("workspace_tool_auth", None)
    if tool_name in WORKSPACE_TOOL_AUTH_TOOLS:
        if materialized.env:
            handler_args[RESOLVED_WORKSPACE_TOOL_ENV_ARG] = dict(materialized.env)
        if materialized.sensitive_values:
            handler_args[RESOLVED_WORKSPACE_TOOL_SENSITIVE_VALUES_ARG] = list(materialized.sensitive_values)
    return handler_args


def _normalize_ref(value: Any) -> str:
    return str(value or "").strip().lower().replace("_", "-")


def _command_names_for_tool_call(tool_name: str, args: dict[str, Any]) -> set[str]:
    if tool_name != "exec_command":
        return set()
    command = str(args.get("command") or "").strip()
    if not command:
        return set()
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.replace("&&", " ").replace("||", " ").replace(";", " ").split()
    names: set[str] = set()
    for token in tokens:
        cleaned = token.strip()
        if not cleaned or cleaned in {"&&", "||", "|", ";", "(", ")"} or cleaned.startswith("-"):
            continue
        names.add(Path(cleaned).name)
    return names


def _matching_auth_profiles(
    *,
    explicit_refs: set[str],
    command_names: set[str],
    org_id: str | None,
) -> list[tuple[str, dict[str, Any]]]:
    from brain.systems.runtime_settings.workspace_tools import (
        installed_workspace_tool_bundle_ids,
        workspace_tool_catalog,
    )

    installed_bundle_ids = {_normalize_ref(item) for item in installed_workspace_tool_bundle_ids(org_id)}
    matches: list[tuple[str, dict[str, Any]]] = []
    for bundle in workspace_tool_catalog():
        bundle_id = _normalize_ref(bundle.id)
        profiles = _auth_profiles(bundle.runtime)
        for profile in profiles:
            profile_id = _normalize_ref(profile.get("id") or "")
            commands = {_normalize_ref(item) for item in _string_list(profile.get("commands"))}
            if not commands:
                commands = {_normalize_ref(item) for item in bundle.provided_commands}
            explicit_match = bundle_id in explicit_refs or (profile_id and profile_id in explicit_refs)
            auto_match = bool(commands & {_normalize_ref(item) for item in command_names})
            if explicit_match or (auto_match and bundle_id in installed_bundle_ids):
                matches.append((bundle.id, profile))
    return matches


def _auth_profiles(runtime: dict[str, Any]) -> list[dict[str, Any]]:
    raw = runtime.get("auth_profiles") if isinstance(runtime, dict) else None
    if not isinstance(raw, list):
        raw = runtime.get("credential_profiles") if isinstance(runtime, dict) else None
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


async def _resolve_credential(
    profile: dict[str, Any],
    *,
    context: dict[str, Any],
    run_id: int,
) -> str:
    source = profile.get("source")
    if not isinstance(source, dict):
        raise ValueError(f"Workspace tool auth profile {profile.get('id') or '<default>'} is missing source")

    source_type = str(source.get("type") or "").strip().lower()
    if source_type == "provider_connection":
        return await _resolve_provider_connection(source, context=context)
    if source_type == "vault_secret":
        return await _resolve_vault_secret(source, profile=profile, context=context, run_id=run_id)
    raise ValueError(f"Unsupported workspace tool credential source type: {source_type or '<empty>'}")


async def _resolve_provider_connection(source: dict[str, Any], *, context: dict[str, Any]) -> str:
    provider = str(source.get("provider") or "").strip().lower()
    credential = str(source.get("credential") or source.get("source") or "").strip().lower()
    scope = str(source.get("scope") or "originating_user").strip().lower()
    user_id = str(context.get("actor_id") or "").strip() or None
    org_id = str(context.get("org_id") or "").strip() or None
    if scope in {"originating_user", "user"} and not user_id:
        raise PermissionError("Workspace tool provider credentials require an originating user")

    from brain.systems.vault import async_resolve_api_key

    if credential in {"codex_subscription", "user_codex_connection"}:
        if provider != "openai":
            raise ValueError("codex_subscription provider credentials are only supported for OpenAI")
        raw, resolved_source = await async_resolve_api_key(
            user_id=user_id,
            org_id=org_id,
            provider=provider,
            auth_mode="chatgpt",
        )
        if resolved_source != "codex_subscription" or not raw:
            raise PermissionError("Workspace tool requires the originating user's Codex/OpenAI connection")
        return raw

    if credential in {"org_main", "org_api_key", "api_key"}:
        raw, resolved_source = await async_resolve_api_key(
            user_id=user_id,
            org_id=org_id,
            provider=provider,
            auth_mode="api_key",
        )
        if resolved_source not in {"user_openai", "org_main", "env"} or not raw:
            raise PermissionError(f"Workspace tool requires a {provider} user or workspace API key")
        return raw

    raise ValueError(f"Unsupported workspace tool provider credential: {credential or '<empty>'}")


async def _resolve_vault_secret(
    source: dict[str, Any],
    *,
    profile: dict[str, Any],
    context: dict[str, Any],
    run_id: int,
) -> str:
    vault_key = str(source.get("vault_key") or source.get("key") or "").strip()
    if not vault_key:
        raise ValueError("vault_secret workspace tool source requires vault_key")

    from brain.systems.vault.runtime_secrets import RuntimeSecretContext, read_runtime_secret

    return await read_runtime_secret(
        vault_key,
        context=RuntimeSecretContext(
            actor_user_id=str(context.get("actor_id") or "").strip() or None,
            org_id=str(context.get("org_id") or "").strip() or None,
            run_id=run_id,
            idea_id=str(context.get("idea_id") or "").strip() or None,
            project_slug=context.get("project_slug"),
            project_slugs=context.get("project_slugs"),
            target_registry_id=context.get("target_registry_id"),
        ),
        reason=str(profile.get("reason") or f"Run workspace tool auth profile {profile.get('id') or '<default>'}"),
        requested_by="workspace_tool_runtime",
    )


def _materialize_profile(
    bundle_id: str,
    profile: dict[str, Any],
    credential: str,
    materialized: WorkspaceToolRuntimeMaterialization,
) -> None:
    specs = profile.get("materialize")
    if isinstance(specs, dict):
        specs = [specs]
    if not isinstance(specs, list) or not specs:
        raise ValueError(f"Workspace tool auth profile {profile.get('id') or '<default>'} has no materialize spec")

    for spec in specs:
        if not isinstance(spec, dict):
            continue
        materialize_type = str(spec.get("type") or "").strip().lower()
        if materialize_type == "env":
            env_name = _require_env_name(spec.get("name") or spec.get("env"))
            value = _format_credential(credential, str(spec.get("format") or "raw"))
            materialized.env[env_name] = value
            materialized.sensitive_values.extend(_sensitive_values(value))
            continue
        if materialize_type == "file":
            env_name = _require_env_name(spec.get("env")) if spec.get("env") else None
            relative_path = str(spec.get("path") or "credential").strip()
            if not relative_path or Path(relative_path).is_absolute() or ".." in Path(relative_path).parts:
                raise ValueError("Workspace tool file materialization path must be relative and contained")
            temp_root = Path(tempfile.mkdtemp(prefix=f"illo-{_normalize_ref(bundle_id)}-auth-"))
            os.chmod(temp_root, 0o700)
            target = temp_root / relative_path
            target.parent.mkdir(parents=True, exist_ok=True)
            value = _format_credential(credential, str(spec.get("format") or "raw"))
            target.write_text(value, encoding="utf-8")
            os.chmod(target, 0o600)
            if env_name:
                materialized.env[env_name] = str(temp_root)
            materialized.sensitive_values.extend(_sensitive_values(value))
            materialized.cleanup_paths.append(temp_root)
            continue
        raise ValueError(f"Unsupported workspace tool materialization type: {materialize_type or '<empty>'}")


def _format_credential(raw: str, format_name: str) -> str:
    normalized = str(format_name or "raw").strip().lower()
    if normalized == "raw":
        return str(raw)
    if normalized == "json":
        parsed = json.loads(raw) if isinstance(raw, str) else raw
        return json.dumps(parsed, separators=(",", ":"))
    if normalized == "codex_auth_json":
        cred = parse_codex_auth_payload(raw, source="workspace_tool_runtime")
        return json.dumps(encode_codex_auth_payload(cred), separators=(",", ":"))
    raise ValueError(f"Unsupported workspace tool credential format: {normalized}")


def _sensitive_values(value: Any) -> list[str]:
    return list(dict.fromkeys(_collect_sensitive_values(value, include_string=True)))


def _collect_sensitive_values(value: Any, *, include_string: bool = False, key_name: str | None = None) -> list[str]:
    values: list[str] = []
    if isinstance(value, str):
        stripped = value.strip()
        key = str(key_name or "").lower()
        sensitive_key = any(part in key for part in ("token", "key", "secret", "credential", "email", "account"))
        if len(stripped) >= 6 and (include_string or sensitive_key):
            values.append(stripped)
        try:
            parsed = json.loads(stripped)
        except Exception:
            return values
        values.extend(_collect_sensitive_values(parsed))
        return values
    if isinstance(value, dict):
        for key, item in value.items():
            values.extend(_collect_sensitive_values(item, key_name=str(key)))
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            values.extend(_collect_sensitive_values(item))
    return values


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        values = value.replace(",", " ").split()
    elif isinstance(value, (list, tuple, set)):
        values = list(value)
    else:
        values = []
    return [str(item).strip() for item in values if str(item or "").strip()]


def _require_env_name(value: Any) -> str:
    name = str(value or "").strip()
    if not name or not name.replace("_", "A").isalnum() or name[0].isdigit():
        raise ValueError(f"Invalid workspace tool env name: {name or '<empty>'}")
    return name
