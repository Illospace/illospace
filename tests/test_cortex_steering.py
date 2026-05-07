from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException


def test_thread_message_live_guidance_appends_run_steering(monkeypatch):
    from brain.app.api.routers.cortex import _idea_ops

    appended: dict[str, object] = {}

    class _Store:
        def __init__(self, session):
            self.session = session

        def append_steering(self, run_id, content, *, user_id=None, thread_message_id=None):
            appended.update({
                "run_id": run_id,
                "content": content,
                "user_id": user_id,
                "thread_message_id": thread_message_id,
            })
            return SimpleNamespace(id=88)

    class _Session:
        def get(self, model, run_id):
            return SimpleNamespace(id=run_id, thread_id="idea-1", status="running")

        def flush(self):
            appended["flushed"] = True

    monkeypatch.setattr(_idea_ops, "AgentRunStore", _Store)
    thread_msg = SimpleNamespace(id=9, metadata_={})

    event_id = _idea_ops._append_live_guidance_from_thread_message(
        session=_Session(),
        idea_id="idea-1",
        role="user",
        content="Don't fetch everything.",
        metadata={"fast_steer": True, "target_run_id": 42},
        thread_msg=thread_msg,
        user_id="user-1",
    )

    assert event_id == 88
    assert appended == {
        "run_id": 42,
        "content": "Don't fetch everything.",
        "user_id": "user-1",
        "thread_message_id": 9,
        "flushed": True,
    }
    assert thread_msg.metadata_["live_guidance"] is True
    assert thread_msg.metadata_["steering_event_id"] == 88


def test_thread_message_live_guidance_rejects_terminal_run():
    from brain.app.api.routers.cortex import _idea_ops

    class _Session:
        def get(self, model, run_id):
            return SimpleNamespace(id=run_id, thread_id="idea-1", status="completed")

    with pytest.raises(HTTPException) as exc:
        _idea_ops._append_live_guidance_from_thread_message(
            session=_Session(),
            idea_id="idea-1",
            role="user",
            content="Still there?",
            metadata={"live_guidance": True, "target_run_id": 42},
            thread_msg=SimpleNamespace(id=1, metadata_={}),
            user_id="user-1",
        )

    assert exc.value.status_code == 409
