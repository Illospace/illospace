"""Slack tool handlers for AgentRun."""

from __future__ import annotations

import json
from typing import Any

from brain.systems.runs.tool_catalog.handlers.common import _agent_context, _current_runtime_secret_context


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


def _coerce_slack_list_limit(value: Any, default: int = 200) -> int:
    try:
        return max(1, min(int(value or default), 1000))
    except (TypeError, ValueError):
        return default


def _coerce_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "y", "on"}:
            return True
        if normalized in {"0", "false", "no", "n", "off"}:
            return False
    return bool(value)


def _normalize_slack_channel_types(value: Any) -> str:
    allowed = {"public_channel", "private_channel", "mpim", "im"}
    if value is None:
        requested = ["public_channel", "private_channel", "mpim", "im"]
    elif isinstance(value, str):
        requested = [part.strip() for part in value.split(",")]
    else:
        requested = [str(part).strip() for part in value]
    normalized = [part for part in requested if part in allowed]
    return ",".join(dict.fromkeys(normalized)) or "public_channel,private_channel,mpim,im"


def _slack_channel_payload(channel: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": channel.get("id"),
        "name": channel.get("name") or channel.get("user"),
        "is_channel": channel.get("is_channel"),
        "is_group": channel.get("is_group"),
        "is_im": channel.get("is_im"),
        "is_mpim": channel.get("is_mpim"),
        "is_private": channel.get("is_private"),
        "is_member": channel.get("is_member"),
        "is_archived": channel.get("is_archived"),
        "num_members": channel.get("num_members"),
        "topic": channel.get("topic") if isinstance(channel.get("topic"), dict) else None,
        "purpose": channel.get("purpose") if isinstance(channel.get("purpose"), dict) else None,
    }


async def _slack_client_from_runtime():
    from brain.systems.slack.client import SlackWebClient
    from brain.systems.vault.runtime_secrets import read_runtime_secret

    token = await read_runtime_secret(
        "SLACK_BOT_TOKEN",
        context=_current_runtime_secret_context(),
        reason="Use the configured Slack app to read and reply from Illo's Slack teammate surface.",
        requested_by="slack_runtime_tool",
        access="service",
        allow_env_fallback=True,
    )
    return SlackWebClient(token)


def _looks_like_slack_user_id(value: str) -> bool:
    return value.startswith(("U", "W"))


async def _resolve_post_channel(client, channel_id: str, visibility: str) -> str:
    if visibility == "ephemeral" or not _looks_like_slack_user_id(channel_id):
        return channel_id
    response = await client.open_conversation(users=channel_id)
    channel = response.get("channel") if isinstance(response.get("channel"), dict) else {}
    resolved = str(channel.get("id") or "").strip()
    return resolved or channel_id


async def _clear_processing_status(client: Any, trigger: dict[str, Any]) -> None:
    set_status = getattr(client, "set_assistant_status", None)
    if not callable(set_status):
        return
    channel_id = str(trigger.get("channel_id") or "").strip()
    thread_ts = str(trigger.get("thread_ts") or trigger.get("message_ts") or "").strip()
    if not channel_id or not thread_ts:
        return
    try:
        await set_status(channel_id=channel_id, thread_ts=thread_ts, status="")
    except Exception:
        return


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
    trigger_channel_id = str(trigger.get("channel_id") or "").strip()
    trigger_message_ts = str(trigger.get("message_ts") or "").strip()
    trigger_channel_type = str(trigger.get("channel_type") or "").strip()
    response_target_thread_ts = response_target.get("thread_ts")
    if target_channel == trigger_channel_id:
        if trigger_channel_type == "im":
            target_thread_ts = None
        elif not response_target_thread_ts and target_thread_ts == trigger_message_ts:
            target_thread_ts = None
    target_visibility = str(visibility or response_target.get("visibility") or "public").strip().lower()
    if target_visibility not in {"public", "ephemeral"}:
        return json.dumps({"error": "post_slack_reply visibility must be public or ephemeral"})
    if not target_channel:
        return json.dumps({"error": "post_slack_reply requires channel_id outside a Slack-triggered run"})

    try:
        client = await _slack_client_from_runtime()
        target_channel = await _resolve_post_channel(client, target_channel, target_visibility)
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
        await _clear_processing_status(client, trigger)
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
        client = await _slack_client_from_runtime()
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


async def _handle_manage_slack(
    action: str,
    connection_id: str | None = None,
    slack_user_id: str | None = None,
    user_id: str | None = None,
    channel_types: str | list[str] | None = None,
    limit: int = 200,
    cursor: str | None = None,
    include_archived: bool = False,
) -> str:
    """Inspect Slack connection health and manage minimal identity mappings."""

    normalized_action = str(action or "").strip().lower()
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
            statuses = {str(row.status or "").strip().lower() for row in rows}
            if not rows:
                setup_state = "not_connected"
            elif statuses & {"connected", "online"}:
                setup_state = "connected"
            elif "error" in statuses:
                setup_state = "error"
            else:
                setup_state = "configured"
            return json.dumps(
                {
                    "ok": True,
                    "setup_state": setup_state,
                    "needs_connection": not bool(rows),
                    "connection_count": len(rows),
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

        if normalized_action == "list_channels":
            try:
                client = await _slack_client_from_runtime()
                response = await client.conversations_list(
                    types=_normalize_slack_channel_types(channel_types),
                    limit=_coerce_slack_list_limit(limit),
                    cursor=str(cursor or "").strip() or None,
                    exclude_archived=not _coerce_bool(include_archived, default=False),
                )
            except Exception as exc:
                return json.dumps({"error": str(exc)})
            channels = [
                _slack_channel_payload(channel)
                for channel in list(response.get("channels") or [])
                if isinstance(channel, dict)
            ]
            metadata = response.get("response_metadata") if isinstance(response.get("response_metadata"), dict) else {}
            return json.dumps(
                {
                    "ok": True,
                    "connection": _slack_connection_payload(connection),
                    "count": len(channels),
                    "channels": channels,
                    "next_cursor": str(metadata.get("next_cursor") or "") or None,
                    "visibility_note": (
                        "Slack only returns conversations visible to the configured bot token; "
                        "private channels may require the bot to be invited and the app to have the right scopes."
                    ),
                },
                default=str,
            )

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

    return json.dumps({"error": "manage_slack action must be status, list_channels, list_mappings, link_identity, or unlink_identity"})


__all__ = [
    "_handle_manage_slack",
    "_handle_post_slack_reply",
    "_handle_read_slack_conversation",
]
