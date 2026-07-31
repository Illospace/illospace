"""Failure-projection regressions for Cortex misc endpoints."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from brain.platform.db.models.agent_run import AgentRunEventRow, AgentRunRow
from brain.platform.db.models.idea import IdeaThread


class _Rows:
    def __init__(self, rows):
        self._rows = list(rows)

    def all(self):
        return list(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class _Session:
    def __init__(self, *, runs=(), messages=(), events=()):
        self._runs = list(runs)
        self._messages = list(messages)
        self._events = list(events)
        self.scalars_calls = 0
        self.add = MagicMock()

    async def scalars(self, statement):
        self.scalars_calls += 1
        entity = statement.column_descriptions[0].get("entity")
        if entity is AgentRunRow:
            return _Rows(self._runs)
        if entity is IdeaThread:
            return _Rows(self._messages)
        if entity is AgentRunEventRow:
            return _Rows(self._events)
        raise AssertionError(f"Unexpected scalar entity: {entity}")

    async def execute(self, _statement, _params=None):
        # Legacy audit/analyze queried only role/content with raw SQL.
        return _Rows(
            SimpleNamespace(role=message.role, content=message.content)
            for message in self._messages
        )


class _Uow:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


def _failed_run(*, run_id: int = 41):
    now = datetime.now(timezone.utc)
    return SimpleNamespace(
        id=run_id,
        thread_id="idea-1",
        status="failed",
        input_message="/audit inspect this idea",
        metadata_={
            "error": "DATABASE_PASSWORD=raw-secret",
            "failure": {"category": "upstream"},
        },
        started_at=now,
        completed_at=now,
        created_at=now,
    )


def _failed_message(*, run_id: int = 41):
    now = datetime.now(timezone.utc)
    return SimpleNamespace(
        id=7,
        idea_id="idea-1",
        role="illo",
        content="Traceback: DATABASE_PASSWORD=raw-secret",
        metadata_={"run_id": run_id, "error": "raw-secret"},
        attachments=[],
        created_at=now,
    )


@pytest.mark.asyncio
async def test_split_projects_legacy_failed_message_before_copying_it():
    from brain.app.api.routers.cortex import _misc

    run = _failed_run()
    message = _failed_message()
    session = _Session(runs=[run], messages=[message])
    parent = SimpleNamespace(
        id="idea-1",
        org_id="org-1",
        position_x=0,
        position_y=0,
        salience_score=5.0,
        active_agents=0,
    )
    request = SimpleNamespace(
        json=AsyncMock(
            return_value={
                "branches": [{"topic": "Safe branch", "message_indices": [0]}],
            }
        )
    )
    copied_commands = []
    cancel_open_runs = AsyncMock(return_value=0)

    async def capture_message(_session, *, idea, command, apply_lifecycle):
        copied_commands.append(command)
        return SimpleNamespace()

    with (
        patch.object(_misc, "UnitOfWork", return_value=_Uow(session)),
        patch.object(
            _misc,
            "_a_require_idea_for_user",
            AsyncMock(return_value=parent),
        ),
        patch.object(_misc, "post_thread_message", side_effect=capture_message),
        patch.object(_misc, "transition_thought_status", AsyncMock()),
        patch.object(
            _misc,
            "async_cancel_open_runs_for_thread",
            cancel_open_runs,
        ),
        patch("brain.systems.cortex.events.publish"),
    ):
        result = await _misc.split_idea(
            "idea-1",
            request,
            user={"id": "user-1", "org_id": "org-1"},
        )

    assert result["ok"] is True
    cancel_open_runs.assert_awaited_once_with(
        session,
        "idea-1",
        reason="Parent split into branches",
    )
    assert len(copied_commands) == 1
    copied = copied_commands[0]
    assert "raw-secret" not in copied.content
    assert copied.metadata == {
        "failure": {
            "status": "failed",
            "category": "upstream",
            "message": copied.content,
        }
    }


@pytest.mark.asyncio
async def test_audit_analyze_authorizes_and_projects_failed_thread_content():
    from brain.app.api.routers.cortex import _misc

    run = _failed_run()
    message = _failed_message()
    session = _Session(runs=[run], messages=[message])
    authorize = AsyncMock(return_value=SimpleNamespace(id="idea-1"))
    captured = {}

    async def capture_admission(_session, event):
        captured["event"] = event
        return SimpleNamespace(ok=True, run_id=99)

    with (
        patch.object(_misc, "UnitOfWork", return_value=_Uow(session)),
        patch.object(_misc, "_a_require_idea_for_user", authorize),
        patch.object(_misc, "admit_work", side_effect=capture_admission),
        patch.object(
            _misc,
            "async_summarize_runs_usage",
            AsyncMock(
                return_value=[
                    {
                        "id": run.id,
                        "tokens_total": 12_345,
                        "estimated_cost": 0.06789,
                    }
                ]
            ),
        ),
    ):
        result = await _misc.idea_audit_analyze(
            "idea-1",
            MagicMock(),
            user={"id": "user-1", "org_id": "org-1"},
        )

    assert result == {"ok": True, "run_id": 99}
    authorize.assert_awaited_once_with(
        session,
        "idea-1",
        {"id": "user-1", "org_id": "org-1"},
    )
    admitted_message = captured["event"].payload["message"]
    assert "raw-secret" not in admitted_message
    assert "temporary upstream problem" in admitted_message
    assert "Total tokens: 12,345" in admitted_message
    assert "Est cost: $0.0679" in admitted_message
    assert captured["event"].payload["metadata"] == {
        "run_profile": "fast",
        "recipe": "fast",
        "thinking_tier": "xhigh",
        "source": "audit",
    }


@pytest.mark.asyncio
async def test_audit_summary_authorizes_before_building_metrics():
    from brain.app.api.routers.cortex import _misc

    session = _Session(runs=[_failed_run()])
    denied = HTTPException(status_code=404, detail="Idea not found")
    build_summary = AsyncMock()

    with (
        patch.object(_misc, "UnitOfWork", return_value=_Uow(session)),
        patch.object(
            _misc,
            "_a_require_idea_for_user",
            AsyncMock(side_effect=denied),
        ),
        patch.object(_misc, "async_build_idea_audit_summary", build_summary),
        pytest.raises(HTTPException) as exc_info,
    ):
        await _misc.idea_audit(
            "idea-1",
            user={"id": "other-user", "org_id": "other-org"},
        )

    assert exc_info.value is denied
    build_summary.assert_not_awaited()


@pytest.mark.asyncio
async def test_audit_analyze_authorizes_before_querying_runs():
    from brain.app.api.routers.cortex import _misc

    session = _Session(runs=[_failed_run()], messages=[_failed_message()])
    denied = HTTPException(status_code=404, detail="Idea not found")

    with (
        patch.object(_misc, "UnitOfWork", return_value=_Uow(session)),
        patch.object(
            _misc,
            "_a_require_idea_for_user",
            AsyncMock(side_effect=denied),
        ),
        pytest.raises(HTTPException) as exc_info,
    ):
        await _misc.idea_audit_analyze(
            "idea-1",
            MagicMock(),
            user={"id": "other-user", "org_id": "other-org"},
        )

    assert exc_info.value is denied
    assert session.scalars_calls == 0


@pytest.mark.asyncio
async def test_audit_analysis_result_authorizes_before_querying_runs():
    from brain.app.api.routers.cortex import _misc

    session = _Session(runs=[_failed_run()], messages=[_failed_message()])
    denied = HTTPException(status_code=404, detail="Idea not found")

    with (
        patch.object(_misc, "UnitOfWork", return_value=_Uow(session)),
        patch.object(
            _misc,
            "_a_require_idea_for_user",
            AsyncMock(side_effect=denied),
        ),
        pytest.raises(HTTPException) as exc_info,
    ):
        await _misc.idea_audit_analysis_result(
            "idea-1",
            user={"id": "other-user", "org_id": "other-org"},
        )

    assert exc_info.value is denied
    assert session.scalars_calls == 0


@pytest.mark.asyncio
async def test_audit_analysis_result_returns_typed_failure_without_diagnostics():
    from brain.app.api.routers.cortex import _misc

    run = _failed_run()
    message = _failed_message()
    session = _Session(runs=[run], messages=[message])

    with (
        patch.object(_misc, "UnitOfWork", return_value=_Uow(session)),
        patch.object(
            _misc,
            "_a_require_idea_for_user",
            AsyncMock(return_value=SimpleNamespace(id="idea-1")),
        ),
    ):
        result = await _misc.idea_audit_analysis_result(
            "idea-1",
            user={"id": "user-1", "org_id": "org-1"},
        )

    serialized = json.dumps(result)
    assert "raw-secret" not in serialized
    assert "Traceback" not in serialized
    assert result["status"] == "failed"
    assert result["error"] == result["failure"]["message"]
    assert result["content"] == result["failure"]["message"]
    assert result["failure"] == {
        "status": "failed",
        "category": "upstream",
        "message": result["failure"]["message"],
    }


@pytest.mark.asyncio
async def test_audit_eval_returns_real_api_call_count_without_legacy_attempts_key():
    from brain.app.api.routers.cortex import _misc

    now = datetime.now(timezone.utc)
    run = SimpleNamespace(
        id=73,
        status="completed",
        input_message="Inspect the routing audit",
        metadata_={},
        completed_at=now,
        created_at=now,
    )
    artifact = SimpleNamespace(
        text="The routing audit is complete.",
        created_at=now,
    )

    class EvalSession:
        async def execute(self, _statement):
            return _Rows([(run, artifact)])

    request = SimpleNamespace(
        json=AsyncMock(
            return_value={
                "proposal": {
                    "type": "skill",
                    "description": "Improve routing guidance",
                    "recommendation": "Use the measured burn",
                }
            }
        )
    )

    with (
        patch.object(_misc, "UnitOfWork", return_value=_Uow(EvalSession())),
        patch.object(
            _misc,
            "async_summarize_runs_usage",
            AsyncMock(
                return_value=[
                    {
                        "id": 73,
                        "tokens_total": 2_500,
                        "api_calls": 4,
                    }
                ]
            ),
        ),
        patch(
            "brain.platform.integrations.completions.simple_text_completion",
            return_value=json.dumps(
                {
                    "score": 8,
                    "task_solved": "yes",
                    "output_better": "yes",
                    "less_waste": "yes",
                    "regression_risk": "low",
                    "reasoning": "Measured routing should improve the proposal.",
                }
            ),
        ),
        patch(
            "brain.platform.providers.model_policy.get_default_model",
            return_value="openai/gpt-5.4",
        ),
    ):
        result = await _misc.audit_eval(
            request,
            user={"id": "user-1", "org_id": "org-1"},
        )

    assert result["benchmarks"][0]["tokens"] == 2_500
    assert result["benchmarks"][0]["api_calls"] == 4
