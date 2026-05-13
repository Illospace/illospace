"""Router for Illo-native triggers."""

from __future__ import annotations

from typing import Any

from brain.systems.runs import AgentRunRequest, RunRecipe, run_execution_profile
from brain.systems.runs.cortex import RunAdmissionRequest, async_admit_run
from brain.systems.runs.store import AsyncAgentRunStore
from brain.app.triggers.contracts import IlloTrigger, TriggerRouteResult

_CORTEX_TRIGGER_EVENTS = {"cortex.idea_created", "cortex.thread_reply"}
_CHAT_TRIGGER_EVENTS = {"chat.room_message_mention", "chat.room_thread_mention"}
_VALID_MODEL_TIERS = {"low", "medium", "high"}
_VALID_EFFORT_LEVELS = {"low", "medium", "high", "xhigh"}
_VALID_MODEL_PROVIDERS = {"anthropic", "openai"}


def _merge_trigger_metadata(trigger: IlloTrigger, metadata: dict[str, Any] | None) -> dict[str, Any]:
    payload = dict(metadata or {})
    payload["illo_trigger"] = {
        "source": trigger.source,
        "event_type": trigger.event_type,
        "idempotency_key": trigger.idempotency_key,
        "target": dict(trigger.target or {}),
        "policy": dict(trigger.policy or {}),
        "actor": trigger.to_payload().get("actor"),
    }
    return payload


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
        "tier": _metadata_choice(
            metadata,
            ("model_tier", "intelligence", "intelligence_tier"),
            _VALID_MODEL_TIERS,
            "high",
        ),
        "thinking": _metadata_choice(
            metadata,
            ("thinking_tier", "effort", "effort_level", "thinking"),
            _VALID_EFFORT_LEVELS,
            "high",
        ),
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


def _chat_thread_id(chat_trigger: dict[str, Any], target: dict[str, Any]) -> str:
    conversation_id = str(
        chat_trigger.get("conversation_id") or target.get("conversation_id") or ""
    )
    message_id = chat_trigger.get("thread_root_message_id") or chat_trigger.get("message_id")
    if not conversation_id or not message_id:
        raise ValueError("Chat run triggers require conversation_id and message_id")
    return f"chat:{conversation_id}:{message_id}"


async def _async_route_chat_trigger(trigger: IlloTrigger, *, session: Any | None = None) -> TriggerRouteResult:
    from brain.platform.db.repositories.unit_of_work import UnitOfWork

    async def _admit(active_session: Any) -> TriggerRouteResult:
        target = dict(trigger.target or {})
        payload = dict(trigger.payload or {})
        policy = dict(trigger.policy or {})
        metadata = _merge_trigger_metadata(
            trigger,
            payload.get("metadata") if isinstance(payload.get("metadata"), dict) else None,
        )
        chat_trigger = dict(metadata.get("chat_trigger") or payload.get("chat") or {})
        run_event = policy.get("run_event") or trigger.event_type.split(".", 1)[-1]
        priority = int(policy.get("priority") or payload.get("priority") or 0)
        user_id = payload.get("user_id")
        if user_id == "system":
            user_id = None
        profile = run_execution_profile(metadata)
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
            "source": f"trigger:{trigger.source}",
            "producer": "trigger",
            "idempotency_key": trigger.idempotency_key,
            "org_id": trigger.org_id,
        }
        run = await AsyncAgentRunStore(active_session).create_run(
            AgentRunRequest(
                org_id=trigger.org_id,
                user_id=str(user_id) if user_id else None,
                thread_id=thread_id,
                message=message,
                profile=profile,
                recipe=RunRecipe.DEEP if profile == "deep" else RunRecipe.FAST,
                target_ref=target_ref,
                workspace_ref={},
                model_policy=_model_policy_from_metadata(metadata),
                metadata=request_metadata,
            )
        )
        return TriggerRouteResult(ok=True, route="run", run_id=run.id)

    if session is not None:
        return await _admit(session)
    async with UnitOfWork() as uow:
        return await _admit(uow.session)


async def async_route_trigger(trigger: IlloTrigger, *, session: Any | None = None) -> TriggerRouteResult:
    """Async trigger routing for request handlers that already own an AsyncSession."""
    if trigger.event_type in _CHAT_TRIGGER_EVENTS:
        return await _async_route_chat_trigger(trigger, session=session)
    if trigger.event_type not in _CORTEX_TRIGGER_EVENTS:
        return TriggerRouteResult(
            ok=False,
            route="unsupported",
            skipped_reason=f"No router registered for {trigger.event_type}",
        )

    target = dict(trigger.target or {})
    payload = dict(trigger.payload or {})
    policy = dict(trigger.policy or {})
    idea_id = str(target.get("idea_id") or payload.get("idea_id") or "")
    if not idea_id:
        raise ValueError("Cortex run triggers require target.idea_id")

    run_event = policy.get("run_event") or trigger.event_type.split(".", 1)[-1]
    priority = int(policy.get("priority") or payload.get("priority") or 0)
    user_id = payload.get("user_id")
    if user_id == "system":
        user_id = None
    message = str(payload.get("run_message") or payload.get("message") or "")
    metadata = _merge_trigger_metadata(
        trigger,
        payload.get("metadata") if isinstance(payload.get("metadata"), dict) else None,
    )
    result = await async_admit_run(
        RunAdmissionRequest(
            idea_id=idea_id,
            event=str(run_event),
            message=message,
            priority=priority,
            user_id=str(user_id) if user_id else None,
            metadata=metadata,
            source=f"trigger:{trigger.source}",
            producer="trigger",
            idempotency_key=trigger.idempotency_key,
        ),
        session=session,
    )
    if not result.ok:
        return TriggerRouteResult(
            ok=False,
            route="run",
            skipped_reason=result.skipped_reason or "run_admission_failed",
        )
    return TriggerRouteResult(
        ok=True,
        route="run",
        run_id=result.run_id,
    )
