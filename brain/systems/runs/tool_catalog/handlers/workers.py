"""Worker orchestration tool handlers."""

from __future__ import annotations

from collections.abc import Mapping
from hashlib import sha256
import json
import logging
import re
import uuid
from typing import Any

from brain.platform.db.repositories.unit_of_work import UnitOfWork
from brain.platform.effort import EFFORT_TIERS, EFFORT_TIER_SET
from brain.platform.integrations.provider_auth_preflight import (
    ProviderAuthPreflightResult,
    async_probe_provider_auth,
    skipped_provider_auth_preflight,
)
from brain.platform.providers.model_policy import (
    DEFAULT_PROVIDER_MODELS,
    PROVIDER_MODEL_OPTIONS,
    async_get_default_model,
    async_get_default_thinking,
    coerce_openai_api_key_model,
    infer_provider_from_model,
    normalize_model_name,
)
from brain.systems.runs.assignments import WorkerAssignment
from brain.systems.runs.domain import RunRecipe
from brain.systems.runs.evidence_health import (
    WorkerEvidenceFailure,
    record_parent_evidence_failures,
)
from brain.systems.runs.events import run_event
from brain.systems.runs.execution_context import _agent_context
from brain.systems.runs.routing_metadata import effective_routing_snapshot
from brain.systems.runs.store import AsyncAgentRunStore
from brain.systems.runs.tool_policy import normalize_tool_policy


logger = logging.getLogger(__name__)

_HEADLESS_BLOCKED_TOOLS = frozenset({
    "cortex_reply",
    "cortex_visual_reply",
    "manage_idea",
    "manage_workspace_app",
    "post_chat_message",
    "post_ai_timeline_message",
    "post_thread_discussion_reply",
})

_MATERIALIZED_PROJECT_CONTEXT_KEYS = frozenset({
    "project_context_materialization",
    "project_context_permission_scope",
    "project_context_snapshot",
    "project_runtime_context",
    "project_workspace_manifest",
    "resolved_workspace_root",
    "workspace_root",
    "workspaces",
})
_SPAWN_WORKER_AUTH_PROVIDERS = frozenset({"anthropic", "openai"})


def _with_spawn_worker_auth_presentation(
    result: ProviderAuthPreflightResult,
) -> ProviderAuthPreflightResult:
    if not result.blocked:
        return result

    credential = result.credential or "provider"
    if credential == "Anthropic API key":
        repair_action = (
            "Add an Anthropic API key in Settings > Access, then retry the run."
        )
        detail = f"the {credential} is not configured"
    elif credential == "OpenAI runtime":
        repair_action = (
            "Add an OpenAI API key in Settings > Access, then retry the run."
        )
        detail = f"the {credential} credential is unavailable"
    else:
        repair_action = (
            "Reconnect OpenAI in Settings > Access by signing in to Codex / ChatGPT again, "
            "then retry the run."
        )
        detail = (
            f"the {credential} credential is unavailable or could not be refreshed"
        )
    return result.with_presentation(
        repair_action=repair_action,
        visible_message=f"spawn_worker auth blocked: {detail}. {repair_action}",
    )


async def _preflight_spawn_worker_auth(
    session: Any,
    *,
    user_id: str | None,
    org_id: str | None,
    model: str,
) -> ProviderAuthPreflightResult:
    provider = infer_provider_from_model(model)
    if provider not in _SPAWN_WORKER_AUTH_PROVIDERS:
        return skipped_provider_auth_preflight(
            provider=provider,
            model=model,
        )
    result = await async_probe_provider_auth(
        session,
        user_id=user_id,
        org_id=org_id,
        provider=provider,
        model=model,
    )
    return _with_spawn_worker_auth_presentation(result)


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
    explicit_run_id = _coerce_run_id(getattr(_agent_context, "run_id", None))
    if explicit_run_id is not None:
        return explicit_run_id

    execution_metadata = getattr(_agent_context, "execution_metadata", None)
    if isinstance(execution_metadata, Mapping):
        metadata_run_id = _coerce_run_id(execution_metadata.get("run_id"))
        if metadata_run_id is not None:
            return metadata_run_id

    run = getattr(_agent_context, "run", None)
    candidates = (
        getattr(run, "run_id", None),
        getattr(run, "id", None),
    )
    for candidate in candidates:
        run_id = _coerce_run_id(candidate)
        if run_id is not None:
            return run_id
    return None


def _coerce_run_id(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
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


def _current_effective_routing() -> dict[str, Any]:
    execution_metadata = getattr(_agent_context, "execution_metadata", None)
    if not isinstance(execution_metadata, Mapping):
        return {}
    routing = execution_metadata.get("routing")
    if not isinstance(routing, Mapping):
        return {}
    effective = routing.get("effective")
    return dict(effective) if isinstance(effective, Mapping) else {}


def _validate_spawn_effort(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip().lower()
    if normalized not in EFFORT_TIER_SET:
        accepted = ", ".join(EFFORT_TIERS)
        raise ValueError(
            f"spawn_worker effort must be one of: {accepted}; got {value!r}"
        )
    return normalized


def _validate_spawn_model(value: Any) -> tuple[str | None, str | None]:
    if value is None:
        return None, None
    requested = str(value).strip().lower()
    providers = ", ".join(sorted(DEFAULT_PROVIDER_MODELS))
    if requested in DEFAULT_PROVIDER_MODELS:
        return f"{requested}/{DEFAULT_PROVIDER_MODELS[requested]}", requested

    normalized = requested.replace(":", "/", 1)
    if "/" not in normalized:
        raise ValueError(
            "spawn_worker model must be a provider name "
            f"({providers}) or a provider-prefixed catalog id such as "
            "'anthropic/claude-sonnet-4-6'"
        )
    allowed_route = coerce_openai_api_key_model(normalize_model_name(normalized))
    if allowed_route:
        raise ValueError(
            f"spawn_worker model {normalized!r} requires an OpenAI API key; "
            f"use the allowed subscription route {allowed_route!r}"
        )
    provider, model_name = normalized.split("/", 1)
    if provider not in PROVIDER_MODEL_OPTIONS:
        raise ValueError(
            f"spawn_worker model provider must be one of: {providers}; got {provider!r}"
        )
    options = PROVIDER_MODEL_OPTIONS[provider]
    if model_name not in options:
        raise ValueError(
            f"spawn_worker model for provider {provider!r} must be one of: "
            f"{', '.join(options)}; got {model_name!r}"
        )
    return f"{provider}/{model_name}", requested


def _canonical_inherited_model(value: Any) -> str:
    model = str(value or "").strip()
    normalized = model.replace(":", "/", 1)
    if "/" in normalized:
        provider, model_name = normalized.split("/", 1)
        if model_name in PROVIDER_MODEL_OPTIONS.get(provider, ()):
            return f"{provider}/{model_name}"
        return model
    for provider, options in PROVIDER_MODEL_OPTIONS.items():
        if normalized in options:
            return f"{provider}/{normalized}"
    return model


async def _materialized_parent_policy(session: Any, parent: Any) -> dict[str, Any]:
    policy = dict(parent.model_policy or {})
    live_routing = _current_effective_routing()

    model = str(live_routing.get("model") or policy.get("model") or "").strip()
    if not model:
        model = await async_get_default_model(
            session,
            include_provider_prefix=True,
            user_id=parent.user_id,
            org_id=parent.org_id,
        )
    inherited_model = _canonical_inherited_model(model)
    coerced_model = coerce_openai_api_key_model(normalize_model_name(inherited_model))
    if coerced_model:
        logger.warning(
            "Coercing inherited API-key OpenAI model %s to %s for spawned worker",
            inherited_model,
            coerced_model,
            extra={
                "event": "api_key_model_coerced",
                "routing_source": "spawn_worker.inherited",
                "requested_value": inherited_model,
                "coerced_value": coerced_model,
            },
        )
        inherited_model = coerced_model
    policy["model"] = inherited_model

    thinking = str(
        live_routing.get("effort")
        or live_routing.get("thinking")
        or policy.get("thinking")
        or ""
    ).strip().lower()
    if not thinking:
        thinking = await async_get_default_thinking(
            session,
            user_id=parent.user_id,
            org_id=parent.org_id,
        )
    policy["thinking"] = thinking
    return policy


def _requested_routing(
    *,
    model: str,
    effort: str,
    requested_model: str | None,
    effort_overridden: bool,
) -> dict[str, Any]:
    model_request: dict[str, Any] = {
        "value": model,
        "source": "spawn_worker.model" if requested_model is not None else "parent_effective",
    }
    if requested_model is not None and requested_model != model:
        model_request["requested_value"] = requested_model
    return {
        "model": model_request,
        "effort": {
            "value": effort,
            "source": "spawn_worker.effort" if effort_overridden else "parent_effective",
        },
    }


def _routing_summary(model: str, effort: str) -> dict[str, Any]:
    return effective_routing_snapshot(model, effort)


def _inherited_run_mapping(
    parent_value: Any,
    current_value: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Merge live tool context without discarding durable Project materialization."""

    parent = dict(parent_value or {}) if isinstance(parent_value, Mapping) else {}
    current = dict(current_value or {}) if isinstance(current_value, Mapping) else {}
    merged = {**parent, **current}
    for key in _MATERIALIZED_PROJECT_CONTEXT_KEYS:
        if key in parent:
            merged[key] = parent[key]
    return merged


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
    return normalize_tool_policy(
        tool_policy,
        disabled_tools=_HEADLESS_BLOCKED_TOOLS if headless else None,
    )


def _delegation_next_action(*, tool_name: str, response_tool: str | None, headless: bool) -> dict[str, Any]:
    instruction = (
        f"{tool_name} has made this delegated work durable. Do not call {tool_name} again "
        "for the same objective unless the user asks for another distinct delegation."
    )
    if response_tool:
        instruction += (
            f" Use {response_tool} to give the waiting surface a brief model-authored update "
            "with the returned run id/status, then continue only with distinct follow-up work."
        )
    elif headless:
        instruction += (
            " This run is headless, so settle with a concise final answer for the caller or "
            "monitor only distinct follow-up work."
        )
    else:
        instruction += (
            " Give the caller a concise model-authored update with the returned run id/status, "
            "then continue only with distinct follow-up work."
        )
    return {
        "instruction": instruction,
        "repeat_guard": {
            "tool": tool_name,
            "same_objective": "do_not_repeat",
        },
    }


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


def _assignment_shard(
    assignment: WorkerAssignment,
    *,
    idempotency_key: str | None,
) -> str:
    metadata = assignment.metadata if isinstance(assignment.metadata, Mapping) else {}
    for source in (metadata, assignment.to_payload()):
        for key in ("shard", "repo", "source", "resource_id"):
            value = str(source.get(key) or "").strip()
            if value:
                return value
    if len(assignment.allowed_resources) == 1:
        return str(assignment.allowed_resources[0])
    if idempotency_key:
        return assignment.id
    return assignment.role


async def _handle_spawn_worker(
    objective: str,
    role: str = "worker",
    message: str | None = None,
    effort: str | None = None,
    model: str | None = None,
    headless: bool = False,
    join_parent: bool = False,
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
    _runtime_run_id: int | str | None = None,
    **_: Any,
) -> str:
    objective_text = str(objective or "").strip()
    if not objective_text:
        return json.dumps({"error": "spawn_worker requires objective"})

    try:
        effort_override = _validate_spawn_effort(effort)
        model_override, requested_model = _validate_spawn_model(model)
    except ValueError as exc:
        return json.dumps({"error": str(exc)})

    parent_run_id = _coerce_run_id(_runtime_run_id) or _current_run_id()
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
        "worker_shard": _assignment_shard(
            assignment,
            idempotency_key=idempotency_key,
        ),
        "worker_assignment": assignment.to_payload(),
        "parent_node_id": assignment.id,
        "tool_policy": merged_tool_policy,
        "thread_attachment_context": inherited_metadata.get("thread_attachment_context"),
    }
    if join_parent:
        worker_metadata["join_parent"] = True
    else:
        worker_metadata.pop("join_parent", None)
    worker_metadata = {key: value for key, value in worker_metadata.items() if value is not None}
    response_tool = _current_agent_value("required_response_tool")
    response_tool_text = str(response_tool).strip() if response_tool else None

    async with UnitOfWork() as uow:
        store = AsyncAgentRunStore(uow.session)
        parent = await store.require_run(parent_run_id)
        child_policy = await _materialized_parent_policy(uow.session, parent)
        if model_override is not None:
            child_policy["model"] = model_override
        if effort_override is not None:
            child_policy["thinking"] = effort_override
        inherited_routing = (
            dict(worker_metadata.get("routing") or {})
            if isinstance(worker_metadata.get("routing"), Mapping)
            else {}
        )
        inherited_routing["requested"] = _requested_routing(
            model=str(child_policy["model"]),
            effort=str(child_policy["thinking"]),
            requested_model=requested_model,
            effort_overridden=effort_override is not None,
        )
        worker_metadata["routing"] = inherited_routing
        existing_child_lookup = getattr(store, "child_run_for_step", None)
        existing_child = (
            await existing_child_lookup(parent.id, step_key)
            if callable(existing_child_lookup)
            else None
        )
        if existing_child is None:
            preflight = await _preflight_spawn_worker_auth(
                uow.session,
                user_id=parent.user_id,
                org_id=parent.org_id,
                model=str(child_policy["model"]),
            )
            if preflight.blocked:
                shard = str(worker_metadata["worker_shard"])
                failure = WorkerEvidenceFailure.for_admission(
                    worker_role=assignment.role,
                    shard=shard,
                    configuration_error=preflight.error_code,
                    provider=preflight.provider,
                    credential=preflight.credential,
                    error=(
                        preflight.visible_message
                        or "spawn_worker provider authentication is unavailable."
                    ),
                )
                await record_parent_evidence_failures(
                    uow.session,
                    parent_run_id=int(parent.id),
                    failures=[failure],
                )
                return json.dumps(
                    {
                        "ok": False,
                        "status": "auth_blocked",
                        "error": preflight.visible_message,
                        "error_code": preflight.error_code,
                        "configuration_error": preflight.error_code,
                        "provider": preflight.provider,
                        "credential": preflight.credential,
                        "parent_run_id": parent_run_id,
                        "shard": shard,
                        "worker_slot_consumed": False,
                    },
                    default=str,
                )
        child, created = await store.create_child_run_with_result(
            parent,
            recipe=RunRecipe.WORKER,
            message=child_message,
            step_key=step_key,
            thread_id=_headless_thread_id(parent.id, step_key) if headless else None,
            profile=parent.profile,
            target_ref=_inherited_run_mapping(
                parent.target_ref,
                _current_mapping("target_ref"),
            ),
            workspace_ref=_inherited_run_mapping(
                parent.workspace_ref,
                _current_mapping("workspace_ref"),
            ),
            model_policy=child_policy,
            metadata=worker_metadata,
        )
        deduplicated = not created
        persisted_policy = dict(getattr(child, "model_policy", None) or child_policy)
        child_model = str(persisted_policy.get("model") or child_policy["model"])
        child_effort = str(
            persisted_policy.get("thinking") or child_policy["thinking"]
        )
        child_routing = _routing_summary(child_model, child_effort)
        if created:
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
                        "routing": child_routing,
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
            "model": child_model,
            "effort": child_effort,
            "routing": child_routing,
            "next_action": _delegation_next_action(
                tool_name="spawn_worker",
                response_tool=response_tool_text,
                headless=bool(headless),
            ),
        },
        default=str,
    )


__all__ = ["_handle_spawn_worker"]
