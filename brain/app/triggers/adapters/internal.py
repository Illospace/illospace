"""Internal Illo/Cortex trigger adapters."""

from __future__ import annotations

from typing import Any

from brain.app.api.authorization import human_identity
from brain.app.triggers.contracts import IlloTrigger, stable_idempotency_key


def _idea_payload(idea: Any) -> dict[str, Any]:
    attachments = getattr(idea, "attachments", None) or []
    return {
        "title": str(getattr(idea, "title", "") or ""),
        "description": getattr(idea, "description", None),
        "status": getattr(idea, "status", None),
        "attachments_count": len(attachments) if isinstance(attachments, list) else 0,
    }


THREAD_DISCUSSION_SURFACE = "thread_discussion"
THREAD_DISCUSSION_REPLY_TOOL = "post_thread_discussion_reply"
THREAD_DISCUSSION_EVENT = "thread_discussion_mention"
THREAD_DISCUSSION_EVENT_TYPE = f"cortex.{THREAD_DISCUSSION_EVENT}"


def _discussion_mention_run_message(
    *,
    idea_data: dict[str, Any],
    idea_id: str,
    thread_message: str,
    discussion_trigger: dict[str, Any],
) -> str:
    comment_id = discussion_trigger.get("comment_id")
    return "\n".join(
        [
            f"[Thread: \"{idea_data['title']}\" | {idea_id}]",
            "",
            "A teammate summoned @illo from Thread Discussion.",
            "This is a separate Discussion conversation attached to the Thread, not an AI Timeline reply.",
            "Acknowledge or answer in Discussion with post_thread_discussion_reply when a visible response fits.",
            "Use read_thread_discussion if more Discussion context is needed.",
            "Treat the AI Timeline as related context only unless a separate tool explicitly asks you to act there.",
            "You may also act headlessly when no visible surface update is appropriate.",
            "",
            f"Triggering Discussion comment id: {comment_id}",
            f"Triggering Discussion comment: {thread_message[:2000]}",
        ]
    )


def build_cortex_notify_trigger(
    *,
    event: str,
    idea_id: str,
    idea: Any,
    user: dict[str, Any],
    thread_message: str = "",
    metadata: dict[str, Any] | None = None,
    priority: int = 0,
    effective_metadata: dict[str, Any] | None = None,
) -> IlloTrigger:
    """Normalize the existing Cortex /notify payload into an IlloTrigger."""
    if event not in {"idea_created", "thread_reply"}:
        raise ValueError(f"Unsupported Cortex notify event: {event}")
    org_id = str(getattr(idea, "org_id", None) or user.get("org_id") or "")
    actor = human_identity(user) if user and user.get("id") and user.get("id") != "system" else None
    idea_data = _idea_payload(idea)
    event_type = f"cortex.{event}"
    target = {
        "kind": "idea",
        "idea_id": idea_id,
        "idea_title": idea_data["title"],
    }

    if event == "idea_created":
        if thread_message:
            run_message = f"[Idea: \"{idea_data['title']}\" | {idea_id}]\n\n{thread_message[:2000]}"
        else:
            att_text = f"\nAttachments: {idea_data['attachments_count']} file(s)" if idea_data["attachments_count"] else ""
            desc_text = f"\n{idea_data['description']}" if idea_data["description"] else ""
            run_message = (
                f"[Idea: \"{idea_data['title']}\" | {idea_id}]\n\n"
                f"New idea created.{desc_text}{att_text}"
            )
        run_metadata = metadata
    else:
        run_metadata = effective_metadata if effective_metadata is not None else metadata
        run_message = f"[Idea: \"{idea_data['title']}\" | {idea_id}]\n\n{thread_message[:2000]}"

    payload = {
        "idea_id": idea_id,
        "idea": idea_data,
        "thread_message": thread_message[:2000],
        "run_message": run_message,
        "metadata": dict(run_metadata or {}),
        "priority": int(priority),
        "user_id": user.get("id") if user else None,
    }
    idempotency_key = stable_idempotency_key(
        source="cortex",
        event_type=event_type,
        org_id=org_id,
        target=target,
        payload={
            "thread_message": payload["thread_message"],
            "priority": payload["priority"],
        },
    )
    return IlloTrigger(
        source="cortex",
        event_type=event_type,
        actor=actor,
        org_id=org_id,
        target=target,
        payload=payload,
        idempotency_key=idempotency_key,
        policy={
            "route": "run",
            "run_event": event,
            "priority": int(priority),
            "auth_path": "cortex_session",
        },
    )


def build_thread_discussion_mention_trigger(
    *,
    idea_id: str,
    idea: Any,
    comment: Any,
    user: dict[str, Any],
    discussion_trigger: dict[str, Any],
    metadata: dict[str, Any] | None = None,
    priority: int = 0,
) -> IlloTrigger:
    """Normalize a Thread Discussion @illo mention into its own conversation trigger.

    Discussion is not a disguised Cortex thread reply. It is a separate comment
    surface linked to the Thread, so this trigger gets its own event and target
    kind. Keeping that split explicit prevents agents from answering Discussion
    mentions in the AI Timeline by accident.
    """
    org_id = str(getattr(idea, "org_id", None) or user.get("org_id") or "")
    actor = human_identity(user) if user and user.get("id") and user.get("id") != "system" else None
    idea_data = _idea_payload(idea)
    comment_id = int(getattr(comment, "id"))
    body = str(getattr(comment, "body", "") or "")
    discussion_trigger = dict(discussion_trigger or {})
    metadata = dict(metadata or {})
    metadata.setdefault("source", THREAD_DISCUSSION_SURFACE)
    metadata.setdefault("originating_surface", THREAD_DISCUSSION_SURFACE)
    metadata.setdefault("triggering_surface", THREAD_DISCUSSION_SURFACE)
    metadata.setdefault("source_surface", THREAD_DISCUSSION_SURFACE)
    metadata.setdefault("required_response_tool", THREAD_DISCUSSION_REPLY_TOOL)
    metadata.setdefault("final_answer_target_surface", THREAD_DISCUSSION_SURFACE)
    metadata.setdefault("discussion_comment_id", comment_id)
    metadata.setdefault("discussion_trigger", discussion_trigger)

    target = {
        "kind": THREAD_DISCUSSION_SURFACE,
        "idea_id": idea_id,
        "parent_thread_id": idea_id,
        "discussion_comment_id": comment_id,
        "surface": THREAD_DISCUSSION_SURFACE,
    }
    payload = {
        "idea_id": idea_id,
        "parent_thread_id": idea_id,
        "idea": idea_data,
        "discussion": discussion_trigger,
        "thread_message": body[:2000],
        "run_message": _discussion_mention_run_message(
            idea_data=idea_data,
            idea_id=idea_id,
            thread_message=body,
            discussion_trigger=discussion_trigger,
        ),
        "metadata": metadata,
        "priority": int(priority),
        "user_id": user.get("id") if user else None,
    }
    idempotency_key = stable_idempotency_key(
        source="cortex",
        event_type=THREAD_DISCUSSION_EVENT_TYPE,
        org_id=org_id,
        target=target,
        payload={
            "discussion_comment_id": comment_id,
            "thread_message": payload["thread_message"],
            "priority": payload["priority"],
        },
    )
    return IlloTrigger(
        source="cortex",
        event_type=THREAD_DISCUSSION_EVENT_TYPE,
        actor=actor,
        org_id=org_id,
        target=target,
        payload=payload,
        idempotency_key=idempotency_key,
        policy={
            "route": "run",
            "run_event": THREAD_DISCUSSION_EVENT,
            "priority": int(priority),
            "auth_path": "cortex_discussion_session",
        },
    )


def build_chat_mention_trigger(
    *,
    event: str,
    conversation: Any,
    message: Any,
    user: dict[str, Any],
    root_message: Any | None = None,
    priority: int = 0,
) -> IlloTrigger:
    """Normalize a native team-room @illo mention into an IlloTrigger."""
    if event not in {"room_message_mention", "room_thread_mention"}:
        raise ValueError(f"Unsupported chat mention event: {event}")

    org_id = str(getattr(conversation, "org_id", None) or user.get("org_id") or "")
    actor = human_identity(user) if user and user.get("id") and user.get("id") != "system" else None
    event_type = f"chat.{event}"
    conversation_id = str(getattr(conversation, "id", "") or "")
    message_id = int(getattr(message, "id"))
    thread_root_message_id = getattr(message, "thread_root_message_id", None)
    if thread_root_message_id is not None:
        thread_root_message_id = int(thread_root_message_id)
    reply_to_message_id = getattr(message, "reply_to_message_id", None)
    if reply_to_message_id is not None:
        reply_to_message_id = int(reply_to_message_id)
    surface = "team_room_thread" if event == "room_thread_mention" else "team_room"
    response_thread_root_message_id = thread_root_message_id if surface == "team_room_thread" else None
    chat_trigger = {
        "conversation_id": conversation_id,
        "message_id": message_id,
        "thread_root_message_id": thread_root_message_id,
        "reply_to_message_id": reply_to_message_id,
        "surface": surface,
        "response_target": {
            "conversation_id": conversation_id,
            "thread_root_message_id": response_thread_root_message_id,
        },
    }
    target = {
        "kind": "chat_message",
        "conversation_id": conversation_id,
        "message_id": message_id,
        "thread_root_message_id": thread_root_message_id,
        "surface": surface,
    }

    sender_name = str(user.get("name") or "A teammate")
    body = str(getattr(message, "body", "") or "")
    root_body = str(getattr(root_message, "body", "") or "") if root_message is not None else ""
    context_lines = [
        f"{sender_name} tagged @illo from the native team room.",
        "Decide whether to answer directly, create Cortex thoughts, or both.",
        "After acting, post a concise response back to the originating chat surface with post_chat_message.",
        "",
        f"Chat surface: {surface}",
        f"Conversation: {conversation_id}",
        f"Message id: {message_id}",
    ]
    if root_body:
        context_lines.extend(["", f"Thread root message: {root_body[:1000]}"])
    context_lines.extend(["", f"Tagged message: {body[:2000]}"])
    run_message = "\n".join(context_lines)
    metadata = {
        "chat_trigger": chat_trigger,
        "origin": "native_chat_mention",
        "required_response_tool": "post_chat_message",
    }
    payload = {
        "chat": chat_trigger,
        "thread_message": body[:2000],
        "run_message": run_message,
        "metadata": metadata,
        "priority": int(priority),
        "user_id": user.get("id") if user else None,
    }
    idempotency_key = stable_idempotency_key(
        source="chat",
        event_type=event_type,
        org_id=org_id,
        target=target,
        payload={"message_id": message_id, "priority": int(priority)},
    )
    return IlloTrigger(
        source="chat",
        event_type=event_type,
        actor=actor,
        org_id=org_id,
        target=target,
        payload=payload,
        idempotency_key=idempotency_key,
        policy={
            "route": "run",
            "run_event": event,
            "priority": int(priority),
            "auth_path": "chat_session",
        },
    )
