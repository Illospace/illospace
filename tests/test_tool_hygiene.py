"""Tool hygiene regression tests.

Covers a production trace audit's findings for the tool_catalog handlers:
- web_search degrades gracefully when no search provider is configured
- manage_project accepts `search` as an alias for `query` instead of raising
- exec_command tolerates unexpected kwargs instead of raising TypeError
- "Path escapes workspace" errors state which root(s) are readable
- post_ai_timeline_message / browser wrap unexpected exceptions cleanly
"""

from __future__ import annotations

import json

import pytest


# ── (1) web_search degrades gracefully when unconfigured ────────────────


@pytest.mark.asyncio
async def test_web_search_handler_returns_clean_error_when_unconfigured(monkeypatch):
    from brain.app.web import WebResearchError
    from brain.systems.runs.tool_catalog.handlers.web import _handle_web_search

    async def _raise_unconfigured(*args, **kwargs):
        raise WebResearchError(
            "BRAVE_SEARCH_API_KEY is not configured; TAVILY_API_KEY is not configured"
        )

    monkeypatch.setattr(
        "brain.app.web.web_search",
        _raise_unconfigured,
    )

    result = await _handle_web_search("illo brain")
    assert result == {
        "error": "web_search is not configured (no search API key set)",
        "unavailable": True,
    }


@pytest.mark.asyncio
async def test_web_search_handler_wraps_other_errors_cleanly(monkeypatch):
    from brain.app.web import WebResearchError

    async def _raise_other(*args, **kwargs):
        raise WebResearchError("brave: timeout; tavily: timeout")

    monkeypatch.setattr("brain.app.web.web_search", _raise_other)

    from brain.systems.runs.tool_catalog.handlers.web import _handle_web_search

    result = await _handle_web_search("illo brain")
    assert "error" in result
    assert "timeout" in result["error"]
    assert "unavailable" not in result


@pytest.mark.asyncio
async def test_web_search_handler_does_not_raise_on_unexpected_exception(monkeypatch):
    async def _raise_runtime(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr("brain.app.web.web_search", _raise_runtime)

    from brain.systems.runs.tool_catalog.handlers.web import _handle_web_search

    result = await _handle_web_search("illo brain")
    assert "error" in result
    assert "boom" in result["error"]


# ── (2a) manage_project accepts `search` as an alias for `query` ────────


@pytest.mark.asyncio
async def test_manage_project_accepts_search_kwarg_without_raising():
    from brain.systems.runs.tool_catalog.handlers.projects import _handle_manage_project

    # No org context bound — expect the existing clean error path, not a TypeError.
    result = await _handle_manage_project(action="list", search="foo")
    payload = json.loads(result)
    assert "error" in payload


@pytest.mark.asyncio
async def test_manage_project_search_alias_does_not_override_explicit_query(monkeypatch):
    from brain.systems.runs.execution_context import bind_agent_context
    from brain.systems.runs.tool_catalog.handlers import projects

    captured_queries = []

    def _spy(_profile, query):
        captured_queries.append(query)
        return False  # exclude the dummy profile so _profile_read never runs

    monkeypatch.setattr(projects, "profile_matches_query", _spy)

    class FakeScalars:
        def all(self):
            return ["dummy-profile"]

    class FakeSession:
        async def scalars(self, _stmt):
            return FakeScalars()

    class FakeUow:
        session = FakeSession()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    monkeypatch.setattr(
        "brain.platform.db.repositories.unit_of_work.UnitOfWork",
        lambda: FakeUow(),
    )

    with bind_agent_context(org_id="org-1", user_id="user-1"):
        result = await projects._handle_manage_project(
            action="list", query="explicit", search="ignored"
        )
    payload = json.loads(result)
    assert payload == {"projects": []}
    assert captured_queries == ["explicit"]


# ── (2b) exec_command tolerates unexpected kwargs ────────────────────────


def test_exec_command_handler_ignores_unexpected_kwargs():
    from brain.systems.runs.tool_handlers import _get_tool_handlers

    handlers = _get_tool_handlers()
    result = handlers["exec_command"]("echo hello", search="unexpected")
    assert result["exit_code"] == 0
    assert "hello" in result["stdout"]


# ── (3) "Path escapes workspace" states the readable root(s) ────────────


def test_read_file_path_escape_message_states_readable_root(tmp_path):
    from brain.systems.runs.tool_catalog.handlers import files

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    result = files._handle_read_file("/etc/passwd", _workspace=str(workspace))
    assert "error" in result
    assert "Path escapes workspace" in result["error"]
    assert str(workspace) in result["error"]
    assert "Readable root" in result["error"]


def test_legacy_tools_resolve_path_message_states_readable_root(tmp_path):
    from brain.systems.tools.handlers import _resolve_path

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with pytest.raises(ValueError) as excinfo:
        _resolve_path("/etc/passwd", workspace_root=str(workspace))
    message = str(excinfo.value)
    assert "Path escapes workspace" in message
    assert str(workspace) in message
    assert "Readable root" in message


# ── (4) raw exceptions are wrapped into clean error payloads ─────────────


@pytest.mark.asyncio
async def test_post_ai_timeline_message_wraps_db_exception(monkeypatch):
    from brain.systems.runs.execution_context import bind_agent_context
    from brain.systems.runs.tool_catalog.handlers import chat

    def _boom():
        raise RuntimeError("connection refused (asyncpg.Error style failure)")

    class FakeUow:
        async def __aenter__(self):
            _boom()

        async def __aexit__(self, *_args):
            return False

    monkeypatch.setattr(
        "brain.platform.db.repositories.unit_of_work.UnitOfWork",
        lambda: FakeUow(),
    )

    with bind_agent_context(org_id="org-1", user_id="user-1", idea_id="idea-1"):
        result = await chat._handle_post_ai_timeline_message(
            body="hello", thread_id="idea-1"
        )
    payload = json.loads(result)
    assert "error" in payload
    assert "traceback" not in payload["error"].lower()
    assert payload["error"].startswith("tool failed:")


@pytest.mark.asyncio
async def test_browser_handler_wraps_unexpected_exception():
    from brain.systems.runs.execution_context import bind_agent_context
    from brain.systems.runs.tool_catalog.handlers import browser

    with bind_agent_context(idea_id=None):
        result = await browser._handle_browser(action="navigate", url="https://example.com")
    assert "error" in result
    assert result["error"].startswith("tool failed:")


@pytest.mark.asyncio
async def test_browser_handler_help_action_is_not_guarded_into_error():
    from brain.systems.runs.tool_catalog.handlers import browser

    result = await browser._handle_browser(action="help")
    assert "error" not in result
    assert result["tool"] == "browser"
