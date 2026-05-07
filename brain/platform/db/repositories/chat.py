"""Repositories for the native chat backend."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Sequence

from sqlalchemy import and_, case, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError

from brain.platform.db.models.chat import (
    CHAT_CONVERSATION_DM,
    CHAT_CONVERSATION_ROOM,
    ChatConversation,
    ChatConversationMember,
    ChatConversationRead,
    ChatMessage,
    ChatMessageMention,
    ChatNotification,
)
from brain.platform.db.models.org import User
from brain.platform.db.repositories.base import BaseRepository


def build_dm_stable_key(user_a_id: str, user_b_id: str) -> str:
    first, second = sorted((str(user_a_id), str(user_b_id)))
    return f"dm:{first}:{second}"


class ChatConversationRepository(BaseRepository[ChatConversation]):
    model = ChatConversation
    pk_column = "id"

    def _approved_users_by_id(
        self,
        org_id: str,
        user_ids: Sequence[str],
    ) -> dict[str, User]:
        normalized_ids = {str(user_id) for user_id in user_ids if str(user_id)}
        if not normalized_ids:
            return {}
        stmt = select(User).where(
            User.org_id == org_id,
            User.id.in_(normalized_ids),
            User.approved.is_(True),
        )
        users = self._session.scalars(stmt).all()
        return {str(user.id): user for user in users}

    def _sync_member_ids(
        self,
        conversation_id: str,
        expected_user_ids: Sequence[str],
        *,
        remove_absent: bool,
    ) -> None:
        normalized_ids = {str(user_id) for user_id in expected_user_ids if str(user_id)}
        existing_members = list(
            self._session.scalars(
                select(ChatConversationMember)
                .where(ChatConversationMember.conversation_id == conversation_id)
                .with_for_update()
            ).all()
        )
        existing_by_user_id = {
            str(member.user_id): member for member in existing_members
        }

        for user_id in sorted(normalized_ids - set(existing_by_user_id)):
            self._session.execute(
                insert(ChatConversationMember)
                .values(
                    conversation_id=conversation_id,
                    user_id=user_id,
                    role="member",
                )
                .on_conflict_do_nothing(
                    index_elements=[
                        ChatConversationMember.conversation_id,
                        ChatConversationMember.user_id,
                    ]
                )
            )

        if not remove_absent:
            return

        for user_id, member in existing_by_user_id.items():
            if user_id in normalized_ids:
                continue
            self._session.delete(member)

    def get_by_stable_key(self, org_id: str, stable_key: str) -> ChatConversation | None:
        stmt = select(ChatConversation).where(
            ChatConversation.org_id == org_id,
            ChatConversation.stable_key == stable_key,
        )
        return self._session.scalars(stmt).first()

    def get_for_user(self, conversation_id: str, user_id: str) -> ChatConversation | None:
        stmt = (
            select(ChatConversation)
            .join(
                ChatConversationMember,
                ChatConversationMember.conversation_id == ChatConversation.id,
            )
            .where(
                ChatConversation.id == conversation_id,
                ChatConversationMember.user_id == user_id,
                ChatConversation.is_archived.is_(False),
            )
        )
        return self._session.scalars(stmt).first()

    def list_for_user(self, org_id: str, user_id: str) -> Sequence[ChatConversation]:
        stmt = (
            select(ChatConversation)
            .join(
                ChatConversationMember,
                ChatConversationMember.conversation_id == ChatConversation.id,
            )
            .where(
                ChatConversation.org_id == org_id,
                ChatConversationMember.user_id == user_id,
                ChatConversation.is_archived.is_(False),
            )
            .order_by(
                case((ChatConversation.type == "room", 0), else_=1),
                ChatConversation.updated_at.desc(),
            )
        )
        return self._session.scalars(stmt).all()

    def ensure_org_room(
        self,
        org_id: str,
        created_by_user_id: str | None,
        *,
        title: str = "Room",
    ) -> ChatConversation:
        conversation = self.get_by_stable_key(org_id, "org-room")
        if conversation is None:
            try:
                with self._session.begin_nested():
                    conversation = ChatConversation(
                        org_id=org_id,
                        type=CHAT_CONVERSATION_ROOM,
                        stable_key="org-room",
                        title=title,
                        visibility="org",
                        created_by_user_id=created_by_user_id,
                    )
                    self._session.add(conversation)
                    self._session.flush()
            except IntegrityError:
                conversation = self.get_by_stable_key(org_id, "org-room")
                if conversation is None:
                    raise

        if conversation.type != CHAT_CONVERSATION_ROOM:
            raise ValueError("Shared room stable key is reserved for room conversations")
        if not conversation.title:
            conversation.title = title
        self.sync_org_room_members(conversation.id, org_id)
        return conversation

    def sync_org_room_members(self, conversation_id: str, org_id: str) -> None:
        conversation = self._session.get(ChatConversation, conversation_id)
        if conversation is None:
            raise LookupError(f"ChatConversation {conversation_id} not found")
        if str(conversation.org_id) != org_id:
            raise ValueError("Shared room membership sync must stay within the same org")
        if conversation.type != CHAT_CONVERSATION_ROOM:
            raise ValueError("Only room conversations support shared-room membership sync")

        approved_user_ids = [
            str(user_id)
            for user_id in self._session.scalars(
                select(User.id).where(
                    User.org_id == org_id,
                    User.approved.is_(True),
                )
            ).all()
        ]
        self._sync_member_ids(
            conversation_id,
            approved_user_ids,
            remove_absent=True,
        )

    def get_or_create_dm(
        self,
        org_id: str,
        initiating_user_id: str,
        other_user_id: str,
    ) -> ChatConversation:
        initiating_user_id = str(initiating_user_id)
        other_user_id = str(other_user_id)
        if initiating_user_id == other_user_id:
            raise ValueError("Direct messages require two distinct users")

        approved_users = self._approved_users_by_id(
            org_id,
            [initiating_user_id, other_user_id],
        )
        if set(approved_users) != {initiating_user_id, other_user_id}:
            raise LookupError("Direct messages require approved users in the same org")

        stable_key = build_dm_stable_key(initiating_user_id, other_user_id)
        conversation = self.get_by_stable_key(org_id, stable_key)
        if conversation is None:
            try:
                with self._session.begin_nested():
                    conversation = ChatConversation(
                        org_id=org_id,
                        type=CHAT_CONVERSATION_DM,
                        stable_key=stable_key,
                        title=None,
                        visibility="members",
                        created_by_user_id=initiating_user_id,
                    )
                    self._session.add(conversation)
                    self._session.flush()
            except IntegrityError:
                conversation = self.get_by_stable_key(org_id, stable_key)
                if conversation is None:
                    raise

        if conversation.type != CHAT_CONVERSATION_DM:
            raise ValueError("DM stable keys are reserved for direct-message conversations")

        self._sync_member_ids(
            conversation.id,
            [initiating_user_id, other_user_id],
            remove_absent=True,
        )
        return conversation

    def list_member_users(self, conversation_id: str) -> Sequence[User]:
        stmt = (
            select(User)
            .join(
                ChatConversationMember,
                ChatConversationMember.user_id == User.id,
            )
            .where(ChatConversationMember.conversation_id == conversation_id)
            .order_by(User.name)
        )
        return self._session.scalars(stmt).all()

    def list_member_ids(self, conversation_id: str) -> list[str]:
        stmt = select(ChatConversationMember.user_id).where(
            ChatConversationMember.conversation_id == conversation_id
        )
        return [str(user_id) for user_id in self._session.scalars(stmt).all()]


class ChatMessageRepository(BaseRepository[ChatMessage]):
    model = ChatMessage

    @staticmethod
    def _normalized_limit(limit: int) -> int:
        return max(1, min(limit, 100))

    def _conversation_for_update(self, conversation_id: str) -> ChatConversation:
        stmt = (
            select(ChatConversation)
            .where(ChatConversation.id == conversation_id)
            .with_for_update()
        )
        return self._session.execute(stmt).scalar_one()

    def _message_for_update(self, message_id: int) -> ChatMessage | None:
        stmt = (
            select(ChatMessage)
            .where(ChatMessage.id == message_id)
            .with_for_update()
        )
        return self._session.execute(stmt).scalar_one_or_none()

    def _validate_sender_membership(
        self,
        *,
        conversation_id: str,
        sender_user_id: str | None,
    ) -> None:
        if sender_user_id is None:
            return
        membership_exists = self._session.scalars(
            select(ChatConversationMember.id).where(
                ChatConversationMember.conversation_id == conversation_id,
                ChatConversationMember.user_id == sender_user_id,
            )
        ).first()
        if membership_exists is None:
            raise LookupError("Sender is not a member of the conversation")

    def _resolve_thread_root(
        self,
        *,
        conversation: ChatConversation,
        thread_root_message_id: int | None,
    ) -> ChatMessage | None:
        if thread_root_message_id is None:
            return None
        if conversation.type != CHAT_CONVERSATION_ROOM:
            raise ValueError("Only room conversations support thread replies")

        root_message = self._message_for_update(thread_root_message_id)
        if (
            root_message is None
            or root_message.deleted_at is not None
            or root_message.conversation_id != conversation.id
        ):
            raise LookupError("Thread root message not found in the conversation")
        if root_message.thread_root_message_id is not None:
            raise ValueError("Thread root message must be a room root message")
        return root_message

    def _normalize_reply_target_id(
        self,
        *,
        conversation_id: str,
        root_message: ChatMessage | None,
        reply_to_message_id: int | None,
    ) -> int | None:
        if root_message is None or reply_to_message_id is None:
            return None

        reply_target = self._message_for_update(reply_to_message_id)
        if (
            reply_target is None
            or reply_target.deleted_at is not None
            or reply_target.conversation_id != conversation_id
        ):
            return None

        target_root_message_id = (
            reply_target.thread_root_message_id or reply_target.id
        )
        if target_root_message_id != root_message.id:
            return None
        return reply_target.id

    def list_conversation_messages(
        self,
        conversation: ChatConversation,
        *,
        before_seq: int | None = None,
        limit: int = 50,
    ) -> Sequence[ChatMessage]:
        limit = self._normalized_limit(limit)
        stmt = select(ChatMessage).where(
            ChatMessage.conversation_id == conversation.id,
            ChatMessage.deleted_at.is_(None),
        )
        if conversation.type == CHAT_CONVERSATION_ROOM:
            stmt = stmt.where(ChatMessage.thread_root_message_id.is_(None))
        if before_seq is not None:
            stmt = stmt.where(ChatMessage.conversation_seq < before_seq)
        stmt = stmt.order_by(ChatMessage.conversation_seq.desc()).limit(limit)
        return list(reversed(self._session.scalars(stmt).all()))

    def list_thread_replies(
        self,
        root_message_id: int,
        *,
        before_seq: int | None = None,
        limit: int = 50,
    ) -> Sequence[ChatMessage]:
        limit = self._normalized_limit(limit)
        stmt = select(ChatMessage).where(
            ChatMessage.thread_root_message_id == root_message_id,
            ChatMessage.deleted_at.is_(None),
        )
        if before_seq is not None:
            stmt = stmt.where(ChatMessage.conversation_seq < before_seq)
        stmt = stmt.order_by(ChatMessage.conversation_seq.desc()).limit(limit)
        return list(reversed(self._session.scalars(stmt).all()))

    def list_thread_preview_sender_ids(
        self,
        root_message_ids: Sequence[int],
        *,
        limit_per_thread: int = 2,
    ) -> dict[int, list[str]]:
        normalized_root_ids = [int(root_message_id) for root_message_id in root_message_ids if root_message_id is not None]
        if not normalized_root_ids or limit_per_thread <= 0:
            return {}

        stmt = (
            select(
                ChatMessage.thread_root_message_id,
                ChatMessage.sender_user_id,
            )
            .where(
                ChatMessage.thread_root_message_id.in_(normalized_root_ids),
                ChatMessage.deleted_at.is_(None),
            )
            .order_by(
                ChatMessage.thread_root_message_id.asc(),
                ChatMessage.conversation_seq.desc(),
            )
        )

        previews: dict[int, list[str]] = {}
        seen_by_root: dict[int, set[str]] = {}
        for root_message_id, sender_user_id in self._session.execute(stmt).all():
            if root_message_id is None or sender_user_id is None:
                continue

            normalized_root_message_id = int(root_message_id)
            normalized_user_id = str(sender_user_id)
            if len(previews.get(normalized_root_message_id, [])) >= limit_per_thread:
                continue

            seen = seen_by_root.setdefault(normalized_root_message_id, set())
            if normalized_user_id in seen:
                continue

            seen.add(normalized_user_id)
            previews.setdefault(normalized_root_message_id, []).append(normalized_user_id)

        return previews

    def get_last_message(self, conversation_id: str) -> ChatMessage | None:
        stmt = (
            select(ChatMessage)
            .where(
                ChatMessage.conversation_id == conversation_id,
                ChatMessage.deleted_at.is_(None),
            )
            .order_by(ChatMessage.conversation_seq.desc())
            .limit(1)
        )
        return self._session.scalars(stmt).first()

    def search_conversation_messages(
        self,
        conversation: ChatConversation,
        *,
        query: str,
        limit: int = 20,
    ) -> Sequence[ChatMessage]:
        normalized_query = (query or "").strip()
        if not normalized_query:
            return []

        stmt = (
            select(ChatMessage)
            .where(
                ChatMessage.conversation_id == conversation.id,
                ChatMessage.deleted_at.is_(None),
                ChatMessage.body.ilike(f"%{normalized_query}%"),
            )
            .order_by(ChatMessage.conversation_seq.desc())
            .limit(self._normalized_limit(limit))
        )
        return self._session.scalars(stmt).all()

    def create_message(
        self,
        *,
        conversation_id: str,
        sender_user_id: str | None,
        sender_kind: str,
        body: str,
        body_format: str = "markdown",
        client_generated_id: str | None = None,
        attachments: list | None = None,
        metadata: dict | None = None,
        thread_root_message_id: int | None = None,
        reply_to_message_id: int | None = None,
    ) -> ChatMessage:
        now = datetime.now(timezone.utc)
        conversation = self._conversation_for_update(conversation_id)
        sender_user_id = str(sender_user_id) if sender_user_id is not None else None
        self._validate_sender_membership(
            conversation_id=conversation_id,
            sender_user_id=sender_user_id,
        )
        root_message = self._resolve_thread_root(
            conversation=conversation,
            thread_root_message_id=thread_root_message_id,
        )
        normalized_reply_to_message_id = self._normalize_reply_target_id(
            conversation_id=conversation_id,
            root_message=root_message,
            reply_to_message_id=reply_to_message_id,
        )

        next_seq = (conversation.last_message_seq or 0) + 1
        conversation.last_message_seq = next_seq
        conversation.updated_at = now

        message = ChatMessage(
            conversation_id=conversation_id,
            sender_user_id=sender_user_id,
            sender_kind=sender_kind,
            body=body,
            body_format=body_format,
            client_generated_id=client_generated_id,
            thread_root_message_id=root_message.id if root_message is not None else None,
            reply_to_message_id=normalized_reply_to_message_id,
            attachments=attachments or [],
            metadata_=metadata,
            conversation_seq=next_seq,
        )
        self._session.add(message)
        self._session.flush()

        if root_message is not None:
            root_message.reply_count = (root_message.reply_count or 0) + 1
            root_message.last_reply_at = now
            root_message.last_reply_message_id = message.id

        if sender_user_id is not None:
            ChatConversationReadRepository(self._session).upsert_cursor(
                conversation_id=conversation_id,
                user_id=sender_user_id,
                last_read_conversation_seq=message.conversation_seq,
                last_read_message_id=message.id,
            )

        return message


class ChatMessageMentionRepository(BaseRepository[ChatMessageMention]):
    model = ChatMessageMention

    def mark_seen_through_conversation_seq(
        self,
        *,
        user_id: str,
        conversation_id: str,
        last_read_conversation_seq: int,
    ) -> int:
        now = datetime.now(timezone.utc)
        mentions = self._session.scalars(
            select(ChatMessageMention)
            .join(ChatMessage, ChatMessage.id == ChatMessageMention.message_id)
            .where(
                ChatMessageMention.mentioned_user_id == user_id,
                ChatMessageMention.seen_at.is_(None),
                ChatMessage.conversation_id == conversation_id,
                ChatMessage.conversation_seq <= last_read_conversation_seq,
            )
        ).all()
        for mention in mentions:
            mention.seen_at = now
        return len(mentions)

    def mark_seen_for_thread(
        self,
        *,
        user_id: str,
        conversation_id: str,
        thread_root_message_id: int,
    ) -> int:
        now = datetime.now(timezone.utc)
        mentions = self._session.scalars(
            select(ChatMessageMention)
            .join(ChatMessage, ChatMessage.id == ChatMessageMention.message_id)
            .where(
                ChatMessageMention.mentioned_user_id == user_id,
                ChatMessageMention.seen_at.is_(None),
                ChatMessage.conversation_id == conversation_id,
                case(
                    (
                        ChatMessage.thread_root_message_id.is_not(None),
                        ChatMessage.thread_root_message_id,
                    ),
                    else_=ChatMessage.id,
                )
                == thread_root_message_id,
            )
        ).all()
        for mention in mentions:
            mention.seen_at = now
        return len(mentions)


class ChatNotificationRepository(BaseRepository[ChatNotification]):
    model = ChatNotification

    def list_for_user(self, user_id: str, *, limit: int = 50) -> Sequence[ChatNotification]:
        stmt = (
            select(ChatNotification)
            .where(ChatNotification.user_id == user_id)
            .order_by(ChatNotification.created_at.desc())
            .limit(limit)
        )
        return self._session.scalars(stmt).all()

    def mark_read(self, notification_id: int, user_id: str) -> ChatNotification | None:
        stmt = select(ChatNotification).where(
            ChatNotification.id == notification_id,
            ChatNotification.user_id == user_id,
        )
        notification = self._session.scalars(stmt).first()
        if notification is None:
            return None
        if notification.read_at is None:
            notification.read_at = datetime.now(timezone.utc)
        return notification

    def mark_all_read(self, user_id: str) -> int:
        now = datetime.now(timezone.utc)
        notifications = self._session.scalars(
            select(ChatNotification).where(
                ChatNotification.user_id == user_id,
                ChatNotification.read_at.is_(None),
            )
        ).all()
        for notification in notifications:
            notification.read_at = now
        return len(notifications)

    def mark_read_through_conversation_seq(
        self,
        *,
        user_id: str,
        conversation_id: str,
        last_read_conversation_seq: int,
    ) -> int:
        now = datetime.now(timezone.utc)
        notifications = self._session.scalars(
            select(ChatNotification)
            .join(ChatMessage, ChatMessage.id == ChatNotification.message_id)
            .where(
                ChatNotification.user_id == user_id,
                ChatNotification.read_at.is_(None),
                ChatNotification.conversation_id == conversation_id,
                ChatMessage.conversation_seq <= last_read_conversation_seq,
            )
        ).all()
        for notification in notifications:
            notification.read_at = now
        return len(notifications)

    def mark_read_for_thread(
        self,
        *,
        user_id: str,
        conversation_id: str,
        thread_root_message_id: int,
    ) -> int:
        now = datetime.now(timezone.utc)
        notifications = self._session.scalars(
            select(ChatNotification)
            .join(ChatMessage, ChatMessage.id == ChatNotification.message_id)
            .where(
                ChatNotification.user_id == user_id,
                ChatNotification.read_at.is_(None),
                ChatNotification.conversation_id == conversation_id,
                case(
                    (
                        ChatMessage.thread_root_message_id.is_not(None),
                        ChatMessage.thread_root_message_id,
                    ),
                    else_=ChatMessage.id,
                )
                == thread_root_message_id,
            )
        ).all()
        for notification in notifications:
            notification.read_at = now
        return len(notifications)

    def mark_read_for_conversation(
        self,
        *,
        user_id: str,
        conversation_id: str,
    ) -> int:
        now = datetime.now(timezone.utc)
        notifications = self._session.scalars(
            select(ChatNotification).where(
                ChatNotification.user_id == user_id,
                ChatNotification.read_at.is_(None),
                ChatNotification.conversation_id == conversation_id,
            )
        ).all()
        for notification in notifications:
            notification.read_at = now
        return len(notifications)


class ChatConversationReadRepository(BaseRepository[ChatConversationRead]):
    model = ChatConversationRead

    def unread_counts_for_users(
        self,
        *,
        org_id: str,
        user_ids: Sequence[str],
    ) -> dict[str, tuple[int, int]]:
        normalized_ids = sorted({str(user_id) for user_id in user_ids if str(user_id)})
        if not normalized_ids:
            return {}

        read_seq = func.coalesce(ChatConversationRead.last_read_conversation_seq, 0)
        unread_delta = ChatConversation.last_message_seq - read_seq
        unread_count = case((unread_delta > 0, unread_delta), else_=0)
        stmt = (
            select(
                ChatConversationMember.user_id,
                func.coalesce(
                    func.sum(
                        case((ChatConversation.type == CHAT_CONVERSATION_ROOM, unread_count), else_=0)
                    ),
                    0,
                ).label("room"),
                func.coalesce(
                    func.sum(
                        case((ChatConversation.type == CHAT_CONVERSATION_DM, unread_count), else_=0)
                    ),
                    0,
                ).label("dms"),
            )
            .select_from(ChatConversationMember)
            .join(
                ChatConversation,
                ChatConversation.id == ChatConversationMember.conversation_id,
            )
            .outerjoin(
                ChatConversationRead,
                and_(
                    ChatConversationRead.conversation_id == ChatConversationMember.conversation_id,
                    ChatConversationRead.user_id == ChatConversationMember.user_id,
                ),
            )
            .where(
                ChatConversation.org_id == org_id,
                ChatConversation.is_archived.is_(False),
                ChatConversationMember.user_id.in_(normalized_ids),
            )
            .group_by(ChatConversationMember.user_id)
        )
        counts = {user_id: (0, 0) for user_id in normalized_ids}
        for row in self._session.execute(stmt).all():
            counts[str(row.user_id)] = (int(row.room or 0), int(row.dms or 0))
        return counts

    def _normalized_cursor(
        self,
        *,
        conversation_id: str,
        last_read_conversation_seq: int,
        last_read_message_id: int | None,
    ) -> tuple[int, int | None]:
        conversation = self._session.execute(
            select(ChatConversation)
            .where(ChatConversation.id == conversation_id)
            .with_for_update()
        ).scalar_one()
        normalized_seq = max(
            0,
            min(last_read_conversation_seq, conversation.last_message_seq or 0),
        )
        if normalized_seq == 0:
            return 0, None

        if last_read_message_id is not None:
            message = self._session.scalars(
                select(ChatMessage).where(ChatMessage.id == last_read_message_id)
            ).first()
            if (
                message is not None
                and message.deleted_at is None
                and message.conversation_id == conversation_id
                and message.conversation_seq <= normalized_seq
            ):
                return normalized_seq, message.id

        resolved_message_id = self._session.scalars(
            select(ChatMessage.id)
            .where(
                ChatMessage.conversation_id == conversation_id,
                ChatMessage.deleted_at.is_(None),
                ChatMessage.conversation_seq <= normalized_seq,
            )
            .order_by(ChatMessage.conversation_seq.desc())
            .limit(1)
        ).first()
        return normalized_seq, resolved_message_id

    def get_for_user(self, conversation_id: str, user_id: str) -> ChatConversationRead | None:
        stmt = select(ChatConversationRead).where(
            ChatConversationRead.conversation_id == conversation_id,
            ChatConversationRead.user_id == user_id,
        )
        return self._session.scalars(stmt).first()

    def upsert_cursor(
        self,
        *,
        conversation_id: str,
        user_id: str,
        last_read_conversation_seq: int,
        last_read_message_id: int | None = None,
    ) -> ChatConversationRead:
        now = datetime.now(timezone.utc)
        normalized_seq, normalized_message_id = self._normalized_cursor(
            conversation_id=conversation_id,
            last_read_conversation_seq=last_read_conversation_seq,
            last_read_message_id=last_read_message_id,
        )
        self._session.execute(
            insert(ChatConversationRead)
            .values(
                conversation_id=conversation_id,
                user_id=user_id,
                last_read_message_id=normalized_message_id,
                last_read_conversation_seq=normalized_seq,
                last_read_at=now,
            )
            .on_conflict_do_nothing(
                index_elements=[
                    ChatConversationRead.conversation_id,
                    ChatConversationRead.user_id,
                ]
            )
        )
        row = self._session.scalars(
            select(ChatConversationRead)
            .where(
                ChatConversationRead.conversation_id == conversation_id,
                ChatConversationRead.user_id == user_id,
            )
            .with_for_update()
        ).first()
        if row is None:
            raise LookupError("Chat conversation read cursor could not be created")

        current_seq = row.last_read_conversation_seq or 0
        if normalized_seq > current_seq:
            row.last_read_conversation_seq = normalized_seq
            row.last_read_message_id = normalized_message_id
        elif normalized_seq == current_seq and normalized_message_id is not None:
            row.last_read_message_id = normalized_message_id
        row.last_read_at = now
        return row
