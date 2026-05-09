from types import SimpleNamespace
from unittest.mock import MagicMock, patch


def _uow_with_session(session):
    uow = MagicMock()
    uow.__enter__.return_value = uow
    uow.__exit__.return_value = False
    uow.session = session
    return uow


def test_runtime_ready_intro_reuses_existing_thread():
    from brain.app.api.routers.onboarding import start_runtime_ready_intro

    session = MagicMock()
    existing = SimpleNamespace(id="idea-existing")
    completed_run = SimpleNamespace(status="completed")
    session.scalars.return_value.first.side_effect = [existing, completed_run]

    with patch(
        "brain.app.api.routers.onboarding.UnitOfWork",
        return_value=_uow_with_session(session),
    ), patch(
        "brain.app.api.routers.onboarding.get_provider_auth_status",
        return_value={"runtime_key_available": True},
    ), patch("brain.app.api.routers.onboarding.route_trigger") as route_trigger:
        result = start_runtime_ready_intro(
            {"id": "user-1", "org_id": "org-1", "role": "owner", "name": "Alice"},
        )

    assert result == {
        "ok": True,
        "idea_id": "idea-existing",
        "created": False,
        "run_id": None,
    }
    route_trigger.assert_not_called()


def test_runtime_ready_intro_recovers_failed_existing_thread():
    from brain.app.api.routers.onboarding import start_runtime_ready_intro
    from brain.app.triggers.contracts import TriggerRouteResult

    session = MagicMock()
    existing = SimpleNamespace(id="idea-existing")
    failed_run = SimpleNamespace(status="failed")
    session.scalars.return_value.first.side_effect = [existing, failed_run]

    with patch(
        "brain.app.api.routers.onboarding.UnitOfWork",
        return_value=_uow_with_session(session),
    ), patch(
        "brain.app.api.routers.onboarding.get_provider_auth_status",
        return_value={"runtime_key_available": True},
    ), patch(
        "brain.app.api.routers.onboarding.route_trigger",
        return_value=TriggerRouteResult(ok=True, route="run", run_id=77),
    ) as route_trigger:
        result = start_runtime_ready_intro(
            {"id": "user-1", "org_id": "org-1", "role": "owner", "name": "Alice"},
        )

    assert result["created"] is False
    assert result["idea_id"] == "idea-existing"
    assert result["run_id"] == 77
    route_trigger.assert_called_once()


def test_runtime_ready_intro_creates_thread_and_run():
    from brain.app.api.routers.onboarding import INTRO_ORIGIN, INTRO_PROMPT, start_runtime_ready_intro
    from brain.app.triggers.contracts import TriggerRouteResult
    from brain.platform.db.models.idea import Idea

    session = MagicMock()
    session.scalars.return_value.first.return_value = None
    added = []

    def add(obj):
        added.append(obj)

    def flush():
        for obj in added:
            if isinstance(obj, Idea) and obj.id is None:
                obj.id = "idea-1"

    session.add.side_effect = add
    session.flush.side_effect = flush

    with patch(
        "brain.app.api.routers.onboarding.UnitOfWork",
        return_value=_uow_with_session(session),
    ), patch(
        "brain.app.api.routers.onboarding.get_provider_auth_status",
        return_value={"runtime_key_available": True},
    ), patch(
        "brain.app.api.routers.onboarding.route_trigger",
        return_value=TriggerRouteResult(ok=True, route="run", run_id=42),
    ) as route_trigger:
        result = start_runtime_ready_intro(
            {"id": "user-1", "org_id": "org-1", "role": "owner", "name": "Alice"},
        )

    idea = next(obj for obj in added if isinstance(obj, Idea))

    assert result["created"] is True
    assert result["idea_id"] == "idea-1"
    assert result["run_id"] == 42
    assert idea.origin == INTRO_ORIGIN
    assert idea.origin_ref == "runtime-ready-intro:user-1"
    assert idea.title == INTRO_PROMPT
    assert idea.display_title is None
    route_trigger.assert_called_once()
    trigger = route_trigger.call_args.args[0]
    assert trigger.payload["thread_message"] == INTRO_PROMPT
    assert trigger.payload["metadata"]["prompt_visibility"] == "hidden"
    assert trigger.payload["metadata"]["provider"] == "openai"
    assert trigger.payload["metadata"]["model"] == "openai/gpt-5.5"
    assert "thread_message_id" not in trigger.payload["metadata"]


def test_runtime_ready_intro_requires_openai_runtime():
    import pytest
    from fastapi import HTTPException

    from brain.app.api.routers.onboarding import start_runtime_ready_intro

    with patch(
        "brain.app.api.routers.onboarding.get_provider_auth_status",
        return_value={"runtime_key_available": False},
    ):
        with pytest.raises(HTTPException) as exc:
            start_runtime_ready_intro(
                {"id": "user-1", "org_id": "org-1", "role": "owner", "name": "Alice"},
            )

    assert exc.value.status_code == 409
    assert "OpenAI runtime is not connected" in exc.value.detail
