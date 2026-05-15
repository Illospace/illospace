from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from brain.app.api.schemas.chat import ChatReadUpdate
from brain.app.api.services.chat import ChatService


class _FakeMessageRepo:
    def __init__(self, message):
        self.message = message

    async def a_get(self, message_id: int):
        if self.message is None or self.message.id != message_id:
            return None
        return self.message


def _chat_service_with_message(message):
    service = object.__new__(ChatService)
    service.message_repo = _FakeMessageRepo(message)
    return service


def _conversation():
    return SimpleNamespace(id="conversation-1", last_message_seq=50)


def _message(*, message_id: int, conversation_id: str = "conversation-1", seq: int = 1, deleted=False):
    return SimpleNamespace(
        id=message_id,
        conversation_id=conversation_id,
        conversation_seq=seq,
        deleted_at=object() if deleted else None,
    )


async def test_read_cursor_target_cannot_move_before_the_target_message():
    conversation = _conversation()

    for message_seq in range(1, 25):
        message = _message(message_id=1000 + message_seq, seq=message_seq)
        service = _chat_service_with_message(message)

        for requested_seq in (None, message_seq, message_seq + 1, message_seq + 10):
            resolved_seq, resolved_message_id = await service._resolve_read_target_or_400(
                conversation=conversation,
                body=ChatReadUpdate(
                    last_read_message_id=message.id,
                    last_read_conversation_seq=requested_seq,
                ),
            )
            assert resolved_seq == (requested_seq if requested_seq is not None else message_seq)
            assert resolved_message_id == message.id

        for requested_seq in range(message_seq):
            with pytest.raises(HTTPException) as exc_info:
                await service._resolve_read_target_or_400(
                    conversation=conversation,
                    body=ChatReadUpdate(
                        last_read_message_id=message.id,
                        last_read_conversation_seq=requested_seq,
                    ),
                )
            assert exc_info.value.status_code == 400
            assert exc_info.value.detail == "Read sequence cannot be before the target message"


async def test_read_cursor_rejects_missing_deleted_or_cross_conversation_targets():
    conversation = _conversation()
    invalid_targets = [
        None,
        _message(message_id=10, conversation_id="other-conversation", seq=1),
        _message(message_id=10, seq=1, deleted=True),
    ]

    for target in invalid_targets:
        service = _chat_service_with_message(target)
        with pytest.raises(HTTPException) as exc_info:
            await service._resolve_read_target_or_400(
                conversation=conversation,
                body=ChatReadUpdate(last_read_message_id=10),
            )
        assert exc_info.value.status_code == 400
        assert exc_info.value.detail == "Read target message not found in conversation"


async def test_read_cursor_defaults_to_the_latest_visible_conversation_position():
    for last_message_seq in range(0, 30):
        conversation = SimpleNamespace(
            id="conversation-1",
            last_message_seq=last_message_seq,
        )
        service = _chat_service_with_message(None)

        assert await service._resolve_read_target_or_400(
            conversation=conversation,
            body=ChatReadUpdate(),
        ) == (last_message_seq, None)

        assert await service._resolve_read_target_or_400(
            conversation=conversation,
            body=ChatReadUpdate(last_read_conversation_seq=last_message_seq),
        ) == (last_message_seq, None)
