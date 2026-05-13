from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest


def _user(**overrides):
    user = {
        "id": "user-1",
        "name": "Alex",
        "email": "alex@example.com",
        "role": "owner",
        "org_id": "org-1",
        "org_name": "Example",
    }
    user.update(overrides)
    return user


def _idea(**overrides):
    idea = SimpleNamespace(
        title="Build triggers",
        description="Trigger contracts",
        attachments=[],
        status="queued",
        org_id="org-1",
    )
    for key, value in overrides.items():
        setattr(idea, key, value)
    return idea


def test_stable_idempotency_key_is_deterministic():
    from brain.app.triggers.contracts import stable_idempotency_key

    first = stable_idempotency_key(
        source="cortex",
        event_type="cortex.thread_reply",
        org_id="org-1",
        target={"idea_id": "idea-1"},
        payload={"thread_message": "hello"},
    )
    second = stable_idempotency_key(
        source="cortex",
        event_type="cortex.thread_reply",
        org_id="org-1",
        target={"idea_id": "idea-1"},
        payload={"thread_message": "hello"},
    )

    assert first == second
    assert len(first) == 40


def test_internal_adapter_builds_cortex_thread_trigger():
    from brain.app.triggers.adapters.internal import build_cortex_notify_trigger

    trigger = build_cortex_notify_trigger(
        event="thread_reply",
        idea_id="idea-1",
        idea=_idea(),
        user=_user(),
        thread_message="@illo ship it",
        effective_metadata={"target": {"repo": "illo-brain"}},
        priority=1,
    )

    assert trigger.source == "cortex"
    assert trigger.event_type == "cortex.thread_reply"
    assert trigger.org_id == "org-1"
    assert trigger.actor.id == "user-1"
    assert trigger.target["idea_id"] == "idea-1"
    assert trigger.payload["run_message"] == '[Idea: "Build triggers" | idea-1]\n\n@illo ship it'
    assert trigger.payload["metadata"]["target"]["repo"] == "illo-brain"
    assert trigger.policy["route"] == "run"


def test_trigger_rejects_actor_cross_org():
    from brain.app.triggers.adapters.internal import build_cortex_notify_trigger

    with pytest.raises(ValueError, match="actor org_id"):
        build_cortex_notify_trigger(
            event="thread_reply",
            idea_id="idea-1",
            idea=_idea(org_id="org-2"),
            user=_user(org_id="org-1"),
            thread_message="@illo nope",
        )


def test_router_sends_cortex_trigger_through_run_queue():
    from brain.app.triggers.adapters.internal import build_cortex_notify_trigger
    from brain.systems.runs.cortex import RunAdmissionResult
    from brain.app.triggers.router import route_trigger

    trigger = build_cortex_notify_trigger(
        event="idea_created",
        idea_id="idea-1",
        idea=_idea(),
        user=_user(),
        thread_message="/hello4 hello",
        metadata={"target": {"repo": "illo-brain"}},
        priority=1,
    )

    session = object()
    with patch(
        "brain.app.triggers.router.admit_run",
        return_value=RunAdmissionResult(ok=True, run_id=123),
    ) as mock_admit:
        result = route_trigger(trigger, session=session)

    assert result.ok is True
    assert result.route == "run"
    assert result.run_id == 123
    request = mock_admit.call_args.args[0]
    assert request.idea_id == "idea-1"
    assert request.event == "idea_created"
    assert request.message == '[Idea: "Build triggers" | idea-1]\n\n/hello4 hello'
    assert request.priority == 1
    assert request.user_id == "user-1"
    assert request.source == "trigger:cortex"
    assert request.producer == "trigger"
    assert request.idempotency_key == trigger.idempotency_key
    assert request.metadata["target"]["repo"] == "illo-brain"
    assert mock_admit.call_args.kwargs["session"] is session
    assert request.metadata["illo_trigger"]["event_type"] == "cortex.idea_created"
    assert request.metadata["illo_trigger"]["idempotency_key"] == trigger.idempotency_key


@pytest.mark.asyncio
async def test_async_router_sends_cortex_trigger_through_run_queue():
    from brain.app.triggers.adapters.internal import build_cortex_notify_trigger
    from brain.systems.runs.cortex import RunAdmissionResult
    from brain.app.triggers.router import async_route_trigger

    trigger = build_cortex_notify_trigger(
        event="idea_created",
        idea_id="idea-1",
        idea=_idea(),
        user=_user(),
        thread_message="/hello4 hello",
        metadata={"target": {"repo": "illo-brain"}},
        priority=1,
    )

    session = object()
    with patch(
        "brain.app.triggers.router.async_admit_run",
        AsyncMock(return_value=RunAdmissionResult(ok=True, run_id=123)),
    ) as mock_admit:
        result = await async_route_trigger(trigger, session=session)

    assert result.ok is True
    assert result.route == "run"
    assert result.run_id == 123
    request = mock_admit.call_args.args[0]
    assert request.idea_id == "idea-1"
    assert request.event == "idea_created"
    assert request.message == '[Idea: "Build triggers" | idea-1]\n\n/hello4 hello'
    assert request.priority == 1
    assert request.user_id == "user-1"
    assert request.source == "trigger:cortex"
    assert request.producer == "trigger"
    assert request.idempotency_key == trigger.idempotency_key
    assert request.metadata["target"]["repo"] == "illo-brain"
    assert mock_admit.call_args.kwargs["session"] is session
    assert request.metadata["illo_trigger"]["event_type"] == "cortex.idea_created"
    assert request.metadata["illo_trigger"]["idempotency_key"] == trigger.idempotency_key


def test_internal_adapter_builds_chat_thread_trigger():
    from brain.app.triggers.adapters.internal import build_chat_mention_trigger

    conversation = SimpleNamespace(id="conv-1", org_id="org-1", type="room")
    root = SimpleNamespace(id=10, body="Original topic")
    message = SimpleNamespace(
        id=11,
        body="@illo please turn this into a thought",
        thread_root_message_id=10,
        reply_to_message_id=None,
    )

    trigger = build_chat_mention_trigger(
        event="room_thread_mention",
        conversation=conversation,
        message=message,
        root_message=root,
        user=_user(),
    )

    assert trigger.source == "chat"
    assert trigger.event_type == "chat.room_thread_mention"
    assert trigger.target["kind"] == "chat_message"
    assert trigger.payload["metadata"]["chat_trigger"]["response_target"] == {
        "conversation_id": "conv-1",
        "thread_root_message_id": 10,
    }
    assert "post_chat_message" in trigger.payload["run_message"]


def test_router_sends_chat_trigger_through_run_queue():
    from brain.app.triggers.adapters.internal import build_chat_mention_trigger
    from brain.app.triggers.router import route_trigger

    conversation = SimpleNamespace(id="conv-1", org_id="org-1", type="room")
    message = SimpleNamespace(
        id=22,
        body="@illo summarize this",
        thread_root_message_id=None,
        reply_to_message_id=None,
    )
    trigger = build_chat_mention_trigger(
        event="room_message_mention",
        conversation=conversation,
        message=message,
        user=_user(),
    )
    with patch("brain.app.triggers.router.AgentRunStore") as store_cls:
        store_instance = store_cls.return_value
        store_instance.create_run.return_value = SimpleNamespace(id=456)
        result = route_trigger(trigger, session=object())

    assert result.ok is True
    assert result.route == "run"
    assert result.run_id == 456
    request = store_instance.create_run.call_args.args[0]
    assert request.thread_id == "chat:conv-1:22"
    assert request.user_id == "user-1"
    assert request.org_id == "org-1"
    assert request.target_ref["kind"] == "chat_message"
    assert request.target_ref["chat_trigger"]["message_id"] == 22
    assert request.metadata["illo_trigger"]["event_type"] == "chat.room_message_mention"


def test_router_reports_run_admission_failure():
    from brain.app.triggers.adapters.internal import build_cortex_notify_trigger
    from brain.systems.runs.cortex import RunAdmissionResult
    from brain.app.triggers.router import route_trigger

    trigger = build_cortex_notify_trigger(
        event="thread_reply",
        idea_id="idea-1",
        idea=_idea(),
        user=_user(),
        thread_message="@illo ship it",
    )

    with patch(
        "brain.app.triggers.router.admit_run",
        return_value=RunAdmissionResult(ok=False, skipped_reason="idea_running"),
    ):
        result = route_trigger(trigger)

    assert result.ok is False
    assert result.route == "run"
    assert result.skipped_reason == "idea_running"


def test_router_rejects_unregistered_native_route():
    from brain.app.api.authorization import human_identity
    from brain.app.triggers.contracts import IlloTrigger, stable_idempotency_key
    from brain.app.triggers.router import route_trigger

    target = {"kind": "memory_review", "memory_id": 7}
    trigger = IlloTrigger(
        source="memory",
        event_type="memory.review_due",
        actor=human_identity(_user()),
        org_id="org-1",
        target=target,
        payload={},
        idempotency_key=stable_idempotency_key(
            source="memory",
            event_type="memory.review_due",
            org_id="org-1",
            target=target,
        ),
        policy={"route": "scheduler"},
    )

    result = route_trigger(trigger)

    assert result.ok is False
    assert result.route == "unsupported"
    assert "memory.review_due" in result.skipped_reason
