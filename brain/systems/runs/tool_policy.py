"""Shared helpers for model-visible tool policy metadata."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from threading import RLock
from typing import Any

_policy_metadata_lock = RLock()


def _tool_name_set(value: Any) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, Iterable) or isinstance(value, Mapping):
        return set()
    return {str(name).strip() for name in value if str(name or "").strip()}


def normalize_tool_policy(
    policy: Any,
    *,
    disabled_tools: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Normalize legacy policy aliases into one canonical disabled-tools list."""

    normalized = dict(policy) if isinstance(policy, Mapping) else {}
    disabled = (
        _tool_name_set(normalized.get("disabled_tools"))
        | _tool_name_set(normalized.get("blocked_tools"))
        | _tool_name_set(disabled_tools)
    )
    normalized.pop("blocked_tools", None)
    if disabled:
        normalized["disabled_tools"] = sorted(disabled)
    else:
        normalized.pop("disabled_tools", None)
    return normalized


def disabled_tool_names_from_metadata(metadata: Any) -> set[str]:
    with _policy_metadata_lock:
        payload = metadata if isinstance(metadata, Mapping) else {}
        policy = payload.get("tool_policy")
        if not isinstance(policy, Mapping):
            return set()
        return _tool_name_set(policy.get("disabled_tools")) | _tool_name_set(
            policy.get("blocked_tools")
        )


POLICY_GUIDANCE = (
    "say what you cannot do and offer to file a ticket; do not look for a workaround"
)
POLICY_CALL_GUIDANCE = "Tell the user what you cannot do and offer to file a ticket. Do not look for a workaround."


def policy_metadata_containers(metadata: Any):
    """Read policy-bearing runtime envelopes without accepting allow overrides."""
    if not isinstance(metadata, Mapping):
        return
    yield metadata
    for key in (
        "execution_provenance",
        "runtime_envelope",
        "context_snapshot",
        "launch_envelope",
    ):
        child = metadata.get(key)
        if isinstance(child, Mapping):
            yield from policy_metadata_containers(child)


def policy_org_id(org_id: str | None, metadata: Any) -> str | None:
    return org_id or next(
        (
            str(item["org_id"])
            for item in policy_metadata_containers(metadata)
            if item.get("org_id")
        ),
        None,
    )


def policy_skill_names(metadata: Any, message: str = "") -> set[str]:
    from brain.systems.runs.skill_commands import parse_slash_skill_names

    names = set(parse_slash_skill_names(message))
    for item in policy_metadata_containers(metadata):
        names |= _tool_name_set(item.get("skill_name"))
        names |= _tool_name_set(item.get("slash_skill_names"))
        names |= _tool_name_set(item.get("loaded_skill_names"))
        snapshot = item.get("schedule_skills")
        if isinstance(snapshot, Mapping):
            names |= {
                str(skill["name"])
                for skill in snapshot.get("skills", [])
                if isinstance(skill, Mapping) and skill.get("name")
            }
        # Cycle run envelopes carry the resolved skill snapshot as `skills`.
        for skill in (
            item.get("skills", []) if isinstance(item.get("skills"), list) else []
        ):
            if isinstance(skill, Mapping) and skill.get("name"):
                names.add(str(skill["name"]))
    return {name.strip().lstrip("/") for name in names if name.strip().lstrip("/")}


def merge_tool_policies(*policies: Any) -> dict[str, Any]:
    """Union disabled names and keep the strongest explanation for each name."""
    with _policy_metadata_lock:
        disabled: set[str] = set()
        sources: dict[str, dict[str, str]] = {}
        rank = {"run": 0, "skill": 1, "organization": 2}
        for policy in policies:
            normalized = normalize_tool_policy(policy)
            names = _tool_name_set(normalized.get("disabled_tools"))
            disabled |= names
            raw_sources = normalized.get("disabled_tool_sources")
            raw_sources = raw_sources if isinstance(raw_sources, Mapping) else {}
            for name in sorted(names):
                value = raw_sources.get(name)
                value = value if isinstance(value, Mapping) else {}
                source = str(value.get("source") or "run")
                if source not in rank:
                    source = "run"
                candidate = {"source": source}
                if source == "skill":
                    candidate["skill"] = str(value.get("skill") or "unknown")
                if name not in sources or rank[source] > rank[sources[name]["source"]]:
                    sources[name] = candidate
        return {"disabled_tools": sorted(disabled), "disabled_tool_sources": sources}


async def resolve_effective_tool_policy(
    org_id: str | None,
    skill_names: Iterable[str],
    metadata: dict,
    *,
    policy_loader=None,
) -> tuple[set[str], dict[str, dict[str, str]]]:
    """Resolve saved and inherited policy. A lookup failure must stop the run."""
    from brain.systems.runs.tool_policy_store import load_tool_policies

    policies = [
        item.get("tool_policy") for item in policy_metadata_containers(metadata)
    ]
    effective_org_id = policy_org_id(org_id, metadata)
    names = set(skill_names) | policy_skill_names(metadata)
    if effective_org_id:
        rows = await (policy_loader or load_tool_policies)(effective_org_id, names)
        for row in sorted(rows, key=lambda row: (row.scope_type, row.scope_key)):
            source = (
                {"source": "organization"}
                if row.scope_type == "organization"
                else {"source": "skill", "skill": row.scope_key}
            )
            policies.append(
                {
                    "disabled_tools": row.disabled_tools,
                    "disabled_tool_sources": {
                        name: source for name in row.disabled_tools
                    },
                }
            )
    with _policy_metadata_lock:
        resolved = merge_tool_policies(*policies, metadata.get("tool_policy"))
        current = metadata.get("tool_policy")
        if isinstance(current, dict):
            current.clear()
            current.update(resolved)
        else:
            metadata["tool_policy"] = resolved
    return set(resolved["disabled_tools"]), resolved["disabled_tool_sources"]


def current_policy_metadata(agent_context) -> dict:
    """Use the run control's shared metadata across cloned tool-call contexts."""
    if isinstance(agent_context, Mapping):
        control = agent_context.get("loop_control")
        metadata = agent_context.get("execution_metadata")
    else:
        control = getattr(agent_context, "loop_control", None)
        metadata = getattr(agent_context, "execution_metadata", None)
    shared = getattr(control, "policy_metadata", None)
    if isinstance(shared, dict):
        with _policy_metadata_lock:
            if isinstance(metadata, dict):
                resolved = merge_tool_policies(
                    shared.get("tool_policy"), metadata.get("tool_policy")
                )
                policy = shared.get("tool_policy")
                if isinstance(policy, dict):
                    policy.clear()
                    policy.update(resolved)
                else:
                    shared["tool_policy"] = resolved
                metadata["tool_policy"] = shared["tool_policy"]
        return shared
    return metadata if isinstance(metadata, dict) else {}


def policy_unavailable_tools(metadata: Any) -> dict[str, dict[str, str]]:
    with _policy_metadata_lock:
        policy = (
            metadata.get("tool_policy", {}) if isinstance(metadata, Mapping) else {}
        )
        merged = merge_tool_policies(policy)
    result = {}
    for name in merged["disabled_tools"]:
        source = merged["disabled_tool_sources"][name]
        kind = source["source"]
        reason = f"disabled by {kind} policy"
        if kind == "skill":
            reason += f" ({source['skill']})"
        result[name] = {**source, "reason": reason, "guidance": POLICY_GUIDANCE}
    return result


def tool_policy_error(tool_name: str, metadata: Any) -> str | None:
    detail = policy_unavailable_tools(metadata).get(tool_name)
    if detail is None:
        return None
    return f"Tool '{tool_name}' is {detail['reason']}. {POLICY_CALL_GUIDANCE}"


async def apply_loaded_skill_policy(name: str, agent_context) -> None:
    """Latch a skill policy before its procedure or assets reach the model."""
    metadata = current_policy_metadata(agent_context)
    names = policy_skill_names(metadata) | {name}
    await resolve_effective_tool_policy(
        policy_org_id(getattr(agent_context, "org_id", None), metadata),
        names,
        metadata,
    )
    metadata["loaded_skill_names"] = sorted(names | policy_skill_names(metadata))
    execution_metadata = getattr(agent_context, "execution_metadata", None)
    if isinstance(execution_metadata, dict):
        execution_metadata["tool_policy"] = metadata["tool_policy"]
        execution_metadata["loaded_skill_names"] = metadata["loaded_skill_names"]


def guard_tool_handler(tool_name: str, handler, agent_context):
    """Keep indirect catalog calls subject to the same run policy."""
    import inspect
    from functools import wraps

    if inspect.iscoroutinefunction(handler):

        @wraps(handler)
        async def guarded(*args, **kwargs):
            error = tool_policy_error(tool_name, current_policy_metadata(agent_context))
            if error:
                return {"error": error}
            return await handler(*args, **kwargs)

    else:

        @wraps(handler)
        def guarded(*args, **kwargs):
            error = tool_policy_error(tool_name, current_policy_metadata(agent_context))
            if error:
                return {"error": error}
            return handler(*args, **kwargs)

    return guarded
