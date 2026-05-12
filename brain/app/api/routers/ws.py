"""WebSocket endpoint — real-time events, replacing SSE."""
from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Mapping
from typing import Any

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect

from brain.platform.browser import BrowserCapabilityError, browser_sessions
from brain.systems.runs.event_log import (
    run_event_to_message,
    list_run_events_after_for_principal,
)
from brain.systems.cortex.events import (
    cortex_event_to_message,
    list_cortex_events_after_for_principal,
)
from brain.platform.db.repositories.chat import ChatConversationRepository, ChatMessageRepository
from brain.app.api.schemas.chat import ChatReadUpdate
from brain.app.api.services.chat import ChatReadPublishState, ChatService
from brain.app.api.services.notifications import build_notification_summary
from brain.app.api.ws.manager import ConnectionManager
from brain.app.api.ws.events import ServerEvent, ClientEvent
from brain.app.api.ws.auth import WsTokenError, validate_auth_frame_claims, verify_ws_token
from brain.platform.db.repositories.unit_of_work import UnitOfWork, run_sync_with_unit_of_work

logger = logging.getLogger(__name__)
router = APIRouter()
ws_manager = ConnectionManager()

EVENT_REPLAY_CHANNEL_RUN = "run"
EVENT_REPLAY_CHANNEL_CORTEX = "cortex"
EVENT_REPLAY_CHANNELS = (EVENT_REPLAY_CHANNEL_RUN, EVENT_REPLAY_CHANNEL_CORTEX)
EVENT_REPLAY_LIMIT = 100


@router.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    await ws.send_json({"type": ServerEvent.CONNECTED})

    # Wait for auth message
    try:
        auth_msg = await asyncio.wait_for(ws.receive_json(), timeout=10)
    except (asyncio.TimeoutError, Exception):
        await ws.send_json({"type": ServerEvent.ERROR, "code": "AUTH_TIMEOUT"})
        await ws.close()
        return

    if not isinstance(auth_msg, dict) or auth_msg.get("type") != ClientEvent.AUTH:
        await ws.send_json({"type": ServerEvent.ERROR, "code": "AUTH_REQUIRED"})
        await ws.close()
        return

    try:
        claims = verify_ws_token(str(auth_msg.get("token") or ""))
        validate_auth_frame_claims(auth_msg, claims)
    except WsTokenError as exc:
        await ws.send_json({"type": ServerEvent.ERROR, "code": exc.code})
        await ws.close()
        return
    replay_cursors, replay_cursor_errors = _parse_event_replay_cursors(auth_msg)

    user_id = claims.user_id
    org_id = claims.org_id
    await ws_manager.connect(claims, ws, accept=False)
    await ws.send_json(
        {
            "type": ServerEvent.AUTHENTICATED,
            "user_id": user_id,
            "org_id": org_id,
            "session_id": claims.session_id,
            "tab_id": claims.tab_id,
        }
    )
    for replay_error in replay_cursor_errors:
        await ws.send_json(
            {
                "type": ServerEvent.EVENT_REPLAY_ERROR,
                **replay_error,
            }
        )
    if replay_cursors:
        await _replay_durable_events(ws, claims, replay_cursors)

    ping_task = asyncio.create_task(_ping_loop(ws))

    try:
        while True:
            data = await ws.receive_json()
            event_type = data.get("type")
            if event_type == ClientEvent.TYPING:
                await ws_manager.broadcast_to_org(
                    org_id,
                    "typing",
                    {"user_id": user_id, "idea_id": data.get("idea_id")},
                    exclude=user_id,
                )
            elif event_type == ClientEvent.CHAT_OPEN:
                await ws_manager.open_chat(user_id, ws)
            elif event_type == ClientEvent.CHAT_CLOSE:
                await ws_manager.close_chat(user_id, ws)
            elif event_type == ClientEvent.CHAT_TYPING:
                await _handle_chat_typing(user_id, org_id, data, ws)
            elif event_type == ClientEvent.CHAT_MARK_READ:
                await _handle_chat_mark_read(user_id, org_id, data, ws)
            elif event_type == ClientEvent.CHAT_SUBSCRIBE_CONVERSATION:
                conversation_id = _require_text_field(data, "conversation_id")
                if conversation_id is None:
                    await _send_chat_error(ws, "CHAT_CONVERSATION_REQUIRED")
                    continue
                error_code = await _authorize_chat_subscription(
                    user_id,
                    org_id=org_id,
                    conversation_id=conversation_id,
                )
                if error_code is not None:
                    await _send_chat_error(ws, error_code)
                    continue
                await ws_manager.subscribe_conversation(user_id, ws, conversation_id)
            elif event_type == ClientEvent.CHAT_UNSUBSCRIBE_CONVERSATION:
                conversation_id = _require_text_field(data, "conversation_id")
                if conversation_id is None:
                    await _send_chat_error(ws, "CHAT_CONVERSATION_REQUIRED")
                    continue
                await ws_manager.unsubscribe_conversation(user_id, ws, conversation_id)
            elif event_type == ClientEvent.CHAT_SUBSCRIBE_THREAD:
                conversation_id = _require_text_field(data, "conversation_id")
                thread_root_message_id = _coerce_int_field(
                    data,
                    "thread_root_message_id",
                    alias="message_id",
                )
                if conversation_id is None or thread_root_message_id is None:
                    await _send_chat_error(ws, "CHAT_THREAD_SUBSCRIPTION_INVALID")
                    continue
                error_code = await _authorize_chat_subscription(
                    user_id,
                    org_id=org_id,
                    conversation_id=conversation_id,
                    thread_root_message_id=thread_root_message_id,
                )
                if error_code is not None:
                    await _send_chat_error(ws, error_code)
                    continue
                await ws_manager.subscribe_thread(
                    user_id,
                    ws,
                    conversation_id=conversation_id,
                    thread_root_message_id=thread_root_message_id,
                )
            elif event_type == ClientEvent.CHAT_UNSUBSCRIBE_THREAD:
                thread_root_message_id = _coerce_int_field(
                    data,
                    "thread_root_message_id",
                    alias="message_id",
                )
                if thread_root_message_id is None:
                    await _send_chat_error(ws, "CHAT_THREAD_SUBSCRIPTION_INVALID")
                    continue
                await ws_manager.unsubscribe_thread(
                    user_id,
                    ws,
                    thread_root_message_id=thread_root_message_id,
                )
            elif event_type == ClientEvent.FOCUS_IDEA:
                await ws_manager.broadcast_to_org(
                    org_id,
                    "focus_idea",
                    {"user_id": user_id, "idea_id": data.get("idea_id")},
                    exclude=user_id,
                )
            elif event_type == ClientEvent.UNFOCUS_IDEA:
                await ws_manager.broadcast_to_org(
                    org_id,
                    "unfocus_idea",
                    {"user_id": user_id},
                    exclude=user_id,
                )
            elif event_type == ClientEvent.BROWSER_SUBSCRIBE:
                session_id = str(data.get("session_id", "")).strip()
                if not await _authorize_browser_session_for_ws(user_id, org_id, session_id):
                    continue
                await browser_sessions.subscribe(session_id, user_id)
            elif event_type == ClientEvent.BROWSER_UNSUBSCRIBE:
                session_id = str(data.get("session_id", "")).strip()
                if not await _authorize_browser_session_for_ws(user_id, org_id, session_id):
                    continue
                await browser_sessions.unsubscribe(session_id, user_id)
            elif event_type in {
                ClientEvent.BROWSER_NAVIGATE,
                ClientEvent.BROWSER_CLICK,
                ClientEvent.BROWSER_TYPE,
                ClientEvent.BROWSER_KEY,
                ClientEvent.BROWSER_SCROLL,
                ClientEvent.BROWSER_REFRESH,
                ClientEvent.BROWSER_BACK,
                ClientEvent.BROWSER_FORWARD,
                ClientEvent.BROWSER_NEW_TAB,
                ClientEvent.BROWSER_SWITCH_TAB,
                ClientEvent.BROWSER_CLOSE_TAB,
                ClientEvent.BROWSER_LIST_TABS,
                ClientEvent.BROWSER_DISCOVER,
                ClientEvent.BROWSER_EXTRACT,
                ClientEvent.BROWSER_UPLOAD_ATTACHMENT,
                ClientEvent.BROWSER_SAVE_SCREENSHOT,
                ClientEvent.BROWSER_PRINT_PDF,
                ClientEvent.BROWSER_SNAPSHOT,
            }:
                await _handle_browser_command(user_id, org_id, event_type, data)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.warning("ws error user=%s: %s", user_id, e)
    finally:
        ping_task.cancel()
        await ws_manager.disconnect(user_id, ws)


def _principal_from_ws_claims(claims) -> dict[str, Any]:
    return {
        "id": claims.user_id,
        "org_id": claims.org_id,
        "principal_type": getattr(claims, "principal_type", "human"),
        "permissions": list(getattr(claims, "permissions", []) or []),
    }


def _parse_event_replay_cursors(
    frame: Mapping[str, Any],
) -> tuple[dict[str, int], list[dict[str, Any]]]:
    cursors: dict[str, int] = {}
    errors: list[dict[str, Any]] = []

    def add_cursor(channel: str, value: Any) -> None:
        if value is None or value == "":
            return
        cursor = _coerce_replay_cursor(value)
        if cursor is None:
            errors.append(
                {
                    "code": "EVENT_REPLAY_CURSOR_INVALID",
                    "channel": channel,
                }
            )
            return
        cursors[channel] = cursor

    cursor = frame.get("cursor")
    if isinstance(cursor, Mapping):
        _read_cursor_mapping(cursor, add_cursor)
    else:
        add_cursor(EVENT_REPLAY_CHANNEL_RUN, cursor)

    raw_cursors = frame.get("cursors")
    if isinstance(raw_cursors, Mapping):
        _read_cursor_mapping(raw_cursors, add_cursor)

    for key, channel in (
        ("run_cursor", EVENT_REPLAY_CHANNEL_RUN),
        ("run_event_cursor", EVENT_REPLAY_CHANNEL_RUN),
        ("run_event_id", EVENT_REPLAY_CHANNEL_RUN),
        ("cortex_cursor", EVENT_REPLAY_CHANNEL_CORTEX),
        ("cortex_event_cursor", EVENT_REPLAY_CHANNEL_CORTEX),
        ("cortex_event_id", EVENT_REPLAY_CHANNEL_CORTEX),
    ):
        if key in frame:
            add_cursor(channel, frame.get(key))

    return cursors, errors


def _read_cursor_mapping(
    mapping: Mapping[str, Any],
    add_cursor,
) -> None:
    aliases = {
        EVENT_REPLAY_CHANNEL_RUN: EVENT_REPLAY_CHANNEL_RUN,
        "run_events": EVENT_REPLAY_CHANNEL_RUN,
        "run_event": EVENT_REPLAY_CHANNEL_RUN,
        "run_event_id": EVENT_REPLAY_CHANNEL_RUN,
        EVENT_REPLAY_CHANNEL_CORTEX: EVENT_REPLAY_CHANNEL_CORTEX,
        "cortex_events": EVENT_REPLAY_CHANNEL_CORTEX,
        "cortex_event": EVENT_REPLAY_CHANNEL_CORTEX,
        "cortex_event_id": EVENT_REPLAY_CHANNEL_CORTEX,
    }
    for key, value in mapping.items():
        channel = aliases.get(str(key))
        if channel is None:
            continue
        add_cursor(channel, value)


def _coerce_replay_cursor(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    try:
        cursor = int(value)
    except (TypeError, ValueError):
        return None
    if cursor < 0:
        return None
    return cursor


async def _replay_durable_events(
    ws: WebSocket,
    claims,
    cursors: Mapping[str, int],
    *,
    limit: int = EVENT_REPLAY_LIMIT,
) -> None:
    replay_limit = max(1, int(limit))
    principal = _principal_from_ws_claims(claims)
    for channel in EVENT_REPLAY_CHANNELS:
        if channel not in cursors:
            continue
        cursor = int(cursors[channel])
        try:
            loaded_events = _load_replay_events(
                channel,
                principal,
                last_event_id=cursor,
                limit=replay_limit + 1,
            )
            events = await loaded_events if inspect.isawaitable(loaded_events) else loaded_events
        except Exception as exc:
            logger.warning(
                "event_replay_failed user=%s org=%s channel=%s cursor=%s error=%s",
                claims.user_id,
                claims.org_id,
                channel,
                cursor,
                exc,
            )
            await ws.send_json(
                {
                    "type": ServerEvent.EVENT_REPLAY_ERROR,
                    "code": "EVENT_REPLAY_FAILED",
                    "channel": channel,
                    "from_cursor": cursor,
                }
            )
            continue

        has_more = len(events) > replay_limit
        delivered_events = events[:replay_limit]
        last_event_id = cursor
        sent_count = 0
        for event in delivered_events:
            last_event_id = int(event.id)
            message = _replay_event_to_message(channel, event)
            if message is None:
                continue
            await ws.send_json(message)
            sent_count += 1

        if has_more:
            await ws.send_json(
                {
                    "type": ServerEvent.EVENT_REPLAY_CAPPED,
                    "code": "EVENT_REPLAY_CAPPED",
                    "channel": channel,
                    "from_cursor": cursor,
                    "last_event_id": last_event_id,
                    "limit": replay_limit,
                    "message": (
                        "Replay was capped to protect the socket; reconnect with "
                        "last_event_id or refresh state before requesting more."
                    ),
                }
            )

        await ws.send_json(
            {
                "type": ServerEvent.EVENT_REPLAY_COMPLETE,
                "channel": channel,
                "from_cursor": cursor,
                "last_event_id": last_event_id,
                "delivered": sent_count,
                "has_more": has_more,
                "limit": replay_limit,
            }
        )


async def _load_replay_events(
    channel: str,
    principal: Mapping[str, Any],
    *,
    last_event_id: int,
    limit: int,
):
    def _load():
        with UnitOfWork() as uow:
            if channel == EVENT_REPLAY_CHANNEL_RUN:
                return list_run_events_after_for_principal(
                    uow.session,
                    principal,
                    last_event_id=last_event_id,
                    limit=limit,
                )
            if channel == EVENT_REPLAY_CHANNEL_CORTEX:
                return list_cortex_events_after_for_principal(
                    uow.session,
                    principal,
                    last_event_id=last_event_id,
                    limit=limit,
                )
            return []

    return await run_sync_with_unit_of_work(_load)


def _replay_event_to_message(channel: str, event) -> dict[str, Any] | None:
    if channel == EVENT_REPLAY_CHANNEL_RUN:
        return run_event_to_message(event, replayed=True)
    return cortex_event_to_message(event, replayed=True)


async def _ping_loop(ws: WebSocket):
    try:
        while True:
            await asyncio.sleep(30)
            await ws.send_json({"type": "ping"})
    except Exception:
        pass


async def _authorize_browser_session_for_ws(user_id: str, org_id: str | None, session_id: str) -> bool:
    if not session_id:
        await ws_manager.send_to(user_id, ServerEvent.ERROR, {"code": "BROWSER_SESSION_REQUIRED"})
        return False
    if not org_id:
        await ws_manager.send_to(user_id, ServerEvent.BROWSER_SESSION_ERROR, {
            "session_id": session_id,
            "code": "BROWSER_SESSION_FORBIDDEN",
            "error": "Browser session not found",
        })
        return False
    try:
        record = await browser_sessions.get_session_record_for_org_async(session_id, org_id=org_id)
    except Exception as exc:
        logger.warning("browser ws authorization failed user=%s session=%s: %s", user_id, session_id, exc)
        await ws_manager.send_to(user_id, ServerEvent.BROWSER_SESSION_ERROR, {
            "session_id": session_id,
            "code": "BROWSER_SESSION_AUTH_FAILED",
            "error": "Browser session authorization failed",
        })
        return False
    if record is None:
        await ws_manager.send_to(user_id, ServerEvent.BROWSER_SESSION_ERROR, {
            "session_id": session_id,
            "code": "BROWSER_SESSION_FORBIDDEN",
            "error": "Browser session not found",
        })
        return False
    return True


async def _handle_browser_command(user_id: str, org_id: str | None, event_type: str, data: dict):
    session_id = str(data.get("session_id", "")).strip()
    if not await _authorize_browser_session_for_ws(user_id, org_id, session_id):
        return

    action_map = {
        ClientEvent.BROWSER_NAVIGATE: "navigate",
        ClientEvent.BROWSER_CLICK: "click",
        ClientEvent.BROWSER_TYPE: "type",
        ClientEvent.BROWSER_KEY: "key",
        ClientEvent.BROWSER_SCROLL: "scroll",
        ClientEvent.BROWSER_REFRESH: "refresh",
        ClientEvent.BROWSER_BACK: "back",
        ClientEvent.BROWSER_FORWARD: "forward",
        ClientEvent.BROWSER_NEW_TAB: "new_tab",
        ClientEvent.BROWSER_SWITCH_TAB: "switch_tab",
        ClientEvent.BROWSER_CLOSE_TAB: "close_tab",
        ClientEvent.BROWSER_LIST_TABS: "list_tabs",
        ClientEvent.BROWSER_DISCOVER: "discover",
        ClientEvent.BROWSER_EXTRACT: "extract",
        ClientEvent.BROWSER_UPLOAD_ATTACHMENT: "upload_attachment",
        ClientEvent.BROWSER_SAVE_SCREENSHOT: "save_screenshot",
        ClientEvent.BROWSER_PRINT_PDF: "print_pdf",
        ClientEvent.BROWSER_SNAPSHOT: "snapshot",
    }
    action = action_map[event_type]
    payload = {k: v for k, v in data.items() if k not in {"type", "session_id"}}
    try:
        result = await browser_sessions.command(session_id, action, payload)
        await ws_manager.send_to(user_id, ServerEvent.BROWSER_SESSION_DELTA, {
            "session_id": session_id,
            "action": action,
            "result": result,
        })
    except BrowserCapabilityError as e:
        await ws_manager.send_to(user_id, ServerEvent.BROWSER_SESSION_ERROR, {
            "session_id": session_id,
            "error": str(e),
        })
    except Exception as e:
        logger.warning("browser ws command failed user=%s session=%s action=%s: %s", user_id, session_id, action, e)
        await ws_manager.send_to(user_id, ServerEvent.BROWSER_SESSION_ERROR, {
            "session_id": session_id,
            "error": str(e),
        })


async def _handle_chat_typing(user_id: str, org_id: str, data: dict, ws: WebSocket) -> None:
    conversation_id = _require_text_field(data, "conversation_id")
    if conversation_id is None:
        await _send_chat_error(ws, "CHAT_CONVERSATION_REQUIRED")
        return

    thread_root_message_id = _coerce_int_field(
        data,
        "thread_root_message_id",
        alias="message_id",
    )
    error_code = await _authorize_chat_subscription(
        user_id,
        org_id=org_id,
        conversation_id=conversation_id,
        thread_root_message_id=thread_root_message_id,
    )
    if error_code is not None:
        await _send_chat_error(ws, error_code)
        return

    await ws_manager.publish_chat_typing(
        user_id=user_id,
        conversation_id=conversation_id,
        thread_root_message_id=thread_root_message_id,
    )


async def _handle_chat_mark_read(user_id: str, org_id: str, data: dict, ws: WebSocket) -> None:
    conversation_id = _require_text_field(data, "conversation_id")
    if conversation_id is None:
        await _send_chat_error(ws, "CHAT_CONVERSATION_REQUIRED")
        return

    if _invalid_int_field(data, "last_read_conversation_seq") or _invalid_int_field(
        data,
        "last_read_message_id",
    ):
        await _send_chat_error(ws, "CHAT_READ_CURSOR_INVALID")
        return

    last_read_conversation_seq = _coerce_int_field(data, "last_read_conversation_seq")
    last_read_message_id = _coerce_int_field(data, "last_read_message_id")
    if last_read_conversation_seq is None and last_read_message_id is None and (
        "unread_count" in data or "unread_summary" in data
    ):
        await _send_chat_error(ws, "CHAT_READ_CURSOR_REQUIRED")
        return

    if last_read_conversation_seq is not None and last_read_conversation_seq < 0:
        await _send_chat_error(ws, "CHAT_READ_CURSOR_INVALID")
        return
    if last_read_message_id is not None and last_read_message_id < 0:
        await _send_chat_error(ws, "CHAT_READ_CURSOR_INVALID")
        return

    try:
        def _mark_read():
            with UnitOfWork() as uow:
                _, publish = ChatService(
                    uow.session,
                    {"id": user_id, "org_id": org_id},
                ).mark_conversation_read(
                    conversation_id,
                    ChatReadUpdate(
                        last_read_conversation_seq=last_read_conversation_seq,
                        last_read_message_id=last_read_message_id,
                    ),
                )
                summary = build_notification_summary(uow.session, user_id=user_id, org_id=org_id)
                return publish, summary.model_dump(mode="json")

        publish, summary_payload = await run_sync_with_unit_of_work(_mark_read)
    except HTTPException as exc:
        await _send_chat_error(ws, _chat_error_code_from_http_exception(exc))
        return
    except Exception as exc:
        logger.warning(
            "chat_mark_read_failed user=%s conversation=%s: %s",
            user_id,
            conversation_id,
            exc,
        )
        await _send_chat_error(ws, "CHAT_MARK_READ_FAILED")
        return
    try:
        await _publish_chat_read_state(publish)
        await ws_manager.publish_notification_summary_updated(
            user_id=user_id,
            summary=summary_payload,
        )
    except Exception as exc:
        logger.warning(
            "chat_mark_read_publish_failed user=%s conversation=%s: %s",
            user_id,
            conversation_id,
            exc,
        )


async def _publish_chat_read_state(publish: ChatReadPublishState) -> None:
    await ws_manager.publish_chat_read_updated(
        user_id=publish.user_id,
        conversation_id=publish.conversation_id,
        last_read_message_id=publish.last_read_message_id,
        last_read_conversation_seq=publish.last_read_conversation_seq,
    )
    await ws_manager.publish_chat_unread_updated(
        user_id=publish.user_id,
        conversation_id=publish.conversation_id,
        unread_summary=publish.unread_summary.model_dump(mode="json"),
    )


async def _send_chat_error(ws: WebSocket, code: str) -> None:
    await ws.send_json({"type": ServerEvent.CHAT_ERROR, "code": code})


def _require_text_field(data: dict, key: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _coerce_int_field(data: dict, key: str, *, alias: str | None = None) -> int | None:
    value = data.get(key)
    if value is None and alias is not None:
        value = data.get(alias)
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _invalid_int_field(data: dict, key: str, *, alias: str | None = None) -> bool:
    value = data.get(key)
    if value is None and alias is not None:
        value = data.get(alias)
    if value is None or value == "":
        return False
    if isinstance(value, bool):
        return True
    try:
        int(value)
    except (TypeError, ValueError):
        return True
    return False


def _chat_error_code_from_http_exception(exc: HTTPException) -> str:
    if exc.status_code == 404:
        return "CHAT_CONVERSATION_FORBIDDEN"
    if exc.status_code != 400:
        return "CHAT_MARK_READ_FAILED"
    detail = str(exc.detail)
    if "Read target message not found" in detail:
        return "CHAT_READ_TARGET_INVALID"
    if "Read sequence cannot be before" in detail:
        return "CHAT_READ_CURSOR_INVALID"
    return "CHAT_MARK_READ_INVALID"


async def _authorize_chat_subscription(
    user_id: str,
    *,
    org_id: str,
    conversation_id: str,
    thread_root_message_id: int | None = None,
) -> str | None:
    def _authorize():
        with UnitOfWork() as uow:
            conversation = ChatConversationRepository(uow.session).get_for_user(
                conversation_id,
                user_id,
            )
            if conversation is None:
                return "CHAT_CONVERSATION_FORBIDDEN"
            if str(conversation.org_id) != str(org_id):
                return "CHAT_CONVERSATION_FORBIDDEN"

            if thread_root_message_id is None:
                return None

            root_message = ChatMessageRepository(uow.session).get(thread_root_message_id)
            if (
                root_message is None
                or root_message.deleted_at is not None
                or root_message.conversation_id != conversation.id
                or root_message.thread_root_message_id is not None
            ):
                return "CHAT_THREAD_SUBSCRIPTION_INVALID"
            return None

    return await run_sync_with_unit_of_work(_authorize)
