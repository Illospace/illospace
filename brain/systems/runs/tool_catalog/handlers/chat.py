"""Native chat tool handlers for AgentRun."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from typing import Any

from brain.systems.runs.tool_catalog.handlers.common import _agent_context, logger


def _run_coro_sync(coro):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(asyncio.run, coro).result()


def _current_chat_trigger() -> dict[str, Any]:
    trigger = getattr(_agent_context, "chat_trigger", None)
    if isinstance(trigger, dict):
        return dict(trigger)
    execution_metadata = getattr(_agent_context, "execution_metadata", None)
    if isinstance(execution_metadata, dict) and isinstance(
        execution_metadata.get("chat_trigger"),
        dict,
    ):
        return dict(execution_metadata["chat_trigger"])
    return {}


def _current_run_id() -> int | None:
    run_id = getattr(_agent_context, "run_id", None)
    if run_id is None:
        run = getattr(_agent_context, "run", None)
        run_id = getattr(run, "run_id", None) or getattr(run, "id", None)
    try:
        return int(run_id) if run_id is not None else None
    except Exception:
        return None


def _coerce_optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValueError("thread_root_message_id must be an integer")


async def _publish_chat_events(publish, summaries: dict[str, dict[str, Any]]) -> None:
    from brain.app.api.routers.chat import (
        _publish_message_events,
        _publish_notification_summary_payloads,
    )

    await _publish_message_events(
        publish,
        is_thread_reply=publish.root_message is not None,
    )
    await _publish_notification_summary_payloads(summaries)


def _handle_post_chat_message(
    body: str,
    conversation_id: str | None = None,
    thread_root_message_id: int | None = None,
) -> str:
    """Post an Illo-authored message to the native team room."""
    from brain.app.api.routers.chat import _notification_summary_payloads
    from brain.app.api.services.chat import ChatService
    from brain.platform.db.repositories.unit_of_work import UnitOfWork

    trigger = _current_chat_trigger()
    response_target = trigger.get("response_target") if isinstance(trigger.get("response_target"), dict) else {}
    target_conversation_id = str(conversation_id or response_target.get("conversation_id") or "").strip()
    target_thread_root_message_id = (
        _coerce_optional_int(thread_root_message_id)
        if thread_root_message_id is not None
        else _coerce_optional_int(response_target.get("thread_root_message_id"))
    )
    if not target_conversation_id:
        return json.dumps({"error": "post_chat_message requires conversation_id outside a chat-triggered run"})
    actor_user_id = str(getattr(_agent_context, "user_id", "") or "").strip()
    org_id = str(getattr(_agent_context, "org_id", "") or "").strip()
    if not actor_user_id or not org_id:
        return json.dumps({"error": "post_chat_message requires a user-scoped org run"})

    async def _create_message():
        async with UnitOfWork() as uow:
            message, publish = await ChatService(
                uow.session,
                {
                    "id": actor_user_id,
                    "org_id": org_id,
                    "role": "member",
                },
            ).post_agent_message(
                conversation_id=target_conversation_id,
                body=body,
                thread_root_message_id=target_thread_root_message_id,
                metadata={
                    "created_by_run_id": _current_run_id(),
                    "chat_trigger_message_id": trigger.get("message_id"),
                },
            )
            summaries = await _notification_summary_payloads(
                uow.session,
                org_id=org_id,
                user_ids=publish.member_ids,
            )
            return message.model_dump(mode="json"), publish, summaries

    message_payload, publish, summaries = _run_coro_sync(_create_message())
    if publish is not None:
        try:
            _run_coro_sync(_publish_chat_events(publish, summaries))
        except Exception as exc:
            logger.warning("agent_chat_publish_failed: %s", exc)
    return json.dumps({"ok": True, "message": message_payload}, default=str)


__all__ = ["_handle_post_chat_message"]
