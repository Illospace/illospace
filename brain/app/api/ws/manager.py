"""WebSocket connection manager — supports multiple tabs per user."""
from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from fastapi import WebSocket

from brain.app.api.ws.auth import WsTokenClaims
from brain.app.api.ws.events import ServerEvent

logger = logging.getLogger(__name__)


def _target_user_ids(data: Mapping[str, Any]) -> set[str]:
    targets: set[str] = set()
    single = str(data.get("target_user_id") or "").strip()
    if single:
        targets.add(single)
    raw_many = data.get("target_user_ids")
    if isinstance(raw_many, Iterable) and not isinstance(raw_many, (str, bytes, bytearray, Mapping)):
        targets.update(str(item).strip() for item in raw_many if str(item).strip())
    return targets


@dataclass(slots=True)
class SocketState:
    user_id: str
    org_id: str
    session_id: str
    token_expires_at: str
    websocket: WebSocket
    tab_id: str | None = None
    chat_open: bool = False
    conversation_ids: set[str] = field(default_factory=set)
    thread_conversations: dict[int, str] = field(default_factory=dict)


class ConnectionManager:
    def __init__(self):
        self.connections: dict[str, list[WebSocket]] = {}
        self._socket_states: dict[int, SocketState] = {}
        self._conversation_subscribers: dict[str, set[int]] = defaultdict(set)
        self._thread_subscribers: dict[int, set[int]] = defaultdict(set)

    async def connect(
        self,
        claims: WsTokenClaims,
        ws: WebSocket,
        *,
        accept: bool = True,
    ) -> None:
        if accept:
            await ws.accept()
        user_id = claims.user_id
        self.connections.setdefault(user_id, []).append(ws)
        self._socket_states[id(ws)] = SocketState(
            user_id=user_id,
            org_id=claims.org_id,
            session_id=claims.session_id,
            token_expires_at=claims.expires_at.isoformat(),
            websocket=ws,
            tab_id=claims.tab_id,
        )
        logger.info("ws_connect user=%s tabs=%d", user_id, len(self.connections[user_id]))
        await self.broadcast_to_org(
            claims.org_id,
            ServerEvent.PRESENCE_CHANGE,
            {"user_id": user_id, "status": "online"},
            exclude=user_id,
        )

    async def disconnect(self, user_id: str, ws: WebSocket) -> None:
        conversation_ids, thread_root_ids, became_offline, org_id = self._remove_socket(user_id, ws)
        for conversation_id in conversation_ids:
            await self._broadcast_chat_presence_for_conversation(conversation_id)
        for thread_root_message_id in thread_root_ids:
            await self._broadcast_chat_presence_for_thread(thread_root_message_id)
        if became_offline and org_id:
            await self.broadcast_to_org(
                org_id,
                ServerEvent.PRESENCE_CHANGE,
                {"user_id": user_id, "status": "offline"},
            )

    async def broadcast(
        self,
        event_type: str,
        data: Mapping[str, Any],
        exclude: str | None = None,
        *,
        conversation_id: str | None = None,
        thread_root_message_id: int | None = None,
    ) -> None:
        message = {"type": str(event_type), **dict(data)}
        stale: list[tuple[str, WebSocket]] = []
        for state in self._iter_target_states(
            exclude=exclude,
            conversation_id=conversation_id,
            thread_root_message_id=thread_root_message_id,
        ):
            try:
                await state.websocket.send_json(message)
            except Exception:
                stale.append((state.user_id, state.websocket))
        for uid, ws in stale:
            await self.disconnect(uid, ws)

    async def broadcast_to_org(
        self,
        org_id: str,
        event_type: str,
        data: Mapping[str, Any],
        exclude: str | None = None,
        *,
        conversation_id: str | None = None,
        thread_root_message_id: int | None = None,
    ) -> None:
        normalized_org_id = str(org_id or "").strip()
        if not normalized_org_id:
            logger.warning("ws_drop_unscoped_org_broadcast event_type=%s", event_type)
            return
        message = {"type": str(event_type), **dict(data)}
        stale: list[tuple[str, WebSocket]] = []
        for state in self._iter_target_states(
            exclude=exclude,
            conversation_id=conversation_id,
            thread_root_message_id=thread_root_message_id,
        ):
            if state.org_id != normalized_org_id:
                continue
            try:
                await state.websocket.send_json(message)
            except Exception:
                stale.append((state.user_id, state.websocket))
        for uid, ws in stale:
            await self.disconnect(uid, ws)

    async def broadcast_product_event(
        self,
        event_type: str,
        data: Mapping[str, Any],
        *,
        org_id: str | None = None,
        allow_global: bool = False,
    ) -> bool:
        """Broadcast a product event, requiring org scope unless explicitly global."""
        payload = dict(data)
        target_user_ids = _target_user_ids(payload)
        scoped_org_id = str(org_id or payload.get("org_id") or "").strip()
        if scoped_org_id:
            payload["org_id"] = scoped_org_id
            if target_user_ids:
                await self.broadcast_to_org_users(scoped_org_id, target_user_ids, event_type, payload)
            else:
                await self.broadcast_to_org(scoped_org_id, event_type, payload)
            return True
        if allow_global:
            if target_user_ids:
                for user_id in sorted(target_user_ids):
                    await self.send_to(user_id, event_type, payload)
            else:
                await self.broadcast(event_type, payload)
            return True
        logger.warning("ws_drop_unscoped_product_event event_type=%s", event_type)
        return False

    async def broadcast_to_org_users(
        self,
        org_id: str,
        user_ids: Iterable[str],
        event_type: str,
        data: Mapping[str, Any],
    ) -> None:
        normalized_org_id = str(org_id or "").strip()
        target_user_ids = {str(user_id).strip() for user_id in user_ids if str(user_id).strip()}
        if not normalized_org_id or not target_user_ids:
            logger.warning("ws_drop_unscoped_user_broadcast event_type=%s", event_type)
            return
        message = {"type": str(event_type), **dict(data)}
        stale: list[tuple[str, WebSocket]] = []
        for state in self._iter_target_states():
            if state.org_id != normalized_org_id or state.user_id not in target_user_ids:
                continue
            try:
                await state.websocket.send_json(message)
            except Exception:
                stale.append((state.user_id, state.websocket))
        for uid, ws in stale:
            await self.disconnect(uid, ws)

    async def send_to(
        self,
        user_id: str,
        event_type: str,
        data: Mapping[str, Any],
        *,
        conversation_id: str | None = None,
        thread_root_message_id: int | None = None,
    ) -> None:
        message = {"type": str(event_type), **dict(data)}
        stale: list[tuple[str, WebSocket]] = []
        for state in self._iter_target_states(
            conversation_id=conversation_id,
            thread_root_message_id=thread_root_message_id,
        ):
            if state.user_id != user_id:
                continue
            try:
                await state.websocket.send_json(message)
            except Exception:
                stale.append((state.user_id, state.websocket))
        for uid, ws in stale:
            await self.disconnect(uid, ws)

    async def open_chat(self, user_id: str, ws: WebSocket) -> None:
        self._get_socket_state(user_id, ws).chat_open = True

    async def close_chat(self, user_id: str, ws: WebSocket) -> None:
        state = self._get_socket_state(user_id, ws)
        state.chat_open = False
        await self.clear_chat_subscriptions(user_id, ws)

    async def subscribe_conversation(self, user_id: str, ws: WebSocket, conversation_id: str) -> None:
        state = self._get_socket_state(user_id, ws)
        state.chat_open = True
        state.conversation_ids.add(conversation_id)
        self._conversation_subscribers[conversation_id].add(id(ws))
        await self._broadcast_chat_presence_for_conversation(conversation_id)

    async def unsubscribe_conversation(self, user_id: str, ws: WebSocket, conversation_id: str) -> None:
        state = self._get_socket_state(user_id, ws)
        if conversation_id not in state.conversation_ids:
            return
        state.conversation_ids.remove(conversation_id)
        subscribers = self._conversation_subscribers.get(conversation_id)
        if subscribers is not None:
            subscribers.discard(id(ws))
            if not subscribers:
                self._conversation_subscribers.pop(conversation_id, None)
        await self._broadcast_chat_presence_for_conversation(conversation_id)

    async def subscribe_thread(
        self,
        user_id: str,
        ws: WebSocket,
        *,
        conversation_id: str,
        thread_root_message_id: int,
    ) -> None:
        state = self._get_socket_state(user_id, ws)
        state.chat_open = True
        state.thread_conversations[thread_root_message_id] = conversation_id
        self._thread_subscribers[thread_root_message_id].add(id(ws))
        await self._broadcast_chat_presence_for_thread(thread_root_message_id)

    async def unsubscribe_thread(
        self,
        user_id: str,
        ws: WebSocket,
        *,
        thread_root_message_id: int,
    ) -> None:
        state = self._get_socket_state(user_id, ws)
        if thread_root_message_id not in state.thread_conversations:
            return
        state.thread_conversations.pop(thread_root_message_id, None)
        subscribers = self._thread_subscribers.get(thread_root_message_id)
        if subscribers is not None:
            subscribers.discard(id(ws))
            if not subscribers:
                self._thread_subscribers.pop(thread_root_message_id, None)
        await self._broadcast_chat_presence_for_thread(thread_root_message_id)

    async def clear_chat_subscriptions(self, user_id: str, ws: WebSocket) -> None:
        state = self._get_socket_state(user_id, ws)
        conversation_ids = set(state.conversation_ids)
        thread_root_ids = set(state.thread_conversations)
        for conversation_id in conversation_ids:
            subscribers = self._conversation_subscribers.get(conversation_id)
            if subscribers is not None:
                subscribers.discard(id(ws))
                if not subscribers:
                    self._conversation_subscribers.pop(conversation_id, None)
        for thread_root_message_id in thread_root_ids:
            subscribers = self._thread_subscribers.get(thread_root_message_id)
            if subscribers is not None:
                subscribers.discard(id(ws))
                if not subscribers:
                    self._thread_subscribers.pop(thread_root_message_id, None)
        state.conversation_ids.clear()
        state.thread_conversations.clear()
        for conversation_id in conversation_ids:
            await self._broadcast_chat_presence_for_conversation(conversation_id)
        for thread_root_message_id in thread_root_ids:
            await self._broadcast_chat_presence_for_thread(thread_root_message_id)

    async def publish_chat_typing(
        self,
        *,
        user_id: str,
        conversation_id: str,
        thread_root_message_id: int | None = None,
    ) -> None:
        payload = {
            "scope": "thread" if thread_root_message_id is not None else "conversation",
            "conversation_id": conversation_id,
            "thread_root_message_id": thread_root_message_id,
            "user_id": user_id,
        }
        if thread_root_message_id is None:
            await self.broadcast(
                ServerEvent.CHAT_TYPING,
                payload,
                exclude=user_id,
                conversation_id=conversation_id,
            )
            return
        await self.broadcast(
            ServerEvent.CHAT_TYPING,
            payload,
            exclude=user_id,
            thread_root_message_id=thread_root_message_id,
        )

    async def publish_chat_message_created(
        self,
        *,
        conversation_id: str,
        message: Mapping[str, Any],
    ) -> None:
        await self.broadcast(
            ServerEvent.CHAT_MESSAGE_CREATED,
            {"conversation_id": conversation_id, "message": dict(message)},
            conversation_id=conversation_id,
        )

    async def publish_chat_thread_reply_created(
        self,
        *,
        conversation_id: str,
        thread_root_message_id: int,
        message: Mapping[str, Any],
    ) -> None:
        await self.broadcast(
            ServerEvent.CHAT_THREAD_REPLY_CREATED,
            {
                "conversation_id": conversation_id,
                "thread_root_message_id": thread_root_message_id,
                "message": dict(message),
            },
            conversation_id=conversation_id,
            thread_root_message_id=thread_root_message_id,
        )

    async def publish_chat_thread_summary_updated(
        self,
        *,
        conversation_id: str,
        thread_root_message_id: int,
        root_message: Mapping[str, Any],
    ) -> None:
        await self.broadcast(
            ServerEvent.CHAT_THREAD_SUMMARY_UPDATED,
            {
                "conversation_id": conversation_id,
                "thread_root_message_id": thread_root_message_id,
                "root_message": dict(root_message),
            },
            conversation_id=conversation_id,
            thread_root_message_id=thread_root_message_id,
        )

    async def publish_chat_unread_updated(
        self,
        *,
        user_id: str,
        unread_summary: Mapping[str, Any] | None = None,
        conversation_id: str | None = None,
        unread_count: int | None = None,
        last_read_conversation_seq: int | None = None,
        last_read_message_id: int | None = None,
    ) -> None:
        payload: dict[str, Any] = {}
        if conversation_id is not None:
            payload["conversation_id"] = conversation_id
        if unread_count is not None:
            payload["unread_count"] = unread_count
        if unread_summary is not None:
            payload["unread_summary"] = dict(unread_summary)
        if last_read_conversation_seq is not None:
            payload["last_read_conversation_seq"] = last_read_conversation_seq
        if last_read_message_id is not None:
            payload["last_read_message_id"] = last_read_message_id
        await self.send_to(user_id, ServerEvent.CHAT_UNREAD_UPDATED, payload)

    async def publish_chat_notification_created(
        self,
        *,
        user_id: str,
        notification: Mapping[str, Any],
    ) -> None:
        await self.send_to(
            user_id,
            ServerEvent.CHAT_NOTIFICATION_CREATED,
            {"notification": dict(notification)},
        )

    async def publish_chat_read_updated(
        self,
        *,
        user_id: str,
        conversation_id: str,
        last_read_conversation_seq: int | None = None,
        last_read_message_id: int | None = None,
    ) -> None:
        payload: dict[str, Any] = {"conversation_id": conversation_id}
        if last_read_conversation_seq is not None:
            payload["last_read_conversation_seq"] = last_read_conversation_seq
        if last_read_message_id is not None:
            payload["last_read_message_id"] = last_read_message_id
        await self.send_to(user_id, ServerEvent.CHAT_READ_UPDATED, payload)

    async def publish_notification_summary_updated(
        self,
        *,
        user_id: str,
        summary: Mapping[str, Any],
    ) -> None:
        await self.send_to(
            user_id,
            ServerEvent.NOTIFICATION_SUMMARY_UPDATED,
            {"summary": dict(summary)},
        )

    async def broadcast_run_event(
        self,
        event_type: str,
        payload: dict[str, Any],
        *,
        org_id: str | None = None,
    ) -> bool:
        """Broadcast a durable run event to active websocket subscribers."""
        return await self.broadcast_product_event(
            event_type,
            payload,
            org_id=org_id,
            allow_global=False,
        )

    @property
    def connected_org_ids(self) -> list[str]:
        return sorted({state.org_id for state in self._socket_states.values()})

    @property
    def online_users(self) -> list[str]:
        return list(self.connections.keys())

    def _get_socket_state(self, user_id: str, ws: WebSocket) -> SocketState:
        state = self._socket_states.get(id(ws))
        if state is None:
            raise KeyError(f"Socket for user {user_id} is not connected")
        if state.user_id != user_id:
            raise KeyError(f"Socket for user {user_id} is not authenticated")
        return state

    def _iter_target_states(
        self,
        *,
        exclude: str | None = None,
        conversation_id: str | None = None,
        thread_root_message_id: int | None = None,
    ) -> list[SocketState]:
        if conversation_id is None and thread_root_message_id is None:
            socket_ids = list(self._socket_states.keys())
        else:
            socket_ids_set: set[int] = set()
            if conversation_id is not None:
                socket_ids_set.update(self._conversation_subscribers.get(conversation_id, set()))
            if thread_root_message_id is not None:
                socket_ids_set.update(self._thread_subscribers.get(thread_root_message_id, set()))
            socket_ids = list(socket_ids_set)

        states: list[SocketState] = []
        for socket_id in socket_ids:
            state = self._socket_states.get(socket_id)
            if state is None or state.user_id == exclude:
                continue
            states.append(state)
        return states

    def _remove_socket(self, user_id: str, ws: WebSocket) -> tuple[set[str], set[int], bool, str | None]:
        state = self._socket_states.pop(id(ws), None)
        conversation_ids = set(state.conversation_ids) if state else set()
        thread_root_ids = set(state.thread_conversations) if state else set()
        org_id = state.org_id if state else None

        for conversation_id in conversation_ids:
            subscribers = self._conversation_subscribers.get(conversation_id)
            if subscribers is not None:
                subscribers.discard(id(ws))
                if not subscribers:
                    self._conversation_subscribers.pop(conversation_id, None)

        for thread_root_message_id in thread_root_ids:
            subscribers = self._thread_subscribers.get(thread_root_message_id)
            if subscribers is not None:
                subscribers.discard(id(ws))
                if not subscribers:
                    self._thread_subscribers.pop(thread_root_message_id, None)

        sockets = self.connections.get(user_id, [])
        if sockets:
            remaining = [socket for socket in sockets if socket is not ws]
            if remaining:
                self.connections[user_id] = remaining
            else:
                self.connections.pop(user_id, None)

        became_offline = bool(org_id) and not any(
            socket_state.user_id == user_id and socket_state.org_id == org_id
            for socket_state in self._socket_states.values()
        )
        return conversation_ids, thread_root_ids, became_offline, org_id

    def _conversation_presence_payload(self, conversation_id: str) -> dict[str, Any]:
        user_ids = sorted(
            {
                state.user_id
                for socket_id in self._conversation_subscribers.get(conversation_id, set())
                if (state := self._socket_states.get(socket_id)) is not None
                and state.chat_open
                and conversation_id in state.conversation_ids
            }
        )
        return {
            "scope": "conversation",
            "conversation_id": conversation_id,
            "thread_root_message_id": None,
            "user_ids": user_ids,
            "count": len(user_ids),
        }

    def _thread_presence_payload(self, thread_root_message_id: int) -> dict[str, Any]:
        user_ids: set[str] = set()
        conversation_id: str | None = None
        for socket_id in self._thread_subscribers.get(thread_root_message_id, set()):
            state = self._socket_states.get(socket_id)
            if state is None or not state.chat_open:
                continue
            mapped_conversation_id = state.thread_conversations.get(thread_root_message_id)
            if mapped_conversation_id is None:
                continue
            conversation_id = conversation_id or mapped_conversation_id
            user_ids.add(state.user_id)
        return {
            "scope": "thread",
            "conversation_id": conversation_id,
            "thread_root_message_id": thread_root_message_id,
            "user_ids": sorted(user_ids),
            "count": len(user_ids),
        }

    async def _broadcast_chat_presence_for_conversation(self, conversation_id: str) -> None:
        await self.broadcast(
            ServerEvent.CHAT_PRESENCE,
            self._conversation_presence_payload(conversation_id),
            conversation_id=conversation_id,
        )

    async def _broadcast_chat_presence_for_thread(self, thread_root_message_id: int) -> None:
        await self.broadcast(
            ServerEvent.CHAT_PRESENCE,
            self._thread_presence_payload(thread_root_message_id),
            thread_root_message_id=thread_root_message_id,
        )
