"""Shared work-intake builders for product triggers and Cortex runs."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, replace
from datetime import datetime
from types import SimpleNamespace
from typing import Any

from sqlalchemy import text

from brain.platform.db.models.idea import Idea
from brain.platform.integrations.provider_auth_preflight import (
    ProviderAuthBlockedPreflightResult,
    async_probe_provider_auth,
)
from brain.platform.providers.model_policy import (
    EFFORT_TIER_SET,
    async_get_default_model,
    coerce_openai_api_key_model,
    infer_provider_from_model,
    normalize_model_name,
)
from brain.systems.cortex.project_context.resolution import resolve_effective_project_context
from brain.systems.cortex.thread_context import async_build_agent_visible_thread_context
from brain.systems.runs.direct_targets import (
    DirectHeadlessTarget,
    resolve_direct_target,
)
from brain.systems.runs.domain import AgentRunRequest, RunProfile, RunRecipe
from brain.systems.runs.interactive_reply import is_interactive_slack_reply_context
from brain.systems.runs.skill_commands import annotate_metadata_with_slash_skill_commands
from brain.systems.runs.status_questions import (
    build_same_thread_run_context,
    is_status_question,
)
from brain.systems.runs.store import AsyncAgentRunStore

_VALID_MODEL_PROVIDERS = {"anthropic", "openai"}
THREAD_DISCUSSION_SURFACE = "thread_discussion"
THREAD_DISCUSSION_REPLY_TOOL = "post_thread_discussion_reply"
THREAD_DISCUSSION_THREAD_PREFIX = "thread-discussion:"
AGENT_RUN_CONTINUATION_TARGET = "agent_run_continuation"

logger = logging.getLogger("work_intake")


@dataclass(frozen=True)
class WorkIntakeActor:
    id: str | None = None
    org_id: str | None = None
    internal: bool = False
    principal_type: str | None = None
    name: str | None = None


@dataclass(frozen=True)
class WorkIntakeTarget:
    kind: str
    idea_id: str | None = None
    conversation_id: str | None = None
    message_id: int | str | None = None
    thread_root_message_id: int | str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class WorkIntakePolicy:
    priority: int = 0
    producer: str | None = None
    idempotency_key: str | None = None
    run_event: str | None = None


@dataclass(frozen=True)
class WorkIntakeEvent:
    source: str
    event_type: str
    org_id: str
    actor: dict[str, Any] | WorkIntakeActor | None
    target: dict[str, Any] | WorkIntakeTarget
    payload: dict[str, Any] = field(default_factory=dict)
    policy: dict[str, Any] | WorkIntakePolicy = field(default_factory=dict)

    @classmethod
    def from_trigger_payload(cls, trigger_payload: dict[str, Any] | Any) -> "WorkIntakeEvent":
        trigger = _trigger_dict(trigger_payload)
        policy = _trigger_policy(trigger)
        policy.setdefault("idempotency_key", trigger.get("idempotency_key"))
        policy.setdefault("producer", "trigger")
        return cls(
            source=str(trigger.get("source") or ""),
            event_type=str(trigger.get("event_type") or ""),
            org_id=str(_trigger_org_id(trigger) or ""),
            actor=trigger.get("actor"),
            target=_trigger_target(trigger),
            payload=_trigger_payload(trigger),
            policy=policy,
        )


@dataclass(frozen=True)
class WorkIntakeResult:
    ok: bool
    run_id: int | None = None
    skipped_reason: str | None = None


def _as_mapping(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    if hasattr(value, "__dataclass_fields__"):
        return {
            key: item
            for key, item in value.__dict__.items()
            if item not in (None, "", {}, [])
        }
    return dict(value or {})


def _trigger_dict(trigger_payload: dict[str, Any] | Any) -> dict[str, Any]:
    if hasattr(trigger_payload, "to_payload"):
        return dict(trigger_payload.to_payload())
    return dict(trigger_payload or {})


def _trigger_payload(trigger_payload: dict[str, Any] | Any) -> dict[str, Any]:
    trigger = _trigger_dict(trigger_payload)
    payload = trigger.get("payload")
    return dict(payload or {}) if isinstance(payload, dict) else {}


def _trigger_target(trigger_payload: dict[str, Any] | Any) -> dict[str, Any]:
    trigger = _trigger_dict(trigger_payload)
    target = trigger.get("target")
    return dict(target or {}) if isinstance(target, dict) else {}


def _trigger_policy(trigger_payload: dict[str, Any] | Any) -> dict[str, Any]:
    trigger = _trigger_dict(trigger_payload)
    policy = trigger.get("policy")
    return dict(policy or {}) if isinstance(policy, dict) else {}


def _trigger_actor_user_id(trigger_payload: dict[str, Any] | Any, *, org_id: str | None = None) -> str | None:
    actor = _trigger_dict(trigger_payload).get("actor")
    if not isinstance(actor, dict):
        return None
    if actor.get("internal") is True:
        return None
    if org_id and str(actor.get("org_id") or "") not in {"", str(org_id)}:
        return None
    actor_id = str(actor.get("id") or "").strip()
    return actor_id or None


def _actor_org_id(actor: Any) -> str | None:
    return str(_as_mapping(actor).get("org_id") or "").strip() or None


def _trigger_org_id(trigger_payload: dict[str, Any] | Any) -> str | None:
    trigger = _trigger_dict(trigger_payload)
    return str(trigger.get("org_id") or "").strip() or _actor_org_id(trigger.get("actor"))


def _merge_trigger_metadata(
    trigger_payload: dict[str, Any] | Any,
    metadata: dict[str, Any] | None,
) -> dict[str, Any]:
    trigger = _trigger_dict(trigger_payload)
    payload = dict(metadata or {})
    payload["illo_trigger"] = {
        "source": trigger.get("source"),
        "event_type": trigger.get("event_type"),
        "idempotency_key": trigger.get("idempotency_key"),
        "target": dict(trigger.get("target") or {}),
        "policy": dict(trigger.get("policy") or {}),
        "actor": trigger.get("actor"),
    }
    return payload


def metadata_choice(
    metadata: dict[str, Any],
    keys: tuple[str, ...],
    valid_values: set[str] | frozenset[str],
    default: str,
) -> str:
    for key in keys:
        raw = metadata.get(key)
        if raw is None:
            continue
        value = str(raw).strip().lower()
        if value in valid_values:
            return value
        logger.warning(
            "Ignoring invalid metadata value for %s; falling through to lower-priority keys",
            key,
        )
    return default


def model_policy_from_metadata(metadata: dict[str, Any] | None) -> dict[str, str]:
    metadata = metadata or {}
    policy: dict[str, str] = {}
    thinking = metadata_choice(
        metadata,
        ("thinking_tier", "effort", "effort_level", "thinking"),
        EFFORT_TIER_SET,
        "",
    )
    if thinking:
        policy["thinking"] = thinking
    raw_model = metadata.get("model") or metadata.get("model_name")
    if isinstance(raw_model, str) and raw_model.strip():
        policy["model"] = raw_model.strip().replace(":", "/", 1)
    provider = metadata_choice(
        metadata,
        ("provider", "preferred_provider", "model_provider"),
        _VALID_MODEL_PROVIDERS,
        "",
    )
    if provider:
        policy["provider"] = provider
    return policy


def _routing_metadata_source(metadata: dict[str, Any]) -> str:
    source = str(metadata.get("source") or "").strip()
    if source:
        return source
    for container_key in ("work_intake", "illo_trigger"):
        container = metadata.get(container_key)
        if isinstance(container, dict):
            source = str(container.get("source") or "").strip()
            if source:
                return source
    return "unknown"


def _warn_retired_recipe_coercion(
    metadata: dict[str, Any],
    *,
    field: str,
    requested_value: str,
) -> None:
    source = _routing_metadata_source(metadata)
    logger.warning(
        "Coercing retired %s run %s to fast (source=%s)",
        requested_value,
        field,
        source,
        extra={
            "event": "deep_run_coerced",
            "routing_source": source,
            "routing_field": field,
            "requested_value": requested_value,
            "coerced_value": "fast",
        },
    )


def _warn_api_key_model_coercion(
    metadata: dict[str, Any],
    *,
    requested_value: str,
    coerced_value: str,
) -> None:
    source = _routing_metadata_source(metadata)
    logger.warning(
        "Coercing API-key OpenAI model %s to %s (source=%s)",
        requested_value,
        coerced_value,
        source,
        extra={
            "event": "api_key_model_coerced",
            "routing_source": source,
            "requested_value": requested_value,
            "coerced_value": coerced_value,
        },
    )


def profile_from_metadata(metadata: dict[str, Any] | None) -> RunProfile:
    metadata = metadata or {}
    raw = (
        metadata.get("execution_profile")
        or metadata.get("requested_run_profile")
        or metadata.get("run_profile")
        or metadata.get("profile")
        or "fast"
    )
    try:
        profile = RunProfile(str(raw).strip().lower())
    except Exception:
        return RunProfile.FAST
    if profile is RunProfile.DEEP:
        _warn_retired_recipe_coercion(
            metadata,
            field="profile",
            requested_value=profile.value,
        )
        return RunProfile.FAST
    return profile


def recipe_for_profile(profile: RunProfile, metadata: dict[str, Any] | None) -> RunRecipe:
    metadata = metadata or {}
    raw_recipe = metadata.get("recipe")
    if raw_recipe:
        try:
            recipe = RunRecipe(str(raw_recipe).strip().lower())
        except Exception:
            pass
        else:
            if recipe in {RunRecipe.DEEP, RunRecipe.SCOUT}:
                _warn_retired_recipe_coercion(
                    metadata,
                    field="recipe",
                    requested_value=recipe.value,
                )
                return RunRecipe.FAST
            return recipe
    if profile is RunProfile.DEEP:
        _warn_retired_recipe_coercion(
            metadata,
            field="profile",
            requested_value=profile.value,
        )
    return RunRecipe.FAST


def _chat_thread_id(chat_trigger: dict[str, Any], target: dict[str, Any]) -> str:
    conversation_id = str(
        chat_trigger.get("conversation_id") or target.get("conversation_id") or ""
    )
    message_id = chat_trigger.get("thread_root_message_id") or chat_trigger.get("message_id")
    if not conversation_id or not message_id:
        raise ValueError("Chat run triggers require conversation_id and message_id")
    return f"chat:{conversation_id}:{message_id}"


def _thread_discussion_parent_thread_id(
    discussion_trigger: dict[str, Any],
    target: dict[str, Any],
) -> str:
    response_target = discussion_trigger.get("response_target")
    if not isinstance(response_target, dict):
        response_target = {}
    thread_id = str(
        response_target.get("thread_id")
        or discussion_trigger.get("thread_id")
        or target.get("parent_thread_id")
        or target.get("idea_id")
        or ""
    ).strip()
    if not thread_id:
        raise ValueError("Thread Discussion run triggers require a parent Thread id")
    return thread_id


def _thread_discussion_conversation_id(parent_thread_id: str) -> str:
    return f"{THREAD_DISCUSSION_THREAD_PREFIX}{parent_thread_id}"


def _run_event(trigger: dict[str, Any], policy: dict[str, Any]) -> str:
    event_type = str(trigger.get("event_type") or "")
    return str(policy.get("run_event") or event_type.split(".", 1)[-1])


def _priority(payload: dict[str, Any], policy: dict[str, Any]) -> int:
    try:
        return int(policy.get("priority") or payload.get("priority") or 0)
    except (TypeError, ValueError):
        return 0


def _payload_user_id(
    payload: dict[str, Any],
    trigger_payload: dict[str, Any] | Any,
    *,
    org_id: str | None,
) -> str | None:
    raw_user_id = payload.get("user_id")
    if raw_user_id and raw_user_id != "system":
        return str(raw_user_id)
    return _trigger_actor_user_id(trigger_payload, org_id=org_id)


def _event_actor_user_id(event: WorkIntakeEvent) -> str | None:
    actor = _as_mapping(event.actor)
    if actor.get("internal") is True:
        return None
    actor_id = str(actor.get("id") or actor.get("user_id") or "").strip()
    return actor_id or None


def _event_org_id(event: WorkIntakeEvent) -> str | None:
    return str(event.org_id or "").strip() or _actor_org_id(event.actor)


def _event_target(event: WorkIntakeEvent) -> dict[str, Any]:
    target = _as_mapping(event.target)
    extra = target.pop("extra", None)
    if isinstance(extra, dict):
        target.update(extra)
    return target


def _event_policy(event: WorkIntakeEvent) -> dict[str, Any]:
    return _as_mapping(event.policy)


def _event_metadata(event: WorkIntakeEvent) -> dict[str, Any]:
    payload = dict(event.payload or {})
    metadata = dict(payload.get("metadata") or {}) if isinstance(payload.get("metadata"), dict) else {}
    policy = _event_policy(event)
    org_id = _event_org_id(event)
    if org_id:
        metadata["org_id"] = org_id
    metadata["work_intake"] = {
        "source": event.source,
        "event_type": event.event_type,
        "org_id": org_id,
        "target": _event_target(event),
        "policy": policy,
        "actor": _as_mapping(event.actor),
    }
    return metadata


def _event_message(event: WorkIntakeEvent) -> str:
    payload = dict(event.payload or {})
    return str(payload.get("run_message") or payload.get("message") or payload.get("thread_message") or "")


def _event_priority(event: WorkIntakeEvent) -> int:
    return _priority(dict(event.payload or {}), _event_policy(event))


def _event_idempotency_key(event: WorkIntakeEvent) -> str | None:
    policy = _event_policy(event)
    return policy.get("idempotency_key") or policy.get("idempotencyKey")


def _event_producer(event: WorkIntakeEvent) -> str:
    return str(_event_policy(event).get("producer") or "work_intake")


def _event_run_event(event: WorkIntakeEvent) -> str:
    policy = _event_policy(event)
    return str(policy.get("run_event") or event.event_type.split(".", 1)[-1] or event.event_type)


def _event_workspace_ref(event: WorkIntakeEvent) -> dict[str, Any]:
    payload = dict(event.payload or {})
    workspace_ref = payload.get("workspace_ref")
    return dict(workspace_ref or {}) if isinstance(workspace_ref, dict) else {}


def _event_model_policy(event: WorkIntakeEvent, metadata: dict[str, Any]) -> dict[str, Any]:
    payload = dict(event.payload or {})
    model_policy = payload.get("model_policy")
    if isinstance(model_policy, dict):
        return dict(model_policy)
    return model_policy_from_metadata(metadata)


def _agent_run_request_for_chat(trigger_payload: dict[str, Any] | Any) -> AgentRunRequest:
    trigger = _trigger_dict(trigger_payload)
    target = _trigger_target(trigger)
    payload = _trigger_payload(trigger)
    policy = _trigger_policy(trigger)
    trigger_org_id = _trigger_org_id(trigger)
    metadata = _merge_trigger_metadata(
        trigger,
        payload.get("metadata") if isinstance(payload.get("metadata"), dict) else None,
    )
    chat_trigger = dict(metadata.get("chat_trigger") or payload.get("chat") or {})
    run_event = _run_event(trigger, policy)
    priority = _priority(payload, policy)
    profile = profile_from_metadata(metadata)
    thread_id = _chat_thread_id(chat_trigger, target)
    message = str(payload.get("run_message") or payload.get("message") or "")
    target_ref = {
        **target,
        "kind": "chat_message",
        "event": str(run_event),
        "chat_trigger": chat_trigger,
    }
    request_metadata = {
        **metadata,
        "event": str(run_event),
        "priority": priority,
        "source": f"trigger:{trigger.get('source')}",
        "producer": "trigger",
        "idempotency_key": trigger.get("idempotency_key"),
        "org_id": trigger_org_id,
    }
    return AgentRunRequest(
        org_id=trigger_org_id,
        user_id=_payload_user_id(payload, trigger, org_id=trigger_org_id),
        thread_id=thread_id,
        message=message,
        profile=profile,
        recipe=recipe_for_profile(profile, metadata),
        target_ref=target_ref,
        workspace_ref={},
        model_policy=model_policy_from_metadata(metadata),
        metadata=request_metadata,
    )


async def _agent_run_request_for_thread_discussion(
    session: Any,
    trigger_payload: dict[str, Any] | Any,
) -> AgentRunRequest:
    trigger = _trigger_dict(trigger_payload)
    target = _trigger_target(trigger)
    payload = _trigger_payload(trigger)
    policy = _trigger_policy(trigger)
    trigger_org_id = _trigger_org_id(trigger)
    actor_user_id = _payload_user_id(payload, trigger, org_id=trigger_org_id)
    metadata = _merge_trigger_metadata(
        trigger,
        payload.get("metadata") if isinstance(payload.get("metadata"), dict) else None,
    )
    discussion_trigger = dict(metadata.get("discussion_trigger") or payload.get("discussion") or {})
    parent_thread_id = _thread_discussion_parent_thread_id(discussion_trigger, target)
    idea = await _a_get_idea_for_intake(
        session,
        parent_thread_id,
        fallback_user_id=actor_user_id,
    )
    if idea is None:
        raise LookupError(f"Thread {parent_thread_id} not found")

    surface_context = {
        "originating_surface": THREAD_DISCUSSION_SURFACE,
        "triggering_surface": THREAD_DISCUSSION_SURFACE,
        "source_surface": THREAD_DISCUSSION_SURFACE,
        "required_response_tool": THREAD_DISCUSSION_REPLY_TOOL,
        "final_answer_target_surface": THREAD_DISCUSSION_SURFACE,
    }
    for key, value in surface_context.items():
        metadata.setdefault(key, value)
    metadata["discussion_trigger"] = discussion_trigger

    message = str(payload.get("run_message") or payload.get("message") or payload.get("thread_message") or "")
    metadata = annotate_metadata_with_slash_skill_commands(metadata, message)
    profile = profile_from_metadata(metadata)
    recipe = recipe_for_profile(profile, metadata)
    priority = _priority(payload, policy)
    run_event = _run_event(trigger, policy)
    project_context, project_context_snapshot, project_context_validation_errors = await _a_select_project_context(
        session,
        idea=idea,
        idea_id=parent_thread_id,
        metadata=metadata,
    )
    target_ref = {
        **target,
        "kind": THREAD_DISCUSSION_SURFACE,
        "event": str(run_event),
        "surface": THREAD_DISCUSSION_SURFACE,
        "idea_id": parent_thread_id,
        "parent_thread_id": parent_thread_id,
        "discussion_trigger": discussion_trigger,
        **surface_context,
        "related_surfaces": {
            "ai_timeline": {
                "kind": "ai_timeline",
                "thread_id": parent_thread_id,
            }
        },
    }
    workspace_ref = dict(project_context) if project_context_snapshot else {}
    if project_context_validation_errors:
        metadata["project_context_validation_errors"] = project_context_validation_errors
    if project_context_snapshot:
        metadata["project_context"] = project_context
        metadata.pop("project_context_snapshot", None)
        target_ref["project_context_snapshot"] = project_context_snapshot
        workspace_ref["project_context_snapshot"] = project_context_snapshot
    else:
        metadata.pop("project_context", None)
        metadata.pop("project_context_snapshot", None)
    if not isinstance(metadata.get("thread_context"), dict):
        thread_context = await async_build_agent_visible_thread_context(
            session,
            parent_thread_id,
            current_message=message,
        )
        if thread_context:
            metadata["thread_context"] = thread_context

    return AgentRunRequest(
        org_id=str(getattr(idea, "org_id", None) or trigger_org_id or "") or None,
        user_id=actor_user_id or getattr(idea, "user_id", None),
        thread_id=_thread_discussion_conversation_id(parent_thread_id),
        message=message,
        profile=profile,
        recipe=recipe,
        target_ref=target_ref,
        workspace_ref=workspace_ref,
        model_policy=model_policy_from_metadata(metadata),
        metadata={
            **metadata,
            "event": str(run_event),
            "priority": priority,
            "source": trigger.get("source"),
            "producer": "trigger",
            "idempotency_key": trigger.get("idempotency_key"),
            "org_id": trigger_org_id,
        },
    )


def _build_direct_target_request(
    event: WorkIntakeEvent,
    *,
    target: DirectHeadlessTarget,
    metadata: dict[str, Any],
    message: str,
    producer: str,
    idempotency_key: str | None,
    priority: int,
) -> AgentRunRequest:
    profile = profile_from_metadata(metadata)
    return AgentRunRequest(
        org_id=_event_org_id(event),
        user_id=_event_actor_user_id(event),
        thread_id=target.thread_id,
        message=message,
        profile=profile,
        recipe=recipe_for_profile(profile, metadata),
        target_ref=dict(target.value),
        workspace_ref=_event_workspace_ref(event),
        model_policy=_event_model_policy(event, metadata),
        metadata={
            **metadata,
            "event": _event_run_event(event),
            "priority": priority,
            "source": event.source,
            "producer": producer,
            "idempotency_key": idempotency_key,
        },
    )


def _build_agent_run_continuation_request(
    event: WorkIntakeEvent,
    *,
    target: dict[str, Any],
    metadata: dict[str, Any],
    message: str,
    producer: str,
    idempotency_key: str | None,
    priority: int,
) -> AgentRunRequest:
    thread_id = str(target.get("thread_id") or "").strip()
    if not thread_id:
        raise ValueError("Agent run continuation requires thread_id")
    target_ref = target.get("target_ref")
    if not isinstance(target_ref, dict):
        raise ValueError("Agent run continuation requires target_ref")
    profile = profile_from_metadata(metadata)
    return AgentRunRequest(
        org_id=_event_org_id(event),
        user_id=_event_actor_user_id(event),
        thread_id=thread_id,
        message=message,
        profile=profile,
        recipe=recipe_for_profile(profile, metadata),
        target_ref=dict(target_ref),
        workspace_ref=_event_workspace_ref(event),
        model_policy=_event_model_policy(event, metadata),
        metadata={
            **metadata,
            "event": _event_run_event(event),
            "priority": priority,
            "source": event.source,
            "producer": producer,
            "idempotency_key": idempotency_key,
        },
    )


async def build_agent_run_request(
    session: Any,
    event: WorkIntakeEvent,
) -> AgentRunRequest:
    request = await _build_agent_run_request(session, event)
    model_policy = dict(request.model_policy or {})
    requested_model = str(
        model_policy.get("model") or model_policy.get("model_override") or ""
    ).strip()
    if requested_model:
        normalized_model = normalize_model_name(requested_model)
        coerced_model = coerce_openai_api_key_model(normalized_model)
        # A bare API-key name (e.g. "gpt-4.1") is re-routed by normalization
        # itself; store the normalized id so the raw value never executes.
        target_model = coerced_model or normalized_model
        if coerced_model or coerce_openai_api_key_model(requested_model):
            _warn_api_key_model_coercion(
                request.metadata,
                requested_value=requested_model,
                coerced_value=target_model,
            )
        if (
            model_policy.get("model") != target_model
            or "model_override" in model_policy
        ):
            model_policy["model"] = target_model
            model_policy.pop("model_override", None)
            request = replace(request, model_policy=model_policy)
    status_question = is_status_question(request.message)
    interactive_slack_thread = _is_interactive_slack_thread(request)
    if not status_question and not interactive_slack_thread:
        return request

    same_thread_context = await build_same_thread_run_context(
        session,
        thread_id=request.thread_id,
        org_id=request.org_id,
        include_status_details=status_question,
    )
    live_siblings = (
        list(same_thread_context.get("live_sibling_runs") or [])
        if isinstance(same_thread_context, dict)
        else []
    )
    if not status_question and not live_siblings:
        return request
    return replace(
        request,
        metadata={
            **request.metadata,
            "same_thread_run_context": same_thread_context,
        },
    )


def _is_interactive_slack_thread(request: AgentRunRequest) -> bool:
    metadata = request.metadata if isinstance(request.metadata, dict) else {}
    target = request.target_ref if isinstance(request.target_ref, dict) else {}
    return bool(
        str(request.thread_id or "").startswith("slack:")
        and target.get("kind") == "slack_message"
        and is_interactive_slack_reply_context(metadata, target)
    )


async def _build_agent_run_request(
    session: Any,
    event: WorkIntakeEvent,
) -> AgentRunRequest:
    target = _event_target(event)
    metadata = _event_metadata(event)
    message = _event_message(event)
    producer = _event_producer(event)
    idempotency_key = _event_idempotency_key(event)
    priority = _event_priority(event)

    if event.source == "chat" or target.get("kind") == "chat_message":
        org_id = _event_org_id(event)
        trigger_payload = {
            "source": event.source,
            "event_type": event.event_type,
            "actor": _as_mapping(event.actor),
            "org_id": org_id,
            "target": target,
            "payload": {
                **dict(event.payload or {}),
                "message": message,
                "metadata": metadata,
            },
            "idempotency_key": idempotency_key,
            "policy": {
                **_event_policy(event),
                "priority": priority,
            },
        }
        request = _agent_run_request_for_chat(trigger_payload)
        return AgentRunRequest(
            **{
                **request.__dict__,
                "metadata": {
                    **request.metadata,
                    "source": event.source,
                    "producer": producer,
                    "idempotency_key": idempotency_key,
                    "work_intake": metadata["work_intake"],
                },
            }
        )

    if event.source == "slack" or target.get("kind") == "slack_message":
        from brain.systems.runs.work_intake_slack import agent_run_request_for_slack

        org_id = _event_org_id(event)
        trigger_payload = {
            "source": event.source,
            "event_type": event.event_type,
            "actor": _as_mapping(event.actor),
            "org_id": org_id,
            "target": target,
            "payload": {
                **dict(event.payload or {}),
                "message": message,
                "metadata": metadata,
            },
            "idempotency_key": idempotency_key,
            "policy": {
                **_event_policy(event),
                "priority": priority,
            },
        }
        request = agent_run_request_for_slack(trigger_payload)
        return AgentRunRequest(
            **{
                **request.__dict__,
                "metadata": {
                    **request.metadata,
                    "source": event.source,
                    "producer": producer,
                    "idempotency_key": idempotency_key,
                    "work_intake": metadata["work_intake"],
                },
            }
        )

    if target.get("kind") == THREAD_DISCUSSION_SURFACE:
        org_id = _event_org_id(event)
        trigger_payload = {
            "source": event.source,
            "event_type": event.event_type,
            "actor": _as_mapping(event.actor),
            "org_id": org_id,
            "target": target,
            "payload": {
                **dict(event.payload or {}),
                "message": message,
                "metadata": metadata,
            },
            "idempotency_key": idempotency_key,
            "policy": {
                **_event_policy(event),
                "priority": priority,
            },
        }
        request = await _agent_run_request_for_thread_discussion(session, trigger_payload)
        return AgentRunRequest(
            **{
                **request.__dict__,
                "metadata": {
                    **request.metadata,
                    "source": event.source,
                    "producer": producer,
                    "idempotency_key": idempotency_key,
                    "work_intake": metadata["work_intake"],
                },
            }
        )

    direct_target = resolve_direct_target(target)
    if direct_target is not None:
        return _build_direct_target_request(
            event,
            target=direct_target,
            metadata=metadata,
            message=message,
            producer=producer,
            idempotency_key=idempotency_key,
            priority=priority,
        )

    if target.get("kind") == AGENT_RUN_CONTINUATION_TARGET:
        return _build_agent_run_continuation_request(
            event,
            target=target,
            metadata=metadata,
            message=message,
            producer=producer,
            idempotency_key=idempotency_key,
            priority=priority,
        )

    idea_id = str(target.get("idea_id") or "")
    if not idea_id:
        raise ValueError("Work intake target requires idea_id for Cortex run admission")
    payload = dict(event.payload or {})
    payload_model_policy = payload.get("model_policy")
    return await _agent_run_request_for_cortex(
        session,
        idea_id=idea_id,
        event=_event_run_event(event),
        message=message,
        user_id=_event_actor_user_id(event),
        metadata=metadata,
        priority=priority,
        source=event.source,
        producer=producer,
        idempotency_key=idempotency_key,
        payload_model_policy=(
            payload_model_policy if isinstance(payload_model_policy, dict) else None
        ),
        deadline_at=payload.get("deadline_at"),
    )


def _metadata_int(metadata: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        raw = metadata.get(key)
        if raw in (None, ""):
            continue
        try:
            return int(str(raw))
        except (TypeError, ValueError):
            continue
    return None


def _surface_context_for_target(metadata: dict[str, Any]) -> dict[str, Any]:
    surface_context: dict[str, Any] = {}
    for key in (
        "originating_surface",
        "triggering_surface",
        "source_surface",
        "required_response_tool",
        "final_answer_target_surface",
    ):
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            surface_context[key] = value.strip()
    discussion_trigger = metadata.get("discussion_trigger")
    if isinstance(discussion_trigger, dict):
        surface_context["discussion_trigger"] = dict(discussion_trigger)
    return surface_context


def _session_dialect_name(session: Any) -> str | None:
    try:
        bind = session.get_bind()
    except Exception:
        bind = getattr(session, "bind", None)
    dialect = getattr(bind, "dialect", None)
    name = getattr(dialect, "name", None)
    return str(name) if name else None


async def _a_get_idea_for_intake(
    session: Any,
    idea_id: str,
    *,
    fallback_user_id: str | None = None,
) -> Any | None:
    if hasattr(session, "get"):
        try:
            idea = await session.get(Idea, idea_id)
            if idea is not None:
                return idea
        except (AttributeError, TypeError, ValueError):
            pass
    if not hasattr(session, "execute"):
        return None
    result = await session.execute(
        text(
            "SELECT id, title, org_id, user_id, agent_details "
            "FROM ideas WHERE id = :idea_id"
        ),
        {"idea_id": idea_id},
    )
    row = result.mappings().first()
    if row is None:
        if _session_dialect_name(session) == "sqlite":
            return SimpleNamespace(
                id=idea_id,
                title=None,
                org_id=None,
                user_id=fallback_user_id,
                agent_details=None,
            )
        return None
    agent_details = row.get("agent_details")
    if isinstance(agent_details, str):
        try:
            agent_details = json.loads(agent_details)
        except json.JSONDecodeError:
            agent_details = None
    return SimpleNamespace(
        id=str(row.get("id")),
        title=row.get("title"),
        org_id=str(row.get("org_id")) if row.get("org_id") else None,
        user_id=str(row.get("user_id")) if row.get("user_id") else None,
        agent_details=agent_details,
    )


async def _a_select_project_context(
    session: Any,
    *,
    idea: Idea,
    idea_id: str,
    metadata: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any] | None, list[dict[str, Any]]]:
    resolution = await resolve_effective_project_context(
        session,
        idea=idea,
        idea_id=idea_id,
        metadata=metadata,
    )
    return resolution.project_context, resolution.snapshot, resolution.validation_errors


async def _agent_run_request_for_cortex(
    session: Any,
    *,
    idea_id: str,
    event: str,
    message: str,
    user_id: str | None,
    metadata: dict[str, Any] | None = None,
    priority: int = 0,
    source: str | None = None,
    producer: str | None = None,
    idempotency_key: str | None = None,
    payload_model_policy: dict[str, Any] | None = None,
    deadline_at: datetime | None = None,
) -> AgentRunRequest:
    idea = await _a_get_idea_for_intake(session, idea_id, fallback_user_id=user_id)
    if idea is None:
        raise LookupError(f"Idea {idea_id} not found")
    metadata = annotate_metadata_with_slash_skill_commands(metadata, message)
    profile = profile_from_metadata(metadata)
    recipe = recipe_for_profile(profile, metadata)
    project_context, project_context_snapshot, project_context_validation_errors = await _a_select_project_context(
        session,
        idea=idea,
        idea_id=idea_id,
        metadata=metadata,
    )
    target_ref = {
        "kind": "cortex_idea",
        "idea_id": idea_id,
        "event": event,
        "title": getattr(idea, "title", None),
    }
    target_ref.update(_surface_context_for_target(metadata))
    workspace_ref = dict(project_context) if project_context_snapshot else {}
    if project_context_validation_errors:
        metadata["project_context_validation_errors"] = project_context_validation_errors
    if project_context_snapshot:
        metadata["project_context"] = project_context
        metadata.pop("project_context_snapshot", None)
        target_ref["project_context_snapshot"] = project_context_snapshot
        workspace_ref["project_context_snapshot"] = project_context_snapshot
    else:
        metadata.pop("project_context", None)
        metadata.pop("project_context_snapshot", None)
    if not isinstance(metadata.get("thread_context"), dict):
        thread_context = await async_build_agent_visible_thread_context(
            session,
            idea_id,
            current_thread_message_id=_metadata_int(
                metadata,
                "thread_message_id",
                "current_thread_message_id",
            ),
            current_message=message,
        )
        if thread_context:
            metadata["thread_context"] = thread_context
    owner_user_id = user_id or getattr(idea, "user_id", None)
    org_id = str(getattr(idea, "org_id", None) or metadata.get("org_id") or "") or None
    return AgentRunRequest(
        org_id=org_id,
        user_id=owner_user_id,
        thread_id=idea_id,
        message=message,
        profile=profile,
        recipe=recipe,
        target_ref=target_ref,
        workspace_ref=workspace_ref,
        model_policy=(
            dict(payload_model_policy)
            if payload_model_policy
            else model_policy_from_metadata(metadata)
        ),
        deadline_at=deadline_at,
        metadata={
            **metadata,
            "event": event,
            "priority": priority,
            "source": source,
            "producer": producer,
            "idempotency_key": idempotency_key,
            "org_id": org_id,
        },
    )


async def _mark_cortex_working_if_possible(
    session: Any,
    *,
    request: AgentRunRequest,
    run_id: int,
) -> None:
    target = request.target_ref if isinstance(request.target_ref, dict) else {}
    if target.get("kind") != "cortex_idea" or not hasattr(session, "add"):
        return
    idea_id = str(target.get("idea_id") or request.thread_id or "")
    if not idea_id or not hasattr(session, "get"):
        return
    try:
        idea = await session.get(Idea, idea_id)
    except Exception:
        return
    if idea is None:
        return
    previous_status = str(getattr(idea, "status", "") or "")
    if previous_status in {"archived", "resolved", "working"}:
        return
    from brain.systems.cortex.thought_lifecycle import ThoughtStatusCommand, transition_thought_status

    await transition_thought_status(
        session,
        idea=idea,
        command=ThoughtStatusCommand(
            to_status="working",
            trigger="agent_run_admitted",
            run_id=int(run_id),
        ),
    )


async def admit_work(
    session: Any,
    event: WorkIntakeEvent,
) -> WorkIntakeResult:
    try:
        request = await build_agent_run_request(session, event)
        model_policy = dict(request.model_policy or {})
        model = str(
            model_policy.get("model")
            or model_policy.get("model_override")
            or ""
        ).strip()
        requested_provider = str(model_policy.get("provider") or "").strip().lower()
        if not model:
            model = await async_get_default_model(
                session,
                provider=requested_provider or None,
                include_provider_prefix=True,
                user_id=request.user_id,
                org_id=request.org_id,
            )
        provider = infer_provider_from_model(model, default=requested_provider or None)
        if provider == "anthropic":
            auth_preflight = await async_probe_provider_auth(
                session,
                user_id=request.user_id,
                org_id=request.org_id,
                provider=provider,
                model=model,
            )
            if isinstance(auth_preflight, ProviderAuthBlockedPreflightResult):
                reason = (
                    f"{auth_preflight.error_code}: provider={provider} model={model} "
                    f"credential={auth_preflight.credential or 'unavailable'}"
                )
                logger.warning("Work admission blocked before run creation: %s", reason)
                return WorkIntakeResult(ok=False, skipped_reason=reason)
        from brain.systems.runs.open_asks import (
            annotate_request_with_open_ask,
            record_open_ask,
        )

        request, open_ask_context = annotate_request_with_open_ask(request)

        async def _admit_and_record():
            run = await AsyncAgentRunStore(session).create_run(request)
            await record_open_ask(
                session,
                context=open_ask_context,
                run_id=int(run.id),
            )
            await _mark_cortex_working_if_possible(
                session,
                request=request,
                run_id=int(run.id),
            )
            return run

        if open_ask_context is not None and hasattr(session, "begin_nested"):
            async with session.begin_nested():
                run = await _admit_and_record()
        else:
            run = await _admit_and_record()
        return WorkIntakeResult(ok=True, run_id=int(run.id))
    except Exception as exc:
        return WorkIntakeResult(ok=False, skipped_reason=str(exc))


__all__ = [
    "AGENT_RUN_CONTINUATION_TARGET",
    "build_agent_run_request",
    "admit_work",
    "model_policy_from_metadata",
    "profile_from_metadata",
    "recipe_for_profile",
    "WorkIntakeActor",
    "WorkIntakeEvent",
    "WorkIntakePolicy",
    "WorkIntakeResult",
    "WorkIntakeTarget",
]
