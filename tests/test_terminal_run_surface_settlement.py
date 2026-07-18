import uuid
from types import SimpleNamespace

import pytest


class _ScalarRows:
    def __init__(self, rows):
        self._rows = list(rows)

    def all(self):
        return list(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None


@pytest.mark.parametrize(
    ("run_status", "failure_category"),
    [
        ("failed", "upstream"),
        ("canceled", "internal"),
        ("expired", "verification"),
    ],
)
async def test_non_success_discussion_settlement_uses_typed_message_not_final_answer_artifact(
    monkeypatch,
    run_status,
    failure_category,
):
    from brain.platform.db.models.idea import IdeaThread, ThreadDiscussionComment
    from brain.systems.runs.cortex import runner
    from brain.systems.runs.failures import (
        CANCELED_RUN_MESSAGE,
        EXPIRED_RUN_MESSAGE,
        UPSTREAM_FAILED_RUN_MESSAGE,
    )

    raw_error = "database password=swordfish host=internal.example"
    expected_message = {
        "failed": UPSTREAM_FAILED_RUN_MESSAGE,
        "canceled": CANCELED_RUN_MESSAGE,
        "expired": EXPIRED_RUN_MESSAGE,
    }[run_status]
    discussion_trigger = {
        "thread_id": "idea-1",
        "comment_id": 7,
        "response_target": {
            "surface": "thread_discussion",
            "thread_id": "idea-1",
            "reply_to_comment_id": 7,
        },
    }
    run = SimpleNamespace(
        id=91,
        parent_run_id=None,
        thread_id="thread-discussion:idea-1",
        status=run_status,
        org_id="org-1",
        target_ref={
            "kind": "thread_discussion",
            "idea_id": "idea-1",
            "parent_thread_id": "idea-1",
            "discussion_trigger": discussion_trigger,
        },
        metadata_={
            "originating_surface": "thread_discussion",
            "failure": {"category": failure_category},
        },
    )
    raw_final_artifact = SimpleNamespace(
        id=201,
        run_id=91,
        artifact_type="final_answer",
        text=raw_error,
    )
    added = []
    published = []

    class FakeSession:
        def __init__(self):
            self.scalar_calls = 0

        async def get(self, _model, _key):
            return run

        async def scalars(self, _stmt):
            self.scalar_calls += 1
            if self.scalar_calls == 1:
                return _ScalarRows([])
            return _ScalarRows([raw_final_artifact])

        def add(self, obj):
            if isinstance(obj, ThreadDiscussionComment):
                obj.id = 8
            added.append(obj)

        async def flush(self):
            pass

    monkeypatch.setattr(
        runner,
        "publish_safe",
        lambda event, payload: published.append((event, payload)),
    )

    session = FakeSession()
    payload = await runner._settle_terminal_root_run_async(session, 91)

    assert payload == {
        "surface": "thread_discussion",
        "idea_id": "idea-1",
        "run_id": 91,
        "comment_id": 8,
    }
    assert session.scalar_calls == 1
    assert not any(isinstance(obj, IdeaThread) for obj in added)
    comment = next(obj for obj in added if isinstance(obj, ThreadDiscussionComment))
    assert comment.body == expected_message
    assert comment.attachments == []
    assert comment.metadata_ == {
        "source": "agent_run_final_answer",
        "surface": "thread_discussion",
        "created_by_run_id": 91,
        "artifact_id": None,
        "reply_to_comment_id": 7,
    }
    assert published[0][1]["comment"]["body"] == expected_message
    assert raw_error not in str(added)
    assert raw_error not in str(published)


@pytest.mark.parametrize(
    ("run_status", "failure_category"),
    [
        ("failed", "verification"),
        ("canceled", "upstream"),
        ("expired", "internal"),
    ],
)
async def test_non_success_timeline_settlement_uses_typed_message_and_keeps_run_attachments(
    run_status,
    failure_category,
):
    from brain.platform.db.models.agent_run import AgentRunArtifactRow, AgentRunRow
    from brain.platform.db.models.idea import (
        Idea,
        IdeaStateLog,
        IdeaThread,
        ThreadDiscussionComment,
    )
    from brain.systems.runs.cortex import runner
    from brain.systems.runs.failures import (
        CANCELED_RUN_MESSAGE,
        EXPIRED_RUN_MESSAGE,
        VERIFICATION_FAILED_RUN_MESSAGE,
    )

    idea_id = str(uuid.uuid4())
    raw_error = "verification rejected token=private-diagnostic"
    expected_message = {
        "failed": VERIFICATION_FAILED_RUN_MESSAGE,
        "canceled": CANCELED_RUN_MESSAGE,
        "expired": EXPIRED_RUN_MESSAGE,
    }[run_status]
    run = SimpleNamespace(
        id=92,
        parent_run_id=None,
        thread_id=idea_id,
        status=run_status,
        target_ref={"originating_surface": "ai_timeline"},
        metadata_={"failure": {"category": failure_category}},
    )
    idea = SimpleNamespace(
        id=idea_id,
        status="working",
        org_id=None,
        user_id="owner-1",
        agent_details=None,
        updated_at=None,
    )
    raw_final_artifact = SimpleNamespace(
        id=202,
        run_id=92,
        artifact_type="final_answer",
        text=raw_error,
    )
    attachment = {
        "url": f"/static/uploads/thread-assets/{idea_id}/evidence.png",
        "kind": "image",
        "content_type": "image/png",
    }
    same_run_comment = SimpleNamespace(
        id=29,
        thread_id=idea_id,
        attachments=[attachment],
        metadata_={"created_by_run_id": 92},
    )
    artifact_queries = 0
    added = []

    class FakeSession:
        async def get(self, model, key):
            if model is AgentRunRow and int(key) == 92:
                return run
            if model is Idea and str(key) == idea_id:
                return idea
            return None

        async def scalars(self, stmt):
            nonlocal artifact_queries
            entity = stmt.column_descriptions[0].get("entity")
            if entity is AgentRunArtifactRow:
                artifact_queries += 1
                return _ScalarRows([raw_final_artifact])
            if entity is ThreadDiscussionComment:
                return _ScalarRows([same_run_comment])
            if entity is IdeaThread:
                return _ScalarRows([])
            raise AssertionError(f"Unexpected settlement query: {stmt}")

        def add(self, obj):
            added.append(obj)

        async def flush(self):
            pass

    payload = await runner._settle_idea_for_terminal_root_run_async(FakeSession(), 92)

    assert payload == {
        "idea_id": idea_id,
        "old_status": "working",
        "new_status": "failed",
        "run_id": 92,
    }
    assert artifact_queries == 0
    assert idea.status == "failed"
    assert any(isinstance(obj, IdeaStateLog) for obj in added)
    response = next(obj for obj in added if isinstance(obj, IdeaThread))
    assert response.content == expected_message
    assert response.attachments == [attachment]
    assert response.metadata_ == {
        "run_id": 92,
        "artifact_id": None,
        "source": "agent_run_final_answer",
    }
    assert raw_error not in str(added)
