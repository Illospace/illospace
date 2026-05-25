"""Worker orchestration tool handlers."""

from __future__ import annotations

from collections.abc import Mapping
from hashlib import sha256
import json
import re
import uuid
from typing import Any

from brain.platform.db.repositories.unit_of_work import UnitOfWork
from brain.systems.runs.assignments import WorkerAssignment
from brain.systems.runs.domain import AgentRunRequest, RunRecipe
from brain.systems.runs.events import run_event
from brain.systems.runs.execution_context import _agent_context
from brain.systems.runs.store import AsyncAgentRunStore, to_domain


_HEADLESS_BLOCKED_TOOLS = frozenset({
    "cortex_reply",
    "cortex_visual_reply",
    "manage_idea",
    "manage_workspace_app",
    "post_chat_message",
    "post_ai_timeline_message",
    "post_thread_discussion_reply",
})


def _current_agent_value(name: str) -> Any:
    value = getattr(_agent_context, name, None)
    if value not in (None, ""):
        return value
    run = getattr(_agent_context, "run", None)
    value = getattr(run, name, None)
    if value not in (None, ""):
        return value
    execution_metadata = getattr(_agent_context, "execution_metadata", None)
    if isinstance(execution_metadata, Mapping):
        value = execution_metadata.get(name)
        if value not in (None, ""):
            return value
    return None


def _current_run_id() -> int | None:
    run = getattr(_agent_context, "run", None)
    candidates = (
        getattr(run, "run_id", None),
        getattr(run, "id", None),
        getattr(_agent_context, "run_id", None),
        _current_agent_value("run_id"),
    )
    for candidate in candidates:
        if candidate in (None, ""):
            continue
        try:
            return int(candidate)
        except (TypeError, ValueError):
            continue
    return None


def _current_mapping(name: str) -> dict[str, Any]:
    value = getattr(_agent_context, name, None)
    if isinstance(value, Mapping):
        return dict(value)
    execution_metadata = getattr(_agent_context, "execution_metadata", None)
    if isinstance(execution_metadata, Mapping):
        value = execution_metadata.get(name)
        if isinstance(value, Mapping):
            return dict(value)
    return {}


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    try:
        return [str(item).strip() for item in value if str(item).strip()]
    except TypeError:
        text = str(value).strip()
        return [text] if text else []


def _safe_worker_id(role: str, idempotency_key: str | None) -> str:
    if idempotency_key:
        source = idempotency_key
    else:
        source = f"{role}-{uuid.uuid4().hex[:8]}"
    slug = re.sub(r"[^a-zA-Z0-9_.:-]+", "-", str(source).strip()).strip("-")
    return slug[:80] or f"worker-{uuid.uuid4().hex[:8]}"


def _step_key(role: str, objective: str, idempotency_key: str | None) -> str:
    if idempotency_key:
        return f"spawn_worker:{_safe_worker_id(role, idempotency_key)}"
    digest = sha256(f"{role}\n{objective}\n{uuid.uuid4().hex}".encode("utf-8")).hexdigest()[:12]
    return f"spawn_worker:{_safe_worker_id(role, None)}:{digest}"


def _headless_thread_id(parent_run_id: int, step_key: str) -> str:
    digest = sha256(step_key.encode("utf-8")).hexdigest()[:16]
    return f"headless-worker:{parent_run_id}:{digest}"


def _merge_tool_policy(tool_policy: Any, *, headless: bool) -> dict[str, Any]:
    policy = dict(tool_policy) if isinstance(tool_policy, Mapping) else {}
    if not headless:
        return policy
    raw_blocked = policy.get("blocked_tools") or policy.get("disabled_tools") or []
    blocked = set(_string_list(raw_blocked))
    blocked.update(_HEADLESS_BLOCKED_TOOLS)
    policy["blocked_tools"] = sorted(blocked)
    return policy


def _assignment_payload(
    *,
    role: str,
    objective: str,
    assignment_id: str,
    allowed_files: Any,
    forbidden_files: Any,
    allowed_resources: Any,
    forbidden_resources: Any,
    expected_artifacts: Any,
    evidence_requirements: Any,
    acceptance_criteria: Any,
    risk_level: str,
    metadata: Any,
) -> dict[str, Any]:
    return {
        "id": assignment_id,
        "role": role,
        "objective": objective,
        "allowed_files": _string_list(allowed_files),
        "forbidden_files": _string_list(forbidden_files),
        "allowed_resources": _string_list(allowed_resources),
        "forbidden_resources": _string_list(forbidden_resources),
        "expected_artifacts": _string_list(expected_artifacts),
        "evidence_requirements": list(evidence_requirements or []) if isinstance(evidence_requirements, list) else [],
        "acceptance_criteria": dict(acceptance_criteria or {}) if isinstance(acceptance_criteria, Mapping) else {},
        "risk_level": risk_level,
        "metadata": dict(metadata or {}) if isinstance(metadata, Mapping) else {},
    }


async def _handle_spawn_worker(
    objective: str,
    role: str = "worker",
    message: str | None = None,
    headless: bool = False,
    idempotency_key: str | None = None,
    allowed_files: list[str] | None = None,
    forbidden_files: list[str] | None = None,
    allowed_resources: list[str] | None = None,
    forbidden_resources: list[str] | None = None,
    expected_artifacts: list[str] | None = None,
    evidence_requirements: list[dict] | None = None,
    acceptance_criteria: dict | None = None,
    risk_level: str = "medium",
    tool_policy: dict | None = None,
    metadata: dict | None = None,
    **_: Any,
) -> str:
    objective_text = str(objective or "").strip()
    if not objective_text:
        return json.dumps({"error": "spawn_worker requires objective"})

    parent_run_id = _current_run_id()
    if parent_run_id is None:
        return json.dumps({"error": "spawn_worker requires an active AgentRun context"})

    role_text = str(role or "worker").strip() or "worker"
    risk_text = str(risk_level or "medium").strip().lower() or "medium"
    if risk_text not in {"low", "medium", "high"}:
        risk_text = "medium"
    assignment_id = _safe_worker_id(role_text, idempotency_key)
    assignment = WorkerAssignment.from_payload(
        _assignment_payload(
            role=role_text,
            objective=objective_text,
            assignment_id=assignment_id,
            allowed_files=allowed_files,
            forbidden_files=forbidden_files,
            allowed_resources=allowed_resources,
            forbidden_resources=forbidden_resources,
            expected_artifacts=expected_artifacts,
            evidence_requirements=evidence_requirements,
            acceptance_criteria=acceptance_criteria,
            risk_level=risk_text,
            metadata=metadata,
        )
    )
    child_message = str(message or objective_text).strip()
    step_key = _step_key(role_text, objective_text, idempotency_key)
    merged_tool_policy = _merge_tool_policy(tool_policy, headless=bool(headless))
    execution_metadata = getattr(_agent_context, "execution_metadata", None)
    inherited_metadata = dict(execution_metadata or {}) if isinstance(execution_metadata, Mapping) else {}

    worker_metadata = {
        **(dict(metadata or {}) if isinstance(metadata, Mapping) else {}),
        "origin": "spawn_worker",
        "spawned_by_tool": True,
        "spawned_by_run_id": parent_run_id,
        "headless": bool(headless),
        "worker_role": assignment.role,
        "worker_assignment": assignment.to_payload(),
        "parent_node_id": assignment.id,
        "tool_policy": merged_tool_policy,
        "thread_attachment_context": inherited_metadata.get("thread_attachment_context"),
    }
    worker_metadata = {key: value for key, value in worker_metadata.items() if value is not None}

    async with UnitOfWork() as uow:
        store = AsyncAgentRunStore(uow.session)
        parent = await store.require_run(parent_run_id)
        existing = await store.child_run_for_step(parent.id, step_key)
        deduplicated = existing is not None
        if existing is not None:
            child = to_domain(existing)
        elif headless:
            child = await store.create_run(
                AgentRunRequest(
                    org_id=parent.org_id,
                    user_id=parent.user_id,
                    thread_id=_headless_thread_id(parent.id, step_key),
                    parent_run_id=parent.id,
                    root_run_id=parent.root_run_id or parent.id,
                    profile=parent.profile,
                    recipe=RunRecipe.WORKER,
                    message=child_message,
                    target_ref=_current_mapping("target_ref") or dict(parent.target_ref or {}),
                    workspace_ref=_current_mapping("workspace_ref") or dict(parent.workspace_ref or {}),
                    model_policy=dict(parent.model_policy or {}),
                    metadata={**worker_metadata, "parent_step_key": step_key},
                )
            )
            await store.append_event(
                run_event(
                    parent.id,
                    "run.child_created",
                    {"child_run_id": child.id, "recipe": child.recipe.value, "step_key": step_key},
                    root_run_id=parent.root_run_id or parent.id,
                )
            )
        else:
            child = await store.create_child_run(
                parent,
                recipe=RunRecipe.WORKER,
                message=child_message,
                step_key=step_key,
                profile=parent.profile,
                target_ref=_current_mapping("target_ref") or dict(parent.target_ref or {}),
                workspace_ref=_current_mapping("workspace_ref") or dict(parent.workspace_ref or {}),
                model_policy=dict(parent.model_policy or {}),
                metadata=worker_metadata,
            )
        await store.append_event(
            run_event(
                int(parent.id),
                "run.worker_spawned",
                {
                    "child_run_id": child.id,
                    "step_key": step_key,
                    "headless": bool(headless),
                    "role": assignment.role,
                    "objective": assignment.objective,
                },
                root_run_id=parent.root_run_id or parent.id,
                producer="spawn_worker",
            )
        )

    return json.dumps(
        {
            "ok": True,
            "status": "queued",
            "child_run_id": child.id,
            "run_id": child.id,
            "parent_run_id": parent_run_id,
            "root_run_id": child.root_run_id,
            "recipe": child.recipe.value,
            "role": assignment.role,
            "headless": bool(headless),
            "step_key": step_key,
            "deduplicated": deduplicated,
        },
        default=str,
    )


__all__ = ["_handle_spawn_worker"]
