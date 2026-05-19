"""Minimal attribution for completed inbound Illo triage runs."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from brain.platform.db.models.agent_run import AgentRunEventRow
from brain.systems.runs.status import RunStatus
from brain.systems.runs.tool_catalog.registry import action_policy_for_tool, get_tool_registration


_TOOL_COMPLETED_EVENT = "run.tool_completed"
_MAX_TARGET_REFS = 20

_DIRECT_REF_KINDS = {
    "idea_id": "idea",
    "thread_id": "thread",
    "thread_message_id": "thread_message",
    "message_id": "message",
    "domain_id": "domain",
    "record_id": "domain_record",
    "connection_id": "external_source_connection",
    "policy_id": "inbound_source_policy",
    "projection_id": "inbound_domain_projection",
    "token_id": "external_source_token",
    "event_id": "inbound_event",
    "run_id": "agent_run",
}

_OBJECT_REF_KINDS = {
    "idea": "idea",
    "thread": "thread",
    "thread_message": "thread_message",
    "message": "message",
    "domain": "domain",
    "record": "domain_record",
    "connection": "external_source_connection",
    "policy": "inbound_source_policy",
    "projection": "inbound_domain_projection",
    "token": "external_source_token",
    "event": "inbound_event",
    "run": "agent_run",
}


def _json_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _json_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def _parse_result(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text or text[0] not in "[{":
        return value
    try:
        return json.loads(text)
    except Exception:
        return value


def _enum_value(value: Any) -> str:
    return str(getattr(value, "value", value) or "").strip()


def _tool_name(payload: Mapping[str, Any]) -> str | None:
    value = payload.get("tool_name") or payload.get("tool")
    text = str(value or "").strip()
    return text or None


def _tool_args(payload: Mapping[str, Any]) -> dict[str, Any]:
    args = payload.get("args")
    return dict(args) if isinstance(args, dict) else {}


def _tool_result(payload: Mapping[str, Any]) -> Any:
    return _parse_result(payload.get("result"))


def _tool_is_read_only(tool_name: str, args: Mapping[str, Any]) -> bool:
    try:
        policy = action_policy_for_tool(tool_name, kwargs=dict(args))
    except Exception:
        policy = None
    if policy is None:
        return True
    registration = get_tool_registration(tool_name)
    return _enum_value(getattr(registration, "side_effect_class", "")) == "read_only"


def _add_ref(refs: list[dict[str, str]], seen: set[tuple[str, str]], *, kind: str, value: Any, source: str) -> None:
    if len(refs) >= _MAX_TARGET_REFS:
        return
    text = str(value or "").strip()
    if not text:
        return
    key = (kind, text)
    if key in seen:
        return
    seen.add(key)
    refs.append({"kind": kind, "id": text, "source": source})


def _collect_refs(value: Any, refs: list[dict[str, str]], seen: set[tuple[str, str]], *, source: str) -> None:
    if len(refs) >= _MAX_TARGET_REFS:
        return
    if isinstance(value, Mapping):
        for key, child in value.items():
            key_text = str(key)
            if key_text in _DIRECT_REF_KINDS:
                _add_ref(refs, seen, kind=_DIRECT_REF_KINDS[key_text], value=child, source=source)
            if key_text in _OBJECT_REF_KINDS and isinstance(child, Mapping):
                _add_ref(refs, seen, kind=_OBJECT_REF_KINDS[key_text], value=child.get("id"), source=source)
            if isinstance(child, Mapping | list):
                _collect_refs(child, refs, seen, source=source)
    elif isinstance(value, list):
        for child in value[:10]:
            _collect_refs(child, refs, seen, source=source)


def _target_refs(tool_events: list[AgentRunEventRow]) -> list[dict[str, str]]:
    refs: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for row in tool_events:
        payload = _json_dict(row.payload)
        source = _tool_name(payload) or "tool_result"
        _collect_refs(_tool_result(payload), refs, seen, source=source)
    return refs


def _operation_tag(args: Mapping[str, Any], result: Any) -> str | None:
    result_data = _json_dict(result)
    operation = str(result_data.get("operation") or args.get("action") or "").strip().lower()
    if not operation:
        return None
    if operation.endswith("ed"):
        return operation
    if operation.startswith("create"):
        return "created"
    if operation.startswith("update"):
        return "updated"
    if operation.startswith("delete") or operation.startswith("remove"):
        return "deleted"
    if operation.startswith("archive"):
        return "archived"
    if operation.startswith("restore"):
        return "restored"
    if operation.startswith("mint"):
        return "minted"
    if operation.startswith("revoke"):
        return "revoked"
    if operation.startswith("refresh"):
        return "refreshed"
    if operation.startswith("post"):
        return "posted"
    return operation.replace("_", "-")


def _tool_tag(tool_name: str) -> str:
    registration = get_tool_registration(tool_name)
    side_effect = _enum_value(getattr(registration, "side_effect_class", ""))
    if side_effect:
        return side_effect
    return tool_name.replace("_", "-")


def _tags(
    tool_events: list[AgentRunEventRow],
    *,
    mutating_tools: list[str],
    read_only_seen: bool,
    status: RunStatus,
) -> list[str]:
    tags: list[str] = []
    if status != RunStatus.COMPLETED:
        tags.append(status.value)
    if read_only_seen:
        tags.append("inspected")
    for row in tool_events:
        payload = _json_dict(row.payload)
        tool_name = _tool_name(payload)
        if not tool_name or tool_name not in mutating_tools:
            continue
        args = _tool_args(payload)
        result = _tool_result(payload)
        tags.append(_tool_tag(tool_name))
        operation = _operation_tag(args, result)
        if operation:
            tags.append(operation)
    if not mutating_tools:
        tags.append("no_workspace_change")
    return _unique(tags)


def _summary(
    *,
    status: RunStatus,
    tool_names: list[str],
    mutating_tools: list[str],
    refs: list[dict[str, str]],
    tags: list[str],
) -> str:
    if status != RunStatus.COMPLETED:
        return f"Illo triage ended with status {status.value}."
    if not tool_names:
        return "Illo resolved the signal without a workspace tool action."
    if not mutating_tools:
        return "Illo inspected workspace context and resolved the signal without a workspace mutation."

    target_kinds = _unique([str(ref.get("kind") or "") for ref in refs])[:3]
    target_text = ", ".join(target_kinds) if target_kinds else "workspace state"
    tool_text = ", ".join(mutating_tools[:3])
    operation = next((tag for tag in tags if tag in {"created", "updated", "deleted", "archived", "restored", "minted", "revoked", "refreshed", "posted"}), "updated")
    return f"Illo {operation} {target_text} using {tool_text}."


async def summarize_inbound_run_attribution(
    session: AsyncSession,
    *,
    run_id: int,
    status: RunStatus,
) -> dict[str, Any]:
    """Build compact observed-outcome attribution from completed tool events."""

    tool_events = list(
        (
            await session.scalars(
                select(AgentRunEventRow)
                .where(
                    AgentRunEventRow.run_id == int(run_id),
                    AgentRunEventRow.event_type == _TOOL_COMPLETED_EVENT,
                )
                .order_by(AgentRunEventRow.sequence_no.asc(), AgentRunEventRow.id.asc())
            )
        ).all()
    )
    tool_names = _unique(
        [
            tool_name
            for row in tool_events
            if (tool_name := _tool_name(_json_dict(row.payload))) is not None
        ]
    )
    mutating_tools: list[str] = []
    read_only_seen = False
    for row in tool_events:
        payload = _json_dict(row.payload)
        tool_name = _tool_name(payload)
        if not tool_name:
            continue
        if _tool_is_read_only(tool_name, _tool_args(payload)):
            read_only_seen = True
        else:
            mutating_tools.append(tool_name)
    mutating_tools = _unique(mutating_tools)
    refs = _target_refs(tool_events)
    tags = _tags(tool_events, mutating_tools=mutating_tools, read_only_seen=read_only_seen, status=status)
    return {
        "summary": _summary(
            status=status,
            tool_names=tool_names,
            mutating_tools=mutating_tools,
            refs=refs,
            tags=tags,
        ),
        "tags": tags,
        "tool_names": tool_names,
        "target_refs": refs,
        "run_event_ids": [int(row.id) for row in tool_events if row.id is not None],
    }


__all__ = ["summarize_inbound_run_attribution"]
