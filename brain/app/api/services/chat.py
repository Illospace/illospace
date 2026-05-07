"""Service-layer orchestration for native team chat."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException
from sqlalchemy.orm import Session

from brain.app.mentions import extract_mention_tokens
from brain.app.api.schemas.chat import (
    ChatBootstrapRead,
    ChatConversationSummaryRead,
    ChatDmCreate,
    ChatMessageCreate,
    ChatMessagePageRead,
    ChatMessageRead,
    ChatNotificationRead,
    ChatParticipantRead,
    ChatReadUpdate,
    ChatSearchResultRead,
    ChatThreadRead,
    ChatUnreadSummaryRead,
)
from brain.app.api.services.notifications import compact_notification_text
from brain.platform.db.models.chat import ChatConversation, ChatMessage, ChatNotification
from brain.platform.db.models.notification import (
    NOTIFICATION_KIND_CHAT_DM_MESSAGE,
    NOTIFICATION_KIND_CHAT_MENTION,
    NOTIFICATION_KIND_CHAT_ROOM_MESSAGE,
    NOTIFICATION_SOURCE_CHAT,
)
from brain.platform.db.models.org import User
from brain.platform.db.repositories.chat import (
    ChatConversationReadRepository,
    ChatConversationRepository,
    ChatMessageMentionRepository,
    ChatMessageRepository,
    ChatNotificationRepository,
)
from brain.platform.db.repositories.notifications import NotificationEventRepository
from brain.platform.db.repositories.team import TeamRepository

ILLO_NAME = "Illo"
ILLO_COLOR = "#5ea898"
EMAIL_LOCAL_ALIAS_SPLIT_RE = re.compile(r"[._+-]+")


@dataclass(slots=True)
class ChatPublishState:
    conversation_id: str
    root_message_id: int | None
    member_ids: list[str]
    message: ChatMessageRead
    root_message: ChatMessageRead | None
    unread_by_user: dict[str, ChatUnreadSummaryRead]
    notifications_by_user: dict[str, list[ChatNotificationRead]]


@dataclass(slots=True)
class ChatReadPublishState:
    conversation_id: str
    user_id: str
    last_read_message_id: int | None
    last_read_conversation_seq: int
    unread_summary: ChatUnreadSummaryRead


def resolve_user_mentions(body: str, users: list[User]) -> tuple[list[User], bool]:
    tokens = extract_mention_tokens(body)
    illo_invoked = "illo" in tokens
    user_tokens = tokens - {"illo"}
    if not user_tokens:
        return [], illo_invoked

    matches: list[User] = []
    seen: set[str] = set()
    for user in users:
        email_local = user.email.split("@", 1)[0].lower()
        aliases = {
            user.name.strip().lower(),
            "".join(ch for ch in user.name.lower() if ch.isalnum()),
            (user.name.split()[0].lower() if user.name.split() else ""),
            email_local,
        }
        aliases.update(part for part in EMAIL_LOCAL_ALIAS_SPLIT_RE.split(email_local) if part)
        aliases.discard("")
        if user_tokens.intersection(aliases) and str(user.id) not in seen:
            seen.add(str(user.id))
            matches.append(user)
    return matches, illo_invoked


def validated_limit(limit: int) -> int:
    return max(1, min(limit, 100))


def normalized_body_or_400(body: str, attachments: list[Any] | None = None) -> str:
    normalized = body.strip()
    if not normalized and not (attachments or []):
        raise HTTPException(status_code=400, detail="Message body cannot be empty")
    return normalized


class ChatService:
    def __init__(self, db: Session, user: dict[str, Any]):
        self.db = db
        self.user = user
        self.viewer_user_id = str(user["id"])
        self.org_id = self._require_org_id(user)
        self.team_repo = TeamRepository(db)
        self.conversation_repo = ChatConversationRepository(db)
        self.message_repo = ChatMessageRepository(db)
        self.read_repo = ChatConversationReadRepository(db)
        self.notification_repo = ChatNotificationRepository(db)
        self.mention_repo = ChatMessageMentionRepository(db)
        self.unified_notification_repo = NotificationEventRepository(db)

    def bootstrap(self) -> ChatBootstrapRead:
        room = self.ensure_org_room()
        conversations = list(self.conversation_repo.list_for_user(self.org_id, self.viewer_user_id))
        room_summary = self._serialize_conversation(room, viewer_user_id=self.viewer_user_id)
        dm_summaries = [
            self._serialize_conversation(conversation, viewer_user_id=self.viewer_user_id)
            for conversation in conversations
            if conversation.type == "dm"
        ]
        notifications = [
            self._serialize_notification(notification)
            for notification in self.notification_repo.list_for_user(self.viewer_user_id)
        ]
        return ChatBootstrapRead(
            room=room_summary,
            dms=dm_summaries,
            notifications=notifications,
            unread_summary=self._build_unread_summary(room_summary, dm_summaries),
            default_mode="room",
            default_conversation_id=str(room.id),
        )

    def list_conversations(self) -> list[ChatConversationSummaryRead]:
        room = self.ensure_org_room()
        conversations = list(self.conversation_repo.list_for_user(self.org_id, self.viewer_user_id))
        if all(conversation.id != room.id for conversation in conversations):
            conversations.insert(0, room)
        return [
            self._serialize_conversation(conversation, viewer_user_id=self.viewer_user_id)
            for conversation in conversations
        ]

    def create_or_fetch_dm(self, body: ChatDmCreate) -> ChatConversationSummaryRead:
        target_user_id = body.user_id.strip()
        if target_user_id == self.viewer_user_id:
            raise HTTPException(status_code=400, detail="You cannot DM yourself")

        target_user = self.team_repo.get_by_id(target_user_id)
        if target_user is None or str(target_user.org_id) != self.org_id or not target_user.approved:
            raise HTTPException(status_code=404, detail="User not found")

        conversation = self.conversation_repo.get_or_create_dm(
            self.org_id,
            self.viewer_user_id,
            target_user_id,
        )
        self.db.flush()
        return self._serialize_conversation(conversation, viewer_user_id=self.viewer_user_id)

    def get_conversation_messages(
        self,
        conversation_id: str,
        *,
        before_seq: int | None,
        limit: int,
    ) -> ChatMessagePageRead:
        conversation = self.get_conversation_or_404(conversation_id)
        page_limit = validated_limit(limit)
        messages = list(
            self.message_repo.list_conversation_messages(
                conversation,
                before_seq=before_seq,
                limit=page_limit + 1,
            )
        )
        has_more = len(messages) > page_limit
        if has_more:
            messages = messages[1:]

        user_by_id = self._conversation_user_map(conversation.id)
        thread_preview_by_root_id = self._thread_preview_participants_by_root_id(
            [message.id for message in messages if message.reply_count],
            user_by_id,
        )
        return ChatMessagePageRead(
            conversation=self._serialize_conversation(conversation, viewer_user_id=self.viewer_user_id),
            messages=[
                self._serialize_message(
                    message,
                    user_by_id,
                    thread_preview_participants=thread_preview_by_root_id.get(message.id),
                )
                for message in messages
            ],
            has_more=has_more,
            next_before_seq=messages[0].conversation_seq if has_more else None,
        )

    def post_conversation_message(
        self,
        conversation_id: str,
        body: ChatMessageCreate,
    ) -> tuple[ChatMessageRead, ChatPublishState]:
        conversation = self.get_conversation_or_404(conversation_id)
        if body.reply_to_message_id is not None:
            raise HTTPException(status_code=400, detail="Use the thread endpoint for room replies")
        attachments = list(body.attachments)

        message = self.message_repo.create_message(
            conversation_id=conversation.id,
            sender_user_id=self.viewer_user_id,
            sender_kind="user",
            body=normalized_body_or_400(body.body, attachments),
            body_format=body.body_format,
            client_generated_id=body.client_generated_id,
            attachments=attachments,
            metadata=dict(body.metadata or {}),
        )
        members = list(self.conversation_repo.list_member_users(conversation.id))
        created_notifications = self._store_mentions_and_notifications(
            conversation=conversation,
            message=message,
            sender_user_id=self.viewer_user_id,
            members=members,
        )
        self.db.flush()
        user_by_id = self._user_map(members)
        message_read = self._serialize_message(message, user_by_id)
        publish = self._build_publish_state(
            conversation=conversation,
            message=message,
            message_read=message_read,
            root_message=None,
            created_notifications=created_notifications,
            member_ids=[str(member.id) for member in members],
        )
        return message_read, publish

    def get_message_thread(
        self,
        message_id: int,
        *,
        before_seq: int | None,
        limit: int,
    ) -> ChatThreadRead:
        conversation, root_message = self.get_root_message_or_404(message_id)
        page_limit = validated_limit(limit)
        replies = list(
            self.message_repo.list_thread_replies(
                root_message.id,
                before_seq=before_seq,
                limit=page_limit + 1,
            )
        )
        has_more = len(replies) > page_limit
        if has_more:
            replies = replies[1:]

        user_by_id = self._conversation_user_map(conversation.id)
        thread_preview_by_root_id = self._thread_preview_participants_by_root_id(
            [root_message.id],
            user_by_id,
        )
        return ChatThreadRead(
            conversation=self._serialize_conversation(conversation, viewer_user_id=self.viewer_user_id),
            root_message=self._serialize_message(
                root_message,
                user_by_id,
                thread_preview_participants=thread_preview_by_root_id.get(root_message.id),
            ),
            replies=[self._serialize_message(reply, user_by_id) for reply in replies],
            has_more=has_more,
            next_before_seq=replies[0].conversation_seq if has_more else None,
        )

    def search_room_messages(
        self,
        *,
        query: str,
        limit: int,
    ) -> list[ChatSearchResultRead]:
        normalized_query = query.strip()
        if len(normalized_query) < 2:
            raise HTTPException(
                status_code=400,
                detail="Search query must be at least 2 characters",
            )

        room = self.ensure_org_room()
        messages = list(
            self.message_repo.search_conversation_messages(
                room,
                query=normalized_query,
                limit=validated_limit(limit),
            )
        )
        if not messages:
            return []

        user_by_id = self._conversation_user_map(room.id)
        thread_preview_by_root_id = self._thread_preview_participants_by_root_id(
            [message.thread_root_message_id or message.id for message in messages],
            user_by_id,
        )
        roots_by_id: dict[int, ChatMessageRead] = {}
        results: list[ChatSearchResultRead] = []
        for message in messages:
            root_message_id = message.thread_root_message_id or message.id
            if root_message_id not in roots_by_id:
                root_message = self.message_repo.get(root_message_id)
                if root_message is None or root_message.deleted_at is not None:
                    continue
                roots_by_id[root_message_id] = self._serialize_message(
                    root_message,
                    user_by_id,
                    thread_preview_participants=thread_preview_by_root_id.get(root_message_id),
                )
            results.append(
                ChatSearchResultRead(
                    message=self._serialize_message(message, user_by_id),
                    root_message=roots_by_id[root_message_id],
                )
            )
        return results

    def post_thread_reply(
        self,
        message_id: int,
        body: ChatMessageCreate,
    ) -> tuple[ChatMessageRead, ChatPublishState]:
        conversation, root_message = self.get_root_message_or_404(message_id)
        self._validate_reply_target_or_400(
            conversation=conversation,
            root_message=root_message,
            reply_to_message_id=body.reply_to_message_id,
        )
        attachments = list(body.attachments)
        reply = self.message_repo.create_message(
            conversation_id=conversation.id,
            sender_user_id=self.viewer_user_id,
            sender_kind="user",
            body=normalized_body_or_400(body.body, attachments),
            body_format=body.body_format,
            client_generated_id=body.client_generated_id,
            attachments=attachments,
            metadata=dict(body.metadata or {}),
            thread_root_message_id=root_message.id,
            reply_to_message_id=body.reply_to_message_id,
        )
        members = list(self.conversation_repo.list_member_users(conversation.id))
        created_notifications = self._store_mentions_and_notifications(
            conversation=conversation,
            message=reply,
            sender_user_id=self.viewer_user_id,
            members=members,
        )
        self.db.flush()
        user_by_id = self._user_map(members)
        reply_read = self._serialize_message(reply, user_by_id)
        root_read = self._serialize_message(
            root_message,
            user_by_id,
            thread_preview_participants=self._thread_preview_participants_by_root_id(
                [root_message.id],
                user_by_id,
            ).get(root_message.id),
        )
        publish = self._build_publish_state(
            conversation=conversation,
            message=reply,
            message_read=reply_read,
            root_message=root_read,
            created_notifications=created_notifications,
            member_ids=[str(member.id) for member in members],
        )
        return reply_read, publish

    def post_agent_message(
        self,
        *,
        conversation_id: str,
        body: str,
        thread_root_message_id: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> tuple[ChatMessageRead, ChatPublishState]:
        conversation = self.get_conversation_or_404(conversation_id)
        if conversation.type != "room":
            raise HTTPException(status_code=400, detail="Illo can only post in the team room")

        root_message = None
        if thread_root_message_id is not None:
            root_message = self.message_repo.get(int(thread_root_message_id))
            if (
                root_message is None
                or root_message.deleted_at is not None
                or root_message.conversation_id != conversation.id
                or root_message.thread_root_message_id is not None
            ):
                raise HTTPException(status_code=400, detail="Thread root not found in team room")

        message = self.message_repo.create_message(
            conversation_id=conversation.id,
            sender_user_id=None,
            sender_kind="agent",
            body=normalized_body_or_400(body),
            metadata={
                "source": "illo_agent",
                **dict(metadata or {}),
            },
            thread_root_message_id=root_message.id if root_message is not None else None,
        )
        members = list(self.conversation_repo.list_member_users(conversation.id))
        created_notifications = self._store_mentions_and_notifications(
            conversation=conversation,
            message=message,
            sender_user_id=None,
            sender_name=ILLO_NAME,
            members=members,
        )
        self.db.flush()
        user_by_id = self._user_map(members)
        message_read = self._serialize_message(message, user_by_id)
        root_read = (
            self._serialize_message(
                root_message,
                user_by_id,
                thread_preview_participants=self._thread_preview_participants_by_root_id(
                    [root_message.id],
                    user_by_id,
                ).get(root_message.id),
            )
            if root_message is not None
            else None
        )
        publish = self._build_publish_state(
            conversation=conversation,
            message=message,
            message_read=message_read,
            root_message=root_read,
            created_notifications=created_notifications,
            member_ids=[str(member.id) for member in members],
        )
        return message_read, publish

    def mark_conversation_read(
        self,
        conversation_id: str,
        body: ChatReadUpdate,
    ) -> tuple[ChatUnreadSummaryRead, ChatReadPublishState]:
        conversation = self.get_conversation_or_404(conversation_id)
        last_read_seq, last_read_message_id = self._resolve_read_target_or_400(
            conversation=conversation,
            body=body,
        )
        cursor = self.read_repo.upsert_cursor(
            conversation_id=conversation.id,
            user_id=self.viewer_user_id,
            last_read_conversation_seq=last_read_seq,
            last_read_message_id=last_read_message_id,
        )
        self.notification_repo.mark_read_through_conversation_seq(
            user_id=self.viewer_user_id,
            conversation_id=conversation.id,
            last_read_conversation_seq=cursor.last_read_conversation_seq,
        )
        self.mention_repo.mark_seen_through_conversation_seq(
            user_id=self.viewer_user_id,
            conversation_id=conversation.id,
            last_read_conversation_seq=cursor.last_read_conversation_seq,
        )
        if cursor.last_read_conversation_seq >= (conversation.last_message_seq or 0):
            self.unified_notification_repo.mark_read_for_chat_conversation(
                user_id=self.viewer_user_id,
                conversation_id=conversation.id,
            )
        self.db.flush()
        unread_summary = self.build_unread_summary_for_user(self.viewer_user_id)
        return unread_summary, ChatReadPublishState(
            conversation_id=str(conversation.id),
            user_id=self.viewer_user_id,
            last_read_message_id=cursor.last_read_message_id,
            last_read_conversation_seq=cursor.last_read_conversation_seq,
            unread_summary=unread_summary,
        )

    def list_notifications(self, *, limit: int) -> list[ChatNotificationRead]:
        notifications = self.notification_repo.list_for_user(
            self.viewer_user_id,
            limit=validated_limit(limit),
        )
        return [self._serialize_notification(notification) for notification in notifications]

    def mark_notification_read(self, notification_id: int) -> bool:
        notification = self.notification_repo.mark_read(notification_id, self.viewer_user_id)
        if notification is None:
            raise HTTPException(status_code=404, detail="Notification not found")
        self.db.flush()
        return True

    def mark_all_notifications_read(self) -> int:
        count = self.notification_repo.mark_all_read(self.viewer_user_id)
        self.db.flush()
        return count

    def build_unread_summary_for_user(self, user_id: str) -> ChatUnreadSummaryRead:
        return self.build_unread_summaries_for_users([user_id]).get(
            str(user_id),
            ChatUnreadSummaryRead(),
        )

    def build_unread_summaries_for_users(
        self,
        user_ids: list[str],
    ) -> dict[str, ChatUnreadSummaryRead]:
        counts_by_user = self.read_repo.unread_counts_for_users(
            org_id=self.org_id,
            user_ids=user_ids,
        )
        summaries: dict[str, ChatUnreadSummaryRead] = {}
        for user_id in {str(user_id) for user_id in user_ids if str(user_id)}:
            room, dms = counts_by_user.get(user_id, (0, 0))
            summaries[user_id] = ChatUnreadSummaryRead(room=room, dms=dms, total=room + dms)
        return summaries

    def ensure_org_room(self) -> ChatConversation:
        room = self.conversation_repo.ensure_org_room(
            self.org_id,
            self.viewer_user_id,
            title="Room",
        )
        self.db.flush()
        return room

    def get_conversation_or_404(self, conversation_id: str) -> ChatConversation:
        conversation = self.conversation_repo.get_for_user(conversation_id, self.viewer_user_id)
        if conversation is None or str(conversation.org_id) != self.org_id:
            raise HTTPException(status_code=404, detail="Conversation not found")
        return conversation

    def get_root_message_or_404(self, message_id: int) -> tuple[ChatConversation, ChatMessage]:
        root_message = self.message_repo.get(message_id)
        if root_message is None:
            raise HTTPException(status_code=404, detail="Thread root not found")
        conversation = self.get_conversation_or_404(root_message.conversation_id)
        if conversation.type != "room" or root_message.thread_root_message_id is not None:
            raise HTTPException(
                status_code=400,
                detail="Only root room messages support thread replies",
            )
        return conversation, root_message

    def _conversation_user_map(self, conversation_id: str) -> dict[str, User]:
        return self._user_map(list(self.conversation_repo.list_member_users(conversation_id)))

    def _user_map(self, users: list[User]) -> dict[str, User]:
        return {str(user.id): user for user in users}

    def _serialize_participant(self, user: User) -> ChatParticipantRead:
        return ChatParticipantRead(
            id=str(user.id),
            name=user.name,
            color=user.color,
            email=user.email,
        )

    def _serialize_message(
        self,
        message: ChatMessage,
        user_by_id: dict[str, User],
        *,
        thread_preview_participants: list[ChatParticipantRead] | None = None,
    ) -> ChatMessageRead:
        sender_user = user_by_id.get(str(message.sender_user_id)) if message.sender_user_id else None
        if message.sender_kind == "agent":
            sender_name = ILLO_NAME
            sender_color = ILLO_COLOR
        elif message.sender_kind == "system":
            sender_name = "System"
            sender_color = "#9aa2bd"
        else:
            sender_name = sender_user.name if sender_user else "Unknown"
            sender_color = sender_user.color if sender_user else None

        return ChatMessageRead(
            id=message.id,
            conversation_id=str(message.conversation_id),
            sender_user_id=str(message.sender_user_id) if message.sender_user_id else None,
            sender_kind=message.sender_kind,
            sender_name=sender_name,
            sender_color=sender_color,
            body=message.body,
            body_format=message.body_format,
            client_generated_id=message.client_generated_id,
            thread_root_message_id=message.thread_root_message_id,
            reply_to_message_id=message.reply_to_message_id,
            attachments=message.attachments or [],
            metadata=message.metadata_,
            conversation_seq=message.conversation_seq,
            reply_count=message.reply_count or 0,
            last_reply_at=message.last_reply_at,
            last_reply_message_id=message.last_reply_message_id,
            thread_preview_participants=list(thread_preview_participants or []),
            created_at=message.created_at,
            edited_at=message.edited_at,
            deleted_at=message.deleted_at,
        )

    def _thread_preview_participants_by_root_id(
        self,
        root_message_ids: list[int],
        user_by_id: dict[str, User],
    ) -> dict[int, list[ChatParticipantRead]]:
        preview_user_ids_by_root_id = self.message_repo.list_thread_preview_sender_ids(
            root_message_ids,
            limit_per_thread=2,
        )
        previews: dict[int, list[ChatParticipantRead]] = {}
        for root_message_id, participant_user_ids in preview_user_ids_by_root_id.items():
            participants = [
                self._serialize_participant(user)
                for user_id in participant_user_ids
                if (user := user_by_id.get(str(user_id))) is not None
            ]
            if participants:
                previews[root_message_id] = participants
        return previews

    def _serialize_conversation(
        self,
        conversation: ChatConversation,
        *,
        viewer_user_id: str,
    ) -> ChatConversationSummaryRead:
        members = list(self.conversation_repo.list_member_users(conversation.id))
        user_by_id = self._user_map(members)
        last_message = self.message_repo.get_last_message(conversation.id)
        counterpart = None
        if conversation.type == "dm":
            counterpart_user = next(
                (member for member in members if str(member.id) != viewer_user_id),
                None,
            )
            if counterpart_user is not None:
                counterpart = self._serialize_participant(counterpart_user)

        title = conversation.title
        if conversation.type == "dm" and counterpart is not None:
            title = counterpart.name

        return ChatConversationSummaryRead(
            id=str(conversation.id),
            type=conversation.type,
            stable_key=conversation.stable_key,
            title=title,
            description=conversation.description,
            visibility=conversation.visibility,
            last_message_seq=conversation.last_message_seq or 0,
            unread_count=self._conversation_unread_count(conversation, viewer_user_id),
            participant_count=len(members),
            counterpart=counterpart,
            last_message=self._serialize_message(last_message, user_by_id) if last_message else None,
            created_at=conversation.created_at,
            updated_at=conversation.updated_at,
        )

    def _serialize_notification(self, notification: ChatNotification) -> ChatNotificationRead:
        actor = (
            self.team_repo.get_by_id(str(notification.actor_user_id))
            if notification.actor_user_id
            else None
        )
        return ChatNotificationRead(
            id=notification.id,
            type=notification.type,
            conversation_id=str(notification.conversation_id) if notification.conversation_id else None,
            message_id=notification.message_id,
            actor_user_id=str(notification.actor_user_id) if notification.actor_user_id else None,
            actor_name=actor.name if actor else None,
            actor_color=actor.color if actor else None,
            metadata=notification.metadata_,
            created_at=notification.created_at,
            read_at=notification.read_at,
        )

    def _conversation_unread_count(
        self,
        conversation: ChatConversation,
        user_id: str,
    ) -> int:
        read_state = self.read_repo.get_for_user(conversation.id, user_id)
        read_seq = read_state.last_read_conversation_seq if read_state else 0
        return max((conversation.last_message_seq or 0) - read_seq, 0)

    def _build_unread_summary(
        self,
        room: ChatConversationSummaryRead,
        dms: list[ChatConversationSummaryRead],
    ) -> ChatUnreadSummaryRead:
        dm_unread = sum(item.unread_count for item in dms)
        total = room.unread_count + dm_unread
        return ChatUnreadSummaryRead(room=room.unread_count, dms=dm_unread, total=total)

    def _resolve_read_target_or_400(
        self,
        *,
        conversation: ChatConversation,
        body: ChatReadUpdate,
    ) -> tuple[int, int | None]:
        if body.last_read_message_id is None and body.last_read_conversation_seq is None:
            return conversation.last_message_seq or 0, None

        if body.last_read_message_id is None:
            return body.last_read_conversation_seq or 0, None

        target_message = self.message_repo.get(body.last_read_message_id)
        if (
            target_message is None
            or target_message.deleted_at is not None
            or target_message.conversation_id != conversation.id
        ):
            raise HTTPException(
                status_code=400,
                detail="Read target message not found in conversation",
            )

        if (
            body.last_read_conversation_seq is not None
            and body.last_read_conversation_seq < target_message.conversation_seq
        ):
            raise HTTPException(
                status_code=400,
                detail="Read sequence cannot be before the target message",
            )

        return (
            body.last_read_conversation_seq
            if body.last_read_conversation_seq is not None
            else target_message.conversation_seq,
            target_message.id,
        )

    def _store_mentions_and_notifications(
        self,
        *,
        conversation: ChatConversation,
        message: ChatMessage,
        sender_user_id: str | None,
        sender_name: str | None = None,
        members: list[User],
    ) -> list[ChatNotification]:
        mentioned_users, illo_invoked = resolve_user_mentions(message.body, members)
        metadata = dict(message.metadata_ or {})
        sender_name = sender_name or next(
            (member.name for member in members if str(member.id) == sender_user_id),
            "Someone",
        )
        message_preview = compact_notification_text(message.body)
        if illo_invoked:
            metadata["illo_invoked"] = True
        if mentioned_users:
            metadata["mentioned_user_ids"] = [str(user.id) for user in mentioned_users]
        if metadata != (message.metadata_ or {}):
            message.metadata_ = metadata

        notifications: list[ChatNotification] = []
        notified_user_ids: set[str] = set()
        sender_user_ids = {str(sender_user_id)} if sender_user_id is not None else set()
        for mentioned_user in mentioned_users:
            mentioned_user_id = str(mentioned_user.id)
            if sender_user_id is not None and mentioned_user_id == sender_user_id:
                continue
            self.mention_repo.create(
                message_id=message.id,
                mentioned_user_id=mentioned_user_id,
                mentioned_by_user_id=sender_user_id,
                delivered_at=message.created_at,
            )
            notification = self.notification_repo.create(
                user_id=mentioned_user_id,
                type="mention",
                conversation_id=conversation.id,
                message_id=message.id,
                actor_user_id=sender_user_id,
                metadata={
                    "thread_root_message_id": message.thread_root_message_id or message.id,
                },
            )
            notifications.append(notification)
            self.unified_notification_repo.create_or_coalesce(
                org_id=str(conversation.org_id),
                user_id=mentioned_user_id,
                source=NOTIFICATION_SOURCE_CHAT,
                kind=NOTIFICATION_KIND_CHAT_MENTION,
                actor_user_id=sender_user_id,
                title=f"{sender_name} mentioned you in chat",
                body=message_preview,
                coalesce_key=(
                    f"chat:mention:{mentioned_user_id}:{conversation.id}:"
                    f"{message.thread_root_message_id or message.id}"
                ),
                payload={
                    "preview": message_preview,
                    "thread_root_message_id": message.thread_root_message_id or message.id,
                },
                conversation_id=str(conversation.id),
                thread_root_message_id=message.thread_root_message_id or message.id,
            )
            notified_user_ids.add(mentioned_user_id)

        if conversation.type == "dm":
            notification_type = "dm_message"
            notification_kind = NOTIFICATION_KIND_CHAT_DM_MESSAGE
            notification_title = f"{sender_name} sent you a message"
            coalesce_prefix = "chat:dm"
        else:
            notification_type = "room_message"
            notification_kind = NOTIFICATION_KIND_CHAT_ROOM_MESSAGE
            notification_title = f"{sender_name} posted in team chat"
            coalesce_prefix = "chat:room"

        for member in members:
            member_id = str(member.id)
            if member_id in sender_user_ids or member_id in notified_user_ids:
                continue
            if not getattr(member, "message_notifications_enabled", True):
                continue
            notification = self.notification_repo.create(
                user_id=member_id,
                type=notification_type,
                conversation_id=conversation.id,
                message_id=message.id,
                actor_user_id=sender_user_id,
                metadata=None,
            )
            notifications.append(notification)
            self.unified_notification_repo.create_or_coalesce(
                org_id=str(conversation.org_id),
                user_id=member_id,
                source=NOTIFICATION_SOURCE_CHAT,
                kind=notification_kind,
                actor_user_id=sender_user_id,
                title=notification_title,
                body=message_preview,
                coalesce_key=f"{coalesce_prefix}:{member_id}:{conversation.id}",
                payload={"preview": message_preview},
                conversation_id=str(conversation.id),
                thread_root_message_id=message.thread_root_message_id,
            )

        return notifications

    def _validate_reply_target_or_400(
        self,
        *,
        conversation: ChatConversation,
        root_message: ChatMessage,
        reply_to_message_id: int | None,
    ) -> None:
        if reply_to_message_id is None:
            return

        reply_target = self.message_repo.get(reply_to_message_id)
        if (
            reply_target is None
            or reply_target.deleted_at is not None
            or reply_target.conversation_id != conversation.id
        ):
            raise HTTPException(status_code=400, detail="Reply target not found in thread")

        target_root_message_id = reply_target.thread_root_message_id or reply_target.id
        if target_root_message_id != root_message.id:
            raise HTTPException(status_code=400, detail="Reply target not found in thread")

    def _build_publish_state(
        self,
        *,
        conversation: ChatConversation,
        message: ChatMessage,
        message_read: ChatMessageRead,
        root_message: ChatMessageRead | None,
        created_notifications: list[ChatNotification],
        member_ids: list[str],
    ) -> ChatPublishState:
        notifications_by_user: dict[str, list[ChatNotificationRead]] = {}
        for notification in created_notifications:
            user_id = str(notification.user_id)
            notifications_by_user.setdefault(user_id, []).append(
                self._serialize_notification(notification)
            )

        unread_by_user = self.build_unread_summaries_for_users(member_ids)
        return ChatPublishState(
            conversation_id=str(conversation.id),
            root_message_id=message.thread_root_message_id or message.id,
            member_ids=member_ids,
            message=message_read,
            root_message=root_message,
            unread_by_user=unread_by_user,
            notifications_by_user=notifications_by_user,
        )

    @staticmethod
    def _require_org_id(user: dict[str, Any]) -> str:
        org_id = user.get("org_id")
        if not org_id:
            raise HTTPException(status_code=400, detail="User is not attached to an org")
        return str(org_id)
