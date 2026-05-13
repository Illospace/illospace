from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

pytestmark = pytest.mark.asyncio


class _RequestStub:
    def __init__(self, payload):
        self._payload = payload

    async def json(self):
        return self._payload


class _AsyncResult:
    def __init__(self, *, first=None, scalar_one=None):
        self._first = first
        self._scalar_one = scalar_one

    def first(self):
        return self._first

    def scalar_one_or_none(self):
        return self._scalar_one


class _AsyncSessionStub:
    def __init__(self, idea):
        self.idea = idea

    async def get(self, *_args, **_kwargs):
        return self.idea

    async def scalars(self, *_args, **_kwargs):
        return _AsyncResult(first=self.idea)

    async def execute(self, *_args, **_kwargs):
        return _AsyncResult(scalar_one={})

    def add(self, *_args, **_kwargs):
        return None

    async def flush(self):
        return None

    def get_bind(self):
        return SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))


def _uow_with_idea(idea):
    uow = MagicMock()
    uow.__enter__ = MagicMock(return_value=uow)
    uow.__exit__ = MagicMock(return_value=False)
    uow.__aenter__ = AsyncMock(return_value=uow)
    uow.__aexit__ = AsyncMock(return_value=False)
    uow.session = _AsyncSessionStub(idea)
    return uow


async def test_notify_idea_created_preserves_thread_message_for_skill_routing():
    from brain.app.api.routers.cortex._ideas import notify_illo

    idea = SimpleNamespace(
        title="/hello4",
        attachments=[],
        description=None,
        status="queued",
        org_id="org-1",
    )
    request = _RequestStub(
        {
            "event": "idea_created",
            "idea_id": "idea-123",
            "thread_message": "/hello4 hello",
        }
    )
    user = {"id": "user-1", "org_id": "org-1", "role": "member"}

    route_result = MagicMock()
    route_result.to_response.return_value = {"ok": True}
    uow = _uow_with_idea(idea)
    with patch("brain.app.api.routers.cortex._ideas.UnitOfWork", return_value=uow), \
         patch("brain.app.triggers.router.async_route_trigger", AsyncMock(return_value=route_result)) as mock_route:
        result = await notify_illo(request=request, user=user)

    assert result["ok"] is True
    trigger = mock_route.call_args.args[0]
    msg = trigger.payload["run_message"]
    assert msg == '[Idea: "/hello4" | idea-123]\n\n/hello4 hello'
    assert mock_route.call_args.kwargs["session"] is uow.session


async def test_notify_idea_created_keeps_generic_message_without_thread_message():
    from brain.app.api.routers.cortex._ideas import notify_illo

    idea = SimpleNamespace(
        title="Normal title",
        attachments=[],
        description="Context",
        status="queued",
        org_id="org-1",
    )
    request = _RequestStub(
        {
            "event": "idea_created",
            "idea_id": "idea-456",
        }
    )
    user = {"id": "user-1", "org_id": "org-1", "role": "member"}

    route_result = MagicMock()
    route_result.to_response.return_value = {"ok": True}
    with patch("brain.app.api.routers.cortex._ideas.UnitOfWork", return_value=_uow_with_idea(idea)), \
         patch("brain.app.triggers.router.async_route_trigger", AsyncMock(return_value=route_result)) as mock_route:
        result = await notify_illo(request=request, user=user)

    assert result["ok"] is True
    trigger = mock_route.call_args.args[0]
    msg = trigger.payload["run_message"]
    assert "New idea created." in msg
    assert "Context" in msg


async def test_notify_idea_created_preserves_execution_profile_metadata():
    from brain.app.api.routers.cortex._ideas import notify_illo

    idea = SimpleNamespace(
        title="Fast thought",
        attachments=[],
        description=None,
        status="queued",
        org_id="org-1",
    )
    request = _RequestStub(
        {
            "event": "idea_created",
            "idea_id": "idea-fast",
            "thread_message": "Please fix this quickly",
            "metadata": {"execution_profile": "fast"},
        }
    )
    user = {"id": "user-1", "org_id": "org-1", "role": "member"}

    route_result = MagicMock()
    route_result.to_response.return_value = {"ok": True}
    with patch("brain.app.api.routers.cortex._ideas.UnitOfWork", return_value=_uow_with_idea(idea)), \
         patch("brain.app.triggers.router.async_route_trigger", AsyncMock(return_value=route_result)) as mock_route:
        result = await notify_illo(request=request, user=user)

    assert result["ok"] is True
    trigger = mock_route.call_args.args[0]
    assert trigger.payload["metadata"]["execution_profile"] == "fast"


async def test_notify_idea_created_to_team_member_does_not_enqueue_agent_run():
    from brain.app.api.routers.cortex._ideas import notify_illo

    idea = SimpleNamespace(
        title="Team thought",
        attachments=[],
        description=None,
        status="queued",
        org_id="org-1",
    )
    request = _RequestStub(
        {
            "event": "idea_created",
            "idea_id": "idea-team",
            "thread_message": "@Riley can you take a look?",
            "metadata": {"execution_profile": "fast"},
        }
    )
    user = {"id": "user-1", "org_id": "org-1", "role": "member"}
    uow = _uow_with_idea(idea)

    with patch("brain.app.api.routers.cortex._ideas.UnitOfWork", return_value=uow), \
         patch("brain.app.triggers.router.async_route_trigger") as mock_route:
        result = await notify_illo(request=request, user=user)

    assert result == {
        "ok": True,
        "route": "none",
        "skipped_reason": "team_mention_without_illo",
    }
    assert idea.status == "active"
    mock_route.assert_not_called()


async def test_notify_idea_created_to_team_member_from_thread_metadata_does_not_enqueue_agent_run():
    from brain.app.api.routers.cortex._ideas import notify_illo

    idea = SimpleNamespace(
        title="Team thought",
        attachments=[],
        description=None,
        status="queued",
        org_id="org-1",
    )
    request = _RequestStub(
        {
            "event": "idea_created",
            "idea_id": "idea-team",
            "metadata": {"execution_profile": "fast"},
        }
    )
    user = {"id": "user-1", "org_id": "org-1", "role": "member"}
    uow = _uow_with_idea(idea)

    with patch("brain.app.api.routers.cortex._ideas.UnitOfWork", return_value=uow), \
         patch(
             "brain.app.api.routers.cortex._ideas._latest_user_thread_metadata",
             AsyncMock(return_value={"thread_message": "@Riley can you take a look?"}),
         ), \
         patch("brain.app.triggers.router.async_route_trigger") as mock_route:
        result = await notify_illo(request=request, user=user)

    assert result == {
        "ok": True,
        "route": "none",
        "skipped_reason": "team_mention_without_illo",
    }
    assert idea.status == "active"
    mock_route.assert_not_called()


async def test_notify_idea_created_with_illo_and_teammate_enqueues_agent_run():
    from brain.app.api.routers.cortex._ideas import notify_illo

    idea = SimpleNamespace(
        title="Ask Illo",
        attachments=[],
        description=None,
        status="queued",
        org_id="org-1",
    )
    request = _RequestStub(
        {
            "event": "idea_created",
            "idea_id": "idea-illo",
            "thread_message": "@illo can you summarize what @Riley said?",
            "metadata": {"execution_profile": "fast"},
        }
    )
    user = {"id": "user-1", "org_id": "org-1", "role": "member"}
    route_result = MagicMock()
    route_result.to_response.return_value = {"ok": True}

    with patch("brain.app.api.routers.cortex._ideas.UnitOfWork", return_value=_uow_with_idea(idea)), \
         patch("brain.app.triggers.router.async_route_trigger", AsyncMock(return_value=route_result)) as mock_route:
        result = await notify_illo(request=request, user=user)

    assert result["ok"] is True
    mock_route.assert_called_once()


async def test_notify_thread_reply_preserves_execution_profile_metadata():
    from brain.app.api.routers.cortex._ideas import notify_illo

    idea = SimpleNamespace(
        title="Deep thought",
        attachments=[],
        description=None,
        status="active",
        org_id="org-1",
    )
    request = _RequestStub(
        {
            "event": "thread_reply",
            "idea_id": "idea-deep",
            "thread_message": "Take it deeper",
            "metadata": {"execution_profile": "deep"},
        }
    )
    user = {"id": "user-1", "org_id": "org-1", "role": "member"}

    route_result = MagicMock()
    route_result.to_response.return_value = {"ok": True}
    uow = _uow_with_idea(idea)
    with patch("brain.app.api.routers.cortex._ideas.UnitOfWork", return_value=uow), \
         patch("brain.app.triggers.router.async_route_trigger", AsyncMock(return_value=route_result)) as mock_route:
        result = await notify_illo(request=request, user=user)

    assert result["ok"] is True
    trigger = mock_route.call_args.args[0]
    assert trigger.payload["metadata"]["execution_profile"] == "deep"
    assert mock_route.call_args.kwargs["session"] is uow.session


async def test_notify_thread_reply_to_team_member_does_not_enqueue_agent_run():
    from brain.app.api.routers.cortex._ideas import notify_illo

    request = _RequestStub(
        {
            "event": "thread_reply",
            "idea_id": "idea-team",
            "thread_message": "@Riley je viens de voir, le voici",
            "metadata": {"execution_profile": "fast"},
        }
    )
    user = {"id": "user-1", "org_id": "org-1", "role": "member"}

    with patch("brain.app.triggers.router.async_route_trigger") as mock_route:
        result = await notify_illo(request=request, user=user)

    assert result == {
        "ok": True,
        "route": "none",
        "skipped_reason": "team_mention_without_illo",
    }
    mock_route.assert_not_called()


async def test_notify_thread_reply_with_illo_mention_still_enqueues_agent_run():
    from brain.app.api.routers.cortex._ideas import notify_illo

    idea = SimpleNamespace(
        title="Ask Illo",
        attachments=[],
        description=None,
        status="active",
        org_id="org-1",
    )
    request = _RequestStub(
        {
            "event": "thread_reply",
            "idea_id": "idea-illo",
            "thread_message": "@illo can you summarize what @Riley said?",
            "metadata": {"execution_profile": "fast"},
        }
    )
    user = {"id": "user-1", "org_id": "org-1", "role": "member"}

    route_result = MagicMock()
    route_result.to_response.return_value = {"ok": True}
    with patch("brain.app.api.routers.cortex._ideas.UnitOfWork", return_value=_uow_with_idea(idea)), \
         patch("brain.app.triggers.router.async_route_trigger", AsyncMock(return_value=route_result)) as mock_route:
        result = await notify_illo(request=request, user=user)

    assert result["ok"] is True
    mock_route.assert_called_once()


async def test_notify_thread_reply_rejects_cross_org_idea_before_enqueue():
    from brain.app.api.routers.cortex._ideas import notify_illo

    uow = _uow_with_idea(None)
    request = _RequestStub(
        {
            "event": "thread_reply",
            "idea_id": "idea-cross-org",
            "thread_message": "Should not run",
        }
    )
    user = {"id": "user-1", "org_id": "org-1", "role": "member"}

    with patch("brain.app.api.routers.cortex._ideas.UnitOfWork", return_value=uow), \
         patch("brain.systems.runs.cortex.admit_run") as mock_admit, \
         pytest.raises(HTTPException) as exc_info:
        await notify_illo(request=request, user=user)

    assert exc_info.value.status_code == 404
    mock_admit.assert_not_called()
