from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


class _JoinedRow:
    def __init__(self, comment):
        self.comment = comment
        self.author_name = None
        self.author_color = None

    def __getitem__(self, index):
        if index == 0:
            return self.comment
        raise IndexError(index)


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


def _legacy_failed_comment(raw_diagnostic: str):
    return SimpleNamespace(
        id=1,
        thread_id="idea-1",
        org_id="org-1",
        author_user_id=None,
        author_kind="illo",
        body=raw_diagnostic,
        attachments=[],
        metadata_={"created_by_run_id": 7, "error": raw_diagnostic},
        created_at=datetime(2026, 7, 18, tzinfo=timezone.utc),
    )


@pytest.mark.asyncio
async def test_discussion_rest_projects_runs_from_the_discussion_conversation(monkeypatch):
    from brain.app.api.routers.cortex import _discussion
    from brain.systems.runs.failures import DEFAULT_FAILED_RUN_MESSAGE

    raw_diagnostic = "legacy traceback token=discussion-secret"
    comment = _legacy_failed_comment(raw_diagnostic)

    class Session:
        async def execute(self, _stmt):
            return _Rows([_JoinedRow(comment)])

    failure = {
        "status": "failed",
        "category": "internal",
        "message": DEFAULT_FAILED_RUN_MESSAGE,
    }
    lookup = AsyncMock(return_value={7: failure})
    monkeypatch.setattr(_discussion, "public_failures_for_run_ids", lookup)
    session = Session()

    payload = await _discussion._discussion_comment_payloads(
        session,
        thread_id="idea-1",
        limit=50,
        org_id="org-1",
    )

    lookup.assert_awaited_once_with(
        session,
        {7},
        thread_id="thread-discussion:idea-1",
        org_id="org-1",
    )
    assert payload[0]["body"] == DEFAULT_FAILED_RUN_MESSAGE
    assert raw_diagnostic not in json.dumps(payload)


@pytest.mark.asyncio
async def test_discussion_read_tool_projects_legacy_failed_comments(monkeypatch):
    from brain.platform.db.repositories import unit_of_work
    from brain.systems.runs.cortex import read_models
    from brain.systems.runs.execution_context import bind_agent_context
    from brain.systems.runs.failures import DEFAULT_FAILED_RUN_MESSAGE
    from brain.systems.runs.tool_catalog.handlers import chat

    raw_diagnostic = "legacy traceback token=tool-secret"
    comment = _legacy_failed_comment(raw_diagnostic)

    class Session:
        async def execute(self, _stmt):
            return _Rows([_JoinedRow(comment)])

    session = Session()

    class UnitOfWork:
        async def __aenter__(self):
            self.session = session
            return self

        async def __aexit__(self, *_exc):
            return None

    failure = {
        "status": "failed",
        "category": "internal",
        "message": DEFAULT_FAILED_RUN_MESSAGE,
    }
    lookup = AsyncMock(return_value={7: failure})
    monkeypatch.setattr(unit_of_work, "UnitOfWork", UnitOfWork)
    monkeypatch.setattr(read_models, "public_failures_for_run_ids", lookup)

    with bind_agent_context({"org_id": "org-1"}):
        payload = json.loads(await chat._handle_read_thread_discussion(thread_id="idea-1"))

    lookup.assert_awaited_once_with(
        session,
        {7},
        thread_id="thread-discussion:idea-1",
        org_id="org-1",
    )
    assert payload["comments"][0]["body"] == DEFAULT_FAILED_RUN_MESSAGE
    assert raw_diagnostic not in json.dumps(payload)
