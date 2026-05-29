"""Slack tool handlers for AgentRun."""

from __future__ import annotations

import json
from typing import Any

from brain.systems.runs.tool_catalog.handlers.common import _agent_context


def _execution_metadata() -> dict[str, Any]:
    metadata = getattr(_agent_context, "execution_metadata", None)
    return metadata if isinstance(metadata, dict) else {}


def _metadata_trigger(name: str) -> dict[str, Any]:
    trigger = _execution_metadata().get(name)
    return dict(trigger) if isinstance(trigger, dict) else {}


def _current_target_ref() -> dict[str, Any]:
    target_ref = getattr(_agent_context, "target_ref", None)
    if isinstance(target_ref, dict):
        return dict(target_ref)
    target_ref = _execution_metadata().get("target_ref")
    return dict(target_ref) if isinstance(target_ref, dict) else {}


def _current_slack_trigger() -> dict[str, Any]:
    trigger = getattr(_agent_context, "slack_trigger", None)
    if isinstance(trigger, dict):
        return dict(trigger)
    trigger = _metadata_trigger("slack_trigger")
    if trigger:
        return trigger
    target_ref = _current_target_ref()
    if isinstance(target_ref.get("slack_trigger"), dict):
        return dict(target_ref["slack_trigger"])
    return {}


def _coerce_slack_limit(value: Any, default: int = 50) -> int:
    try:
        return max(1, min(int(value or default), 200))
    except (TypeError, ValueError):
        return default


def _slack_client_from_env():
    from brain.systems.slack.client import slack_web_client_from_env

    return slack_web_client_from_env()


async def _handle_post_slack_reply(
    body: str,
    channel_id: str | None = None,
    thread_ts: str | None = None,
    visibility: str | None = None,
    user_id: str | None = None,
) -> str:
    """Post an Illo-authored reply to the originating Slack surface."""

    text = str(body or "").strip()
    if not text:
        return json.dumps({"error": "post_slack_reply requires body"})

    trigger = _current_slack_trigger()
    response_target = trigger.get("response_target") if isinstance(trigger.get("response_target"), dict) else {}
    target_channel = str(channel_id or response_target.get("channel_id") or trigger.get("channel_id") or "").strip()
    target_thread_ts = thread_ts if thread_ts is not None else response_target.get("thread_ts")
    if target_thread_ts is not None:
        target_thread_ts = str(target_thread_ts or "").strip() or None
    target_visibility = str(visibility or response_target.get("visibility") or "public").strip().lower()
    if target_visibility not in {"public", "ephemeral"}:
        return json.dumps({"error": "post_slack_reply visibility must be public or ephemeral"})
    if not target_channel:
        return json.dumps({"error": "post_slack_reply requires channel_id outside a Slack-triggered run"})

    try:
        client = _slack_client_from_env()
        if target_visibility == "ephemeral":
            target_user = str(user_id or trigger.get("slack_user_id") or "").strip()
            if not target_user:
                return json.dumps({"error": "ephemeral Slack replies require a Slack user id"})
            response = await client.post_ephemeral(
                channel=target_channel,
                user=target_user,
                text=text,
                thread_ts=target_thread_ts,
            )
        else:
            response = await client.post_message(
                channel=target_channel,
                text=text,
                thread_ts=target_thread_ts,
            )
    except Exception as exc:
        return json.dumps({"error": str(exc)})

    return json.dumps(
        {
            "ok": True,
            "channel_id": target_channel,
            "thread_ts": target_thread_ts,
            "visibility": target_visibility,
            "slack": response,
        },
        default=str,
    )


async def _handle_read_slack_conversation(
    scope: str = "thread",
    channel_id: str | None = None,
    thread_ts: str | None = None,
    limit: int = 50,
) -> str:
    """Read bounded Slack context for the current Slack-triggered run."""

    trigger = _current_slack_trigger()
    response_target = trigger.get("response_target") if isinstance(trigger.get("response_target"), dict) else {}
    target_channel = str(channel_id or response_target.get("channel_id") or trigger.get("channel_id") or "").strip()
    target_thread_ts = str(thread_ts or response_target.get("thread_ts") or trigger.get("thread_ts") or "").strip()
    normalized_scope = str(scope or "thread").strip().lower()
    capped_limit = _coerce_slack_limit(limit)

    if normalized_scope == "triggering_message":
        message = {
            "user": trigger.get("slack_user_id"),
            "text": trigger.get("text"),
            "ts": trigger.get("message_ts"),
            "permalink": trigger.get("permalink"),
        }
        return json.dumps({"ok": True, "scope": normalized_scope, "messages": [message]}, default=str)

    if not target_channel:
        return json.dumps({"error": "read_slack_conversation requires channel_id outside a Slack-triggered run"})
    try:
        client = _slack_client_from_env()
        if normalized_scope == "thread":
            if not target_thread_ts:
                return json.dumps({"error": "read_slack_conversation scope=thread requires thread_ts"})
            response = await client.conversation_replies(
                channel=target_channel,
                thread_ts=target_thread_ts,
                limit=capped_limit,
            )
        elif normalized_scope == "recent_channel":
            response = await client.conversation_history(
                channel=target_channel,
                limit=capped_limit,
                latest=trigger.get("message_ts"),
            )
        else:
            return json.dumps({"error": "read_slack_conversation scope must be triggering_message, thread, or recent_channel"})
    except Exception as exc:
        return json.dumps({"error": str(exc)})

    messages = list(response.get("messages") or [])
    return json.dumps(
        {
            "ok": True,
            "scope": normalized_scope,
            "channel_id": target_channel,
            "thread_ts": target_thread_ts or None,
            "count": len(messages),
            "messages": messages[:capped_limit],
        },
        default=str,
    )


async def _slack_connection_for_tool(session, *, org_id: str, connection_id: str | None = None):
    from sqlalchemy import select

    from brain.platform.db.models.external_agent import ExternalAgentConnectionRow

    stmt = select(ExternalAgentConnectionRow).where(
        ExternalAgentConnectionRow.org_id == str(org_id),
        ExternalAgentConnectionRow.agent_kind == "slack",
        ExternalAgentConnectionRow.transport == "slack_socket_mode",
    )
    if connection_id:
        stmt = stmt.where(ExternalAgentConnectionRow.id == str(connection_id))
    stmt = stmt.order_by(ExternalAgentConnectionRow.created_at.asc(), ExternalAgentConnectionRow.id.asc())
    rows = list((await session.scalars(stmt)).all())
    if not rows:
        return None, "Slack connection not found"
    if not connection_id and len(rows) > 1:
        return None, "connection_id is required when multiple Slack connections exist"
    return rows[0], None


def _slack_connection_payload(connection) -> dict[str, Any]:
    return {
        "id": str(connection.id),
        "display_name": connection.display_name,
        "team_id": connection.remote_agent_id,
        "status": connection.status,
        "last_seen_at": connection.last_seen_at.isoformat() if connection.last_seen_at else None,
        "last_error": connection.last_error,
        "metadata": connection.metadata_ or {},
    }


def _slack_setup_instructions() -> dict[str, Any]:
    return {
        "slack_admin_url": "https://api.slack.com/apps",
        "what_illo_can_do": [
            "Check whether Slack is connected to this Illospace workspace.",
            "Link Slack users to Illospace users after Slack is connected.",
            "Read and reply in Slack conversations after Illo is mentioned or DM'd.",
        ],
        "what_illo_cannot_do": [
            "Create or install the Slack app for the workspace.",
            "Receive Slack tokens in chat.",
            "Change Slack or Illospace installation settings.",
        ],
        "setup_boundary": (
            "If Slack is not connected, an Illospace admin must finish the Slack connection outside this chat. "
            "Slack tokens must never be pasted to Illo, Slack chat, or Thread chat, "
            "and they are not Illospace Vault entries."
        ),
        "after_connected": [
            "Ask Illo to check Slack status.",
            "Invite Illo to the Slack channels where it should participate, then mention @Illo or DM it.",
        ],
    }


async def _handle_manage_slack(
    action: str,
    connection_id: str | None = None,
    slack_user_id: str | None = None,
    user_id: str | None = None,
) -> str:
    """Inspect Slack connection health and manage minimal identity mappings."""

    normalized_action = str(action or "").strip().lower()
    if normalized_action == "setup_instructions":
        return json.dumps({"ok": True, "setup": _slack_setup_instructions()}, default=str)

    org_id = str(getattr(_agent_context, "org_id", "") or "").strip()
    if not org_id:
        return json.dumps({"error": "manage_slack could not access this workspace context"})

    from brain.platform.db.models.external_agent import ExternalAgentConnectionRow
    from brain.platform.db.repositories.unit_of_work import UnitOfWork
    from brain.systems.slack.identity import (
        link_slack_identity,
        list_slack_identity_mappings,
        unlink_slack_identity,
    )

    async with UnitOfWork() as uow:
        if normalized_action == "status":
            from sqlalchemy import select

            stmt = (
                select(ExternalAgentConnectionRow)
                .where(
                    ExternalAgentConnectionRow.org_id == org_id,
                    ExternalAgentConnectionRow.agent_kind == "slack",
                    ExternalAgentConnectionRow.transport == "slack_socket_mode",
                )
                .order_by(ExternalAgentConnectionRow.created_at.asc(), ExternalAgentConnectionRow.id.asc())
            )
            rows = list((await uow.session.scalars(stmt)).all())
            setup_state = "connected" if rows else "not_connected"
            return json.dumps(
                {
                    "ok": True,
                    "setup_state": setup_state,
                    "next_step": (
                        "Slack is connected. Invite Illo to a channel, mention @Illo, or DM it."
                        if rows
                        else "Slack is not connected. Ask an Illospace admin to finish the Slack connection outside this chat, then ask Illo to check status again."
                    ),
                    "connections": [_slack_connection_payload(row) for row in rows],
                },
                default=str,
            )

        connection, error = await _slack_connection_for_tool(
            uow.session,
            org_id=org_id,
            connection_id=connection_id,
        )
        if error:
            return json.dumps({"error": error})

        if normalized_action == "list_mappings":
            mappings = await list_slack_identity_mappings(
                uow.session,
                connection_id=str(connection.id),
                org_id=org_id,
            )
            return json.dumps({"ok": True, "connection": _slack_connection_payload(connection), "mappings": mappings}, default=str)
        if normalized_action == "link_identity":
            mapping = await link_slack_identity(
                uow.session,
                connection_id=str(connection.id),
                slack_user_id=str(slack_user_id or ""),
                user_id=str(user_id or ""),
                org_id=org_id,
            )
            return json.dumps({"ok": True, "mapping": mapping}, default=str)
        if normalized_action == "unlink_identity":
            mapping = await unlink_slack_identity(
                uow.session,
                connection_id=str(connection.id),
                slack_user_id=str(slack_user_id or ""),
                org_id=org_id,
            )
            return json.dumps({"ok": True, "mapping": mapping}, default=str)

    return json.dumps({"error": "manage_slack action must be status, list_mappings, link_identity, or unlink_identity"})


__all__ = [
    "_handle_manage_slack",
    "_handle_post_slack_reply",
    "_handle_read_slack_conversation",
]
