"""Tool handlers for launch handoffs."""

from __future__ import annotations

import json
from typing import Any

from brain.systems.runs.execution_context import _agent_context


def _clean_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _clean_object_list(value: Any) -> list[dict[str, Any]]:
    return [dict(item) for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _clean_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _current_source_surface(default: str | None = None) -> str:
    metadata = getattr(_agent_context, "execution_metadata", None)
    trigger = getattr(_agent_context, "chat_trigger", None)
    if isinstance(trigger, dict):
        surface = trigger.get("surface")
        if surface:
            return str(surface)
    if isinstance(metadata, dict):
        request_source = metadata.get("request_source")
        if isinstance(request_source, dict) and request_source.get("surface"):
            return str(request_source["surface"])
    return str(default or "illo_run")


def _current_source_ref(explicit: dict[str, Any] | None = None) -> dict[str, Any]:
    source_ref = _clean_dict(explicit)
    trigger = getattr(_agent_context, "chat_trigger", None)
    if isinstance(trigger, dict) and trigger:
        source_ref.setdefault("trigger", trigger)
    run_id = getattr(_agent_context, "run_id", None)
    if run_id is not None:
        source_ref.setdefault("illo_run_id", run_id)
    thread_id = getattr(_agent_context, "idea_id", None) or getattr(_agent_context, "thread_id", None)
    if thread_id:
        source_ref.setdefault("thread_id", str(thread_id))
    return source_ref


async def _handle_create_launch_handoff(
    title: str,
    instructions: str,
    summary: str | None = None,
    target_tool: str = "codex",
    repo_origin_url: str | None = None,
    branch_hint: str | None = None,
    source_surface: str | None = None,
    source_ref: dict[str, Any] | None = None,
    context_parts: list[dict[str, Any]] | None = None,
    acceptance_criteria: list[Any] | None = None,
    idempotency_key: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> str:
    """Create a durable handoff link that a teammate can open in a local agent."""
    from brain.platform.db.repositories.unit_of_work import UnitOfWork
    from brain.systems import launch_handoffs

    org_id = str(getattr(_agent_context, "org_id", "") or "").strip()
    if not org_id:
        return json.dumps({"error": "create_launch_handoff could not access this workspace context"})

    actor_user_id = str(getattr(_agent_context, "user_id", "") or "").strip() or None
    handoff_metadata = {
        **_clean_dict(metadata),
        "created_by_tool": "create_launch_handoff",
        "illo_run_id": getattr(_agent_context, "run_id", None),
    }
    try:
        async with UnitOfWork() as uow:
            row = await launch_handoffs.create_launch_handoff(
                uow.session,
                launch_handoffs.LaunchHandoffCreateInput(
                    org_id=org_id,
                    created_by_user_id=actor_user_id,
                    title=title,
                    instructions=instructions,
                    target_tool=target_tool,
                    summary=summary,
                    source_surface=_current_source_surface(source_surface),
                    source_ref=_current_source_ref(source_ref),
                    context_parts=_clean_object_list(context_parts or []),
                    acceptance_criteria=_clean_list(acceptance_criteria or []),
                    repo_origin_url=repo_origin_url,
                    branch_hint=branch_hint,
                    idempotency_key=idempotency_key,
                    metadata=handoff_metadata,
                ),
            )
            payload = launch_handoffs.serialize_launch_handoff(row)
    except launch_handoffs.LaunchHandoffError as exc:
        return json.dumps({"error": str(exc)})
    return json.dumps({"ok": True, "handoff": payload, "launch_url": payload["launch_url"]}, default=str)


__all__ = ["_handle_create_launch_handoff"]
