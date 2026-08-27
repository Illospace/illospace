"""Minimal attribution for completed inbound Illo triage runs."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from brain.contracts.github import (
    github_issue_comment_ref,
    github_issue_ref,
    github_pull_request_ref,
)
from brain.platform.db.models.agent_run import AgentRunEventRow
from brain.systems.runs.status import RunStatus
from brain.systems.runs.tool_catalog.registry import action_policy_for_tool, get_tool_registration


_TOOL_COMPLETED_EVENT = "run.tool_completed"
_MAX_TARGET_REFS = 20
# Entity identities are short (uuids, integer ids, owner/repo#N). Anything
# longer is result CONTENT that leaked into a ref-shaped position — drop it
# (never truncate: a clipped id is a wrong ref), which also bounds the
# persisted result_refs payload (cross-family review finding, 2026-07-16).
_MAX_REF_ID_CHARS = 200
_MAX_REF_KIND_CHARS = 64
_MAX_REF_SOURCE_CHARS = 80
_SUMMARY_OPERATION_TAGS = {
    "created",
    "updated",
    "deleted",
    "archived",
    "restored",
    "minted",
    "revoked",
    "refreshed",
    "posted",
}
_EXPLICIT_REF_KEYS = frozenset(
    {
        "target_refs",
        "mutated_target_refs",
        "durable_refs",
        "durable_target_refs",
    }
)

_DIRECT_REF_KINDS = {
    "idea_id": "idea",
    "thread_id": "thread",
    "thread_message_id": "thread_message",
    "message_id": "message",
    "domain_id": "domain",
    "record_id": "domain_record",
    "project_id": "project_context",
    "project_profile_id": "project_context",
    "handoff_id": "launch_handoff",
    "source_id": "memory_source",
    "curation_source_id": "memory_source",
    "replacement_source_id": "memory_source",
    "span_id": "memory_span",
    "span_ids": "memory_span",
    "content_node_id": "memory_node",
    "cue_node_id": "memory_node",
    "cue_node_ids": "memory_node",
    "tag_node_id": "memory_node",
    "tag_node_ids": "memory_node",
    "assertion_id": "memory_assertion",
    "edge_id": "memory_edge",
    "edge_ids": "memory_edge",
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
    "project": "project_context",
    "handoff": "launch_handoff",
    "attachment": "project_context_attachment",
    "connection": "external_source_connection",
    "policy": "inbound_source_policy",
    "projection": "inbound_domain_projection",
    "token": "external_source_token",
    "event": "inbound_event",
    "run": "agent_run",
}

# Ref kinds that represent routed/created WORK (something a teammate or
# their agent picks up), as opposed to conversation, memory, or plumbing
# state. This mirrors how preservation.py filters mutated refs by kind.
WORK_ITEM_REF_KINDS = frozenset(
    {
        "github_issue",
        "github_pull_request",
        "idea",
        "domain_record",
        "agent_run",
        "launch_handoff",
        "thread",
    }
)


def _json_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


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
    kind_text = str(kind or "").strip()
    if not text or not kind_text:
        return
    if len(text) > _MAX_REF_ID_CHARS or len(kind_text) > _MAX_REF_KIND_CHARS:
        return
    key = (kind_text, text)
    if key in seen:
        return
    seen.add(key)
    refs.append({"kind": kind_text, "id": text, "source": str(source or "")[:_MAX_REF_SOURCE_CHARS]})


def _add_ref_values(
    refs: list[dict[str, str]],
    seen: set[tuple[str, str]],
    *,
    kind: str,
    value: Any,
    source: str,
) -> None:
    if isinstance(value, list | tuple | set):
        for item in value:
            _add_ref(refs, seen, kind=kind, value=item, source=source)
        return
    _add_ref(refs, seen, kind=kind, value=value, source=source)


def _add_explicit_ref(
    refs: list[dict[str, str]],
    seen: set[tuple[str, str]],
    *,
    value: Any,
    source: str,
) -> None:
    if not isinstance(value, Mapping):
        return
    kind = str(value.get("kind") or value.get("type") or "").strip()
    ref_id = value.get("id") or value.get("ref") or value.get("value")
    if not ref_id and kind:
        ref_id = value.get(f"{kind}_id")
    if not kind or ref_id is None:
        return
    _add_ref(
        refs,
        seen,
        kind=kind,
        value=ref_id,
        source=str(value.get("source") or source),
    )


def _add_explicit_ref_values(
    refs: list[dict[str, str]],
    seen: set[tuple[str, str]],
    *,
    value: Any,
    source: str,
) -> None:
    if isinstance(value, list | tuple | set):
        for item in value:
            _add_explicit_ref(refs, seen, value=item, source=source)
        return
    _add_explicit_ref(refs, seen, value=value, source=source)


def _collect_refs(value: Any, refs: list[dict[str, str]], seen: set[tuple[str, str]], *, source: str) -> None:
    if len(refs) >= _MAX_TARGET_REFS:
        return
    if isinstance(value, Mapping):
        for key, child in value.items():
            key_text = str(key)
            if key_text in _EXPLICIT_REF_KEYS:
                _add_explicit_ref_values(refs, seen, value=child, source=source)
                continue
            if key_text in _DIRECT_REF_KINDS:
                _add_ref_values(refs, seen, kind=_DIRECT_REF_KINDS[key_text], value=child, source=source)
            if key_text in _OBJECT_REF_KINDS and isinstance(child, Mapping):
                _add_ref(refs, seen, kind=_OBJECT_REF_KINDS[key_text], value=child.get("id"), source=source)
            if isinstance(child, Mapping | list):
                _collect_refs(child, refs, seen, source=source)
    elif isinstance(value, list):
        for child in value[:10]:
            _collect_refs(child, refs, seen, source=source)


def collect_result_refs(result: Any, *, source: str) -> list[dict[str, str]]:
    """Extract entity refs from a FULL tool result (pre-truncation).

    Run events persist only a 1000-char result preview; a bigger mutating
    result truncates into invalid JSON and the ref walk goes blind (found
    live on illo-dev, 2026-07-16: a created tracker record was invisible to
    downstream attribution). The tool executor calls this on the full text
    and stores the refs beside the preview as ``result_refs``.
    """
    refs: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    _collect_refs(_parse_result(result), refs, seen, source=source)
    return refs


_LEGACY_GITHUB_RESULT_TOOLS = frozenset(
    {
        "add_github_issue_comment",
        "create_github_issue",
        "create_github_pull_request",
        "update_github_issue",
    }
)


def _legacy_github_result_refs(tool_name: str, result: Any) -> list[dict[str, str]]:
    """Read GitHub refs only from run events persisted before ``0f3fa6f0``.

    This compatibility path is frozen: it must not learn a new result shape.
    New GitHub result shapes must emit ``mutated_target_refs`` in their tool
    handler. Delete this function when no run events predating ``0f3fa6f0``
    remain readable.
    """
    if not isinstance(result, Mapping) or "mutated_target_refs" in result:
        return []
    repo_slug = str(result.get("repo") or "").strip()
    if repo_slug.count("/") != 1:
        return []

    def positive_int(value: Any) -> int | None:
        try:
            number = int(str(value or "").strip())
        except (TypeError, ValueError):
            return None
        return number if number > 0 else None

    if tool_name in {"create_github_issue", "update_github_issue"}:
        issue = result.get("issue")
        if not isinstance(issue, Mapping):
            return []
        number = positive_int(issue.get("number"))
        artifact_type = str(issue.get("type") or "issue").strip() or "issue"
        if number is None or artifact_type not in {"issue", "pull_request"}:
            return []
        ref = (
            github_pull_request_ref(repo_slug, number)
            if artifact_type == "pull_request"
            else github_issue_ref(repo_slug, number)
        )
        return [ref]

    if tool_name == "create_github_pull_request":
        pull_request = result.get("pull_request")
        if isinstance(pull_request, Mapping):
            number = positive_int(pull_request.get("number"))
            artifact_type = (
                str(pull_request.get("type") or "pull_request").strip() or "pull_request"
            )
        else:
            number = positive_int(result.get("number"))
            artifact_type = "pull_request"
        if number is None or artifact_type not in {"issue", "pull_request"}:
            return []
        ref = (
            github_issue_ref(repo_slug, number)
            if artifact_type == "issue"
            else github_pull_request_ref(repo_slug, number)
        )
        return [ref]

    if tool_name == "add_github_issue_comment":
        comment = result.get("comment")
        issue_number = positive_int(result.get("issue_number"))
        comment_id = positive_int(comment.get("id")) if isinstance(comment, Mapping) else None
        if issue_number is not None and comment_id is not None:
            return [github_issue_comment_ref(repo_slug, issue_number, comment_id)]

    return []


def _target_refs(tool_events: list[AgentRunEventRow]) -> list[dict[str, str]]:
    refs: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for row in tool_events:
        payload = _json_dict(row.payload)
        source = _tool_name(payload) or "tool_result"
        result = _tool_result(payload)
        if (
            source in _LEGACY_GITHUB_RESULT_TOOLS
            and isinstance(result, Mapping)
            and "mutated_target_refs" not in result
        ):
            _add_explicit_ref_values(
                refs,
                seen,
                value=_legacy_github_result_refs(source, result),
                source=source,
            )
        _collect_refs(result, refs, seen, source=source)
        # Full-fidelity channel: refs the executor extracted from the
        # complete result before the stored preview truncated it.
        _add_explicit_ref_values(refs, seen, value=payload.get("result_refs"), source=source)
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
    operation = next((tag for tag in tags if tag in _SUMMARY_OPERATION_TAGS), "updated")
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
    mutating_tool_events: list[AgentRunEventRow] = []
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
            mutating_tool_events.append(row)
    mutating_tools = _unique(mutating_tools)
    refs = _target_refs(tool_events)
    mutating_refs = _target_refs(mutating_tool_events)
    tags = _tags(tool_events, mutating_tools=mutating_tools, read_only_seen=read_only_seen, status=status)
    return {
        "summary": _summary(
            status=status,
            tool_names=tool_names,
            mutating_tools=mutating_tools,
            refs=mutating_refs,
            tags=tags,
        ),
        "tags": tags,
        "tool_names": tool_names,
        "target_refs": refs,
        "mutated_target_refs": mutating_refs,
        "run_event_ids": [int(row.id) for row in tool_events if row.id is not None],
    }


__all__ = [
    "WORK_ITEM_REF_KINDS",
    "collect_result_refs",
    "summarize_inbound_run_attribution",
]
