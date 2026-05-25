"""Shared helpers for model-visible tool policy metadata."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any


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
    payload = metadata if isinstance(metadata, Mapping) else {}
    policy = payload.get("tool_policy")
    if not isinstance(policy, Mapping):
        return set()
    return _tool_name_set(policy.get("disabled_tools")) | _tool_name_set(policy.get("blocked_tools"))

