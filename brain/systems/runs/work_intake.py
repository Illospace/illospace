"""Shared work-intake builders for product triggers and Cortex runs."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

from sqlalchemy import select, text

from brain.platform.db.models.idea import Idea, IdeaProjectAttachment
from brain.systems.cortex.project_context.snapshot import (
    ProjectContextValidationError,
    validated_project_context_snapshot,
)
from brain.systems.cortex.thread_context import async_build_agent_visible_thread_context
from brain.systems.runs.domain import AgentRunRequest, RunProfile, RunRecipe
from brain.systems.runs.skill_commands import annotate_metadata_with_slash_skill_commands

_VALID_MODEL_TIERS = {"low", "medium", "high"}
_VALID_EFFORT_LEVELS = {"low", "medium", "high", "xhigh"}
_VALID_MODEL_PROVIDERS = {"anthropic", "openai"}


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


def merge_trigger_metadata(
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
    valid_values: set[str],
    default: str,
) -> str:
    for key in keys:
        raw = metadata.get(key)
        if raw is None:
            continue
        value = str(raw).strip().lower()
        if value in valid_values:
            return value
    return default


def model_policy_from_metadata(metadata: dict[str, Any] | None) -> dict[str, str]:
    metadata = metadata or {}
    policy = {
        "tier": metadata_choice(
            metadata,
            ("model_tier", "intelligence", "intelligence_tier"),
            _VALID_MODEL_TIERS,
            "high",
        ),
        "thinking": metadata_choice(
            metadata,
            ("thinking_tier", "effort", "effort_level", "thinking"),
            _VALID_EFFORT_LEVELS,
            "high",
        ),
    }
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
        return RunProfile(str(raw).strip().lower())
    except Exception:
        return RunProfile.FAST


def recipe_for_profile(profile: RunProfile, metadata: dict[str, Any] | None) -> RunRecipe:
    raw_recipe = (metadata or {}).get("recipe")
    if raw_recipe:
        try:
            return RunRecipe(str(raw_recipe).strip().lower())
        except Exception:
            pass
    return RunRecipe.DEEP if profile is RunProfile.DEEP else RunRecipe.FAST


def _chat_thread_id(chat_trigger: dict[str, Any], target: dict[str, Any]) -> str:
    conversation_id = str(
        chat_trigger.get("conversation_id") or target.get("conversation_id") or ""
    )
    message_id = chat_trigger.get("thread_root_message_id") or chat_trigger.get("message_id")
    if not conversation_id or not message_id:
        raise ValueError("Chat run triggers require conversation_id and message_id")
    return f"chat:{conversation_id}:{message_id}"


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


def build_chat_agent_run_request(trigger_payload: dict[str, Any] | Any) -> AgentRunRequest:
    trigger = _trigger_dict(trigger_payload)
    target = _trigger_target(trigger)
    payload = _trigger_payload(trigger)
    policy = _trigger_policy(trigger)
    metadata = merge_trigger_metadata(
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
        "org_id": trigger.get("org_id"),
    }
    return AgentRunRequest(
        org_id=str(trigger.get("org_id") or "") or None,
        user_id=_payload_user_id(payload, trigger, org_id=str(trigger.get("org_id") or "") or None),
        thread_id=thread_id,
        message=message,
        profile=profile,
        recipe=recipe_for_profile(profile, metadata),
        target_ref=target_ref,
        workspace_ref={},
        model_policy=model_policy_from_metadata(metadata),
        metadata=request_metadata,
    )


def build_cortex_run_admission_kwargs(trigger_payload: dict[str, Any] | Any) -> dict[str, Any]:
    trigger = _trigger_dict(trigger_payload)
    target = _trigger_target(trigger)
    payload = _trigger_payload(trigger)
    policy = _trigger_policy(trigger)
    idea_id = str(target.get("idea_id") or payload.get("idea_id") or "")
    if not idea_id:
        raise ValueError("Cortex run triggers require target.idea_id")
    metadata = merge_trigger_metadata(
        trigger,
        payload.get("metadata") if isinstance(payload.get("metadata"), dict) else None,
    )
    return {
        "idea_id": idea_id,
        "event": _run_event(trigger, policy),
        "message": str(payload.get("run_message") or payload.get("message") or ""),
        "priority": _priority(payload, policy),
        "user_id": _payload_user_id(payload, trigger, org_id=str(trigger.get("org_id") or "") or None),
        "metadata": metadata,
        "source": f"trigger:{trigger.get('source')}",
        "producer": "trigger",
        "idempotency_key": trigger.get("idempotency_key"),
    }


def _snapshot_for_project_context(project_context: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
    if not project_context:
        return None, []
    try:
        return validated_project_context_snapshot(project_context, validate_local_paths=False), []
    except ProjectContextValidationError as exc:
        return None, exc.errors
    except Exception:
        return None, ["Project Context could not be validated."]


def _project_context_from_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
    metadata = metadata or {}
    for key in ("project_context", "project_context_snapshot"):
        candidate = metadata.get(key)
        if isinstance(candidate, dict):
            return dict(candidate)
    return {}


def _project_context_from_idea(idea: Idea) -> dict[str, Any]:
    details = getattr(idea, "agent_details", None)
    if not isinstance(details, dict):
        return {}
    for key in ("project_context", "project_context_snapshot"):
        candidate = details.get(key)
        if isinstance(candidate, dict):
            return dict(candidate)
    return {}


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


def _trigger_actor_user_id_from_metadata(metadata: dict[str, Any], *, org_id: str | None = None) -> str | None:
    trigger = metadata.get("illo_trigger")
    if not isinstance(trigger, dict):
        return None
    actor = trigger.get("actor")
    if not isinstance(actor, dict):
        return None
    if actor.get("internal") is True:
        return None
    if org_id and str(actor.get("org_id") or "") not in {"", str(org_id)}:
        return None
    actor_id = str(actor.get("id") or "").strip()
    return actor_id or None


async def _a_latest_attached_project_context(session: Any, idea_id: str) -> dict[str, Any]:
    if not hasattr(session, "scalars"):
        return {}
    try:
        result = await session.scalars(
            select(IdeaProjectAttachment)
            .where(
                IdeaProjectAttachment.idea_id == idea_id,
                IdeaProjectAttachment.status != "invalid",
            )
            .order_by(IdeaProjectAttachment.created_at.desc(), IdeaProjectAttachment.id.desc())
        )
        attachment = result.first()
    except Exception:
        return {}
    snapshot = getattr(attachment, "snapshot", None)
    return dict(snapshot) if isinstance(snapshot, dict) else {}


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
    validation_errors: list[dict[str, Any]] = []
    for source, candidate in (
        ("metadata", _project_context_from_metadata(metadata)),
        ("idea", _project_context_from_idea(idea)),
    ):
        if not candidate:
            continue
        snapshot, errors = _snapshot_for_project_context(candidate)
        if snapshot:
            return candidate, snapshot, validation_errors
        validation_errors.append({"source": source, "errors": errors})

    candidate = await _a_latest_attached_project_context(session, idea_id)
    if candidate:
        snapshot, errors = _snapshot_for_project_context(candidate)
        if snapshot:
            return candidate, snapshot, validation_errors
        validation_errors.append({"source": "latest_attachment", "errors": errors})

    return {}, None, validation_errors


async def build_cortex_agent_run_request(
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
    owner_user_id = (
        _trigger_actor_user_id_from_metadata(
            metadata,
            org_id=str(getattr(idea, "org_id", "") or "") or None,
        )
        or user_id
        or getattr(idea, "user_id", None)
    )
    return AgentRunRequest(
        org_id=str(getattr(idea, "org_id", "") or "") or None,
        user_id=owner_user_id,
        thread_id=idea_id,
        message=message,
        profile=profile,
        recipe=recipe,
        target_ref=target_ref,
        workspace_ref=workspace_ref,
        model_policy=model_policy_from_metadata(metadata),
        metadata={
            **metadata,
            "event": event,
            "priority": priority,
            "source": source,
            "producer": producer,
            "idempotency_key": idempotency_key,
        },
    )


__all__ = [
    "build_chat_agent_run_request",
    "build_cortex_agent_run_request",
    "build_cortex_run_admission_kwargs",
    "merge_trigger_metadata",
    "model_policy_from_metadata",
    "profile_from_metadata",
    "recipe_for_profile",
]
