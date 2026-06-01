"""Core lifecycle for Cortex thought thread messages."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from brain.platform.db.models.idea import IdeaStateLog, IdeaThread
from brain.platform.db.models.notification import (
    NOTIFICATION_KIND_WORKSPACE_MENTION,
    NOTIFICATION_KIND_WORKSPACE_THREAD_ATTENTION,
    NOTIFICATION_SOURCE_WORKSPACE,
)
from brain.systems.cortex.object_references import (
    SOURCE_IDEA_THREAD,
    merge_object_reference_metadata,
    store_object_references_for_source,
)
from brain.systems.cortex.thread_read_model import (
    deterministic_preview_summary,
    refresh_thread_read_model,
)
from brain.systems.cortex.status import (
    ASSISTANT_REPLY_BLOCKED_IDEA_STATUSES,
    ASSISTANT_REPLY_UNREAD_IDEA_STATUSES,
    USER_MESSAGE_ACTIVATES_IDEA_STATUSES,
)

MentionResolver = Callable[[list[str], str], Awaitable[dict[str, str]]]
ProductEventPublisher = Callable[[str, dict[str, Any]], Any]
ProjectContextExtractor = Callable[[list[dict[str, Any]], dict[str, Any] | None], dict[str, Any] | None]
ProjectContextValidator = Callable[[dict[str, Any] | None], dict[str, Any] | None]
AttachmentContextBuilder = Callable[[list[dict[str, Any]]], dict[str, Any] | None]
MessageTypeParser = Callable[[str, str], str]
LiveGuidanceAppender = Callable[..., Awaitable[Any]]

_MENTION_RE = re.compile(r"@([A-Za-z0-9_.-]+)")


@dataclass(frozen=True)
class ThreadMessageCommand:
    idea_id: str
    role: str
    content: str
    actor: Mapping[str, Any] = field(default_factory=dict)
    attachments: list[dict[str, Any]] | None = None
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True)
class ThreadMessageResult:
    message: IdeaThread
    message_payload: dict[str, Any]
    status_change: dict[str, Any] | None
    notification_org_id: str | None
    notification_user_ids: set[str]


@dataclass(frozen=True)
class ThoughtStatusCommand:
    to_status: str
    trigger: str | None
    actor: Mapping[str, Any] = field(default_factory=dict)
    run_id: int | None = None
    changed_at: datetime | None = None


@dataclass(frozen=True)
class ThoughtStatusResult:
    status_change: dict[str, Any] | None


@dataclass(frozen=True)
class TerminalRunSettlementCommand:
    run_id: int
    run_status: str
    final_answer: str | None = None
    artifact_id: int | str | None = None


@dataclass(frozen=True)
class TerminalRunSettlementResult:
    status_change: dict[str, Any] | None
    thread_message: IdeaThread | None
    thread_message_payload: dict[str, Any] | None


def compact_notification_text(text: str | None, limit: int = 160) -> str | None:
    normalized = " ".join((text or "").split())
    if not normalized:
        return None
    if len(normalized) <= limit:
        return normalized
    return f"{normalized[: max(limit - 3, 0)].rstrip()}..."


def _actor_user_id(command: ThreadMessageCommand) -> str | None:
    raw_user_id = command.actor.get("user_id") or command.actor.get("id")
    if command.role != "user" or not raw_user_id or raw_user_id == "system":
        return None
    return str(raw_user_id)


def _actor_org_id(command: ThreadMessageCommand, idea: Any) -> str | None:
    return str(getattr(idea, "org_id", None) or command.actor.get("org_id") or "") or None


def _parse_mentions(content: str) -> list[str]:
    seen: set[str] = set()
    mentions: list[str] = []
    for match in _MENTION_RE.finditer(content or ""):
        mention = match.group(1).strip().lower()
        if mention and mention not in seen:
            seen.add(mention)
            mentions.append(mention)
    return mentions


def _default_message_type(_content: str, _role: str) -> str:
    return "message"


def _extract_project_context(
    attachments: list[dict[str, Any]],
    metadata: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if isinstance(metadata, dict):
        for key in ("project_context", "project_context_snapshot"):
            candidate = metadata.get(key)
            if isinstance(candidate, dict):
                return dict(candidate)
    for attachment in attachments:
        if not isinstance(attachment, dict):
            continue
        candidate = attachment.get("project_context")
        if attachment.get("type") == "project_context" and isinstance(candidate, dict):
            return dict(candidate)
    return None


def _merge_project_context_into_idea(idea: Any, project_context: dict[str, Any] | None) -> None:
    if not project_context:
        return
    existing = dict(idea.agent_details or {}) if isinstance(getattr(idea, "agent_details", None), dict) else {}
    existing["project_context"] = project_context
    idea.agent_details = existing


def _metadata_with_context(
    metadata: dict[str, Any] | None,
    *,
    project_context: dict[str, Any] | None,
    thread_attachment_context: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if not project_context and not thread_attachment_context:
        return metadata
    next_metadata = dict(metadata or {})
    if project_context:
        next_metadata["project_context"] = project_context
    if thread_attachment_context:
        next_metadata["thread_attachment_context"] = thread_attachment_context
    return next_metadata


def _next_status_for_message(role: str, current_status: str | None) -> str | None:
    if role == "user" and current_status in USER_MESSAGE_ACTIVATES_IDEA_STATUSES:
        return "active"
    if role in ("illo", "assistant") and current_status in ASSISTANT_REPLY_UNREAD_IDEA_STATUSES:
        return "unread_reply"
    if role == "illo" and current_status not in ASSISTANT_REPLY_BLOCKED_IDEA_STATUSES:
        return "unread_reply"
    return None


def _message_payload(thread_msg: IdeaThread, *, actor: Mapping[str, Any], user_id: str | None) -> dict[str, Any]:
    created_at = getattr(thread_msg, "created_at", None)
    metadata = thread_msg.metadata_ if isinstance(thread_msg.metadata_, dict) else {}
    payload = {
        "id": thread_msg.id,
        "idea_id": thread_msg.idea_id,
        "role": thread_msg.role,
        "content": thread_msg.content,
        "attachments": thread_msg.attachments or [],
        "metadata": thread_msg.metadata_,
        "user_id": thread_msg.user_id,
        "message_type": thread_msg.message_type,
        "created_at": created_at.isoformat() if created_at else None,
    }
    object_references = metadata.get("object_references") or []
    thread_references = metadata.get("thread_references") or []
    if object_references:
        payload["object_references"] = object_references
    if thread_references:
        payload["thread_references"] = thread_references
    if user_id and actor.get("name"):
        payload["user_name"] = actor.get("name")
        payload["user_color"] = actor.get("color", "#6366f1")
    return payload


def _status_change_payload(
    idea: Any,
    *,
    old_status: str | None,
    new_status: str,
    run_id: int | None = None,
) -> dict[str, Any]:
    payload = {
        "idea_id": str(getattr(idea, "id", "") or ""),
        "old_status": old_status,
        "new_status": new_status,
    }
    org_id = str(getattr(idea, "org_id", None) or "") or None
    if org_id:
        payload["org_id"] = org_id
    if run_id is not None:
        payload["run_id"] = int(run_id)
    return payload


def _apply_archival_fields(idea: Any, *, old_status: str | None, new_status: str, now: datetime) -> None:
    if not hasattr(idea, "archived_at"):
        return
    if new_status == "archived":
        idea.archived_at = now
    elif old_status == "archived":
        idea.archived_at = None


async def _add_state_log(
    session: Any,
    *,
    idea: Any,
    old_status: str | None,
    new_status: str,
    trigger: str | None,
    changed_at: datetime,
) -> None:
    session.add(
        IdeaStateLog(
            idea_id=str(getattr(idea, "id", "") or ""),
            from_state=old_status,
            to_state=new_status,
            changed_at=changed_at,
            trigger=trigger,
        )
    )


async def _transition_thought_status_common(
    session: Any,
    *,
    idea: Any,
    command: ThoughtStatusCommand,
    publish: ProductEventPublisher | None = None,
) -> ThoughtStatusResult:
    old_status = str(getattr(idea, "status", "") or "")
    new_status = str(command.to_status or "").strip()
    if not new_status:
        return ThoughtStatusResult(status_change=None)
    if old_status == new_status:
        if new_status == "archived" and hasattr(idea, "archived_at") and idea.archived_at is None:
            now = command.changed_at or datetime.now(timezone.utc)
            idea.archived_at = now
            idea.updated_at = now
            await _add_state_log(
                session,
                idea=idea,
                old_status=old_status or None,
                new_status=new_status,
                trigger=command.trigger,
                changed_at=now,
            )
            return ThoughtStatusResult(
                status_change=_status_change_payload(
                    idea,
                    old_status=old_status,
                    new_status=new_status,
                    run_id=command.run_id,
                )
            )
        return ThoughtStatusResult(status_change=None)
    now = command.changed_at or datetime.now(timezone.utc)
    idea.status = new_status
    idea.updated_at = now
    _apply_archival_fields(idea, old_status=old_status, new_status=new_status, now=now)
    await _add_state_log(
        session,
        idea=idea,
        old_status=old_status or None,
        new_status=new_status,
        trigger=command.trigger,
        changed_at=now,
    )
    status_change = _status_change_payload(
        idea,
        old_status=old_status,
        new_status=new_status,
        run_id=command.run_id,
    )
    if publish is not None:
        publish(
            "status_change",
            {
                "idea_id": status_change["idea_id"],
                "new_status": status_change["new_status"],
                **({"run_id": command.run_id} if command.run_id is not None else {}),
            },
        )
    return ThoughtStatusResult(status_change=status_change)


async def transition_thought_status(
    session: Any,
    *,
    idea: Any,
    command: ThoughtStatusCommand,
    publish: ProductEventPublisher | None = None,
) -> ThoughtStatusResult:
    result = await _transition_thought_status_common(session, idea=idea, command=command, publish=publish)
    await session.flush()
    return result


async def _create_thread_message(
    session: Any,
    *,
    idea_id: str,
    role: str,
    content: str,
    attachments: list[dict[str, Any]] | None = None,
    metadata: dict[str, Any] | None = None,
    user_id: str | None = None,
    message_type: str | None = None,
) -> IdeaThread:
    thread_msg = IdeaThread(
        idea_id=str(idea_id),
        role=role,
        content=content,
        attachments=list(attachments or []),
        metadata_=metadata,
        user_id=user_id,
        message_type=message_type,
    )
    session.add(thread_msg)
    return thread_msg


async def _maybe_append_live_guidance(
    append_live_guidance: LiveGuidanceAppender | None,
    *,
    thread_msg: IdeaThread,
    idea_id: str,
    role: str,
    content: str,
    metadata: dict[str, Any] | None,
    user_id: str | None,
) -> None:
    if append_live_guidance is None:
        return
    await append_live_guidance(
        idea_id=idea_id,
        role=role,
        content=content,
        metadata=metadata,
        thread_msg=thread_msg,
        user_id=user_id,
    )


async def _notify_mentions(
    *,
    idea: Any,
    content: str,
    command: ThreadMessageCommand,
    thread_msg: IdeaThread,
    mention_repo: Any | None,
    notification_repo: Any | None,
    resolve_mentioned_users: MentionResolver | None,
    publish: ProductEventPublisher | None,
    notification_org_id: str | None,
    actor_user_id: str | None,
) -> set[str]:
    notification_user_ids: set[str] = set()
    if not actor_user_id or not notification_org_id or not resolve_mentioned_users:
        return notification_user_ids
    org_id = _actor_org_id(command, idea)
    if not org_id:
        return notification_user_ids

    person_mentions = [mention for mention in _parse_mentions(content) if mention != "illo"]
    if not person_mentions:
        return notification_user_ids
    resolved = await resolve_mentioned_users(person_mentions, org_id)
    preview = compact_notification_text(content)
    for name in person_mentions:
        mentioned_user_id = resolved.get(name)
        if not mentioned_user_id or str(mentioned_user_id) == str(actor_user_id):
            continue
        if mention_repo is not None:
            _, created = await mention_repo.create_if_missing(
                user_id=str(mentioned_user_id),
                idea_id=str(command.idea_id),
                mentioned_by=str(actor_user_id),
                thread_message_id=thread_msg.id,
            )
        else:
            created = True
        if created and notification_repo is not None:
            await notification_repo.create_or_coalesce(
                org_id=notification_org_id,
                user_id=str(mentioned_user_id),
                source=NOTIFICATION_SOURCE_WORKSPACE,
                kind=NOTIFICATION_KIND_WORKSPACE_MENTION,
                actor_user_id=str(actor_user_id),
                title=f"{command.actor.get('name') or 'Someone'} mentioned you in workspace",
                body=preview,
                coalesce_key=f"workspace:mention:{mentioned_user_id}:{command.idea_id}:{thread_msg.id}",
                payload={
                    "preview": preview,
                    "idea_title": getattr(idea, "title", None),
                    "thread_message_id": thread_msg.id,
                },
                idea_id=str(command.idea_id),
            )
            notification_user_ids.add(str(mentioned_user_id))
        if publish is not None:
            publish(
                "mention",
                {
                    "idea_id": str(command.idea_id),
                    "user_id": str(mentioned_user_id),
                    "mentioned_by": {
                        "user_id": str(actor_user_id),
                        "name": command.actor.get("name"),
                        "color": command.actor.get("color"),
                    },
                },
            )
    return notification_user_ids


async def post_thread_message(
    session: Any,
    *,
    idea: Any,
    command: ThreadMessageCommand,
    mention_repo: Any | None = None,
    notification_repo: Any | None = None,
    resolve_mentioned_users: MentionResolver | None = None,
    publish: ProductEventPublisher | None = None,
    validate_project_context: ProjectContextValidator | None = None,
    extract_project_context: ProjectContextExtractor | None = None,
    build_attachment_context: AttachmentContextBuilder | None = None,
    parse_message_type: MessageTypeParser | None = None,
    append_live_guidance: LiveGuidanceAppender | None = None,
    apply_lifecycle: bool = True,
    lifecycle_trigger: str | None = None,
) -> ThreadMessageResult:
    role = command.role
    if role not in ("user", "assistant", "illo"):
        raise ValueError("Role must be 'user', 'assistant', or 'illo'")
    content = command.content.strip()
    if not content:
        raise ValueError("Content is required")

    attachments = list(command.attachments or [])
    metadata = dict(command.metadata) if isinstance(command.metadata, dict) else None
    attachment_context = build_attachment_context(attachments) if build_attachment_context else None
    extract = extract_project_context or _extract_project_context
    project_context = extract(attachments, metadata)
    if validate_project_context:
        project_context = validate_project_context(project_context)
    metadata = _metadata_with_context(
        metadata,
        project_context=project_context,
        thread_attachment_context=attachment_context,
    )
    if project_context:
        _merge_project_context_into_idea(idea, project_context)

    user_id = _actor_user_id(command)
    message_type = (parse_message_type or _default_message_type)(content, role)
    thread_msg = await _create_thread_message(
        session,
        idea_id=command.idea_id,
        role=role,
        content=content,
        attachments=attachments,
        metadata=metadata,
        user_id=user_id,
        message_type=message_type,
    )
    await session.flush()

    notification_org_id = _actor_org_id(command, idea)
    if notification_org_id:
        references = await store_object_references_for_source(
            session,
            source_type=SOURCE_IDEA_THREAD,
            source_id=thread_msg.id,
            org_id=notification_org_id,
            text=content,
            user_id=user_id,
        )
        if references:
            thread_msg.metadata_ = merge_object_reference_metadata(thread_msg.metadata_, references)
            metadata = thread_msg.metadata_

    if role == "user" and not getattr(idea, "preview_summary", None):
        preview = deterministic_preview_summary(title=getattr(idea, "title", None), description=content)
        if preview:
            await refresh_thread_read_model(
                session,
                idea,
                preview_summary=preview,
                preview_source="deterministic",
            )

    await _maybe_append_live_guidance(
        append_live_guidance,
        thread_msg=thread_msg,
        idea_id=command.idea_id,
        role=role,
        content=content,
        metadata=metadata,
        user_id=user_id,
    )

    notification_user_ids = await _notify_mentions(
        idea=idea,
        content=content,
        command=command,
        thread_msg=thread_msg,
        mention_repo=mention_repo,
        notification_repo=notification_repo,
        resolve_mentioned_users=resolve_mentioned_users,
        publish=publish,
        notification_org_id=notification_org_id,
        actor_user_id=user_id,
    )

    status_change = None
    if apply_lifecycle:
        current_status = getattr(idea, "status", None)
        new_status = _next_status_for_message(role, current_status)
        if new_status and new_status != current_status:
            transition = await _transition_thought_status_common(
                session,
                idea=idea,
                command=ThoughtStatusCommand(
                    to_status=new_status,
                    trigger=lifecycle_trigger or f"auto_{role}_message",
                    actor=command.actor,
                ),
                publish=publish,
            )
            status_change = transition.status_change
            if new_status == "unread_reply" and notification_org_id and getattr(idea, "user_id", None):
                owner_user_id = str(idea.user_id)
                preview = compact_notification_text(content)
                if notification_repo is not None:
                    await notification_repo.create_or_coalesce(
                        org_id=notification_org_id,
                        user_id=owner_user_id,
                        source=NOTIFICATION_SOURCE_WORKSPACE,
                        kind=NOTIFICATION_KIND_WORKSPACE_THREAD_ATTENTION,
                        actor_user_id=None,
                        title=f"Illo replied in {getattr(idea, 'title', '')}",
                        body=preview,
                        coalesce_key=f"workspace:thread_attention:{owner_user_id}:{command.idea_id}",
                        payload={
                            "preview": preview,
                            "idea_title": getattr(idea, "title", None),
                            "thread_message_id": thread_msg.id,
                        },
                        idea_id=str(command.idea_id),
                    )
                notification_user_ids.add(owner_user_id)

    return ThreadMessageResult(
        message=thread_msg,
        message_payload=_message_payload(thread_msg, actor=command.actor, user_id=user_id),
        status_change=status_change,
        notification_org_id=notification_org_id,
        notification_user_ids=notification_user_ids,
    )


def _final_answer_metadata(command: TerminalRunSettlementCommand) -> dict[str, Any]:
    return {
        "run_id": int(command.run_id),
        "artifact_id": command.artifact_id,
        "source": "agent_run_final_answer",
    }


async def mirror_run_final_answer(
    session: Any,
    *,
    idea: Any,
    command: TerminalRunSettlementCommand,
) -> tuple[IdeaThread | None, dict[str, Any] | None]:
    text = str(command.final_answer or "").strip()
    if not text:
        return None, None
    thread_msg = await _create_thread_message(
        session,
        idea_id=str(getattr(idea, "id", "") or ""),
        role="illo",
        content=text,
        attachments=[],
        metadata=_final_answer_metadata(command),
        user_id=None,
        message_type="agent_response",
    )
    await session.flush()
    org_id = str(getattr(idea, "org_id", None) or "") or None
    if org_id:
        references = await store_object_references_for_source(
            session,
            source_type=SOURCE_IDEA_THREAD,
            source_id=thread_msg.id,
            org_id=org_id,
            text=text,
            user_id=None,
        )
        if references:
            thread_msg.metadata_ = merge_object_reference_metadata(thread_msg.metadata_, references)
    return thread_msg, _message_payload(thread_msg, actor={}, user_id=None)


def _terminal_run_target_status(run_status: str) -> str | None:
    return {
        "completed": "unread_reply",
        "failed": "failed",
        "canceled": "failed",
        "cancelled": "failed",
    }.get(str(run_status or "").strip().lower())


async def settle_terminal_run(
    session: Any,
    *,
    idea: Any,
    command: TerminalRunSettlementCommand,
    publish: ProductEventPublisher | None = None,
) -> TerminalRunSettlementResult:
    thread_msg, thread_payload = await mirror_run_final_answer(session, idea=idea, command=command)
    if publish is not None and thread_payload is not None:
        publish("thread_message", {"idea_id": str(getattr(idea, "id", "") or ""), "message": thread_payload})
    target_status = _terminal_run_target_status(command.run_status)
    status_change = None
    if target_status and str(getattr(idea, "status", "") or "") not in {"archived", "resolved"}:
        transition = await transition_thought_status(
            session,
            idea=idea,
            command=ThoughtStatusCommand(
                to_status=target_status,
                trigger=f"agent_run_{command.run_status}",
                run_id=int(command.run_id),
            ),
            publish=publish,
        )
        status_change = transition.status_change
    return TerminalRunSettlementResult(
        status_change=status_change,
        thread_message=thread_msg,
        thread_message_payload=thread_payload,
    )


__all__ = [
    "ThreadMessageCommand",
    "ThreadMessageResult",
    "TerminalRunSettlementCommand",
    "TerminalRunSettlementResult",
    "ThoughtStatusCommand",
    "ThoughtStatusResult",
    "compact_notification_text",
    "mirror_run_final_answer",
    "post_thread_message",
    "settle_terminal_run",
    "transition_thought_status",
]
