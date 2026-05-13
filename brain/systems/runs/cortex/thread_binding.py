"""Bind Cortex thoughts/messages to AgentRun requests."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from brain.systems.cortex.project_context.snapshot import (
    ProjectContextValidationError,
    validated_project_context_snapshot,
)
from brain.systems.runs.domain import AgentRunRequest, RunProfile, RunRecipe
from brain.systems.runs.skill_commands import annotate_metadata_with_slash_skill_commands
from brain.systems.cortex.thread_context import (
    async_build_agent_visible_thread_context,
    build_agent_visible_thread_context,
)
from brain.platform.db.models.idea import Idea, IdeaProjectAttachment


_VALID_MODEL_TIERS = {"low", "medium", "high"}
_VALID_EFFORT_LEVELS = {"low", "medium", "high", "xhigh"}
_VALID_MODEL_PROVIDERS = {"anthropic", "openai"}


def _metadata_choice(
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


def _model_policy_from_metadata(metadata: dict[str, Any] | None) -> dict[str, str]:
    metadata = metadata or {}
    policy = {
        "tier": _metadata_choice(metadata, ("model_tier", "intelligence", "intelligence_tier"), _VALID_MODEL_TIERS, "high"),
        "thinking": _metadata_choice(metadata, ("thinking_tier", "effort", "effort_level", "thinking"), _VALID_EFFORT_LEVELS, "high"),
    }
    raw_model = metadata.get("model") or metadata.get("model_name")
    if isinstance(raw_model, str) and raw_model.strip():
        policy["model"] = raw_model.strip().replace(":", "/", 1)
    provider = _metadata_choice(
        metadata,
        ("provider", "preferred_provider", "model_provider"),
        _VALID_MODEL_PROVIDERS,
        "",
    )
    if provider:
        policy["provider"] = provider
    return policy


def _profile_from_metadata(metadata: dict[str, Any] | None) -> RunProfile:
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


def _recipe_for_profile(profile: RunProfile, metadata: dict[str, Any] | None) -> RunRecipe:
    raw_recipe = (metadata or {}).get("recipe")
    if raw_recipe:
        try:
            return RunRecipe(str(raw_recipe).strip().lower())
        except Exception:
            pass
    return RunRecipe.DEEP if profile is RunProfile.DEEP else RunRecipe.FAST


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


def _latest_attached_project_context(session: Session, idea_id: str) -> dict[str, Any]:
    if not hasattr(session, "scalars"):
        return {}
    try:
        attachment = session.scalars(
            select(IdeaProjectAttachment)
            .where(
                IdeaProjectAttachment.idea_id == idea_id,
                IdeaProjectAttachment.status != "invalid",
            )
            .order_by(IdeaProjectAttachment.created_at.desc(), IdeaProjectAttachment.id.desc())
        ).first()
    except Exception:
        return {}
    snapshot = getattr(attachment, "snapshot", None)
    return dict(snapshot) if isinstance(snapshot, dict) else {}


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


def _select_project_context(
    session: Session,
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

    candidate = _latest_attached_project_context(session, idea_id)
    if candidate:
        snapshot, errors = _snapshot_for_project_context(candidate)
        if snapshot:
            return candidate, snapshot, validation_errors
        validation_errors.append({"source": "latest_attachment", "errors": errors})

    return {}, None, validation_errors


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


def build_run_request(
    session: Session,
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
    idea = session.get(Idea, idea_id)
    if idea is None:
        raise LookupError(f"Idea {idea_id} not found")
    metadata = annotate_metadata_with_slash_skill_commands(metadata, message)
    profile = _profile_from_metadata(metadata)
    recipe = _recipe_for_profile(profile, metadata)
    project_context, project_context_snapshot, project_context_validation_errors = _select_project_context(
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
        thread_context = build_agent_visible_thread_context(
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
    return AgentRunRequest(
        org_id=str(getattr(idea, "org_id", "") or "") or None,
        user_id=user_id or getattr(idea, "user_id", None),
        thread_id=idea_id,
        message=message,
        profile=profile,
        recipe=recipe,
        target_ref=target_ref,
        workspace_ref=workspace_ref,
        model_policy=_model_policy_from_metadata(metadata),
        metadata={
            **metadata,
            "event": event,
            "priority": priority,
            "source": source,
            "producer": producer,
            "idempotency_key": idempotency_key,
        },
    )


async def a_build_run_request(
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
    idea = await session.get(Idea, idea_id)
    if idea is None:
        raise LookupError(f"Idea {idea_id} not found")
    metadata = annotate_metadata_with_slash_skill_commands(metadata, message)
    profile = _profile_from_metadata(metadata)
    recipe = _recipe_for_profile(profile, metadata)
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
    return AgentRunRequest(
        org_id=str(getattr(idea, "org_id", "") or "") or None,
        user_id=user_id or getattr(idea, "user_id", None),
        thread_id=idea_id,
        message=message,
        profile=profile,
        recipe=recipe,
        target_ref=target_ref,
        workspace_ref=workspace_ref,
        model_policy=_model_policy_from_metadata(metadata),
        metadata={
            **metadata,
            "event": event,
            "priority": priority,
            "source": source,
            "producer": producer,
            "idempotency_key": idempotency_key,
        },
    )


__all__ = ["a_build_run_request", "build_run_request"]
