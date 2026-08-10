"""Focused tests for the new AgentRun runtime recipes and projections."""

from __future__ import annotations

import asyncio
import inspect
import json
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from brain.systems.runs import slack_delivery


class _AwaitableValue:
    def __init__(self, value):
        self.value = value

    def __await__(self):
        async def _coro():
            return self.value

        return _coro().__await__()

    def __getattr__(self, name):
        return getattr(self.value, name)


class _ScalarRows:
    def __init__(self, rows):
        self.rows = rows

    def first(self):
        return self.rows[0] if self.rows else None

    def all(self):
        return list(self.rows)


def _stub_spawn_worker_auth_preflight(monkeypatch, worker_handlers):
    """Pin the spawn admission preflight to `passed`.

    Tests that exercise the store path must not depend on whether the machine
    running them happens to have a provider credential: without this, the probe
    falls back to ambient env keys, so the handler takes the happy path on a
    developer laptop and the auth_blocked path on credential-free CI.
    """
    from brain.platform.integrations.provider_auth_preflight import (
        ProviderAuthPassedPreflightResult,
    )

    async def passed_preflight(_session, *, model, **_kwargs):
        return ProviderAuthPassedPreflightResult(
            provider="openai",
            model=model,
        )

    monkeypatch.setattr(
        worker_handlers,
        "_preflight_spawn_worker_auth",
        passed_preflight,
    )


class _Store:
    def __init__(self):
        self.events = []
        self.artifacts = []
        self.metadata_updates = []
        self.created_requests = []
        self.child_initial_statuses = []
        self.runs = {}
        self.children_by_step = {}
        self.statuses = {}
        self.session = self

    def append_event(self, event):
        self.events.append(event)
        return _AwaitableValue(event)

    def append_artifact(self, artifact):
        self.artifacts.append(artifact)
        return _AwaitableValue(artifact)

    def update_metadata(self, run_id, patch):
        self.metadata_updates.append((run_id, dict(patch)))
        return _AwaitableValue(dict(patch))

    def create_run(self, request, *, initial_status=None):
        from brain.systems.runs.domain import AgentRun
        from brain.systems.runs.ids import trace_id_for_run_id
        from brain.systems.runs.status import RunStatus

        status = initial_status or RunStatus.QUEUED
        run_id = 100 + len(self.created_requests)
        run = AgentRun(
            id=run_id,
            trace_id=trace_id_for_run_id(run_id),
            thread_id=request.thread_id,
            input_message=request.message,
            profile=request.normalized_profile,
            recipe=request.normalized_recipe,
            status=status,
            org_id=request.org_id,
            user_id=request.user_id,
            parent_run_id=request.parent_run_id,
            root_run_id=request.root_run_id or run_id,
            target_ref=dict(request.target_ref or {}),
            workspace_ref=dict(request.workspace_ref or {}),
            model_policy=dict(request.model_policy or {}),
            metadata=dict(request.metadata or {}),
        )
        self.created_requests.append(request)
        self.runs[run_id] = run
        return _AwaitableValue(run)

    def create_child_run(
        self,
        parent,
        *,
        recipe,
        message,
        profile=None,
        step_key=None,
        target_ref=None,
        workspace_ref=None,
        model_policy=None,
        metadata=None,
        thread_id=None,
        initial_status=None,
    ):
        from brain.systems.runs.domain import AgentRunRequest
        from brain.systems.runs.events import run_event

        if step_key and step_key in self.children_by_step:
            return self.children_by_step[step_key]
        metadata_payload = dict(metadata or {})
        if step_key:
            metadata_payload["parent_step_key"] = step_key
        self.child_initial_statuses.append(initial_status)
        child = self.create_run(
            AgentRunRequest(
                org_id=parent.org_id,
                user_id=parent.user_id,
                thread_id=thread_id or parent.thread_id,
                parent_run_id=parent.id,
                root_run_id=parent.root_run_id or parent.id,
                profile=profile or parent.profile,
                recipe=recipe,
                message=message,
                target_ref=dict(target_ref if target_ref is not None else parent.target_ref or {}),
                workspace_ref=dict(workspace_ref if workspace_ref is not None else parent.workspace_ref or {}),
                model_policy=dict(model_policy if model_policy is not None else parent.model_policy or {}),
                metadata=metadata_payload,
            ),
            initial_status=initial_status,
        ).value
        if step_key:
            self.children_by_step[step_key] = child
        self.append_event(
            run_event(
                parent.id,
                "run.child_created",
                {"child_run_id": child.id, "recipe": child.recipe.value, "step_key": step_key},
                root_run_id=parent.root_run_id or parent.id,
            )
        )
        return _AwaitableValue(child)

    def list_artifacts(self, run_id):
        return [artifact for artifact in self.artifacts if artifact.run_id == run_id]

    def drain_steering(self, run_id):
        return _AwaitableValue([])

    def require_run(self, run_id):
        from brain.systems.runs.status import RunStatus

        return _AwaitableValue(self.runs.get(run_id) or SimpleNamespace(
            id=run_id,
            trace_id=f"run_{run_id}",
            org_id="org-1",
            user_id="user-1",
            thread_id="idea-1",
            root_run_id=run_id,
            recipe="worker",
            status=self.statuses.get(run_id, RunStatus.RUNNING),
        ))

    def to_domain(self, row):
        return row

    def set_status(self, run_id, status, reason=None):
        row = self.require_run(run_id).value
        row.status = status
        self.statuses[run_id] = status
        return _AwaitableValue(row)

class _Stream:
    def __init__(self):
        self.messages = []

    def publish(self, event_type, payload):
        self.messages.append((event_type, payload))


def _stream_has(messages, event_type, expected_subset):
    return any(
        actual_type == event_type and all(payload.get(key) == value for key, value in expected_subset.items())
        for actual_type, payload in messages
    )


def _artifact_type(artifact):
    return str(getattr(artifact.artifact_type, "value", artifact.artifact_type))


def _runtime(recipe: str = "fast", *, message: str = "Read the README", store=None, workspace_ref=None):
    from brain.systems.runs.context import RunContextLoader
    from brain.systems.runs.domain import AgentRun, AgentRunRequest, RunProfile, RunRecipe
    from brain.systems.runs.engine import RunRuntime
    from brain.systems.runs.status import RunStatus
    from brain.systems.runs.steering import SteeringInbox, SteeringMessage

    recipe_enum = {
        "fast": RunRecipe.FAST,
        "worker": RunRecipe.WORKER,
    }[recipe]
    profile = RunProfile.FAST
    run = AgentRun(
        id=42,
        trace_id="run_42",
        thread_id="idea-1",
        input_message=message,
        profile=profile,
        recipe=recipe_enum,
        status=RunStatus.RUNNING,
        root_run_id=42,
        user_id="user-1",
        org_id="org-1",
    )
    request = AgentRunRequest(
        thread_id="idea-1",
        message=message,
        user_id="user-1",
        org_id="org-1",
        profile=run.profile,
        recipe=run.recipe,
        workspace_ref=workspace_ref if workspace_ref is not None else {"workspace_root": "/tmp/work"},
        model_policy={"model": "openai/gpt-5.6-sol", "thinking": "high"},
        metadata={
            "worker_scope": {
                "objective": "Inspect README setup steps",
                "allowed_files": ["README.md"],
                "expected_artifacts": ["setup summary"],
                "risk_level": "low",
            }
        } if recipe == "worker" else {},
    )
    steering = SteeringInbox()
    steering.append(SteeringMessage(run_id=42, content="Focus on setup steps.", user_id="user-1"))
    return RunRuntime(
        run=run,
        request=request,
        store=store or _Store(),
        stream=_Stream(),
        steering=steering,
        context_loader=RunContextLoader(),
    )


async def test_run_admission_marks_idea_working_without_worker_details(monkeypatch):
    from brain.platform.db.models.idea import Idea, IdeaStateLog
    from brain.systems.runs.work_intake import WorkIntakeEvent, admit_work

    idea = SimpleNamespace(id="idea-1", status="idle", updated_at=None, active_agents=7)
    added = []

    class FakeStore:
        def __init__(self, _session):
            pass

        async def create_run(self, _request):
            return SimpleNamespace(id=123)

    class FakeSession:
        async def get(self, model, key):
            return idea if model is Idea and key == "idea-1" else None

        async def scalars(self, *_args, **_kwargs):
            return SimpleNamespace(first=lambda: None)

        def add(self, row):
            added.append(row)

        async def flush(self):
            return None

    async def empty_thread_context(*_args, **_kwargs):
        return {}

    monkeypatch.setattr("brain.systems.runs.work_intake.AsyncAgentRunStore", FakeStore)
    monkeypatch.setattr(
        "brain.systems.runs.work_intake.async_build_agent_visible_thread_context",
        empty_thread_context,
    )

    result = await admit_work(
        FakeSession(),
        WorkIntakeEvent(
            source="cortex",
            event_type="cortex.thread_reply",
            org_id="org-1",
            actor={"id": "user-1", "org_id": "org-1"},
            target={"kind": "cortex_idea", "idea_id": "idea-1"},
            payload={"message": "continue"},
        ),
    )

    assert result.ok is True
    assert result.run_id == 123
    assert idea.status == "working"
    assert idea.active_agents == 7
    assert isinstance(idea.updated_at, datetime)
    assert len(added) == 1
    assert isinstance(added[0], IdeaStateLog)
    assert added[0].from_state == "idle"
    assert added[0].to_state == "working"
    assert added[0].trigger == "agent_run_admitted"


async def test_run_admission_rejects_uncredentialed_anthropic_before_creation(monkeypatch):
    from brain.platform.integrations.provider_auth_preflight import (
        ProviderAuthBlockedPreflightResult,
    )
    from brain.systems.runs.domain import AgentRunRequest
    from brain.systems.runs import work_intake

    request = AgentRunRequest(
        org_id="org-1",
        user_id="user-1",
        thread_id="thread-1",
        message="run",
        model_policy={"model": "anthropic/claude-sonnet-5"},
    )
    create_calls = []

    async def build_request(*_args, **_kwargs):
        return request

    async def blocked_probe(_session, **kwargs):
        return ProviderAuthBlockedPreflightResult(
            provider="anthropic",
            model=kwargs["model"],
            credential="Anthropic API key",
            error_code="provider_credential_unavailable",
        )

    class FailStore:
        def __init__(self, _session):
            create_calls.append("constructed")

    monkeypatch.setattr(work_intake, "build_agent_run_request", build_request)
    monkeypatch.setattr(work_intake, "async_probe_provider_auth", blocked_probe)
    monkeypatch.setattr(work_intake, "AsyncAgentRunStore", FailStore)

    result = await work_intake.admit_work(
        object(),
        work_intake.WorkIntakeEvent(
            source="test",
            event_type="test.run",
            org_id="org-1",
            actor=None,
            target={"kind": "direct", "thread_id": "thread-1"},
        ),
    )

    assert result.ok is False
    assert result.run_id is None
    assert result.skipped_reason == (
        "provider_credential_unavailable: provider=anthropic "
        "model=anthropic/claude-sonnet-5 credential=Anthropic API key"
    )
    assert create_calls == []


async def test_run_admission_preserves_protected_idea_statuses(monkeypatch):
    from brain.platform.db.models.idea import Idea
    from brain.systems.runs.work_intake import WorkIntakeEvent, admit_work

    idea = SimpleNamespace(id="idea-1", status="archived", updated_at=None)
    added = []

    class FakeStore:
        def __init__(self, _session):
            pass

        async def create_run(self, _request):
            return SimpleNamespace(id=123)

    class FakeSession:
        async def get(self, model, key):
            return idea if model is Idea and key == "idea-1" else None

        async def scalars(self, *_args, **_kwargs):
            return SimpleNamespace(first=lambda: None)

        def add(self, row):
            added.append(row)

        async def flush(self):
            return None

    async def empty_thread_context(*_args, **_kwargs):
        return {}

    monkeypatch.setattr("brain.systems.runs.work_intake.AsyncAgentRunStore", FakeStore)
    monkeypatch.setattr(
        "brain.systems.runs.work_intake.async_build_agent_visible_thread_context",
        empty_thread_context,
    )

    result = await admit_work(
        FakeSession(),
        WorkIntakeEvent(
            source="cortex",
            event_type="cortex.thread_reply",
            org_id="org-1",
            actor={"id": "user-1", "org_id": "org-1"},
            target={"kind": "cortex_idea", "idea_id": "idea-1"},
            payload={"message": "continue"},
        ),
    )

    assert result.ok is True
    assert result.run_id == 123
    assert idea.status == "archived"
    assert idea.updated_at is None
    assert added == []


async def test_fast_recipe_invokes_direct_agent_with_streaming_and_live_guidance(monkeypatch):
    from brain.systems.runs.recipes.fast import FastRecipe

    captured = {}

    async def fake_invoke(spec):
        captured["spec"] = spec
        assert await spec.live_guidance_loader() == ["Focus on setup steps."]
        await spec.on_stream_activity("Reading files")
        assert (await spec.tool_handlers["read_file"](path="README.md"))["content"] == "README contents"
        await spec.on_stream_delta("README contents")
        return SimpleNamespace(output="README contents", success=True, error=None)

    monkeypatch.setattr("brain.systems.runs.recipes.fast.build_agent_tools", lambda role: [{"name": role}])
    monkeypatch.setattr(
        "brain.systems.runs.recipes.fast.build_tool_handlers",
        lambda **kwargs: {"read_file": lambda **tool_args: {"content": "README contents", "path": tool_args["path"]}},
    )
    monkeypatch.setattr("brain.systems.runs.recipes.fast.invoke_direct_agent_async", fake_invoke)

    runtime = _runtime("fast")
    result = await FastRecipe().execute(runtime)

    assert result.output == "README contents"
    assert result.status.value == "completed"
    assert captured["spec"].thinking == "high"
    assert captured["spec"].run_id == 42
    assert captured["spec"].idea_id is None
    assert captured["spec"].workspace_root == "/tmp/work"
    assert "interactive single-agent path" in captured["spec"].system_prompt
    assert "Triage first" in captured["spec"].system_prompt
    assert "Response Surface and Delegation" in captured["spec"].system_prompt
    assert "do not disappear into long work" in captured["spec"].system_prompt
    assert "spawn_worker" in captured["spec"].system_prompt
    assert "headless=true" in captured["spec"].system_prompt
    assert "write one brief task-specific assistant sentence" in captured["spec"].system_prompt
    assert "## Agent Contract" in captured["spec"].system_prompt
    assert captured["spec"].metadata["max_parallel_tool_calls"] == 4
    assert "Reply Integrity" in captured["spec"].system_prompt
    assert "When the user only confirms, corrects, asks yes/no" in captured["spec"].system_prompt
    assert "config snippets, caveats, or next steps" in captured["spec"].system_prompt
    assert _stream_has(runtime.stream.messages, "run.text_delta", {"delta": "README contents", "run_id": 42})
    assert any(event.event_type == "run.activity" and event.payload["label"] == "Reading files" for event in runtime.store.events)
    assert any(event.event_type == "run.tool_completed" and event.payload["tool_name"] == "read_file" for event in runtime.store.events)
    assert any(_artifact_type(artifact) == "file_observation" for artifact in runtime.store.artifacts)


async def test_fast_recipe_discards_partial_deltas_when_provider_result_fails(monkeypatch):
    from brain.systems.runs.failures import UPSTREAM_FAILED_RUN_MESSAGE
    from brain.systems.runs.recipes.fast import FastRecipe
    from brain.systems.runs.status import RunStatus

    raw_delta = "partial provider diagnostic token=fast-stream-secret"
    raw_error = "peer closed connection without sending complete message body"

    async def fake_invoke(spec):
        await spec.on_stream_delta(raw_delta)
        return SimpleNamespace(output=raw_delta, success=False, error=raw_error)

    monkeypatch.setattr("brain.systems.runs.recipes.fast.build_agent_tools", lambda _role: [])
    monkeypatch.setattr("brain.systems.runs.recipes.fast.build_tool_handlers", lambda **_kwargs: {})
    monkeypatch.setattr("brain.systems.runs.recipes.fast.invoke_direct_agent_async", fake_invoke)

    runtime = _runtime("fast")
    result = await FastRecipe().execute(runtime)
    streamed = json.dumps(runtime.stream.messages)

    assert result.status == RunStatus.FAILED
    assert result.error == raw_error
    assert result.final_output == UPSTREAM_FAILED_RUN_MESSAGE
    assert raw_delta not in streamed
    assert raw_error not in streamed


@pytest.mark.parametrize(
    ("replay_index", "origin"),
    [
        (1, "slack_teammate"),
        (2, "slack_channel_monitor"),
        (3, "slack_teammate"),
        (4, "slack_channel_monitor"),
    ],
)
async def test_fast_interactive_slack_transport_failure_returns_plain_language_fallback(
    monkeypatch,
    replay_index,
    origin,
):
    from brain.systems.runs.failures import (
        UPSTREAM_FAILED_RUN_MESSAGE,
        RunFailureCategory,
    )
    from brain.systems.runs.recipes.fast import FastRecipe

    raw_error = (
        "peer closed connection without sending complete message body "
        "(incomplete chunked read)"
    )
    calls = []

    async def fake_invoke(_spec):
        calls.append(replay_index)
        return SimpleNamespace(output="", success=False, error=raw_error)

    monkeypatch.setattr("brain.systems.runs.recipes.fast.build_agent_tools", lambda _role: [])
    monkeypatch.setattr("brain.systems.runs.recipes.fast.build_tool_handlers", lambda **_kwargs: {})
    monkeypatch.setattr("brain.systems.runs.recipes.fast.invoke_direct_agent_async", fake_invoke)

    runtime = _runtime("fast")
    runtime.request = replace(
        runtime.request,
        metadata={"origin": origin, "originating_surface": "slack"},
        target_ref={"kind": "slack_message", "originating_surface": "slack"},
    )

    result = await FastRecipe().execute(runtime)

    assert calls == [replay_index]
    assert result.status.value == "failed"
    assert result.error == raw_error
    assert result.failure_category == RunFailureCategory.UPSTREAM
    assert result.final_output == UPSTREAM_FAILED_RUN_MESSAGE
    assert result.output == ""
    assert raw_error not in result.final_output


async def test_fast_interactive_slack_logic_failure_is_not_rewritten_or_retried(monkeypatch):
    from brain.systems.runs.failures import DEFAULT_FAILED_RUN_MESSAGE, RunFailureCategory
    from brain.systems.runs.recipes.fast import FastRecipe

    logic_error = "Final reply policy rejected an invalid response"
    calls = []

    async def fake_invoke(_spec):
        calls.append("invoke")
        return SimpleNamespace(output="", success=False, error=logic_error)

    monkeypatch.setattr("brain.systems.runs.recipes.fast.build_agent_tools", lambda _role: [])
    monkeypatch.setattr("brain.systems.runs.recipes.fast.build_tool_handlers", lambda **_kwargs: {})
    monkeypatch.setattr("brain.systems.runs.recipes.fast.invoke_direct_agent_async", fake_invoke)

    runtime = _runtime("fast")
    runtime.request = replace(
        runtime.request,
        metadata={"origin": "slack_teammate", "originating_surface": "slack"},
        target_ref={"kind": "slack_message", "originating_surface": "slack"},
    )

    result = await FastRecipe().execute(runtime)

    assert calls == ["invoke"]
    assert result.status.value == "failed"
    assert result.error == logic_error
    assert result.failure_category == RunFailureCategory.INTERNAL
    assert result.final_output == DEFAULT_FAILED_RUN_MESSAGE
    assert result.output == ""
    assert logic_error not in result.final_output


def test_failed_run_message_rejects_unknown_category_text():
    from brain.systems.runs.failures import (
        DEFAULT_FAILED_RUN_MESSAGE,
        safe_terminal_run_message,
    )

    raw_error = (
        "peer closed connection without sending complete message body "
        "(incomplete chunked read)"
    )

    message = safe_terminal_run_message("failed", raw_error)

    assert message == DEFAULT_FAILED_RUN_MESSAGE
    assert raw_error not in message


async def test_fast_discussion_run_can_ack_visibly_before_spawning_child(monkeypatch):
    from brain.systems.runs.domain import RunRecipe
    from brain.systems.runs.events import run_event
    from brain.systems.runs.recipes.fast import FastRecipe

    runtime = _runtime(
        "fast",
        message=(
            "Please acknowledge first, then launch a child worker to inspect whether "
            "response-surface delegation tests exist."
        ),
    )
    runtime.request = replace(
        runtime.request,
        metadata={
            **runtime.request.metadata,
            "originating_surface": "thread_discussion",
            "required_response_tool": "post_thread_discussion_reply",
            "final_answer_target_surface": "thread_discussion",
            "discussion_trigger": {
                "thread_id": "idea-1",
                "comment_id": 7,
                "response_target": {
                    "thread_id": "idea-1",
                    "reply_to_comment_id": 7,
                },
            },
        },
        target_ref={
            "kind": "thread_discussion",
            "originating_surface": "thread_discussion",
            "required_response_tool": "post_thread_discussion_reply",
            "final_answer_target_surface": "thread_discussion",
            "discussion_trigger": {
                "thread_id": "idea-1",
                "comment_id": 7,
                "response_target": {
                    "thread_id": "idea-1",
                    "reply_to_comment_id": 7,
                },
            },
        },
    )
    visible_comments: list[dict] = []

    async def fake_reply(**kwargs):
        visible_comments.append(dict(kwargs))
        return {"ok": True, "comment_id": 99}

    async def fake_spawn_worker(**kwargs):
        child = await runtime.store.create_child_run(
            runtime.run,
            recipe=RunRecipe.WORKER,
            message=str(kwargs["objective"]),
            step_key="spawn_worker:response-surface-delegation-probe",
            metadata={
                "origin": "spawn_worker",
                "headless": bool(kwargs.get("headless")),
                "worker_role": kwargs.get("role") or "worker",
            },
        )
        await runtime.store.append_event(
            run_event(
                runtime.run.id,
                "run.worker_spawned",
                {
                    "child_run_id": child.id,
                    "step_key": "spawn_worker:response-surface-delegation-probe",
                    "headless": bool(kwargs.get("headless")),
                    "role": kwargs.get("role") or "worker",
                    "objective": kwargs["objective"],
                },
                root_run_id=runtime.run.root_run_id,
                producer="spawn_worker",
            )
        )
        return {
            "ok": True,
            "status": "queued",
            "child_run_id": child.id,
            "headless": bool(kwargs.get("headless")),
        }

    async def fake_invoke(spec):
        assert "Response Surface and Delegation" in spec.system_prompt
        assert "User-visible response tool for that surface: post_thread_discussion_reply." in spec.system_prompt
        await spec.tool_handlers["post_thread_discussion_reply"](
            body="I will launch a worker now to inspect the response-surface delegation tests.",
        )
        await spec.tool_handlers["spawn_worker"](
            objective="Inspect test names for response-surface delegation coverage.",
            role="inspect",
            headless=False,
        )
        return SimpleNamespace(output="Worker queued; I will report back when it finishes.", success=True, error=None)

    monkeypatch.setattr(
        "brain.systems.runs.recipes.fast.build_agent_tools",
        lambda _role: [
            {"name": "post_thread_discussion_reply"},
            {"name": "spawn_worker"},
        ],
    )
    monkeypatch.setattr(
        "brain.systems.runs.recipes.fast.build_tool_handlers",
        lambda **_kwargs: {
            "post_thread_discussion_reply": fake_reply,
            "spawn_worker": fake_spawn_worker,
        },
    )
    monkeypatch.setattr("brain.systems.runs.recipes.fast.invoke_direct_agent_async", fake_invoke)

    result = await FastRecipe().execute(runtime)

    assert result.status.value == "completed"
    assert visible_comments == [
        {"body": "I will launch a worker now to inspect the response-surface delegation tests."}
    ]
    event_types = [event.event_type for event in runtime.store.events]
    ack_completed_index = next(
        i
        for i, event in enumerate(runtime.store.events)
        if event.event_type == "run.tool_completed"
        and event.payload["tool_name"] == "post_thread_discussion_reply"
    )
    worker_spawned_index = event_types.index("run.worker_spawned")
    spawn_completed_index = next(
        i
        for i, event in enumerate(runtime.store.events)
        if event.event_type == "run.tool_completed" and event.payload["tool_name"] == "spawn_worker"
    )
    assert ack_completed_index < worker_spawned_index < spawn_completed_index
    worker_event = runtime.store.events[worker_spawned_index]
    assert worker_event.payload["headless"] is False
    assert worker_event.payload["child_run_id"] in runtime.store.runs


async def test_fast_recipe_uses_the_product_prompt_pipeline(monkeypatch):
    from brain.systems.runs.recipes.fast import FastRecipe

    captured = {}

    async def fake_invoke(spec):
        captured["spec"] = spec
        return SimpleNamespace(output="Yes, `436411779` is the missing `GA4_PROPERTY_ID`.", success=True, error=None)

    monkeypatch.setattr("brain.systems.runs.recipes.fast.build_agent_tools", lambda role: [])
    monkeypatch.setattr("brain.systems.runs.recipes.fast.build_tool_handlers", lambda **kwargs: {})
    monkeypatch.setattr("brain.systems.runs.recipes.fast.invoke_direct_agent_async", fake_invoke)

    runtime = _runtime("fast", message="you mean you are just missing this? 436411779")

    result = await FastRecipe().execute(runtime)

    assert result.status.value == "completed"
    prompt = captured["spec"].system_prompt
    assert prompt.index("## Agent Soul") < prompt.index("## Agent Contract")
    assert prompt.index("## Agent Contract") < prompt.index("## Fast Runtime Recipe")


async def test_fast_recipe_injects_verified_person_context(monkeypatch):
    from brain.systems.runs.recipes.fast import FastRecipe

    captured = {}

    async def fake_invoke(spec):
        captured["spec"] = spec
        return SimpleNamespace(output="Noted.", success=True, error=None)

    monkeypatch.setattr("brain.systems.runs.recipes.fast.build_agent_tools", lambda _role: [])
    monkeypatch.setattr("brain.systems.runs.recipes.fast.build_tool_handlers", lambda **_kwargs: {})
    monkeypatch.setattr("brain.systems.runs.recipes.fast.invoke_direct_agent_async", fake_invoke)

    runtime = _runtime("fast", message="Thanks, that works.")
    runtime.request = replace(
        runtime.request,
        metadata={
            "person_context": {
                "mapping": "verified",
                "user_id": "user-1",
                "source": "slack_identity_link",
                "preferences": {
                    "address_as": "Alex",
                    "tone": "casual",
                    "humour": "light",
                },
            }
        },
    )

    result = await FastRecipe().execute(runtime)

    assert result.status.value == "completed"
    prompt = captured["spec"].system_prompt
    assert "## Conversation Partner" in prompt
    assert '\"address_as\":\"Alex\"' in prompt
    assert '\"humour\":\"light\"' in prompt
    assert "user-1" not in prompt
    assert prompt.index("## Conversation Partner") < prompt.index("## Agent Contract")


def test_fast_recipe_hides_social_reaction_tool_from_passive_slack_monitor():
    from brain.systems.runs.recipes.fast import _disabled_tool_names

    runtime = _runtime("fast")
    runtime.request = replace(runtime.request, metadata={"slack_monitor": True})

    assert "react_to_slack_message" in _disabled_tool_names(runtime)


async def test_fast_recipe_keeps_large_project_context_out_of_system_prompt(monkeypatch):
    from brain.systems.runs.recipes.fast import FastRecipe

    captured = {}

    async def fake_invoke(spec):
        captured["spec"] = spec
        return SimpleNamespace(output="ok", success=True, error=None)

    huge_value = "RAW_PROJECT_FILE_CONTEXT_SHOULD_NOT_BE_IN_SYSTEM_PROMPT" * 80_000
    large_ref = {
        "kind": "cortex_idea",
        "title": "Port the SEO workflow",
        "project_context_snapshot": {
            "name": "Agent Mission Control Reference",
            "resources": [
                {
                    "id": "resource-1",
                    "kind": "folder",
                    "path": "/workspaces/agent-mission-control-reference",
                    "content": huge_value,
                    "materialization": {
                        "status": "ready",
                        "project_root_file_count": 779,
                        "imports": {
                            "imported": [huge_value],
                            "root_versions": {"before": huge_value},
                        },
                    },
                }
            ],
        },
        "project_runtime_context": {
            "project_context_snapshot": {
                "project_id": "project-1",
                "project_key": "project-1",
                "resources": [
                    {
                        "id": "resource-1",
                        "kind": "folder",
                        "name": "agent-mission-control-reference",
                        "path": "/workspaces/agent-mission-control-reference",
                        "content": huge_value,
                    }
                ],
                "permission_scope": {
                    "allowed_paths": [f"/workspaces/project/file-{index}.md" for index in range(2_000)],
                    "mode": "enforce",
                    "permission_mode": "read_write",
                },
            },
            "project_workspace_manifest": {
                "workspace_root": "/workspaces/ideas/idea-1/.illo-project-context/local/project/project-root",
                "workspaces": [
                    {
                        "name": "/",
                        "path": "/workspaces/ideas/idea-1/.illo-project-context/local/project/project-root",
                    }
                ],
            },
            "project_context_materialization": {
                "status": "materialized",
                "project_root_file_count": 779,
                "project_root_path_count": 779,
                "project_draft_file_count": 779,
                "project_draft_path_count": 779,
            },
        },
        "workspace_root": "/workspaces/agent-mission-control-reference",
    }

    monkeypatch.setattr("brain.systems.runs.recipes.fast.build_agent_tools", lambda role: [])
    monkeypatch.setattr("brain.systems.runs.recipes.fast.build_tool_handlers", lambda **kwargs: {})
    monkeypatch.setattr("brain.systems.runs.recipes.fast.invoke_direct_agent_async", fake_invoke)

    runtime = _runtime("fast", workspace_ref=large_ref)
    runtime.request = replace(runtime.request, target_ref=large_ref)

    result = await FastRecipe().execute(runtime)

    assert result.status.value == "completed"
    assert len(captured["spec"].system_prompt) < 40_000
    assert "RAW_PROJECT_FILE_CONTEXT_SHOULD_NOT_BE_IN_SYSTEM_PROMPT" not in captured["spec"].system_prompt
    assert "large value omitted from prompt context" in captured["spec"].system_prompt
    assert "project_root_file_count" in captured["spec"].system_prompt
    assert captured["spec"].system_prompt.count("## Context") == 1


async def test_direct_agent_invocation_uses_async_kernel(monkeypatch):
    from brain.systems.runs.invocation import build_direct_agent_invocation
    from brain.systems.runs.invocation import invoke_direct_agent_async

    captured = {}

    async def fake_invoke(envelope, **kwargs):
        captured["envelope"] = envelope
        captured["kwargs"] = kwargs
        return SimpleNamespace(output="ok", success=True, error=None)

    monkeypatch.setattr("brain.kernel.runtime.kernel.invoke_run_envelope_async", fake_invoke)

    spec = build_direct_agent_invocation(message="hello", model="openai/gpt-5.5")

    result = await invoke_direct_agent_async(spec)

    assert result.output == "ok"
    assert captured["envelope"].task == "hello"
    assert captured["envelope"].model == "openai/gpt-5.5"


def _scheduled_cycle_test_payload():
    from brain.platform.db.models.cycle import Cycle, CycleRun
    from brain.platform.db.models.idea import Idea
    from brain.systems.cycles.prompts import cycle_run_message, cycle_run_metadata

    idea = Idea()
    idea.id = "idea-1"
    idea.title = "Daily Illo Conversation Improvements"

    cycle = Cycle()
    cycle.id = 5
    cycle.user_id = "user-1"
    cycle.org_id = "org-1"
    cycle.name = "Daily Illo Conversation Improvements"
    cycle.prompt = (
        "Daily Illo Conversation Improvements. Produce the 7-part improvement review: "
        "24h readout, failure map, codebase implications, proposals, tracking summary, "
        "impact loop, and next action. Include evidence health and a short self-review summary."
    )
    cycle.model_override = None
    cycle.thinking_override = None

    run = CycleRun()
    run.id = 12
    run.cycle_id = 5
    run.scheduled_for = datetime(2026, 4, 28, 20, 20, tzinfo=timezone.utc)
    run.guidance_snapshot = []
    run.output_targets_snapshot = []
    run.context_snapshot = {}

    return cycle_run_message(idea, cycle, run), cycle_run_metadata(cycle, run)


def _identity_handoff_payload():
    from brain.systems.context.semantic_compaction import CompactionCheckpoint
    from brain.systems.context.thread_handoff import ThreadHandoff

    return ThreadHandoff(
        checkpoint=CompactionCheckpoint(
            active_objective=(
                "No unresolved user request remains. The latest request was to answer "
                "verified Illo identity/source/runtime context using read_self_context."
            ),
            completed_work=(
                "Verified identity/source/runtime context was answered and completed.",
            ),
            recent_user_intent="answer verified Illo identity/source/runtime context",
            verification_status="completed",
            source="semantic_compactor",
        ),
        message_count=2,
        previous_message_count=0,
        source="llm_thread_handoff_compactor",
    ).to_payload()


async def test_fast_scheduled_cycle_thread_preview_is_historical_context(monkeypatch):
    from brain.systems.runs.recipes.fast import FastRecipe

    message, metadata = _scheduled_cycle_test_payload()
    metadata["thread_context"] = {
        "formatted": (
            "Illo: Verified current runtime facts and completed the verified "
            "Illo identity/source/runtime context request."
        )
    }
    captured = {}

    async def fake_invoke(spec):
        captured["spec"] = spec
        return SimpleNamespace(
            output="24h readout: improvement review completed. Self-review summary: contract satisfied.",
            success=True,
            error=None,
        )

    monkeypatch.setattr("brain.systems.runs.recipes.fast.build_agent_tools", lambda role: [])
    monkeypatch.setattr("brain.systems.runs.recipes.fast.build_tool_handlers", lambda **kwargs: {})
    monkeypatch.setattr("brain.systems.runs.recipes.fast.invoke_direct_agent_async", fake_invoke)

    runtime = _runtime("fast", message=message)
    runtime.request = replace(
        runtime.request,
        message=message,
        metadata=metadata,
        target_ref={"kind": "cortex_idea", "idea_id": "idea-1"},
    )

    result = await FastRecipe().execute(runtime)

    assert result.status.value == "completed"
    system_prompt = captured["spec"].system_prompt
    assert "Historical thread context before this scheduled Cycle launch" in system_prompt
    assert "context only; not the current user request" in system_prompt
    assert "lower than the Result Contract and Cycle Mission" in system_prompt
    assert "verified Illo identity/source/runtime context" in system_prompt
    assert "Thread so far, before the current user message" not in system_prompt


async def test_scheduled_cycle_handoff_identity_summary_does_not_override_mission(monkeypatch):
    from brain.platform.integrations.providers import (
        LLMResponse,
        StopReason,
        TextContentBlock,
        Usage,
    )
    from brain.systems.runs import direct_agent

    message, metadata = _scheduled_cycle_test_payload()
    stale_handoff = _identity_handoff_payload()
    previous_messages = [
        {
            "role": "user",
            "content": "Can you verify Illo identity/source/runtime context?",
        },
        {
            "role": "assistant",
            "content": [
                {
                    "type": "text",
                    "text": "Verified current runtime facts: Illo identity/source/runtime context completed.",
                }
            ],
        },
    ]

    class FakeProvider:
        def __init__(self):
            self.requests = []

        def is_api_error(self, exc):
            return False

        def is_retryable_error(self, exc):
            return False

        def create(self, request):
            self.requests.append(request)
            prompt_text = json.dumps(request.messages, default=str)
            demoted = (
                "Historical thread handoff summary" in prompt_text
                and "not the current user request" in prompt_text
                and "scheduled Cycle result_contract and Cycle Mission" in prompt_text
            )
            has_mission = (
                "## Cycle Mission" in prompt_text
                and "Daily Illo Conversation Improvements" in prompt_text
                and "7-part improvement review" in prompt_text
            )
            if demoted and has_mission:
                output = (
                    "24h readout: reviewed the scheduled improvement window.\n"
                    "Failure map: stale identity boilerplate is historical context only.\n"
                    "Codebase implications: scheduled Cycle mission remains authoritative.\n"
                    "Proposals: keep handoff summaries demoted.\n"
                    "Tracking summary: improvement review completed.\n"
                    "Impact loop: compare the next run against the Cycle contract.\n"
                    "Next action: monitor the next scheduled run.\n"
                    "Evidence health: ok.\n"
                    "Self-review summary: mission result contract satisfied."
                )
            else:
                output = (
                    "Verified current runtime facts: Illo identity/source/runtime context "
                    "completed."
                )
            return LLMResponse(
                content=[TextContentBlock(output)],
                stop_reason=StopReason.END_TURN,
                usage=Usage(input_tokens=1, output_tokens=len(output.split())),
            )

    async def load_session(_session_id):
        return previous_messages, None

    async def load_session_handoff(_session_id):
        return stale_handoff

    async def save_session(*_args, **_kwargs):
        return None

    async def save_session_handoff(*_args, **_kwargs):
        return None

    async def record_api_call(**_kwargs):
        return None

    provider = FakeProvider()
    monkeypatch.setattr(direct_agent, "get_provider", lambda _provider_name, _client: provider)
    monkeypatch.setattr(direct_agent, "_async_record_api_call", record_api_call)
    resolved_llm = SimpleNamespace(
        provider="openai",
        client=object(),
        source="test",
        auth_mode="api_key",
        is_oauth=False,
        token_prefix="test-token",
        build_request_headers=lambda **_kwargs: {},
    )

    result = await direct_agent.run_agent_async(
        message,
        session_id="agent-run-44",
        model="openai/gpt-5.6-sol",
        thinking="low",
        tools=[],
        tool_handlers={},
        max_turns=1,
        persist_session=True,
        cache_system_prompt=False,
        resolved_llm=resolved_llm,
        metadata=metadata,
        load_session=load_session,
        load_session_handoff=load_session_handoff,
        save_session=save_session,
        save_session_handoff=save_session_handoff,
        skip_harvest=True,
    )

    assert result.success is True
    assert result.output.startswith("24h readout")
    assert "Verified current runtime facts" not in result.output
    assert len(provider.requests) == 1
    first_request = provider.requests[0]
    prompt_text = json.dumps(first_request.messages, default=str)
    assert "Historical thread handoff summary" in prompt_text
    assert "verified Illo identity/source/runtime context" in prompt_text
    assert "not the current user request" in prompt_text
    assert "Daily Illo Conversation Improvements" in prompt_text
    assert first_request.messages[-1]["content"] == message


async def test_oversized_system_prompt_fails_admission_before_sampling_and_names_budget(
    monkeypatch,
    caplog,
):
    from brain.systems.runs import direct_agent

    class FakeProvider:
        def __init__(self):
            self.requests = []

        def is_api_error(self, exc):
            return False

        def is_retryable_error(self, exc):
            return False

        def create(self, request):
            self.requests.append(request)
            raise AssertionError("admission failure must happen before provider sampling")

    provider = FakeProvider()
    monkeypatch.setenv("AGENT_MODEL_CONTEXT_WINDOW_TOKENS", "4096")
    monkeypatch.setenv("AGENT_AUTO_COMPACT_TOKEN_LIMIT", "500")
    monkeypatch.setenv("AGENT_CONTEXT_RESERVED_OUTPUT_TOKENS", "0")
    monkeypatch.setenv("AGENT_CONTEXT_RESERVED_REASONING_TOKENS", "0")
    monkeypatch.setenv("AGENT_CONTEXT_RESERVED_TOOL_TOKENS", "0")
    monkeypatch.setenv("AGENT_CONTEXT_SAFETY_MARGIN_TOKENS", "0")
    monkeypatch.setattr(direct_agent, "get_provider", lambda _provider_name, _client: provider)
    resolved_llm = SimpleNamespace(
        provider="openai",
        client=object(),
        source="test",
        auth_mode="api_key",
        is_oauth=False,
        token_prefix="test-token",
        build_request_headers=lambda **_kwargs: {},
    )

    with caplog.at_level("ERROR", logger="agent"):
        result = await direct_agent.run_agent_async(
            "start",
            system_prompt="oversized " + ("x" * 4000),
            session_id="oversized-admission",
            model="openai/gpt-5.5",
            thinking="low",
            tools=[{"name": "read_file"}, {"name": "search_files"}],
            tool_handlers={},
            max_turns=20,
            persist_session=False,
            cache_system_prompt=False,
            resolved_llm=resolved_llm,
        )

    assert result.success is False
    assert result.tokens_input == 0
    assert result.error is not None
    assert result.error.startswith("context_floor_exceeds_budget:")
    assert "floor=" in result.error
    assert "ceiling=500" in result.error
    assert "tools=2" in result.error
    assert provider.requests == []
    assert result.error in caplog.text


async def test_context_admission_healthy_path_still_samples_once(monkeypatch):
    from brain.platform.integrations.providers import (
        LLMResponse,
        StopReason,
        TextContentBlock,
        Usage,
    )
    from brain.systems.runs import direct_agent

    class FakeProvider:
        def __init__(self):
            self.requests = []

        def is_api_error(self, exc):
            return False

        def is_retryable_error(self, exc):
            return False

        def create(self, request):
            self.requests.append(request)
            return LLMResponse(
                content=[TextContentBlock("healthy response")],
                stop_reason=StopReason.END_TURN,
                usage=Usage(input_tokens=12, output_tokens=3),
            )

    provider = FakeProvider()
    monkeypatch.setenv("AGENT_MODEL_CONTEXT_WINDOW_TOKENS", "10000")
    monkeypatch.setenv("AGENT_AUTO_COMPACT_TOKEN_LIMIT", "7000")
    monkeypatch.setenv("AGENT_CONTEXT_RESERVED_OUTPUT_TOKENS", "0")
    monkeypatch.setenv("AGENT_CONTEXT_RESERVED_REASONING_TOKENS", "0")
    monkeypatch.setenv("AGENT_CONTEXT_RESERVED_TOOL_TOKENS", "0")
    monkeypatch.setenv("AGENT_CONTEXT_SAFETY_MARGIN_TOKENS", "0")
    monkeypatch.setattr(direct_agent, "get_provider", lambda _provider_name, _client: provider)
    resolved_llm = SimpleNamespace(
        provider="openai",
        client=object(),
        source="test",
        auth_mode="api_key",
        is_oauth=False,
        token_prefix="test-token",
        build_request_headers=lambda **_kwargs: {},
    )

    result = await direct_agent.run_agent_async(
        "start",
        system_prompt="small healthy system prompt",
        session_id="healthy-admission",
        model="openai/gpt-5.5",
        thinking="low",
        tools=[{"name": "read_file"}],
        tool_handlers={},
        max_turns=1,
        persist_session=False,
        cache_system_prompt=False,
        resolved_llm=resolved_llm,
    )

    assert result.success is True
    assert result.output == "healthy response"
    assert result.error is None
    assert len(provider.requests) == 1


def test_repeated_unwinnable_compaction_stops_and_rate_limits_warning(
    monkeypatch,
    caplog,
):
    from brain.systems.context.errors import ContextCompactionStalledError
    from brain.systems.context.window_policy import ContextWindowPolicy
    from brain.systems.runs import direct_agent

    monkeypatch.setenv("AGENT_MODEL_CONTEXT_WINDOW_TOKENS", "4096")
    monkeypatch.setenv("AGENT_AUTO_COMPACT_TOKEN_LIMIT", "500")
    monkeypatch.setenv("AGENT_CONTEXT_RESERVED_OUTPUT_TOKENS", "0")
    monkeypatch.setenv("AGENT_CONTEXT_RESERVED_REASONING_TOKENS", "0")
    monkeypatch.setenv("AGENT_CONTEXT_RESERVED_TOOL_TOKENS", "0")
    monkeypatch.setenv("AGENT_CONTEXT_SAFETY_MARGIN_TOKENS", "0")
    messages = [
        {"role": "user", "content": "x" * 1200},
        {"role": "assistant", "content": "y" * 1200},
        {"role": "user", "content": "z" * 1200},
        {"role": "assistant", "content": "w" * 1200},
    ]
    policy = ContextWindowPolicy.resolve(
        model="gpt-5.5",
        provider="openai",
        reasoning_effort="low",
        max_output_tokens=0,
        tools=[],
    )

    with caplog.at_level("WARNING", logger="agent"):
        for _ in range(policy.max_consecutive_no_progress - 1):
            outcome = direct_agent._compact_active_context(
                messages,
                policy=policy,
                session_id="stalled-compaction",
                model="gpt-5.5",
                phase="mid_turn",
                provider_name="openai",
            )
            assert outcome.messages == messages
            assert outcome.report is None

        with pytest.raises(
            ContextCompactionStalledError,
            match=r"context_compaction_stalled: .*ceiling=500.*tools=0.*attempts=3",
        ):
            direct_agent._compact_active_context(
                messages,
                policy=policy,
                session_id="stalled-compaction",
                model="gpt-5.5",
                phase="mid_turn",
                provider_name="openai",
            )

    assert caplog.text.count("no safe transcript messages were eligible for compaction") == 1


def test_repeated_compaction_that_stays_over_ceiling_also_stops(monkeypatch):
    from brain.systems.context.errors import ContextCompactionStalledError
    from brain.systems.context.window_policy import ContextWindowPolicy
    from brain.systems.runs import direct_agent

    monkeypatch.setenv("AGENT_MODEL_CONTEXT_WINDOW_TOKENS", "4096")
    monkeypatch.setenv("AGENT_AUTO_COMPACT_TOKEN_LIMIT", "500")
    monkeypatch.setenv("AGENT_CONTEXT_RESERVED_OUTPUT_TOKENS", "0")
    monkeypatch.setenv("AGENT_CONTEXT_RESERVED_REASONING_TOKENS", "0")
    monkeypatch.setenv("AGENT_CONTEXT_RESERVED_TOOL_TOKENS", "0")
    monkeypatch.setenv("AGENT_CONTEXT_SAFETY_MARGIN_TOKENS", "0")
    messages = [
        {"role": "user", "content": "x" * 1600},
        {"role": "assistant", "content": "y" * 1600},
        {"role": "user", "content": "z" * 1600},
        {"role": "assistant", "content": "w" * 1600},
        {"role": "user", "content": "latest"},
    ]

    def oversized_checkpoint(_omitted, _context):
        return {"active_objective": "still too large " + ("q" * 2400)}

    policy = ContextWindowPolicy.resolve(
        model="gpt-5.5",
        provider="openai",
        reasoning_effort="low",
        max_output_tokens=0,
        tools=[],
    )
    for _ in range(policy.max_consecutive_no_progress - 1):
        outcome = direct_agent._compact_active_context(
            messages,
            policy=policy,
            session_id="insufficient-compaction",
            model="gpt-5.5",
            phase="mid_turn",
            provider_name="openai",
            semantic_compactor=oversized_checkpoint,
        )
        assert outcome.report is not None

    with pytest.raises(
        ContextCompactionStalledError,
        match=r"context_compaction_stalled: estimated=\d+ ceiling=500",
    ):
        direct_agent._compact_active_context(
            messages,
            policy=policy,
            session_id="insufficient-compaction",
            model="gpt-5.5",
            phase="mid_turn",
            provider_name="openai",
            semantic_compactor=oversized_checkpoint,
        )


def test_fast_onboarding_tool_surface_uses_standard_fast_surface(monkeypatch):
    from brain.systems.runs.recipes.fast import _agent_tools_for_runtime

    monkeypatch.setattr(
        "brain.systems.runs.recipes.fast.build_agent_tools",
        lambda role: [
            {"name": "read_workspace_overview"},
            {"name": "browser"},
            {"name": "cortex_reply"},
        ],
    )

    runtime = SimpleNamespace(
        request=SimpleNamespace(
            metadata={
                "origin": "onboarding",
            }
        )
    )

    assert [tool["name"] for tool in _agent_tools_for_runtime(runtime)] == ["read_workspace_overview", "browser"]


def test_fast_tool_surface_is_direct_coordinator_without_staged_reply_tools(monkeypatch):
    from brain.systems.runs.recipes.fast import _agent_tools_for_runtime

    roles = []

    def fake_build_agent_tools(role):
        roles.append(role)
        return [
            {"name": "manage_soul"},
            {"name": "cortex_reply"},
            {"name": "cortex_visual_reply"},
            {"name": "read_workspace_overview"},
        ]

    monkeypatch.setattr("brain.systems.runs.recipes.fast.build_agent_tools", fake_build_agent_tools)

    runtime = SimpleNamespace(request=SimpleNamespace(metadata={}))

    assert roles == []
    assert [tool["name"] for tool in _agent_tools_for_runtime(runtime)] == [
        "manage_soul",
        "read_workspace_overview",
    ]
    assert roles == ["coordinator"]


def test_fast_discussion_origin_surface_keeps_timeline_reply_tools_hidden(monkeypatch):
    from brain.systems.runs.recipes.fast import _agent_tools_for_runtime

    monkeypatch.setattr(
        "brain.systems.runs.recipes.fast.build_agent_tools",
        lambda role: [
            {"name": "post_thread_discussion_reply"},
            {"name": "post_ai_timeline_message"},
            {"name": "cortex_reply"},
            {"name": "cortex_visual_reply"},
        ],
    )

    runtime = SimpleNamespace(
        request=SimpleNamespace(
            metadata={"originating_surface": "thread_discussion"},
            target_ref={"originating_surface": "thread_discussion"},
        )
    )

    assert [tool["name"] for tool in _agent_tools_for_runtime(runtime)] == [
        "post_thread_discussion_reply",
        "post_ai_timeline_message",
    ]


def test_fast_timeline_origin_hides_discussion_reply_tool(monkeypatch):
    from brain.systems.runs.recipes.fast import _agent_tools_for_runtime

    monkeypatch.setattr(
        "brain.systems.runs.recipes.fast.build_agent_tools",
        lambda role: [
            {"name": "post_thread_discussion_reply"},
            {"name": "post_ai_timeline_message"},
        ],
    )

    runtime = SimpleNamespace(
        request=SimpleNamespace(
            thread_id="idea-1",
            metadata={},
            target_ref={"originating_surface": "ai_timeline"},
        )
    )

    assert [tool["name"] for tool in _agent_tools_for_runtime(runtime)] == [
        "post_ai_timeline_message",
    ]


def test_discussion_origin_run_does_not_auto_mirror_final_answer_to_timeline():
    from brain.systems.runs.cortex.runner import _run_should_mirror_final_answer_to_timeline

    run = SimpleNamespace(
        target_ref={"originating_surface": "thread_discussion"},
        metadata_={"final_answer_target_surface": "originating_surface"},
    )

    assert _run_should_mirror_final_answer_to_timeline(run) is False


def test_discussion_origin_run_never_mirrors_final_answer_to_ai_timeline():
    from brain.systems.runs.cortex.runner import _run_should_mirror_final_answer_to_timeline

    run = SimpleNamespace(
        target_ref={"originating_surface": "thread_discussion"},
        metadata_={"final_answer_target_surface": "ai_timeline"},
    )

    assert _run_should_mirror_final_answer_to_timeline(run) is False


async def test_runtime_drain_steering_can_use_isolated_durable_drain():
    from brain.systems.runs.steering import SteeringMessage

    runtime = _runtime("fast")
    calls = []

    def fail_shared_drain(_run_id):
        raise AssertionError("shared runtime store should not drain durable steering")

    def isolated_drain(run_id):
        calls.append(run_id)
        return [SteeringMessage(run_id=run_id, content="Use the smaller repro.", user_id="user-2")]

    runtime.store.drain_steering = fail_shared_drain
    runtime.durable_steering_drain = isolated_drain

    messages = await runtime.drain_steering()

    assert calls == [42]
    assert messages == ["Use the smaller repro.", "Focus on setup steps."]
    received = [event for event in runtime.store.events if event.event_type == "run.steering_received"]
    assert [event.payload["content"] for event in received] == ["Use the smaller repro.", "Focus on setup steps."]


def test_project_runtime_workspace_from_ref_uses_thread_project_context_snapshot():
    from brain.systems.runs.recipes.shared import project_runtime_workspace_from_ref

    runtime_workspace = project_runtime_workspace_from_ref(
        {
            "workspaces": [
                {"name": "/repos/frontend", "path": "/tmp/projects/p1/repos/frontend"},
                {"name": "/reports", "path": "/tmp/projects/p1/reports"},
            ],
            "project_context_snapshot": {
                "resources": [
                    {
                        "kind": "repo",
                        "mount_path": "/repos/frontend",
                        "path": "/tmp/projects/p1/repos/frontend",
                    },
                    {
                        "kind": "folder",
                        "mount_path": "/reports",
                        "path": "/tmp/projects/p1/reports",
                    },
                ],
            },
        }
    )
    assert runtime_workspace.workspace_root == "/tmp/projects/p1/repos/frontend"
    assert runtime_workspace.allowed_workspaces == [
        {"name": "/repos/frontend", "path": "/tmp/projects/p1/repos/frontend"},
        {"name": "/reports", "path": "/tmp/projects/p1/reports"},
    ]
    runtime_workspace = project_runtime_workspace_from_ref(
        {
            "workspaces": [{"name": "/specs", "path": "/tmp/projects/p1/specs"}],
            "project_context_permission_scope": {
                "allowed_paths": ["/tmp/projects/p1/specs/spec.md"],
            },
        }
    )
    assert runtime_workspace.allowed_workspaces == [
        {"name": "/specs", "path": "/tmp/projects/p1/specs"},
    ]
    runtime_workspace = project_runtime_workspace_from_ref(
        {
            "workspaces": [{"name": "spec.md", "path": "/tmp/projects/p1/draft/attachment-1"}],
            "project_context_snapshot": {
                "resources": [
                    {
                        "kind": "file",
                        "name": "spec.md",
                        "path": "/tmp/projects/p1/draft/attachment-1/spec.md",
                        "materialization": {
                            "workspace_path": "/tmp/projects/p1/draft/attachment-1",
                        },
                    }
                ],
            },
        }
    )
    assert runtime_workspace.allowed_workspaces == [
        {"name": "spec.md", "path": "/tmp/projects/p1/draft/attachment-1"},
    ]
    runtime_workspace = project_runtime_workspace_from_ref(
        {
            "workspaces": [
                {"name": "/reports", "path": "/tmp/projects/p1/reports-a"},
                {"name": "/reports", "path": "/tmp/projects/p1/reports-b"},
            ],
            "project_workspace_manifest": {
                "workspace_root": "/tmp/projects/p1/reports-a",
                "workspaces": [
                    {"name": "/reports", "path": "/tmp/projects/p1/reports-a"},
                    {"name": "/reports-2", "path": "/tmp/projects/p1/reports-b"},
                ],
            },
        }
    )
    assert runtime_workspace.allowed_workspaces == [
        {"name": "/reports", "path": "/tmp/projects/p1/reports-a"},
        {"name": "/reports-2", "path": "/tmp/projects/p1/reports-b"},
    ]
    runtime_workspace = project_runtime_workspace_from_ref(
        {
            "workspace_root": "/tmp/projects/p1/repos/backend",
            "resolved_workspace_root": "/tmp/projects/p1/repos/backend",
            "project_workspace_manifest": {
                "workspace_root": "/tmp/projects/p1/project-root",
                "workspaces": [
                    {"name": "/", "path": "/tmp/projects/p1/project-root"},
                    {"name": "/uwear-ai/uwear-backend", "path": "/tmp/projects/p1/repos/backend"},
                ],
            },
        }
    )
    assert runtime_workspace.workspace_root == "/tmp/projects/p1/repos/backend"
    assert runtime_workspace.allowed_workspaces == [
        {"name": "/", "path": "/tmp/projects/p1/project-root"},
        {"name": "/uwear-ai/uwear-backend", "path": "/tmp/projects/p1/repos/backend"},
    ]


async def test_fast_recipe_infers_workspace_root_from_project_context_snapshot(monkeypatch):
    from brain.systems.runs.recipes.fast import FastRecipe

    captured = {}

    async def fake_invoke(spec):
        captured["spec"] = spec
        return SimpleNamespace(output="ok", success=True, error=None)

    monkeypatch.setattr("brain.systems.runs.recipes.fast.build_agent_tools", lambda role: [])
    monkeypatch.setattr("brain.systems.runs.recipes.fast.build_tool_handlers", lambda **kwargs: {})
    monkeypatch.setattr("brain.systems.runs.recipes.fast.invoke_direct_agent_async", fake_invoke)

    runtime = _runtime(
        "fast",
        workspace_ref={
            "project_context_snapshot": {
                "resources": [{"kind": "repo", "path": "/tmp/ideas/idea-1/project/repo"}],
                "permission_scope": {"allowed_paths": ["/tmp/ideas/idea-1/project/repo"]},
            },
            "project_context_permission_scope": {"allowed_paths": ["/tmp/ideas/idea-1/project/repo"]},
        },
    )

    result = await FastRecipe().execute(runtime)

    assert result.status.value == "completed"
    assert captured["spec"].workspace_root == "/tmp/ideas/idea-1/project/repo"


async def test_fast_recipe_applies_runtime_tool_policy(monkeypatch):
    from brain.systems.runs.recipes.fast import FastRecipe

    captured = {}

    async def fake_invoke(spec):
        captured["spec"] = spec
        return SimpleNamespace(output="ok", success=True, error=None)

    monkeypatch.setattr(
        "brain.systems.runs.recipes.fast.build_agent_tools",
        lambda role: [{"name": "manage_cycle"}, {"name": "post_chat_message"}, {"name": "web_search"}],
    )
    monkeypatch.setattr(
        "brain.systems.runs.recipes.fast.build_tool_handlers",
        lambda **kwargs: {"manage_cycle": object(), "post_chat_message": object(), "web_search": object()},
    )
    monkeypatch.setattr("brain.systems.runs.recipes.fast.invoke_direct_agent_async", fake_invoke)

    runtime = _runtime("fast")
    runtime.request = replace(
        runtime.request,
        metadata={"tool_policy": {"disabled_tools": ["manage_cycle"], "blocked_tools": ["post_chat_message"]}},
    )

    result = await FastRecipe().execute(runtime)

    assert result.status.value == "completed"
    assert [tool["name"] for tool in captured["spec"].tools] == ["web_search"]
    assert sorted(captured["spec"].tool_handlers) == ["web_search"]


def test_tool_policy_unions_disabled_and_blocked_aliases():
    from brain.systems.runs.direct_agent import _disabled_tool_names

    assert _disabled_tool_names({
        "tool_policy": {
            "disabled_tools": ["manage_cycle"],
            "blocked_tools": ["post_chat_message"],
        }
    }) == {"manage_cycle", "post_chat_message"}


def test_headless_worker_policy_uses_canonical_disabled_tools():
    from brain.systems.runs.tool_catalog.handlers.workers import _merge_tool_policy

    policy = _merge_tool_policy(
        {"disabled_tools": ["manage_cycle"], "blocked_tools": ["post_chat_message"]},
        headless=True,
    )

    assert "blocked_tools" not in policy
    assert set(policy["disabled_tools"]) >= {
        "manage_cycle",
        "post_chat_message",
        "cortex_reply",
        "post_ai_timeline_message",
    }


async def _captured_spawn_worker(
    monkeypatch,
    *,
    parent_model_policy,
    child_model_policy=None,
    execution_metadata=None,
    created_here=True,
    preflight_result=None,
    **spawn_kwargs,
):
    from brain.platform.integrations.provider_auth_preflight import (
        ProviderAuthPassedPreflightResult,
    )
    from brain.systems.runs.domain import RunProfile, RunRecipe
    from brain.systems.runs.evidence_health import WorkerEvidenceReceipt
    from brain.systems.runs.execution_context import bind_agent_context
    import brain.systems.runs.tool_catalog.handlers.workers as worker_handlers

    parent = SimpleNamespace(
        id=42,
        org_id="org-1",
        user_id="user-1",
        root_run_id=42,
        profile=RunProfile.FAST,
        target_ref={"kind": "cortex_idea", "idea_id": "idea-1"},
        workspace_ref={"workspace_root": "/tmp/work"},
        model_policy=dict(parent_model_policy),
        metadata_={},
    )
    captured = {}
    events = []

    class _Uow:
        session = object()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

    class _Store:
        def __init__(self, session):
            self.session = session

        async def require_run(self, run_id):
            assert run_id == parent.id
            return parent

        async def create_child_run_with_result(self, parent_arg, **kwargs):
            assert parent_arg is parent
            captured.update(kwargs)
            actual_policy = (
                dict(child_model_policy)
                if child_model_policy is not None
                else dict(kwargs["model_policy"])
            )
            return (
                SimpleNamespace(
                    id=99,
                    root_run_id=42,
                    recipe=RunRecipe.WORKER,
                    model_policy=actual_policy,
                    metadata=dict(kwargs["metadata"]),
                ),
                created_here,
            )

        async def append_event(self, event):
            events.append(event)

    monkeypatch.setattr(worker_handlers, "UnitOfWork", _Uow)
    monkeypatch.setattr(worker_handlers, "AsyncAgentRunStore", _Store)

    async def fake_preflight(*_args, **_kwargs):
        captured["preflight"] = dict(_kwargs)
        return preflight_result or ProviderAuthPassedPreflightResult(
            provider="openai",
            model="openai/gpt-5.6-sol",
        )

    async def fake_record_parent_failures(
        _session,
        *,
        parent_run_id,
        failures,
    ):
        assert parent_run_id == parent.id
        receipt = WorkerEvidenceReceipt.from_payload(
            parent.metadata_.get("evidence_health")
        )
        receipt, added = receipt.with_failures(failures)
        health = receipt.to_payload()
        parent.metadata_["evidence_health"] = health
        captured["admission_failures"] = [
            failure.to_payload() for failure in failures
        ]
        captured["parent_evidence_health"] = health
        return added
        return list(failures)

    monkeypatch.setattr(
        worker_handlers,
        "_preflight_spawn_worker_auth",
        fake_preflight,
    )
    monkeypatch.setattr(
        worker_handlers,
        "record_parent_evidence_failures",
        fake_record_parent_failures,
    )

    with bind_agent_context(
        run=SimpleNamespace(id=parent.id),
        execution_metadata=dict(execution_metadata or {}),
    ):
        payload = json.loads(
            await worker_handlers._handle_spawn_worker(
                objective="Inspect the routing contract.",
                **spawn_kwargs,
            )
        )
    return payload, captured, events


async def test_spawn_worker_effort_override_merges_over_materialized_parent_policy(monkeypatch):
    payload, captured, events = await _captured_spawn_worker(
        monkeypatch,
        parent_model_policy={
            "model": "openai/gpt-5.6-sol",
            "thinking": "xhigh",
            "future_policy_key": "preserved",
        },
        effort="low",
    )

    assert captured["model_policy"] == {
        "model": "openai/gpt-5.6-sol",
        "thinking": "low",
        "future_policy_key": "preserved",
    }
    requested = captured["metadata"]["routing"]["requested"]
    assert requested["model"] == {
        "value": "openai/gpt-5.6-sol",
        "source": "parent_effective",
    }
    assert requested["effort"] == {
        "value": "low",
        "source": "spawn_worker.effort",
    }
    assert payload["model"] == "openai/gpt-5.6-sol"
    assert payload["effort"] == "low"
    assert payload["routing"]["provider"] == "openai"
    assert payload["routing"]["auth_mode"] == "chatgpt"
    assert events[0].payload["routing"]["effort"] == "low"


async def test_spawn_worker_coerces_inherited_api_key_model(monkeypatch):
    payload, captured, _events = await _captured_spawn_worker(
        monkeypatch,
        parent_model_policy={
            "model": "openai/gpt-5.4-mini",
            "thinking": "low",
        },
    )

    assert captured["model_policy"]["model"] == "openai/gpt-5.6-sol"
    assert payload["model"] == "openai/gpt-5.6-sol"
    assert payload["routing"]["auth_mode"] == "chatgpt"


async def test_spawn_worker_admits_ollama_model_without_coercion(monkeypatch):
    payload, captured, _events = await _captured_spawn_worker(
        monkeypatch,
        parent_model_policy={
            "model": "openai/gpt-5.6-sol",
            "thinking": "high",
        },
        model="ollama/qwen3.6-27b",
        effort="none",
    )

    assert captured["model_policy"] == {
        "model": "ollama/qwen3.6-27b",
        "thinking": "none",
    }
    assert captured["preflight"]["model"] == "ollama/qwen3.6-27b"
    assert captured["metadata"]["routing"]["requested"]["model"] == {
        "value": "ollama/qwen3.6-27b",
        "source": "spawn_worker.model",
    }
    assert payload["model"] == "ollama/qwen3.6-27b"
    assert payload["routing"] == {
        "model": "ollama/qwen3.6-27b",
        "effort": "none",
        "provider": "ollama",
        "auth_mode": None,
    }


async def test_spawn_worker_ollama_auth_preflight_is_skipped(monkeypatch):
    from brain.platform.integrations.provider_auth_preflight import (
        ProviderAuthSkippedPreflightResult,
    )
    import brain.systems.runs.tool_catalog.handlers.workers as worker_handlers

    async def unexpected_probe(*_args, **_kwargs):
        raise AssertionError("Ollama must not enter stored-credential auth probing")

    monkeypatch.setattr(
        worker_handlers,
        "async_probe_provider_auth",
        unexpected_probe,
    )

    result = await worker_handlers._preflight_spawn_worker_auth(
        object(),
        user_id="user-1",
        org_id="org-1",
        model="ollama/qwen3.6-27b",
    )

    assert isinstance(result, ProviderAuthSkippedPreflightResult)
    assert result.to_dict() == {
        "status": "skipped",
        "provider": "ollama",
        "model": "ollama/qwen3.6-27b",
        "credential": None,
        "error_code": None,
        "repair_action": None,
        "visible_message": None,
    }


async def test_spawn_worker_without_overrides_materializes_parent_effective_defaults(monkeypatch):
    import brain.systems.runs.tool_catalog.handlers.workers as worker_handlers

    async def default_model(session, **kwargs):
        assert session is not None
        assert kwargs == {
            "include_provider_prefix": True,
            "user_id": "user-1",
            "org_id": "org-1",
        }
        return "openai/gpt-5.6-sol"

    async def default_thinking(session, **kwargs):
        assert session is not None
        assert kwargs == {"user_id": "user-1", "org_id": "org-1"}
        return "medium"

    monkeypatch.setattr(worker_handlers, "async_get_default_model", default_model)
    monkeypatch.setattr(worker_handlers, "async_get_default_thinking", default_thinking)

    payload, captured, _events = await _captured_spawn_worker(
        monkeypatch,
        parent_model_policy={},
    )

    assert captured["model_policy"] == {
        "model": "openai/gpt-5.6-sol",
        "thinking": "medium",
    }
    assert captured["metadata"]["routing"]["requested"] == {
        "model": {
            "value": "openai/gpt-5.6-sol",
            "source": "parent_effective",
        },
        "effort": {
            "value": "medium",
            "source": "parent_effective",
        },
    }
    assert payload["model"] == "openai/gpt-5.6-sol"
    assert payload["effort"] == "medium"


async def test_spawn_worker_join_parent_metadata_is_strictly_opt_in(monkeypatch):
    _payload, fire_and_forget, _events = await _captured_spawn_worker(
        monkeypatch,
        parent_model_policy={
            "model": "openai/gpt-5.6-sol",
            "thinking": "medium",
        },
    )
    _payload, joined, _events = await _captured_spawn_worker(
        monkeypatch,
        parent_model_policy={
            "model": "openai/gpt-5.6-sol",
            "thinking": "medium",
        },
        join_parent=True,
    )

    assert "join_parent" not in fire_and_forget["metadata"]
    assert joined["metadata"]["join_parent"] is True


async def test_spawn_worker_inherits_live_parent_route_after_parent_fallback(monkeypatch):
    import brain.systems.runs.tool_catalog.handlers.workers as worker_handlers

    async def unexpected_default(*_args, **_kwargs):
        raise AssertionError("live parent effective routing should win over late defaults")

    monkeypatch.setattr(worker_handlers, "async_get_default_model", unexpected_default)
    monkeypatch.setattr(worker_handlers, "async_get_default_thinking", unexpected_default)

    _payload, captured, _events = await _captured_spawn_worker(
        monkeypatch,
        parent_model_policy={},
        execution_metadata={
            "routing": {
                "effective": {
                    "model": "openai/gpt-5.5",
                    "effort": "high",
                    "provider": "openai",
                    "auth_mode": "chatgpt",
                }
            }
        },
    )

    assert captured["model_policy"] == {
        "model": "openai/gpt-5.5",
        "thinking": "high",
    }


async def test_spawn_worker_bare_provider_resolves_provider_default_and_transport(monkeypatch):
    from brain.platform.providers.model_policy import DEFAULT_PROVIDER_MODELS

    # Derived rather than hardcoded: this asserts that a bare provider name
    # resolves to *that provider's default*, so it must not need editing every
    # time the catalogued lineup is refreshed.
    expected_model = f"anthropic/{DEFAULT_PROVIDER_MODELS['anthropic']}"

    payload, captured, _events = await _captured_spawn_worker(
        monkeypatch,
        parent_model_policy={
            "model": "openai/gpt-5.6-sol",
            "thinking": "high",
        },
        model="anthropic",
        effort="xhigh",
    )

    assert captured["model_policy"] == {
        "model": expected_model,
        "thinking": "xhigh",
    }
    assert captured["metadata"]["routing"]["requested"]["model"] == {
        "value": expected_model,
        "source": "spawn_worker.model",
        "requested_value": "anthropic",
    }
    assert payload["model"] == expected_model
    assert payload["effort"] == "xhigh"
    assert payload["routing"] == {
        "model": expected_model,
        "effort": "xhigh",
        "provider": "anthropic",
        "auth_mode": None,
    }


async def test_spawn_worker_auth_preflight_rejects_missing_provider_credential_before_child_creation(
    monkeypatch,
):
    from brain.platform.integrations.provider_auth_preflight import (
        ProviderAuthBlockedPreflightResult,
    )

    blocked = ProviderAuthBlockedPreflightResult(
        provider="anthropic",
        model="anthropic/claude-sonnet-4-6",
        credential="Anthropic API key",
        error_code="provider_credential_unavailable",
        repair_action="Add an Anthropic API key in Settings > Access.",
        visible_message=(
            "spawn_worker auth blocked: the Anthropic API key is not configured."
        ),
    )

    payload, captured, events = await _captured_spawn_worker(
        monkeypatch,
        parent_model_policy={
            "model": "openai/gpt-5.6-sol",
            "thinking": "high",
        },
        preflight_result=blocked,
        model="anthropic",
        metadata={"shard": "github:Illospace/illospace"},
    )

    from brain.platform.providers.model_policy import DEFAULT_PROVIDER_MODELS

    assert captured["preflight"] == {
        "user_id": "user-1",
        "org_id": "org-1",
        "model": f"anthropic/{DEFAULT_PROVIDER_MODELS['anthropic']}",
    }

    assert payload == {
        "ok": False,
        "status": "auth_blocked",
        "error": blocked.visible_message,
        "error_code": "provider_credential_unavailable",
        "configuration_error": "provider_credential_unavailable",
        "provider": "anthropic",
        "credential": "Anthropic API key",
        "parent_run_id": 42,
        "shard": "github:Illospace/illospace",
        "worker_slot_consumed": False,
    }
    assert "model_policy" not in captured
    assert captured["parent_evidence_health"]["status"] == "degraded"
    assert captured["parent_evidence_health"]["missing_shards"] == [
        "github:Illospace/illospace"
    ]
    assert captured["admission_failures"][0]["stage"] == "worker_admission"
    assert events == []


async def test_neutral_auth_probe_names_missing_anthropic_configuration(
    monkeypatch,
):
    from brain.platform.integrations import provider_auth_preflight

    async def missing_credential(**_kwargs):
        raise RuntimeError(
            "No API key found for anthropic. Add one in Settings."
        )

    monkeypatch.setattr(
        provider_auth_preflight,
        "async_resolve_llm_client",
        missing_credential,
    )

    result = await provider_auth_preflight.async_probe_provider_auth(
        object(),
        user_id="user-1",
        org_id="org-1",
        provider="anthropic",
        model="anthropic/claude-sonnet-4-6",
    )

    assert result.to_dict()["status"] == "auth_blocked"
    assert result.error_code == "provider_credential_unavailable"
    assert result.credential == "Anthropic API key"
    assert result.visible_message is None
    assert result.repair_action is None


async def test_spawn_worker_auth_policy_probes_anthropic_and_owns_wording(
    monkeypatch,
):
    from brain.platform.integrations.provider_auth_preflight import (
        ProviderAuthBlockedPreflightResult,
    )
    import brain.systems.runs.tool_catalog.handlers.workers as worker_handlers

    captured = {}

    async def blocked_probe(_session, **kwargs):
        captured.update(kwargs)
        return ProviderAuthBlockedPreflightResult(
            provider="anthropic",
            model=kwargs["model"],
            credential="Anthropic API key",
            error_code="provider_credential_unavailable",
        )

    monkeypatch.setattr(
        worker_handlers,
        "async_probe_provider_auth",
        blocked_probe,
    )

    result = await worker_handlers._preflight_spawn_worker_auth(
        object(),
        user_id="user-1",
        org_id="org-1",
        model="anthropic/claude-sonnet-4-6",
    )

    assert captured == {
        "user_id": "user-1",
        "org_id": "org-1",
        "provider": "anthropic",
        "model": "anthropic/claude-sonnet-4-6",
    }
    assert isinstance(result, ProviderAuthBlockedPreflightResult)
    assert result.repair_action == (
        "Add an Anthropic API key in Settings > Access, then retry the run."
    )
    assert result.visible_message.startswith(
        "spawn_worker auth blocked: the Anthropic API key is not configured."
    )


async def test_cycle_auth_policy_blocks_anthropic_without_credentials(monkeypatch):
    from brain.systems.cycles import auth_preflight
    from brain.systems.cycles.admission import CycleProviderRoute
    from brain.platform.integrations.provider_auth_preflight import (
        ProviderAuthBlockedPreflightResult,
    )

    probes = []

    async def blocked_probe(*_args, **kwargs):
        probes.append(kwargs)
        return ProviderAuthBlockedPreflightResult(
            provider="anthropic",
            model=kwargs["model"],
            credential="Anthropic API key",
            error_code="provider_credential_unavailable",
        )

    monkeypatch.setattr(
        auth_preflight,
        "async_probe_provider_auth",
        blocked_probe,
    )

    result = await auth_preflight.async_preflight_cycle_external_auth(
        object(),
        route=CycleProviderRoute(
            user_id="user-1",
            org_id="org-1",
            model="anthropic/claude-sonnet-4-6",
        ),
    )

    assert isinstance(result, ProviderAuthBlockedPreflightResult)
    assert result.provider == "anthropic"
    assert probes[0]["provider"] == "anthropic"
    assert "Add an Anthropic API key" in result.visible_message


@pytest.mark.parametrize(
    ("spawn_kwargs", "message"),
    [
        ({"effort": "ultra"}, "effort must be one of"),
        ({"model": "gpt-5.6-sol"}, "model must be a provider name"),
        (
            {"model": "anthropic/not-a-real-model"},
            "model for provider 'anthropic' must be one of",
        ),
        (
            {"model": "ollama/not-a-real-model"},
            "model for provider 'ollama' must be one of",
        ),
        (
            {"model": "openai/gpt-4.1"},
            "requires an OpenAI API key; use the allowed subscription route "
            "'openai/gpt-5.6-sol'",
        ),
    ],
)
async def test_spawn_worker_rejects_invalid_routing_before_creating_child(
    monkeypatch,
    spawn_kwargs,
    message,
):
    import brain.systems.runs.tool_catalog.handlers.workers as worker_handlers

    class _UnexpectedUow:
        async def __aenter__(self):
            raise AssertionError("invalid routing must be rejected before persistence")

        async def __aexit__(self, *_):
            return None

    monkeypatch.setattr(worker_handlers, "UnitOfWork", _UnexpectedUow)

    payload = json.loads(
        await worker_handlers._handle_spawn_worker(
            objective="Inspect the routing contract.",
            **spawn_kwargs,
        )
    )

    assert message in payload["error"]


async def test_spawn_worker_deduplicated_result_echoes_existing_child_route(monkeypatch):
    payload, _captured, events = await _captured_spawn_worker(
        monkeypatch,
        parent_model_policy={
            "model": "openai/gpt-5.6-sol",
            "thinking": "high",
        },
        child_model_policy={
            "model": "anthropic/claude-sonnet-4-6",
            "thinking": "medium",
        },
        created_here=False,
        idempotency_key="stable-verifier",
        effort="low",
    )

    assert payload["deduplicated"] is True
    assert payload["model"] == "anthropic/claude-sonnet-4-6"
    assert payload["effort"] == "medium"
    assert events == []


@pytest.mark.parametrize("created_here", [True, False])
async def test_spawn_worker_handler_uses_child_run_store_path_for_headless(monkeypatch, created_here):
    from brain.systems.runs.domain import RunProfile, RunRecipe
    from brain.systems.runs.execution_context import bind_agent_context
    import brain.systems.runs.tool_catalog.handlers.workers as worker_handlers

    project_context_snapshot = {
        "status": "validated",
        "resources": [{"kind": "repo", "repo": "uwear-ai/uwear-backend"}],
    }
    parent = SimpleNamespace(
        id=42,
        org_id="org-1",
        user_id="user-1",
        root_run_id=42,
        profile=RunProfile.FAST,
        target_ref={
            "kind": "cortex_idea",
            "project_context_snapshot": project_context_snapshot,
        },
        workspace_ref={
            "workspace_root": "/tmp/work",
            "project_context_snapshot": project_context_snapshot,
            "project_runtime_context": {
                "schema_version": 1,
                "project_context_snapshot": project_context_snapshot,
            },
        },
        model_policy={"model": "openai/gpt-5.5"},
    )
    child = SimpleNamespace(id=99, root_run_id=42, recipe=RunRecipe.WORKER)
    events = []
    create_kwargs = {}

    class _Uow:
        session = object()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

    class _Store:
        def __init__(self, session):
            self.session = session

        async def require_run(self, run_id):
            assert run_id == parent.id
            return parent

        async def child_run_for_step(self, parent_id, step_key):
            assert parent_id == parent.id
            assert step_key == "spawn_worker:bug-report"
            return None

        async def create_child_run_with_result(self, parent_arg, **kwargs):
            assert parent_arg is parent
            create_kwargs.update(kwargs)
            return child, created_here

        async def append_event(self, event):
            events.append(event)

    monkeypatch.setattr(worker_handlers, "UnitOfWork", _Uow)
    monkeypatch.setattr(worker_handlers, "AsyncAgentRunStore", _Store)
    _stub_spawn_worker_auth_preflight(monkeypatch, worker_handlers)

    with bind_agent_context(
        run=SimpleNamespace(id=parent.id),
        target_ref={"kind": "cortex_idea", "idea_id": "idea-1"},
        workspace_ref={"allowed_workspaces": ["/tmp/work"]},
    ):
        payload = json.loads(
            await worker_handlers._handle_spawn_worker(
                objective="File the reproducible blocker.",
                role="report_bug",
                headless=True,
                idempotency_key="bug-report",
                tool_policy={"disabled_tools": ["manage_cycle"]},
            )
        )

    assert payload["ok"] is True
    assert payload["child_run_id"] == child.id
    assert payload["deduplicated"] is (not created_here)
    assert payload["next_action"]["repeat_guard"] == {
        "tool": "spawn_worker",
        "same_objective": "do_not_repeat",
    }
    assert "Do not call spawn_worker again" in payload["next_action"]["instruction"]
    assert "headless" in payload["next_action"]["instruction"]
    assert create_kwargs["thread_id"].startswith("headless-worker:42:")
    assert create_kwargs["target_ref"] == {
        "kind": "cortex_idea",
        "idea_id": "idea-1",
        "project_context_snapshot": project_context_snapshot,
    }
    assert create_kwargs["workspace_ref"]["workspace_root"] == "/tmp/work"
    assert create_kwargs["workspace_ref"]["allowed_workspaces"] == ["/tmp/work"]
    assert create_kwargs["workspace_ref"]["project_context_snapshot"] == project_context_snapshot
    assert create_kwargs["workspace_ref"]["project_runtime_context"]["schema_version"] == 1
    assert create_kwargs["metadata"]["headless"] is True
    assert set(create_kwargs["metadata"]["tool_policy"]["disabled_tools"]) >= {
        "manage_cycle",
        "cortex_reply",
        "post_ai_timeline_message",
    }
    assert [event.event_type for event in events] == (
        ["run.worker_spawned"] if created_here else []
    )


async def test_spawn_worker_handler_prefers_runtime_run_id_over_stale_context(monkeypatch):
    from brain.systems.runs.domain import RunProfile, RunRecipe
    from brain.systems.runs.execution_context import bind_agent_context
    import brain.systems.runs.tool_catalog.handlers.workers as worker_handlers

    stale_parent = SimpleNamespace(id=375)
    current_parent = SimpleNamespace(
        id=377,
        org_id="org-1",
        user_id="user-1",
        root_run_id=377,
        profile=RunProfile.FAST,
        target_ref={"kind": "cortex_idea", "idea_id": "idea-1"},
        workspace_ref={},
        model_policy={},
    )
    child = SimpleNamespace(id=378, root_run_id=377, recipe=RunRecipe.WORKER)
    created = {}

    class _Uow:
        session = object()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

    class _Store:
        def __init__(self, session):
            self.session = session

        async def require_run(self, run_id):
            assert run_id == current_parent.id
            return current_parent

        async def child_run_for_step(self, parent_id, step_key):
            assert parent_id == current_parent.id
            return None

        async def create_child_run_with_result(self, parent_arg, **kwargs):
            assert parent_arg is current_parent
            created.update(kwargs)
            return child, True

        async def append_event(self, event):
            assert event.run_id == current_parent.id

    monkeypatch.setattr(worker_handlers, "UnitOfWork", _Uow)
    monkeypatch.setattr(worker_handlers, "AsyncAgentRunStore", _Store)
    _stub_spawn_worker_auth_preflight(monkeypatch, worker_handlers)

    with bind_agent_context(run=stale_parent):
        payload = json.loads(
            await worker_handlers._handle_spawn_worker(
                objective="File the browser status bug.",
                role="report_bug",
                headless=True,
                idempotency_key="bug-report",
                _runtime_run_id=current_parent.id,
            )
        )

    assert payload["ok"] is True
    assert payload["parent_run_id"] == current_parent.id
    assert payload["root_run_id"] == current_parent.root_run_id
    assert created["thread_id"].startswith("headless-worker:377:")


async def test_fast_recipe_passes_project_workspace_registry_to_tools(monkeypatch):
    from brain.systems.runs.recipes.fast import FastRecipe

    captured = {}

    def fake_build_tool_handlers(**kwargs):
        captured["tool_kwargs"] = kwargs
        return {}

    async def fake_invoke(spec):
        captured["spec"] = spec
        return SimpleNamespace(output="ok", success=True, error=None)

    monkeypatch.setattr("brain.systems.runs.recipes.fast.build_agent_tools", lambda role: [])
    monkeypatch.setattr("brain.systems.runs.recipes.fast.build_tool_handlers", fake_build_tool_handlers)
    monkeypatch.setattr("brain.systems.runs.recipes.fast.invoke_direct_agent_async", fake_invoke)

    runtime = _runtime(
        "fast",
        workspace_ref={
            "workspaces": [
                {"name": "/repos/frontend", "path": "/tmp/projects/p1/repos/frontend"},
                {"name": "/reports", "path": "/tmp/projects/p1/reports"},
            ],
        },
    )

    result = await FastRecipe().execute(runtime)

    assert result.status.value == "completed"
    assert captured["spec"].workspace_root == "/tmp/projects/p1/repos/frontend"
    assert captured["tool_kwargs"] == {
        "workspace_root": "/tmp/projects/p1/repos/frontend",
        "allowed_workspaces": [
            {"name": "/repos/frontend", "path": "/tmp/projects/p1/repos/frontend"},
            {"name": "/reports", "path": "/tmp/projects/p1/reports"},
        ],
    }


async def test_runner_delegates_cycle_settlement_for_terminal_run(monkeypatch):
    from brain.systems.runs.cortex import runner
    from brain.systems.cycles import service as cycle_service

    calls = []
    async def fake_finalize(run_id, *, status, error=None):
        calls.append((run_id, status, error))

    monkeypatch.setattr(
        cycle_service,
        "async_finalize_cycle_run_from_run",
        fake_finalize,
    )

    await runner._finalize_cycle_run_if_needed_async(99, status="completed")
    await runner._finalize_cycle_run_if_needed_async(100, status="running")
    await runner._finalize_cycle_run_if_needed_async(101, status="failed", error="boom")

    assert calls == [(99, "completed", None), (101, "failed", "boom")]


def test_runner_prepares_cycle_contract_before_visible_terminal_settlement():
    from brain.systems.runs.cortex import runner

    source = inspect.getsource(runner._process_claimed_run_async)

    assert "async_prepare_cycle_run_visible_finalization" in source
    assert source.index("async_prepare_cycle_run_visible_finalization") < source.index(
        "_settle_terminal_root_run_async"
    )


async def test_runtime_tool_executor_records_public_events_and_redacted_artifact():
    from brain.systems.runs.domain import ArtifactType, EventVisibility
    from brain.systems.runs.tools import AsyncRunToolExecutor, ToolExecution

    runtime = _runtime("worker")
    executor = AsyncRunToolExecutor(runtime.store, stream=runtime.stream)

    result = await executor.execute(
        42,
        ToolExecution(
            name="brain_vault",
            args={"token": "secret-token", "query": "api key"},
            handler=lambda **kwargs: {"secret": "value"},
        ),
        root_run_id=42,
    )

    assert result == {"secret": "value"}
    event_types = [event.event_type for event in runtime.store.events]
    assert "run.activity" in event_types
    assert "run.tool_started" in event_types
    assert "run.tool_completed" in event_types
    completed = next(event for event in runtime.store.events if event.event_type == "run.tool_completed")
    assert completed.visibility == EventVisibility.PUBLIC
    assert completed.payload["tool_name"] == "brain_vault"
    assert completed.payload["args"]["token"] == "[redacted]"
    assert completed.payload["result"] == "[secret redacted]"
    assert runtime.store.artifacts[-1].artifact_type == ArtifactType.COMMAND_OUTPUT
    assert runtime.store.artifacts[-1].text == "[secret redacted]"
    assert _stream_has(runtime.stream.messages, "run.tool_completed", completed.payload | {"run_id": 42})


async def test_runtime_tool_executor_commits_event_boundaries_around_handler():
    from brain.systems.runs.tools import AsyncRunToolExecutor, ToolExecution

    runtime = _runtime("worker")
    observed = []

    async def commit_event_boundary(run_id):
        observed.append(("commit", run_id, runtime.store.events[-1].event_type))

    async def handler(**_kwargs):
        assert observed == [("commit", 42, "run.tool_started")]
        observed.append(("handler", 42))
        return {"ok": True}

    runtime.store.commit_event_boundary = commit_event_boundary
    executor = AsyncRunToolExecutor(runtime.store, stream=runtime.stream)

    result = await executor.execute(
        42,
        ToolExecution(name="spawn_worker", args={"objective": "check"}, handler=handler),
        root_run_id=42,
    )

    assert result == {"ok": True}
    assert observed == [
        ("commit", 42, "run.tool_started"),
        ("handler", 42),
        ("commit", 42, "run.tool_completed"),
    ]


async def test_agent_execution_context_is_isolated_between_parallel_tool_tasks():
    from brain.systems.runs.execution_context import bind_agent_context, current_agent_context

    async def worker(org_id: str, delay: float):
        with bind_agent_context({"org_id": org_id}):
            await asyncio.sleep(delay)
            return getattr(current_agent_context(), "org_id", None)

    assert await asyncio.gather(
        worker("org-a", 0.01),
        worker("org-b", 0.02),
    ) == ["org-a", "org-b"]


async def test_runtime_tool_executor_binds_run_workspace_context_for_handler():
    from brain.systems.runs.execution_context import bind_agent_context, current_agent_context
    from brain.systems.runs.tools import AsyncRunToolExecutor, ToolExecution

    runtime = _runtime("worker")
    runtime.store.runs[42] = replace(runtime.run, user_id=None)
    executor = AsyncRunToolExecutor(runtime.store, stream=runtime.stream)

    def handler(**_kwargs):
        context = current_agent_context()
        metadata = getattr(context, "execution_metadata", {}) or {}
        return {
            "org_id": getattr(context, "org_id", None),
            "user_id": getattr(context, "user_id", None),
            "run_id": getattr(context, "run_id", None),
            "idea_id": getattr(context, "idea_id", None),
            "metadata_org_id": metadata.get("org_id"),
            "metadata_user_id": metadata.get("user_id"),
            "metadata_other": metadata.get("other"),
        }

    with bind_agent_context(
        {
            "org_id": "stale-org",
            "user_id": "stale-user",
            "execution_metadata": {
                "org_id": "stale-org",
                "user_id": "stale-user",
                "other": "keep",
            },
        }
    ):
        result = await executor.execute(
            42,
            ToolExecution(name="manage_slack", args={}, handler=handler),
            root_run_id=42,
        )

    assert result == {
        "org_id": "org-1",
        "user_id": None,
        "run_id": 42,
        "idea_id": "idea-1",
        "metadata_org_id": "org-1",
        "metadata_user_id": None,
        "metadata_other": "keep",
    }


async def test_runtime_tool_executor_fails_when_run_context_cannot_load():
    from brain.systems.runs.tools import AsyncRunToolExecutor, ToolExecution

    runtime = _runtime("worker")
    executor = AsyncRunToolExecutor(runtime.store, stream=runtime.stream)

    async def missing_run(_run_id):
        raise LookupError("run vanished")

    runtime.store.require_run = missing_run

    with pytest.raises(LookupError, match="run vanished"):
        await executor.execute(
            42,
            ToolExecution(name="manage_slack", args={}, handler=lambda: "should not run"),
            root_run_id=42,
        )


async def test_runtime_tool_executor_resolves_secret_env_mount_without_public_value(monkeypatch):
    from brain.systems.runs.tools import AsyncRunToolExecutor, ToolExecution

    secret_value = "ghp-secret-value"
    vault_calls = []

    async def fake_get_secret_record(key, actor_user_id, *, org_id):
        return object() if key == "DataForSeoLogin" else None

    async def fake_runtime_secret_read(key, **kwargs):
        vault_calls.append({"key": key, **kwargs})
        return {"key": key, "value": secret_value}

    monkeypatch.setattr("brain.systems.vault.async_get_secret_record", fake_get_secret_record)
    monkeypatch.setattr("brain.systems.vault.agent_access.read_agent_secret_for_runtime", fake_runtime_secret_read)

    runtime = _runtime("worker")
    executor = AsyncRunToolExecutor(runtime.store, stream=runtime.stream)
    seen = {}

    def handler(**kwargs):
        seen["kwargs"] = kwargs
        return {"exit_code": 0, "stdout": "ok", "stderr": ""}

    result = await executor.execute(
        42,
        ToolExecution(
            name="exec_command",
            args={
                "command": "gh api user",
                "secret_env": {
                    "DATAFORSEO_LOGIN": {
                        "vault_key": "DataForSeoLogin",
                        "reason": "Run a bounded DataForSEO SERP check without exposing the login.",
                    },
                },
            },
            handler=handler,
        ),
        root_run_id=42,
    )

    assert result["exit_code"] == 0
    assert seen["kwargs"]["_resolved_secret_env"] == {"DATAFORSEO_LOGIN": secret_value}
    assert vault_calls == [{
        "key": "DataForSeoLogin",
        "reason": "Run a bounded DataForSEO SERP check without exposing the login.",
        "user_id": "user-1",
        "org_id": "org-1",
        "run_id": 42,
        "idea_id": "idea-1",
        "requested_by": "secret_env_mount",
        "project_slug": None,
        "project_slugs": None,
        "target_registry_id": None,
    }]
    started = next(event for event in runtime.store.events if event.event_type == "run.tool_started")
    assert started.payload["args"]["secret_env"] == "[redacted]"
    public_payload = json.dumps([event.payload for event in runtime.store.events], default=str)
    artifact_payload = json.dumps([artifact.text for artifact in runtime.store.artifacts], default=str)
    assert secret_value not in public_payload
    assert secret_value not in artifact_payload


async def test_runtime_tool_executor_materializes_workspace_tool_auth_for_installed_command(monkeypatch, tmp_path):
    from brain.platform.integrations.openai_codex_auth import OpenAICodexCredential, encode_codex_auth_payload
    from brain.systems.runs.tools import AsyncRunToolExecutor, ToolExecution
    from brain.systems.runtime_settings.schemas import WorkspaceToolBundleRead

    credential_payload = json.dumps(encode_codex_auth_payload(OpenAICodexCredential(
        access_token="access-token-secret",
        refresh_token="refresh-token-secret",
        account_id="acct_123",
        email="reda@uwear.ai",
        expires_at=2_222_222_222,
        last_refresh=1_700_000_000,
        auth_mode="chatgpt",
    )))
    catalog = [
        WorkspaceToolBundleRead(
            id="codex-cli",
            name="Codex CLI",
            description="test bundle",
            version="latest",
            provided_commands=["codex"],
            runtime={
                "auth_profiles": [{
                    "id": "originating-user-openai-codex",
                    "commands": ["codex"],
                    "source": {
                        "type": "provider_connection",
                        "provider": "openai",
                        "credential": "codex_subscription",
                        "scope": "originating_user",
                    },
                    "materialize": {
                        "type": "file",
                        "env": "CODEX_HOME",
                        "path": "auth.json",
                        "format": "codex_auth_json",
                    },
                }],
            },
        )
    ]

    async def fake_resolve_api_key(**kwargs):
        assert kwargs["user_id"] == "user-1"
        assert kwargs["org_id"] == "org-1"
        assert kwargs["provider"] == "openai"
        assert kwargs["auth_mode"] == "chatgpt"
        return credential_payload, "codex_subscription"

    monkeypatch.setattr("brain.systems.runtime_settings.workspace_tools.workspace_tool_catalog", lambda: catalog)
    monkeypatch.setattr("brain.systems.runtime_settings.workspace_tools.installed_workspace_tool_bundle_ids", lambda org_id: ["codex-cli"])
    monkeypatch.setattr("brain.systems.vault.async_resolve_api_key", fake_resolve_api_key)

    runtime = _runtime("worker")
    executor = AsyncRunToolExecutor(runtime.store, stream=runtime.stream)
    seen = {}

    def handler(**kwargs):
        codex_home = kwargs["_resolved_workspace_tool_env"]["CODEX_HOME"]
        auth_path = tmp_path.__class__(codex_home) / "auth.json"
        auth_payload = json.loads(auth_path.read_text(encoding="utf-8"))
        seen["codex_home"] = codex_home
        seen["auth_payload"] = auth_payload
        seen["sensitive_values"] = kwargs["_resolved_workspace_tool_sensitive_values"]
        return {"exit_code": 0, "stdout": "ok", "stderr": ""}

    result = await executor.execute(
        42,
        ToolExecution(
            name="exec_command",
            args={"command": "codex --version"},
            handler=handler,
        ),
        root_run_id=42,
    )

    assert result["exit_code"] == 0
    assert seen["auth_payload"]["auth_mode"] == "chatgpt"
    assert seen["auth_payload"]["last_refresh"] == "2023-11-14T22:13:20Z"
    assert seen["auth_payload"]["tokens"]["account_id"] == "acct_123"
    assert seen["auth_payload"]["tokens"]["expires_at"] == "2040-06-02T03:57:02Z"
    assert "access-token-secret" in seen["sensitive_values"]
    assert not tmp_path.__class__(seen["codex_home"]).exists()
    public_payload = json.dumps([event.payload for event in runtime.store.events], default=str)
    artifact_payload = json.dumps([artifact.text for artifact in runtime.store.artifacts], default=str)
    assert "access-token-secret" not in public_payload
    assert "access-token-secret" not in artifact_payload


async def test_runtime_tool_executor_supports_explicit_workspace_tool_auth_for_script(monkeypatch):
    from brain.systems.runs.tools import AsyncRunToolExecutor, ToolExecution
    from brain.systems.runtime_settings.schemas import WorkspaceToolBundleRead

    catalog = [
        WorkspaceToolBundleRead(
            id="generic-token-tool",
            name="Generic Token Tool",
            description="test bundle",
            provided_commands=["generic-token-tool"],
            runtime={
                "auth_profiles": [{
                    "id": "workspace-api-token",
                    "source": {
                        "type": "provider_connection",
                        "provider": "openai",
                        "credential": "org_api_key",
                        "scope": "workspace",
                    },
                    "materialize": {
                        "type": "env",
                        "name": "GENERIC_TOOL_TOKEN",
                        "format": "raw",
                    },
                }],
            },
        )
    ]

    async def fake_resolve_api_key(**kwargs):
        assert kwargs["auth_mode"] == "api_key"
        return "sk-workspace-secret", "org_main"

    monkeypatch.setattr("brain.systems.runtime_settings.workspace_tools.workspace_tool_catalog", lambda: catalog)
    monkeypatch.setattr("brain.systems.runtime_settings.workspace_tools.installed_workspace_tool_bundle_ids", lambda org_id: [])
    monkeypatch.setattr("brain.systems.vault.async_resolve_api_key", fake_resolve_api_key)

    runtime = _runtime("worker")
    executor = AsyncRunToolExecutor(runtime.store, stream=runtime.stream)
    seen = {}

    def handler(**kwargs):
        seen.update(kwargs)
        return {"exit_code": 0, "stdout": "ok", "stderr": ""}

    await executor.execute(
        42,
        ToolExecution(
            name="run_script",
            args={"script": "print('ok')", "workspace_tool_auth": ["generic-token-tool"]},
            handler=handler,
        ),
        root_run_id=42,
    )

    assert seen["_resolved_workspace_tool_env"] == {"GENERIC_TOOL_TOKEN": "sk-workspace-secret"}
    assert seen["_resolved_workspace_tool_sensitive_values"] == ["sk-workspace-secret"]


async def test_runtime_tool_executor_rejects_secret_env_on_unsupported_tool():
    from brain.systems.runs.tools import AsyncRunToolExecutor, ToolExecution

    runtime = _runtime("worker")
    executor = AsyncRunToolExecutor(runtime.store, stream=runtime.stream)
    called = False

    def handler(**kwargs):
        nonlocal called
        called = True
        return {"ok": True}

    with pytest.raises(ValueError, match="does not support secret_env"):
        await executor.execute(
            42,
            ToolExecution(
                name="read_file",
                args={"path": "README.md", "secret_env": {"GH_TOKEN": {"vault_key": "GITHUB_TOKEN"}}},
                handler=handler,
            ),
            root_run_id=42,
        )

    assert called is False


async def test_runtime_tool_executor_rejects_secret_env_shorthand():
    from brain.systems.runs.tools import AsyncRunToolExecutor, ToolExecution

    runtime = _runtime("worker")
    executor = AsyncRunToolExecutor(runtime.store, stream=runtime.stream)
    called = False

    def handler(**kwargs):
        nonlocal called
        called = True
        return {"exit_code": 0, "stdout": "ok", "stderr": ""}

    with pytest.raises(ValueError, match="must be an object with vault_key"):
        await executor.execute(
            42,
            ToolExecution(
                name="exec_command",
                args={"command": "gh api user", "secret_env": {"GH_TOKEN": "GITHUB_TOKEN"}},
                handler=handler,
            ),
            root_run_id=42,
        )

    assert called is False


async def test_runtime_tool_executor_stops_before_command_when_secret_mount_fails(monkeypatch):
    from brain.systems.runs.tools import AsyncRunToolExecutor, ToolExecution

    async def fake_runtime_secret_read(key, **kwargs):
        return {"error": "Vault grant required before this agent can read the secret", "key_name": key}

    async def fake_secret_record(*_args, **_kwargs):
        return None

    monkeypatch.setattr("brain.systems.vault.agent_access.read_agent_secret_for_runtime", fake_runtime_secret_read)
    monkeypatch.setattr("brain.systems.vault.async_get_secret_record", fake_secret_record)

    runtime = _runtime("worker")
    executor = AsyncRunToolExecutor(runtime.store, stream=runtime.stream)
    called = False

    def handler(**kwargs):
        nonlocal called
        called = True
        return {"exit_code": 0, "stdout": "ok", "stderr": ""}

    with pytest.raises(PermissionError, match="Vault grant required"):
        await executor.execute(
            42,
            ToolExecution(
                name="exec_command",
                args={"command": "gh api user", "secret_env": {"GH_TOKEN": {"vault_key": "GITHUB_TOKEN"}}},
                handler=handler,
            ),
            root_run_id=42,
        )

    assert called is False


async def test_runtime_tool_executor_redacts_brain_vault_reference_from_public_run_event():
    from brain.systems.runs.tools import AsyncRunToolExecutor, ToolExecution

    runtime = _runtime("worker")
    executor = AsyncRunToolExecutor(runtime.store, stream=runtime.stream)

    result = await executor.execute(
        42,
            ToolExecution(
                name="brain_vault",
                args={"key": "OPENAI_API_KEY"},
                handler=lambda **kwargs: {
                    "key": "OPENAI_API_KEY",
                    "secret_ref": "vault:OPENAI_API_KEY",
                    "status": "available",
                },
            ),
        root_run_id=42,
    )

    completed = next(event for event in runtime.store.events if event.event_type == "run.tool_completed")
    assert result["secret_ref"] == "vault:OPENAI_API_KEY"
    assert "value" not in result
    assert completed.payload["result"] == "[secret redacted]"
    assert runtime.store.artifacts[-1].text == completed.payload["result"]


async def test_runtime_tool_executor_blocks_out_of_scope_worker_mutation():
    import pytest

    from brain.systems.runs.tools import AsyncRunToolExecutor, ToolExecution, ToolScope, ToolScopeViolation

    runtime = _runtime("worker")
    executor = AsyncRunToolExecutor(runtime.store, stream=runtime.stream)

    with pytest.raises(ToolScopeViolation):
        await executor.execute(
            42,
            ToolExecution(
                name="write_file",
                args={"path": "src/app.py", "content": "changed"},
                handler=lambda **kwargs: {"ok": True},
            ),
            root_run_id=42,
            scope=ToolScope(allowed_files=("README.md",)),
        )

    failed = next(event for event in runtime.store.events if event.event_type == "run.tool_failed")
    assert failed.payload["tool_name"] == "write_file"
    assert "outside worker scope" in failed.payload["error"] or "needs approval" in failed.payload["error"]
    assert runtime.store.artifacts[-1].payload["status"] == "failed"


async def test_runtime_tool_executor_records_policy_blocked_results_as_failed():
    from brain.systems.runs.tools import AsyncRunToolExecutor, ToolExecution

    runtime = _runtime("worker")
    executor = AsyncRunToolExecutor(runtime.store, stream=runtime.stream)
    collector = []

    result = await executor.execute(
        42,
        ToolExecution(
            name="exec_command",
            args={"command": "git reset --hard"},
            handler=lambda **kwargs: {
                "ok": False,
                "blocked": True,
                "error": "Action denied by policy",
                "policy_result": "deny",
                "policy_mode": "enforce",
            },
        ),
        root_run_id=42,
        collector=collector,
    )

    assert result["blocked"] is True
    assert collector[-1].status == "failed"
    failed = next(event for event in runtime.store.events if event.event_type == "run.tool_failed")
    assert failed.payload["tool_name"] == "exec_command"
    assert "Action denied by policy" in failed.payload["error"]
    assert runtime.store.artifacts[-1].title == "exec_command blocked"
    assert runtime.store.artifacts[-1].payload["status"] == "failed"


async def test_runtime_tool_executor_classifies_structured_handler_errors_as_failed():
    from brain.systems.runs.failures import DEFAULT_FAILED_RUN_MESSAGE
    from brain.systems.runs.presentation import public_tool_event_payload
    from brain.systems.runs.tools import AsyncRunToolExecutor, ToolExecution

    raw_diagnostic = "idea update failed token=tool-handler-secret"
    runtime = _runtime("worker")
    executor = AsyncRunToolExecutor(runtime.store, stream=runtime.stream)

    result = await executor.execute(
        42,
        ToolExecution(
            name="manage_idea",
            args={"action": "update"},
            handler=lambda **_kwargs: {"status": "error", "error": raw_diagnostic},
        ),
        root_run_id=42,
    )

    assert result["error"] == raw_diagnostic
    assert not any(event.event_type == "run.tool_completed" for event in runtime.store.events)
    failed = next(event for event in runtime.store.events if event.event_type == "run.tool_failed")
    public = public_tool_event_payload(failed.payload, failed.event_type)
    assert public["failure"]["message"] == DEFAULT_FAILED_RUN_MESSAGE
    assert public["error"] == DEFAULT_FAILED_RUN_MESSAGE
    assert raw_diagnostic not in json.dumps(public)
    assert runtime.store.artifacts[-1].payload["status"] == "failed"


async def test_runtime_tool_executor_enforces_policy_before_raw_handler(monkeypatch):
    from brain.systems.runs.tools import AsyncRunToolExecutor, ToolExecution

    monkeypatch.setenv("AGENT_ACTION_POLICY_MODE", "enforce")
    records = []
    completions = []
    monkeypatch.setattr(
        "brain.systems.runs.tools.record_action_manifest",
        lambda manifest: records.append(manifest.to_db_values()) or len(records),
    )
    monkeypatch.setattr(
        "brain.systems.runs.tools.complete_action_manifest",
        lambda manifest_id, **kwargs: completions.append({"manifest_id": manifest_id, **kwargs}),
    )
    runtime = _runtime("worker")
    executor = AsyncRunToolExecutor(runtime.store, stream=runtime.stream)
    calls = []

    result = await executor.execute(
        42,
        ToolExecution(
            name="exec_command",
            args={"command": "git reset --hard"},
            handler=lambda **kwargs: calls.append(kwargs) or {"exit_code": 0},
        ),
        root_run_id=42,
    )

    assert calls == []
    assert result["blocked"] is True
    assert result["policy_result"] == "deny"
    assert records[0]["policy_result"] == "deny"
    assert completions == [{
        "manifest_id": 1,
        "outcome_status": "failed",
        "outcome_error": result["error"],
    }]


async def test_runtime_tool_executor_audits_high_risk_actions_autonomously(monkeypatch):
    from brain.systems.runs.actions import wrap_action_manifest_audit
    from brain.systems.runs.tools import AsyncRunToolExecutor, ToolExecution

    monkeypatch.setenv("AGENT_ACTION_POLICY_MODE", "enforce")
    records = []
    completions = []
    monkeypatch.setattr(
        "brain.systems.runs.tools.record_action_manifest",
        lambda manifest: records.append(manifest.to_db_values()) or len(records),
    )
    monkeypatch.setattr(
        "brain.systems.runs.tools.complete_action_manifest",
        lambda manifest_id, **kwargs: completions.append({"manifest_id": manifest_id, **kwargs}),
    )
    runtime = _runtime("worker")
    executor = AsyncRunToolExecutor(runtime.store, stream=runtime.stream)
    calls = []
    audited_handler = wrap_action_manifest_audit(
        "browser",
        lambda **kwargs: calls.append(kwargs) or {"clicked": True},
        context_factory=lambda: {"run_id": 42, "org_id": "org-1", "idea_id": "idea-1"},
    )
    tool = ToolExecution(
        name="browser",
        args={"action": "click", "selector": "#ship"},
        handler=audited_handler,
    )

    result = await executor.execute(42, tool, root_run_id=42)

    assert result == {"clicked": True}
    assert calls == [{"action": "click", "selector": "#ship"}]
    assert len(records) == 1
    assert records[0]["policy_result"] == "allow_audit"
    assert records[0]["approval_required"] is False
    assert records[0]["approval_requirement"] == "not_required_autonomous_policy"
    assert completions == [{"manifest_id": 1, "outcome_status": "succeeded", "outcome_error": None}]


async def test_runtime_tool_executor_offloads_sync_handlers():
    import time

    from brain.systems.runs.tools import AsyncRunToolExecutor, ToolExecution

    runtime = _runtime("worker")
    executor = AsyncRunToolExecutor(runtime.store, stream=runtime.stream)
    stop_ticker = asyncio.Event()
    ticker_count = 0

    async def ticker():
        nonlocal ticker_count
        while not stop_ticker.is_set():
            ticker_count += 1
            await asyncio.sleep(0.01)

    def blocking_handler(**kwargs):
        time.sleep(0.1)
        return {"ok": True, **kwargs}

    ticker_task = asyncio.create_task(ticker())
    try:
        result = await executor.execute(
            42,
            ToolExecution(name="read_file", args={"path": "README.md"}, handler=blocking_handler),
            root_run_id=42,
        )
    finally:
        stop_ticker.set()
        await ticker_task

    assert result == {"ok": True, "path": "README.md"}
    assert ticker_count >= 3


@pytest.mark.asyncio
async def test_direct_loop_returns_timeout_as_tool_error(monkeypatch):
    from brain.systems.runs.direct_loop.tool_execution import PendingToolCall, async_resolve_tool_call

    monkeypatch.setenv("AGENT_TOOL_TIMEOUT_SECONDS", "0.02")

    async def slow_handler():
        await asyncio.sleep(0.2)
        return {"ok": True}

    resolved = await async_resolve_tool_call(PendingToolCall("tool-1", "slow_tool", {}, slow_handler))

    assert resolved.outcome.failure is not None
    assert "slow_tool" in resolved.result_text
    assert "timed out" in resolved.result_text


@pytest.mark.asyncio
async def test_direct_loop_cancels_production_async_adapter_at_timeout(monkeypatch):
    import brain.systems.runs.tool_handlers as tool_handlers
    from brain.platform.async_io import callable_uses_blocking_thread
    from brain.systems.runs.direct_loop.tool_execution import PendingToolCall, async_resolve_tool_call
    from brain.systems.runs.tool_catalog.handlers.composition import _get_tool_handlers

    monkeypatch.setenv("AGENT_TOOL_TIMEOUT_SECONDS", "0.02")
    canceled = asyncio.Event()

    async def slow_manage_cycle(**_kwargs):
        try:
            await asyncio.sleep(1)
        except asyncio.CancelledError:
            canceled.set()
            raise

    monkeypatch.setattr(tool_handlers, "_handle_manage_cycle", slow_manage_cycle)
    handler = _get_tool_handlers()["manage_cycle"]

    assert callable_uses_blocking_thread(handler) is False
    resolved = await async_resolve_tool_call(
        PendingToolCall("tool-1", "manage_cycle", {"action": "list"}, handler)
    )

    assert resolved.outcome.failure is not None
    assert "timed out" in resolved.result_text
    assert canceled.is_set()


@pytest.mark.asyncio
async def test_direct_loop_detects_unmarked_sync_adapter_returning_coroutine(monkeypatch):
    from brain.systems.runs.direct_loop.tool_execution import PendingToolCall, async_resolve_tool_call

    monkeypatch.setenv("AGENT_TOOL_TIMEOUT_SECONDS", "0.02")
    canceled = asyncio.Event()

    def unmarked_adapter():
        async def inner():
            try:
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                canceled.set()
                raise

        return inner()

    resolved = await async_resolve_tool_call(
        PendingToolCall("tool-1", "unregistered_read", {}, unmarked_adapter)
    )

    assert resolved.outcome.failure is not None
    assert "timed out" in resolved.result_text
    assert canceled.is_set()


@pytest.mark.asyncio
async def test_direct_loop_caller_cancellation_cancels_pure_async_read(monkeypatch):
    import brain.systems.runs.direct_loop.tool_execution as tool_execution

    started = asyncio.Event()
    canceled = asyncio.Event()

    async def never_finishes():
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            canceled.set()
            raise

    task = asyncio.create_task(
        tool_execution.async_resolve_tool_call(
            tool_execution.PendingToolCall("tool-1", "unregistered_read", {}, never_finishes)
        )
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0)

    assert canceled.is_set()
    assert not tool_execution._background_tool_tasks


@pytest.mark.asyncio
async def test_repeated_cancellation_still_drains_submitted_sync_handler():
    import threading
    import time

    from brain.platform.async_io import invoke_maybe_async

    started = threading.Event()
    finished = threading.Event()

    def slow_handler():
        started.set()
        time.sleep(0.08)
        finished.set()
        return {"ok": True}

    task = asyncio.create_task(invoke_maybe_async(slow_handler))
    while not started.is_set():
        await asyncio.sleep(0.001)
    task.cancel()
    await asyncio.sleep(0.005)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert finished.is_set()


@pytest.mark.asyncio
async def test_cancellation_before_sync_tool_admission_never_runs_handler(monkeypatch):
    from brain.platform.async_io import invoke_maybe_async

    class SaturatedAdmission:
        def acquire(self, *, blocking):
            assert blocking is False
            return False

    called = False

    def handler():
        nonlocal called
        called = True

    monkeypatch.setenv("AGENT_SYNC_TOOL_ADMISSION_TIMEOUT_SECONDS", "1")
    monkeypatch.setattr(
        "brain.platform.async_io._TOOL_ADMISSION",
        SaturatedAdmission(),
    )
    task = asyncio.create_task(invoke_maybe_async(handler))
    await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert called is False


@pytest.mark.asyncio
async def test_cancelled_queued_sync_handler_never_starts(monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    import brain.platform.async_io as async_io

    executor = ThreadPoolExecutor(max_workers=1)
    admission = threading.BoundedSemaphore(2)
    first_started = threading.Event()
    release_first = threading.Event()
    second_called = threading.Event()

    def occupy_worker():
        first_started.set()
        assert release_first.wait(timeout=1)

    def queued_handler():
        second_called.set()

    monkeypatch.setattr(async_io, "_TOOL_EXECUTOR", executor)
    monkeypatch.setattr(async_io, "_TOOL_ADMISSION", admission)
    first = asyncio.create_task(async_io.invoke_maybe_async(occupy_worker))
    second = None
    try:
        while not first_started.is_set():
            await asyncio.sleep(0.001)
        second = asyncio.create_task(async_io.invoke_maybe_async(queued_handler))
        await asyncio.sleep(0.02)
        second.cancel()
        with pytest.raises(asyncio.CancelledError):
            await second
        release_first.set()
        await first
        await asyncio.sleep(0.02)
    finally:
        release_first.set()
        if not first.done():
            await first
        executor.shutdown(wait=True)

    assert not second_called.is_set()


@pytest.mark.asyncio
async def test_direct_loop_cancels_mutation_before_sync_admission(monkeypatch):
    from brain.systems.runs.direct_loop.tool_execution import PendingToolCall, async_resolve_tool_call

    class SaturatedAdmission:
        def acquire(self, *, blocking):
            assert blocking is False
            return False

    called = False

    def handler(action):
        nonlocal called
        assert action == "create"
        called = True
        return {"ok": True}

    monkeypatch.setenv("AGENT_TOOL_TIMEOUT_SECONDS", "1")
    monkeypatch.setenv("AGENT_SYNC_TOOL_ADMISSION_TIMEOUT_SECONDS", "1")
    monkeypatch.setattr("brain.platform.async_io._TOOL_ADMISSION", SaturatedAdmission())
    task = asyncio.create_task(
        async_resolve_tool_call(
            PendingToolCall("tool-1", "manage_cycle", {"action": "create"}, handler)
        )
    )
    await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.02)

    assert called is False


@pytest.mark.asyncio
async def test_direct_loop_cancels_later_queued_mutation_after_completed_blocking_phase(monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    import brain.platform.async_io as async_io
    from brain.systems.runs.direct_loop.tool_execution import PendingToolCall, async_resolve_tool_call

    executor = ThreadPoolExecutor(max_workers=1)
    admission = threading.BoundedSemaphore(2)
    preflight_done = asyncio.Event()
    allow_mutation = asyncio.Event()
    occupier_started = threading.Event()
    release_occupier = threading.Event()
    mutation_called = threading.Event()

    def occupy_worker():
        occupier_started.set()
        assert release_occupier.wait(timeout=1)

    def mutate():
        mutation_called.set()
        return {"ok": True}

    async def handler(action):
        assert action == "create"
        await async_io.run_tool_blocking(lambda: "preflight")
        preflight_done.set()
        await allow_mutation.wait()
        return await async_io.run_tool_blocking(mutate)

    monkeypatch.setattr(async_io, "_TOOL_EXECUTOR", executor)
    monkeypatch.setattr(async_io, "_TOOL_ADMISSION", admission)
    resolver = asyncio.create_task(
        async_resolve_tool_call(
            PendingToolCall("tool-1", "manage_cycle", {"action": "create"}, handler)
        )
    )
    occupier = None
    try:
        await preflight_done.wait()
        occupier = asyncio.create_task(async_io.invoke_maybe_async(occupy_worker))
        while not occupier_started.is_set():
            await asyncio.sleep(0.001)
        allow_mutation.set()
        await asyncio.sleep(0.02)
        resolver.cancel()
        with pytest.raises(asyncio.CancelledError):
            await resolver
        release_occupier.set()
        await occupier
        await asyncio.sleep(0.02)
    finally:
        release_occupier.set()
        if occupier is not None and not occupier.done():
            await occupier
        executor.shutdown(wait=True)

    assert not mutation_called.is_set()


@pytest.mark.asyncio
async def test_direct_loop_cancels_mutation_during_async_audit_preflight(monkeypatch):
    from brain.systems.runs.actions import wrap_action_manifest_audit
    from brain.systems.runs.direct_loop.tool_execution import PendingToolCall, async_resolve_tool_call

    preflight_started = asyncio.Event()
    preflight_canceled = asyncio.Event()
    mutation_called = False

    async def record_manifest(_manifest):
        preflight_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            preflight_canceled.set()
            raise

    async def mutation(path, content):
        nonlocal mutation_called
        mutation_called = True
        return {"path": path, "bytes": len(content)}

    wrapped = wrap_action_manifest_audit(
        "write_file",
        mutation,
        context_factory=lambda: {"run_id": 42, "org_id": "org-1"},
    )
    monkeypatch.setattr("brain.systems.runs.actions.record_action_manifest", record_manifest)
    task = asyncio.create_task(
        async_resolve_tool_call(
            PendingToolCall(
                "tool-1",
                "write_file",
                {"path": "note.txt", "content": "hello"},
                wrapped,
            )
        )
    )
    await preflight_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0)

    assert preflight_canceled.is_set()
    assert mutation_called is False


@pytest.mark.asyncio
async def test_direct_loop_watchdog_tracks_public_blocking_helper(monkeypatch):
    import threading
    import time

    from brain.platform.async_io import run_tool_blocking
    from brain.systems.runs.direct_loop.tool_execution import PendingToolCall, async_resolve_tool_call

    monkeypatch.setenv("AGENT_TOOL_TIMEOUT_SECONDS", "0.02")
    finished = threading.Event()

    def slow_read():
        time.sleep(0.12)
        finished.set()
        return {"ok": True}

    async def handler():
        return await run_tool_blocking(slow_read)

    started_at = time.monotonic()
    resolved = await async_resolve_tool_call(
        PendingToolCall("tool-1", "unregistered_read", {}, handler)
    )
    elapsed = time.monotonic() - started_at

    assert resolved.outcome.failure is not None
    assert "timed out" in resolved.result_text
    assert elapsed < 0.08
    assert not finished.is_set()
    assert finished.wait(timeout=1)


@pytest.mark.asyncio
async def test_direct_loop_waits_for_sync_action_before_reporting_outcome(monkeypatch):
    import time

    from brain.systems.runs.actions import wrap_action_manifest_audit
    from brain.systems.runs.direct_loop.tool_execution import PendingToolCall, async_resolve_tool_call

    monkeypatch.setenv("AGENT_TOOL_TIMEOUT_SECONDS", "0.02")
    records = []
    completions = []
    completed = asyncio.Event()

    def record(manifest):
        records.append(manifest.to_db_values())
        return 1

    def complete(manifest_id, **kwargs):
        completions.append({"manifest_id": manifest_id, **kwargs})
        completed.set()

    def slow_handler(action):
        assert action == "create"
        time.sleep(0.1)
        return {"ok": True}

    wrapped = wrap_action_manifest_audit(
        "manage_cycle",
        slow_handler,
        context_factory=lambda: {"run_id": 42, "org_id": "org-1"},
    )
    monkeypatch.setattr("brain.systems.runs.actions.record_action_manifest", record)
    monkeypatch.setattr("brain.systems.runs.actions.complete_action_manifest", complete)

    resolved = await async_resolve_tool_call(
        PendingToolCall("tool-1", "manage_cycle", {"action": "create"}, wrapped)
    )

    assert resolved.outcome.failure is None
    assert json.loads(resolved.result_text) == {"ok": True}
    assert completed.is_set()
    assert len(records) == 1
    assert completions == [{"manifest_id": 1, "outcome_status": "succeeded", "outcome_error": None}]


@pytest.mark.asyncio
async def test_direct_loop_waits_for_async_mutation_before_reporting_outcome(monkeypatch):
    from brain.systems.runs.actions import wrap_action_manifest_audit
    from brain.systems.runs.direct_loop.tool_execution import PendingToolCall, async_resolve_tool_call

    monkeypatch.setenv("AGENT_TOOL_TIMEOUT_SECONDS", "0.02")
    completions = []

    async def slow_mutation(action):
        assert action == "create"
        await asyncio.sleep(0.05)
        return {"ok": True}

    wrapped = wrap_action_manifest_audit(
        "manage_cycle",
        slow_mutation,
        context_factory=lambda: {"run_id": 42, "org_id": "org-1"},
    )
    monkeypatch.setattr("brain.systems.runs.actions.record_action_manifest", lambda _manifest: 1)
    monkeypatch.setattr(
        "brain.systems.runs.actions.complete_action_manifest",
        lambda manifest_id, **kwargs: completions.append({"manifest_id": manifest_id, **kwargs}),
    )

    resolved = await async_resolve_tool_call(
        PendingToolCall("tool-1", "manage_cycle", {"action": "create"}, wrapped)
    )

    assert resolved.outcome.failure is None
    assert json.loads(resolved.result_text) == {"ok": True}
    assert completions == [{"manifest_id": 1, "outcome_status": "succeeded", "outcome_error": None}]


@pytest.mark.asyncio
async def test_caller_cancellation_after_watchdog_does_not_abort_async_mutation(monkeypatch):
    from brain.systems.runs.direct_loop.tool_execution import PendingToolCall, async_resolve_tool_call

    monkeypatch.setenv("AGENT_TOOL_TIMEOUT_SECONDS", "0.02")
    started = asyncio.Event()
    completed = asyncio.Event()
    canceled = asyncio.Event()

    async def mutation(action):
        assert action == "create"
        started.set()
        try:
            await asyncio.sleep(0.1)
            completed.set()
            return {"ok": True}
        except asyncio.CancelledError:
            canceled.set()
            raise

    task = asyncio.create_task(
        async_resolve_tool_call(
            PendingToolCall("tool-1", "manage_cycle", {"action": "create"}, mutation)
        )
    )
    await started.wait()
    await asyncio.sleep(0.04)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.wait_for(completed.wait(), timeout=1)
    await asyncio.sleep(0)

    assert not canceled.is_set()


@pytest.mark.asyncio
async def test_sync_action_cancellation_drains_handler_and_audit(monkeypatch):
    import threading
    import time

    from brain.systems.runs.actions import wrap_action_manifest_audit

    started = threading.Event()
    finished = threading.Event()
    completions = []

    def slow_mutation(action):
        assert action == "create"
        started.set()
        time.sleep(0.08)
        finished.set()
        return {"ok": True}

    wrapped = wrap_action_manifest_audit(
        "manage_cycle",
        slow_mutation,
        context_factory=lambda: {"run_id": 42, "org_id": "org-1"},
    )
    monkeypatch.setattr("brain.systems.runs.actions.record_action_manifest", lambda _manifest: 1)
    monkeypatch.setattr(
        "brain.systems.runs.actions.complete_action_manifest",
        lambda manifest_id, **kwargs: completions.append({"manifest_id": manifest_id, **kwargs}),
    )

    task = asyncio.create_task(wrapped(action="create"))
    while not started.is_set():
        await asyncio.sleep(0.001)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert finished.is_set()
    assert completions == [{"manifest_id": 1, "outcome_status": "succeeded", "outcome_error": None}]


@pytest.mark.asyncio
async def test_runtime_tool_cleanup_waits_for_canceled_sync_handler(monkeypatch):
    import threading
    import time

    from brain.systems.runs.tools import AsyncRunToolExecutor, ToolExecution
    from brain.systems.runs.workspace_tool_runtime import WorkspaceToolRuntimeMaterialization

    started = threading.Event()
    finished = threading.Event()
    cleaned = []
    materialization = WorkspaceToolRuntimeMaterialization()
    materialization.cleanup = lambda: cleaned.append(finished.is_set())

    async def fake_materialization(*_args, **_kwargs):
        return materialization

    def slow_handler(**_kwargs):
        started.set()
        time.sleep(0.08)
        finished.set()
        return {"ok": True}

    monkeypatch.setattr(
        "brain.systems.runs.tools.resolve_workspace_tool_runtime",
        fake_materialization,
    )
    monkeypatch.setattr("brain.systems.runs.tools.record_action_manifest", lambda _manifest: None)
    runtime = _runtime("worker")
    task = asyncio.create_task(
        AsyncRunToolExecutor(runtime.store).execute(
            42,
            ToolExecution(name="exec_command", args={"command": "echo ok"}, handler=slow_handler),
            root_run_id=42,
        )
    )
    while not started.is_set():
        await asyncio.sleep(0.001)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert finished.is_set()
    assert cleaned == [True]
    assert [
        event.event_type
        for event in runtime.store.events
        if event.event_type in {"run.tool_completed", "run.tool_failed"}
    ] == ["run.tool_completed"]


@pytest.mark.asyncio
async def test_sync_tool_executor_rejects_overload_without_using_default_pool(monkeypatch):
    from brain.platform.async_io import ToolExecutorOverloaded, invoke_maybe_async

    class SaturatedAdmission:
        def acquire(self, *, blocking):
            assert blocking is False
            return False

    monkeypatch.setenv("AGENT_SYNC_TOOL_ADMISSION_TIMEOUT_SECONDS", "0.01")
    monkeypatch.setattr(
        "brain.platform.async_io._TOOL_ADMISSION",
        SaturatedAdmission(),
    )

    with pytest.raises(ToolExecutorOverloaded, match="capacity is saturated"):
        await invoke_maybe_async(lambda: {"ok": True})


@pytest.mark.asyncio
async def test_production_file_summary_handler_keeps_event_loop_responsive(monkeypatch):
    import time

    import brain.systems.tools.handlers as extended_handlers
    from brain.systems.runs.tool_catalog.handlers.composition import _get_tool_handlers

    def slow_file_summary(path, workspace_root=None):
        time.sleep(0.08)
        return {"path": path, "workspace_root": workspace_root}

    monkeypatch.setattr(extended_handlers, "handle_file_summary", slow_file_summary)
    handler = _get_tool_handlers(workspace_root="/tmp/work")["file_summary"]
    ticker_count = 0
    stop_ticker = asyncio.Event()

    async def ticker():
        nonlocal ticker_count
        while not stop_ticker.is_set():
            ticker_count += 1
            await asyncio.sleep(0.005)

    ticker_task = asyncio.create_task(ticker())
    try:
        result = await handler(path="README.md")
    finally:
        stop_ticker.set()
        await ticker_task

    assert result == {"path": "README.md", "workspace_root": "/tmp/work"}
    assert ticker_count >= 5


async def test_worker_recipe_invokes_direct_agent_with_runtime_tools_and_worker_result_artifact(monkeypatch):
    from brain.systems.runs.domain import ArtifactType
    from brain.systems.runs.recipes.workers import WorkerRecipe

    captured = {}

    async def fake_invoke(spec):
        captured["spec"] = spec
        assert spec.run_id == 42
        assert spec.idea_id is None
        assert spec.workspace_root == "/tmp/work"
        assert "Inspect README setup steps" in spec.system_prompt
        await spec.on_stream_activity("Inspecting README.md")
        read_result = await spec.tool_handlers["read_file"](path="README.md")
        assert read_result["content"] == "setup steps"
        await spec.on_stream_delta("Found setup steps")
        return SimpleNamespace(
            output="README setup documented",
            success=True,
            error=None,
            effective_routing={
                "model": "anthropic/claude-sonnet-4-6",
                "effort": "xhigh",
                "provider": "anthropic",
                "auth_mode": "api_key",
            },
        )

    monkeypatch.setattr("brain.systems.runs.recipes.workers.build_agent_tools", lambda role: [{"name": "read_file"}])
    monkeypatch.setattr(
        "brain.systems.runs.recipes.workers.build_tool_handlers",
        lambda **kwargs: {"read_file": lambda **tool_args: {"content": "setup steps", "path": tool_args["path"]}},
    )
    monkeypatch.setattr("brain.systems.runs.recipes.workers.invoke_direct_agent_async", fake_invoke)
    monkeypatch.setattr(
        "brain.systems.runs.recipes.workers.async_summarize_run_usage_in_savepoint",
        lambda *_args, **_kwargs: _AwaitableValue(
            {
                "api_calls": 2,
                "tokens_input": 1_200,
                "tokens_output": 300,
                "tokens_total": 1_500,
                "cache_read": 800,
                "cache_write": 0,
                "estimated_cost": 0.009,
                "by_effort": [
                    {
                        "effort": "high",
                        "api_calls": 2,
                        "tokens_input": 1_200,
                        "tokens_output": 300,
                        "tokens_total": 1_500,
                        "cache_read": 800,
                        "cache_write": 0,
                        "estimated_cost": 0.009,
                    }
                ],
            }
        ),
    )

    runtime = _runtime("worker")
    result = await WorkerRecipe().execute(runtime)

    assert result.output == "README setup documented"
    assert result.status.value == "completed"
    assert _stream_has(runtime.stream.messages, "run.text_delta", {"delta": "Found setup steps", "run_id": 42})
    assert any(event.event_type == "run.tool_started" for event in runtime.store.events)
    assert any(event.event_type == "run.tool_completed" for event in runtime.store.events)
    assert any(artifact.artifact_type == ArtifactType.FILE_OBSERVATION for artifact in runtime.store.artifacts)
    worker_result = result.artifacts[0]
    assert worker_result.artifact_type == ArtifactType.WORKER_RESULT
    assert worker_result.payload["scope"]["allowed_files"] == ["README.md"]
    assert worker_result.payload["evidence"]["tool_names"] == ["read_file"]
    assert runtime.store.metadata_updates == [
        (
            42,
            {
                "routing": {
                    "model": "anthropic/claude-sonnet-4-6",
                    "effort": "xhigh",
                    "effective": {
                        "model": "anthropic/claude-sonnet-4-6",
                        "effort": "xhigh",
                        "provider": "anthropic",
                        "auth_mode": "api_key",
                    },
                }
            },
        )
    ]
    assert worker_result.payload["routing"] == {
        "model": "anthropic/claude-sonnet-4-6",
        "effort": "xhigh",
        "provider": "anthropic",
        "auth_mode": "api_key",
    }
    assert worker_result.payload["usage"]["tokens_total"] == 1_500
    assert worker_result.payload["usage"]["estimated_cost"] == 0.009
    assert worker_result.payload["usage"]["by_effort"][0]["effort"] == "high"


async def test_worker_recipe_keeps_failed_provider_diagnostics_internal(monkeypatch):
    from brain.systems.runs.failures import UPSTREAM_FAILED_RUN_MESSAGE
    from brain.systems.runs.recipes.workers import WorkerRecipe

    raw_error = "peer closed connection without sending complete message body"

    async def fake_invoke(_spec):
        return SimpleNamespace(output="", success=False, error=raw_error)

    monkeypatch.setattr("brain.systems.runs.recipes.workers.build_agent_tools", lambda _role: [])
    monkeypatch.setattr("brain.systems.runs.recipes.workers.build_tool_handlers", lambda **_kwargs: {})
    monkeypatch.setattr("brain.systems.runs.recipes.workers.invoke_direct_agent_async", fake_invoke)

    runtime = _runtime("worker")
    result = await WorkerRecipe().execute(runtime)

    assert result.status.value == "failed"
    assert result.output == ""
    assert result.error == raw_error
    assert result.final_output == UPSTREAM_FAILED_RUN_MESSAGE
    assert result.artifacts[0].text == UPSTREAM_FAILED_RUN_MESSAGE
    assert result.artifacts[0].payload["failure"] == {
        "status": "failed",
        "category": "upstream",
        "message": UPSTREAM_FAILED_RUN_MESSAGE,
    }
    assert raw_error not in json.dumps(runtime.stream.messages)
    assert raw_error not in json.dumps(result.artifacts[0].payload)


async def test_worker_recipe_treats_exhausted_provider_text_retry_as_upstream_failure(
    monkeypatch,
):
    from brain.systems.runs.failures import UPSTREAM_FAILED_RUN_MESSAGE
    from brain.systems.runs.recipes.workers import WorkerRecipe

    async def fake_invoke(_spec):
        return SimpleNamespace(
            output="upstream_provider_error: overloaded_error",
            success=True,
            error=None,
        )

    monkeypatch.setattr(
        "brain.systems.runs.recipes.workers.build_agent_tools",
        lambda _role: [],
    )
    monkeypatch.setattr(
        "brain.systems.runs.recipes.workers.build_tool_handlers",
        lambda **_kwargs: {},
    )
    monkeypatch.setattr(
        "brain.systems.runs.recipes.workers.invoke_direct_agent_async",
        fake_invoke,
    )

    result = await WorkerRecipe().execute(_runtime("worker"))

    assert result.status.value == "failed"
    assert result.error == "upstream_provider_error: overloaded_error"
    assert result.final_output == UPSTREAM_FAILED_RUN_MESSAGE
    assert result.artifacts[0].payload["failure"]["category"] == "upstream"


async def test_worker_recipe_buffers_partial_deltas_until_success_is_known(monkeypatch):
    from brain.systems.runs.failures import UPSTREAM_FAILED_RUN_MESSAGE
    from brain.systems.runs.recipes.workers import WorkerRecipe
    from brain.systems.runs.status import RunStatus

    raw_delta = "partial provider diagnostic token=stream-secret"
    raw_error = "peer closed connection without sending complete message body"

    async def fake_invoke(spec):
        await spec.on_stream_delta(raw_delta)
        return SimpleNamespace(output=raw_delta, success=False, error=raw_error)

    monkeypatch.setattr("brain.systems.runs.recipes.workers.build_agent_tools", lambda _role: [])
    monkeypatch.setattr("brain.systems.runs.recipes.workers.build_tool_handlers", lambda **_kwargs: {})
    monkeypatch.setattr("brain.systems.runs.recipes.workers.invoke_direct_agent_async", fake_invoke)

    runtime = _runtime("worker")
    result = await WorkerRecipe().execute(runtime)
    streamed = json.dumps(runtime.stream.messages)

    assert result.status == RunStatus.FAILED
    assert result.error == raw_error
    assert raw_delta not in streamed
    assert raw_error not in streamed
    assert any(
        event_type == "run.text_delta"
        and payload.get("delta") == UPSTREAM_FAILED_RUN_MESSAGE
        for event_type, payload in runtime.stream.messages
    )


async def test_worker_recipe_keeps_unexpected_exception_internal(monkeypatch):
    from brain.systems.runs.failures import DEFAULT_FAILED_RUN_MESSAGE
    from brain.systems.runs.recipes.workers import WorkerRecipe

    raw_error = "provider exploded with private diagnostic"

    calls = 0

    async def fake_invoke(_spec):
        nonlocal calls
        calls += 1
        raise RuntimeError(raw_error)

    monkeypatch.setattr("brain.systems.runs.recipes.workers.build_agent_tools", lambda _role: [])
    monkeypatch.setattr("brain.systems.runs.recipes.workers.build_tool_handlers", lambda **_kwargs: {})
    monkeypatch.setattr("brain.systems.runs.recipes.workers.invoke_direct_agent_async", fake_invoke)

    runtime = _runtime("worker")
    result = await WorkerRecipe().execute(runtime)

    assert result.error == raw_error
    assert result.final_output == DEFAULT_FAILED_RUN_MESSAGE
    assert result.artifacts[0].text == DEFAULT_FAILED_RUN_MESSAGE
    assert raw_error not in json.dumps(runtime.stream.messages)
    assert raw_error not in json.dumps(result.artifacts[0].payload)
    assert calls == 1


async def test_worker_recipe_propagates_headless_tool_policy(monkeypatch):
    from brain.systems.runs.recipes.workers import WorkerRecipe

    captured = {}

    async def fake_invoke(spec):
        captured["spec"] = spec
        return SimpleNamespace(output="queued report", success=True, error=None)

    monkeypatch.setattr("brain.systems.runs.recipes.workers.build_agent_tools", lambda role: [{"name": "read_file"}])
    monkeypatch.setattr("brain.systems.runs.recipes.workers.build_tool_handlers", lambda **kwargs: {})
    monkeypatch.setattr("brain.systems.runs.recipes.workers.invoke_direct_agent_async", fake_invoke)

    runtime = _runtime("worker")
    runtime.request = replace(
        runtime.request,
        metadata={
            **runtime.request.metadata,
            "headless": True,
            "tool_policy": {"blocked_tools": ["post_chat_message"]},
        },
    )

    result = await WorkerRecipe().execute(runtime)

    assert result.status.value == "completed"
    assert "Response Surface and Delegation" in captured["spec"].system_prompt
    assert "visible delegated run completes" in captured["spec"].system_prompt
    assert captured["spec"].metadata["headless"] is True
    assert captured["spec"].metadata["tool_policy"] == {"blocked_tools": ["post_chat_message"]}


async def test_worker_recipe_infers_workspace_root_from_project_context_permission_scope(monkeypatch):
    from brain.systems.runs.recipes.workers import WorkerRecipe

    captured = {}

    async def fake_invoke(spec):
        captured["spec"] = spec
        return SimpleNamespace(output="ok", success=True, error=None)

    monkeypatch.setattr("brain.systems.runs.recipes.workers.build_agent_tools", lambda role: [])
    monkeypatch.setattr("brain.systems.runs.recipes.workers.build_tool_handlers", lambda **kwargs: {})
    monkeypatch.setattr("brain.systems.runs.recipes.workers.invoke_direct_agent_async", fake_invoke)

    runtime = _runtime(
        "worker",
        workspace_ref={
            "project_context_permission_scope": {
                "allowed_paths": ["/tmp/ideas/idea-1/project/repo"],
            },
        },
    )

    result = await WorkerRecipe().execute(runtime)

    assert result.status.value == "completed"
    assert captured["spec"].workspace_root == "/tmp/ideas/idea-1/project/repo"


async def test_worker_recipe_passes_project_workspace_registry_to_tools(monkeypatch):
    from brain.systems.runs.recipes.workers import WorkerRecipe

    captured = {}

    def fake_build_tool_handlers(**kwargs):
        captured["tool_kwargs"] = kwargs
        return {}

    async def fake_invoke(spec):
        captured["spec"] = spec
        return SimpleNamespace(output="ok", success=True, error=None)

    monkeypatch.setattr("brain.systems.runs.recipes.workers.build_agent_tools", lambda role: [])
    monkeypatch.setattr("brain.systems.runs.recipes.workers.build_tool_handlers", fake_build_tool_handlers)
    monkeypatch.setattr("brain.systems.runs.recipes.workers.invoke_direct_agent_async", fake_invoke)

    runtime = _runtime(
        "worker",
        workspace_ref={
            "workspaces": [
                {"name": "/repos/frontend", "path": "/tmp/projects/p1/repos/frontend"},
                {"name": "/reports", "path": "/tmp/projects/p1/reports"},
            ],
        },
    )

    result = await WorkerRecipe().execute(runtime)

    assert result.status.value == "completed"
    assert captured["spec"].workspace_root == "/tmp/projects/p1/repos/frontend"
    assert captured["tool_kwargs"] == {
        "workspace_root": "/tmp/projects/p1/repos/frontend",
        "allowed_workspaces": [
            {"name": "/repos/frontend", "path": "/tmp/projects/p1/repos/frontend"},
            {"name": "/reports", "path": "/tmp/projects/p1/reports"},
        ],
    }


def test_run_stream_payload_is_the_single_cortex_projection():
    from brain.systems.runs.cortex.read_models import run_stream_payload

    now = datetime(2026, 5, 3, tzinfo=timezone.utc)
    row = SimpleNamespace(
        id=7,
        thread_id="idea-1",
        org_id="org-1",
        user_id="user-1",
        parent_run_id=None,
        root_run_id=7,
        trace_id="run_7",
        profile="fast",
        recipe="fast",
        status="running",
        input_message="Read README",
        target_ref={"kind": "cortex_idea"},
        workspace_ref={"workspace_root": "/tmp/work"},
        model_policy={"model": "openai/gpt-5.5", "thinking": "high"},
        metadata_={"event": "thread_reply"},
        created_at=now,
        updated_at=now,
        started_at=now,
        paused_at=None,
        completed_at=None,
        failed_at=None,
        canceled_at=None,
    )

    payload = run_stream_payload(row)

    assert payload["run_id"] == 7
    assert payload["timestamp"] == "2026-05-03T00:00:00+00:00"
    assert payload["idea_id"] == "idea-1"
    assert payload["profile"] == "fast"
    assert payload["recipe"] == "fast"
    assert payload["status"] == "running"
    assert payload["model_policy"] == {"model": "openai/gpt-5.5", "thinking": "high"}


def test_completed_run_stream_scrubs_embedded_subfailure_diagnostics_only():
    from brain.systems.runs.cortex.read_models import run_stream_payload

    now = datetime(2026, 5, 3, tzinfo=timezone.utc)
    raw_diagnostic = "child clone failed token=super-secret"
    row = SimpleNamespace(
        id=8,
        thread_id="idea-1",
        org_id="org-1",
        user_id="user-1",
        parent_run_id=None,
        root_run_id=8,
        trace_id="run_8",
        profile="deep",
        recipe="deep",
        status="completed",
        input_message="Inspect the project",
        target_ref={"kind": "cortex_idea"},
        workspace_ref={},
        model_policy={},
        metadata_={
            "safe_summary": "Completed with partial evidence",
            "evidence_health": {
                "status": "degraded",
                "failures": [
                    {"status": "failed", "error": raw_diagnostic, "resource_id": "repo-1"}
                ],
            },
        },
        created_at=now,
        updated_at=now,
        started_at=now,
        paused_at=None,
        completed_at=now,
        failed_at=None,
        canceled_at=None,
    )

    payload = run_stream_payload(row)

    assert payload["status"] == "completed"
    assert payload["metadata"]["safe_summary"] == "Completed with partial evidence"
    assert payload["metadata"]["evidence_health"]["failures"] == [
        {"status": "failed", "resource_id": "repo-1"}
    ]
    assert raw_diagnostic not in json.dumps(payload)


def _async_work_intake_session(idea, *, attachment=None, profiles=None):
    class _Session:
        def __init__(self):
            self.idea = idea
            self.profiles = profiles or {}

        async def get(self, model, identity):
            if getattr(model, "__name__", "") == "ProjectProfile":
                return self.profiles.get(str(identity))
            return self.idea

        async def scalars(self, stmt):
            return SimpleNamespace(first=lambda: attachment)

    return _Session()


async def _build_cortex_intake_run_request(
    session,
    *,
    idea_id: str,
    event: str,
    message: str,
    user_id: str | None,
    metadata: dict | None = None,
    priority: int = 0,
    source: str = "cortex",
    producer: str | None = None,
    idempotency_key: str | None = None,
):
    from brain.systems.runs.work_intake import WorkIntakeEvent, build_agent_run_request

    idea = getattr(session, "idea", None)
    org_id = str(getattr(idea, "org_id", "") or "")
    actor = {"id": user_id, "org_id": org_id, "internal": False} if user_id else None
    policy = {"priority": priority, "run_event": event}
    if producer is not None:
        policy["producer"] = producer
    if idempotency_key is not None:
        policy["idempotency_key"] = idempotency_key
    return await build_agent_run_request(
        session,
        WorkIntakeEvent(
            source=source,
            event_type=f"cortex.{event}",
            org_id=org_id,
            actor=actor,
            target={"kind": "cortex_idea", "idea_id": idea_id},
            payload={"message": message, "metadata": metadata or {}},
            policy=policy,
        ),
    )


async def test_work_intake_defers_unspecified_effort_to_workspace_default():

    session = _async_work_intake_session(SimpleNamespace(id="idea-1", org_id="org-1", user_id="u1", title="Thread"))

    request = await _build_cortex_intake_run_request(
        session,
        idea_id="idea-1",
        event="thread_reply",
        message="What is in the README?",
        user_id="u1",
        metadata={"run_profile": "fast"},
    )

    assert request.profile == "fast"
    assert request.recipe == "fast"
    assert request.model_policy == {}
    assert request.metadata["event"] == "thread_reply"


async def test_work_intake_prefers_normalized_actor_over_idea_owner():
    session = _async_work_intake_session(
        SimpleNamespace(id="idea-1", org_id="org-1", user_id="u1", title="Thread")
    )

    request = await _build_cortex_intake_run_request(
        session,
        idea_id="idea-1",
        event="idea_created",
        message="Build a workspace app",
        user_id="u1",
    )

    assert request.user_id == "u1"


async def test_work_intake_records_slash_skill_interest():
    session = _async_work_intake_session(SimpleNamespace(id="idea-1", org_id="org-1", user_id="u1", title="Thread"))

    request = await _build_cortex_intake_run_request(
        session,
        idea_id="idea-1",
        event="thread_reply",
        message="what does /debug do? also inspect /api/foo",
        user_id="u1",
        metadata={"run_profile": "fast"},
    )

    assert request.metadata["slash_skill_names"] == ["debug"]
    assert request.metadata["slash_skill_commands"][0]["token"] == "/debug"


async def test_work_intake_inherits_project_context_from_idea():
    idea = SimpleNamespace(
        id="idea-1",
        org_id="org-1",
        user_id="u1",
        title="Thread",
        agent_details={
            "project_context": {
                "name": "YC Application",
                "resources": [{"type": "folder", "path": "projects/yc-application"}],
            },
        },
    )
    session = _async_work_intake_session(idea)

    request = await _build_cortex_intake_run_request(
        session,
        idea_id="idea-1",
        event="thread_reply",
        message="Keep going",
        user_id="u1",
        metadata={"run_profile": "fast"},
    )

    assert request.metadata["project_context"]["name"] == "YC Application"
    assert request.target_ref["project_context_snapshot"]["resources"][0]["path"] == "projects/yc-application"
    assert request.workspace_ref["project_context_snapshot"]["resources"][0]["path"] == "projects/yc-application"


async def test_work_intake_prefers_empty_metadata_project_context_over_idea_context():
    idea = SimpleNamespace(
        id="idea-1",
        org_id="org-1",
        user_id="u1",
        title="Thread",
        agent_details={
            "project_context": {
                "name": "Illospace",
                "resources": [{"type": "repo", "name": "Illospace/illospace"}],
            },
        },
    )
    session = _async_work_intake_session(idea)

    request = await _build_cortex_intake_run_request(
        session,
        idea_id="idea-1",
        event="thread_reply",
        message="Try again",
        user_id="u1",
        metadata={"project_context": {"name": "Stale empty project", "resources": []}},
    )

    assert request.metadata["project_context"]["name"] == "Stale empty project"
    assert request.target_ref["project_context_snapshot"]["resources"] == []
    assert "project_context_validation_errors" not in request.metadata


async def test_work_intake_keeps_empty_legacy_project_context_when_no_resource_fallback():
    idea = SimpleNamespace(
        id="idea-1",
        org_id="org-1",
        user_id="u1",
        title="Thread",
        agent_details={"project_context": {"name": "Legacy empty project", "resources": []}},
    )
    session = _async_work_intake_session(idea)

    request = await _build_cortex_intake_run_request(
        session,
        idea_id="idea-1",
        event="thread_reply",
        message="Try again",
        user_id="u1",
        metadata={},
    )

    assert request.metadata["project_context"]["name"] == "Legacy empty project"
    assert request.target_ref["project_context_snapshot"]["resources"] == []
    assert request.workspace_ref["project_context_snapshot"]["resources"] == []
    assert "project_context_validation_errors" not in request.metadata


async def test_work_intake_falls_back_to_latest_project_attachment():
    idea = SimpleNamespace(id="idea-1", org_id="org-1", user_id="u1", title="Thread", agent_details={})
    attachment = SimpleNamespace(
        snapshot={
            "name": "Attached Project",
            "resources": [{"type": "folder", "path": "attached/project"}],
        },
    )
    session = _async_work_intake_session(idea, attachment=attachment)

    request = await _build_cortex_intake_run_request(
        session,
        idea_id="idea-1",
        event="thread_reply",
        message="Continue in the project",
        user_id="u1",
        metadata={},
    )

    assert request.metadata["project_context"]["name"] == "Attached Project"
    assert request.target_ref["project_context_snapshot"]["resources"][0]["path"] == "attached/project"


async def test_work_intake_resolves_attached_profile_to_latest_project_root():
    idea = SimpleNamespace(
        id="idea-1",
        org_id="org-1",
        user_id="u1",
        title="Thread",
        agent_details={
            "project_context": {
                "name": "Stale attached snapshot",
                "resources": [{"type": "folder", "path": "old/root"}],
            },
        },
    )
    attachment = SimpleNamespace(
        project_profile_id="project-1",
        snapshot={
            "name": "Attached Project",
            "resources": [{"type": "folder", "path": "attached/old"}],
        },
    )
    profile = SimpleNamespace(
        active=True,
        project_context={
            "name": "Attached Project",
            "resources": [{"type": "folder", "path": "attached/latest"}],
        },
    )

    session = _async_work_intake_session(idea, attachment=attachment, profiles={"project-1": profile})

    request = await _build_cortex_intake_run_request(
        session,
        idea_id="idea-1",
        event="thread_reply",
        message="Continue in the project",
        user_id="u1",
        metadata={},
    )

    assert request.metadata["project_context"]["resources"][0]["path"] == "attached/latest"
    assert request.target_ref["project_context_snapshot"]["resources"][0]["path"] == "attached/latest"


async def test_work_intake_applies_model_and_effort_overrides():
    session = _async_work_intake_session(SimpleNamespace(id="idea-1", org_id="org-1", user_id="u1", title="Thread"))

    request = await _build_cortex_intake_run_request(
        session,
        idea_id="idea-1",
        event="thread_reply",
        message="Use cheaper settings",
        user_id="u1",
        metadata={"execution_profile": "fast", "model": "openai/gpt-5.6-sol", "effort": "xhigh"},
    )

    assert request.profile == "fast"
    assert request.recipe == "fast"
    assert request.model_policy == {"model": "openai/gpt-5.6-sol", "thinking": "xhigh"}


async def test_work_intake_applies_explicit_model_override():
    session = _async_work_intake_session(SimpleNamespace(id="idea-1", org_id="org-1", user_id="u1", title="Thread"))

    request = await _build_cortex_intake_run_request(
        session,
        idea_id="idea-1",
        event="idea_created",
        message="Introduce yourself",
        user_id="u1",
        metadata={"execution_profile": "fast", "provider": "openai", "model": "openai/gpt-5.5"},
    )

    assert request.model_policy == {
        "model": "openai/gpt-5.5",
        "provider": "openai",
    }


async def test_work_intake_preserves_explicit_none_effort():
    session = _async_work_intake_session(SimpleNamespace(id="idea-1", org_id="org-1", user_id="u1", title="Thread"))

    request = await _build_cortex_intake_run_request(
        session,
        idea_id="idea-1",
        event="thread_reply",
        message="Use no extra reasoning",
        user_id="u1",
        metadata={"execution_profile": "fast", "effort": "none"},
    )

    assert request.model_policy == {"thinking": "none"}


async def test_runner_settles_root_run_idea_status():
    from brain.systems.runs.cortex.runner import _settle_idea_for_terminal_root_run_async
    from brain.platform.db.models.agent_run import AgentRunRow
    from brain.platform.db.models.idea import Idea, IdeaStateLog, IdeaThread

    idea_id = str(uuid.uuid4())
    run = SimpleNamespace(id=42, parent_run_id=None, thread_id=idea_id, status="completed")
    idea = SimpleNamespace(id=idea_id, status="active", updated_at=None)
    final_artifact = SimpleNamespace(
        id=99,
        run_id=42,
        artifact_type="final_answer",
        text="Done. I created the teammate handoffs.",
    )
    added = []
    scalar_calls = 0

    class FakeSession:
        async def get(self, model, key):
            if model is AgentRunRow and int(key) == 42:
                return run
            if model is Idea and str(key) == idea_id:
                return idea
            return None

        def add(self, obj):
            added.append(obj)

        async def scalars(self, stmt):
            nonlocal scalar_calls
            scalar_calls += 1
            if scalar_calls == 1:
                return _ScalarRows([final_artifact])
            return _ScalarRows([])

        async def flush(self):
            pass

    payload = await _settle_idea_for_terminal_root_run_async(FakeSession(), 42)

    assert idea.status == "unread_reply"
    assert payload == {
        "idea_id": idea_id,
        "old_status": "active",
        "new_status": "unread_reply",
        "run_id": 42,
    }
    assert any(isinstance(obj, IdeaStateLog) for obj in added)
    response = next(obj for obj in added if isinstance(obj, IdeaThread))
    assert response.idea_id == idea_id
    assert response.role == "illo"
    assert response.message_type == "agent_response"
    assert response.content == "Done. I created the teammate handoffs."
    assert response.metadata_ == {
        "run_id": 42,
        "artifact_id": 99,
        "source": "agent_run_final_answer",
    }


async def test_runner_does_not_duplicate_final_answer_thread_message():
    from brain.systems.runs.cortex.runner import _settle_idea_for_terminal_root_run_async
    from brain.platform.db.models.agent_run import AgentRunRow
    from brain.platform.db.models.idea import Idea, IdeaThread

    idea_id = str(uuid.uuid4())
    run = SimpleNamespace(id=45, parent_run_id=None, thread_id=idea_id, status="completed")
    idea = SimpleNamespace(id=idea_id, status="unread_reply", updated_at=None)
    final_artifact = SimpleNamespace(id=100, run_id=45, artifact_type="final_answer", text="Already visible")
    existing_message = SimpleNamespace(message_type="agent_response", metadata_={"run_id": 45})
    added = []
    scalar_calls = 0

    class FakeSession:
        async def get(self, model, key):
            if model is AgentRunRow and int(key) == 45:
                return run
            if model is Idea and str(key) == idea_id:
                return idea
            return None

        def add(self, obj):
            added.append(obj)

        async def scalars(self, stmt):
            nonlocal scalar_calls
            scalar_calls += 1
            if scalar_calls == 1:
                return _ScalarRows([final_artifact])
            return _ScalarRows([existing_message])

        async def flush(self):
            pass

    assert await _settle_idea_for_terminal_root_run_async(FakeSession(), 45) is None
    assert not any(isinstance(obj, IdeaThread) for obj in added)


async def test_runner_reports_slack_origin_final_answer_back_to_slack(monkeypatch):
    from brain.systems.runs.cortex import runner

    run = SimpleNamespace(
        id=77,
        parent_run_id=None,
        thread_id=str(uuid.uuid4()),
        status="completed",
        org_id="org-1",
        user_id="user-1",
        target_ref={
            "kind": "slack_message",
            "slack_trigger": {
                "channel_id": "C456",
                "channel_type": "channel",
                "message_ts": "1716900000.000100",
                "thread_ts": "1716900000.000100",
                "response_target": {
                    "channel_id": "C456",
                    "thread_ts": None,
                    "visibility": "public",
                },
            },
        },
        metadata_={"final_answer_target_surface": "slack"},
    )
    final_artifact = SimpleNamespace(
        id=101,
        run_id=77,
        artifact_type="final_answer",
        text="Done. I checked the package list and posted the findings.",
    )
    calls = []

    class FakeSession:
        def __init__(self):
            self.scalar_calls = 0

        async def scalars(self, stmt):
            del stmt
            self.scalar_calls += 1
            if self.scalar_calls == 1:
                return _ScalarRows([final_artifact])
            return _ScalarRows([])

    class FakeSlackClient:
        async def post_message(self, **kwargs):
            calls.append(("message", kwargs))
            return {"ok": True, "channel": kwargs["channel"], "ts": "1716900400.000500"}

        async def set_assistant_status(self, **kwargs):
            calls.append(("status", kwargs))
            return {"ok": True}

    async def fake_client_for_run(run_arg):
        assert run_arg is run
        return FakeSlackClient()

    monkeypatch.setattr(slack_delivery, "slack_client_for_run", fake_client_for_run)

    result = await runner._settle_slack_origin_run_async(FakeSession(), run)

    assert result["surface"] == "slack"
    assert result["run_id"] == 77
    assert result["artifact_id"] == 101
    assert calls == [
        (
            "message",
            {
                "channel": "C456",
                "text": "Done. I checked the package list and posted the findings.",
                "thread_ts": None,
            },
        ),
        (
            "status",
            {
                "channel_id": "C456",
                "thread_ts": "1716900000.000100",
                "status": "",
            },
        ),
    ]


async def test_runner_reports_interruption_run_id_and_requeue_status_to_slack(monkeypatch):
    from brain.systems.runs import interruption
    from brain.systems.runs.failures import DEFAULT_FAILED_RUN_MESSAGE

    run = SimpleNamespace(
        id=2330,
        parent_run_id=None,
        thread_id="slack:T789:C456:1716900000.000100",
        status="queued",
        org_id="org-1",
        user_id="user-1",
        target_ref={
            "kind": "slack_message",
            "slack_trigger": {
                "channel_id": "C456",
                "channel_type": "channel",
                "message_ts": "1716900000.000100",
                "response_target": {"channel_id": "C456", "thread_ts": None},
            },
        },
        metadata_={"final_answer_target_surface": "slack"},
    )
    calls = []

    class FakeSlackClient:
        async def post_message(self, **kwargs):
            calls.append(kwargs)
            return {"ok": True, "channel": kwargs["channel"], "ts": "1716900400.000500"}

        async def set_assistant_status(self, **_kwargs):
            return {"ok": True}

    async def fake_client_for_run(run_arg):
        assert run_arg is run
        return FakeSlackClient()

    monkeypatch.setattr(slack_delivery, "slack_client_for_run", fake_client_for_run)
    deferral_recorder = AsyncMock(
        return_value=(
            SimpleNamespace(),
            SimpleNamespace(id=1, state="pending"),
            True,
        )
    )
    monkeypatch.setattr(
        "brain.systems.runs.run_deferrals.record_run_deferral",
        deferral_recorder,
    )
    monkeypatch.setattr(
        "brain.systems.runs.obligation_notices.schedule_post_commit_notice_delivery",
        lambda *_args, **_kwargs: False,
    )

    class FakeSession:
        async def get(self, _model, run_id):
            assert run_id == 2330
            return run

    class FakeUnitOfWork:
        session = FakeSession()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc_info):
            return None

    monkeypatch.setattr(interruption, "UnitOfWork", FakeUnitOfWork)
    result = await interruption.notify_run_interruption(
        interruption.RunInterruption(
            run_id=2330,
            reason="worker_shutdown_drain_timeout",
            interrupted_at=datetime(2026, 7, 22, 17, 55, tzinfo=timezone.utc),
            requeued=True,
        )
    )

    assert result["surface"] == "slack"
    assert result["queued"] is True
    assert calls == []
    deferral_text = deferral_recorder.await_args.kwargs["deferral_text"]
    assert "I've re-queued it and will reply here when it finishes." in deferral_text
    assert DEFAULT_FAILED_RUN_MESSAGE not in deferral_text


async def test_runner_does_not_override_model_authored_slack_reply(monkeypatch):
    from brain.systems.runs.cortex import runner

    run = SimpleNamespace(
        id=78,
        parent_run_id=None,
        thread_id=str(uuid.uuid4()),
        status="completed",
        org_id="org-1",
        user_id="user-1",
        target_ref={
            "kind": "slack_message",
            "slack_trigger": {
                "channel_id": "C456",
                "channel_type": "channel",
                "message_ts": "1716900000.000100",
                "thread_ts": "1716900000.000100",
                "response_target": {"channel_id": "C456", "thread_ts": None},
            },
        },
        metadata_={"final_answer_target_surface": "slack"},
    )
    final_artifact = SimpleNamespace(
        id=102,
        run_id=78,
        artifact_type="final_answer",
        text="Done. I checked the package list and posted the findings.",
    )
    posted_event = SimpleNamespace(
        payload={
            "tool_name": "post_slack_reply",
            "args": {"body": "I started a Cortex thread for this and will report back there."},
            "result": '{"ok": true, "ts": "1716900001.000200"}',
        }
    )

    class FakeSession:
        def __init__(self):
            self.scalar_calls = 0

        async def scalars(self, stmt):
            del stmt
            self.scalar_calls += 1
            if self.scalar_calls == 1:
                return _ScalarRows([final_artifact])
            return _ScalarRows([posted_event])

    async def fail_client_for_run(_run):
        raise AssertionError("Slack client should not be requested after post_slack_reply")

    monkeypatch.setattr(slack_delivery, "slack_client_for_run", fail_client_for_run)

    assert await runner._settle_slack_origin_run_async(FakeSession(), run) is None


async def test_runner_does_not_post_text_after_model_authored_slack_reaction(monkeypatch):
    from brain.systems.runs.cortex import runner

    run = SimpleNamespace(
        id=781,
        parent_run_id=None,
        thread_id=str(uuid.uuid4()),
        status="completed",
        org_id="org-1",
        user_id="user-1",
        target_ref={
            "kind": "slack_message",
            "slack_trigger": {
                "channel_id": "C456",
                "channel_type": "channel",
                "message_ts": "1716900000.000100",
                "thread_ts": "1716900000.000100",
                "response_target": {"channel_id": "C456", "thread_ts": None},
            },
        },
        metadata_={"final_answer_target_surface": "slack"},
    )
    final_artifact = SimpleNamespace(
        id=1021,
        run_id=781,
        artifact_type="final_answer",
        text="Thanks acknowledged.",
    )
    reaction_event = SimpleNamespace(
        payload={
            "tool_name": "react_to_slack_message",
            "args": {"emoji": "thumbsup"},
            "result": (
                '{"ok": true, "emoji": "thumbsup", '
                '"counts_as_visible_response": true}'
            ),
        }
    )

    class FakeSession:
        def __init__(self):
            self.scalar_calls = 0

        async def scalars(self, stmt):
            del stmt
            self.scalar_calls += 1
            if self.scalar_calls == 1:
                return _ScalarRows([final_artifact])
            return _ScalarRows([reaction_event])

    async def fail_client_for_run(_run):
        raise AssertionError("Slack client should not post text after a successful reaction")

    monkeypatch.setattr(slack_delivery, "slack_client_for_run", fail_client_for_run)

    assert await runner._settle_slack_origin_run_async(FakeSession(), run) is None


@pytest.mark.parametrize(
    "result",
    [
        '{"ok": false, "error": "missing_scope"}',
        '{"ok": true, "emoji": "thumbsup"}',
    ],
)
async def test_runner_does_not_count_failed_or_unscoped_reaction_as_visible(result):
    from brain.systems.runs.cortex import runner

    event = SimpleNamespace(
        payload={
            "tool_name": "react_to_slack_message",
            "result": result,
        }
    )

    class FakeSession:
        async def scalars(self, _stmt):
            return _ScalarRows([event])

    run = SimpleNamespace(id=782)

    assert await runner._slack_visible_action_already_recorded(
        FakeSession(),
        run=run,
    ) is False


async def test_runner_reports_non_headless_slack_child_final_answer_back_to_slack(monkeypatch):
    from brain.systems.runs.cortex import runner

    run = SimpleNamespace(
        id=79,
        parent_run_id=78,
        root_run_id=78,
        thread_id="slack:T789:C456:1716900000.000100",
        status="completed",
        org_id="org-1",
        user_id="user-1",
        target_ref={
            "kind": "slack_message",
            "slack_trigger": {
                "channel_id": "C456",
                "channel_type": "channel",
                "message_ts": "1716900000.000100",
                "thread_ts": "1716900000.000100",
                "response_target": {"channel_id": "C456", "thread_ts": "1716900000.000100"},
            },
        },
        metadata_={"final_answer_target_surface": "slack", "headless": False},
    )
    final_artifact = SimpleNamespace(
        id=103,
        run_id=79,
        artifact_type="final_answer",
        text="Worker finished: the affected package was not present in the scanned manifests.",
    )
    calls = []

    class FakeSession:
        def __init__(self):
            self.scalar_calls = 0

        async def get(self, model, key):
            del model, key
            return run

        async def scalars(self, stmt):
            del stmt
            self.scalar_calls += 1
            if self.scalar_calls == 1:
                return _ScalarRows([final_artifact])
            return _ScalarRows([])

    class FakeSlackClient:
        async def post_message(self, **kwargs):
            calls.append(("message", kwargs))
            return {"ok": True, "channel": kwargs["channel"], "ts": "1716900400.000500"}

        async def set_assistant_status(self, **kwargs):
            calls.append(("status", kwargs))
            return {"ok": True}

    async def fake_client_for_run(run_arg):
        assert run_arg is run
        return FakeSlackClient()

    monkeypatch.setattr(slack_delivery, "slack_client_for_run", fake_client_for_run)

    result = await runner._settle_terminal_root_run_async(FakeSession(), 79)

    assert result["surface"] == "slack"
    assert result["run_id"] == 79
    assert calls[0] == (
        "message",
        {
            "channel": "C456",
            "text": "Worker finished: the affected package was not present in the scanned manifests.",
            "thread_ts": "1716900000.000100",
        },
    )


async def test_runner_replaces_failed_run_artifact_with_typed_safe_message(monkeypatch):
    from brain.systems.runs.cortex import runner
    from brain.systems.runs.failures import UPSTREAM_FAILED_RUN_MESSAGE

    raw_error = (
        "peer closed connection without sending complete message body "
        "(incomplete chunked read)"
    )
    run = SimpleNamespace(
        id=80,
        parent_run_id=None,
        root_run_id=80,
        thread_id="slack:T789:C456:1716900000.000100",
        status="failed",
        org_id="org-1",
        user_id="user-1",
        target_ref={
            "kind": "slack_message",
            "slack_trigger": {
                "channel_id": "C456",
                "channel_type": "channel",
                "message_ts": "1716900000.000100",
                "thread_ts": "1716900000.000100",
                "response_target": {
                    "channel_id": "C456",
                    "thread_ts": "1716900000.000100",
                },
            },
        },
        metadata_={
            "final_answer_target_surface": "slack",
            "failure": {"category": "upstream"},
        },
    )
    raw_final_artifact = SimpleNamespace(
        id=104,
        run_id=80,
        artifact_type="final_answer",
        text=raw_error,
    )
    calls = []

    class FakeSession:
        def __init__(self):
            self.scalar_calls = 0

        async def scalars(self, _stmt):
            self.scalar_calls += 1
            return _ScalarRows([raw_final_artifact])

    class FakeSlackClient:
        async def post_message(self, **kwargs):
            calls.append(kwargs)
            return {"ok": True, "channel": kwargs["channel"], "ts": "1716900400.000500"}

        async def set_assistant_status(self, **_kwargs):
            return {"ok": True}

    async def fake_client_for_run(run_arg):
        assert run_arg is run
        return FakeSlackClient()

    monkeypatch.setattr(slack_delivery, "slack_client_for_run", fake_client_for_run)
    deferral_recorder = AsyncMock(
        return_value=(
            SimpleNamespace(),
            SimpleNamespace(id=2, state="pending"),
            True,
        )
    )
    monkeypatch.setattr(
        "brain.systems.runs.run_deferrals.record_run_deferral",
        deferral_recorder,
    )
    monkeypatch.setattr(
        "brain.systems.runs.obligation_notices.schedule_post_commit_notice_delivery",
        lambda *_args, **_kwargs: False,
    )

    session = FakeSession()
    result = await runner._settle_slack_origin_run_async(session, run)

    assert result["artifact_id"] is None
    assert session.scalar_calls == 0
    assert result["queued"] is True
    assert calls == []
    notice_text = deferral_recorder.await_args.kwargs["deferral_text"]
    assert notice_text == UPSTREAM_FAILED_RUN_MESSAGE
    assert raw_error not in notice_text


async def test_runner_keeps_headless_slack_child_silent(monkeypatch):
    from brain.systems.runs.cortex import runner

    run = SimpleNamespace(
        id=80,
        parent_run_id=78,
        root_run_id=78,
        thread_id="headless-worker:78:abc",
        status="completed",
        org_id="org-1",
        user_id="user-1",
        target_ref={
            "kind": "slack_message",
            "slack_trigger": {
                "channel_id": "C456",
                "response_target": {"channel_id": "C456", "thread_ts": "1716900000.000100"},
            },
        },
        metadata_={"final_answer_target_surface": "slack", "headless": True},
    )

    async def fail_client_for_run(_run):
        raise AssertionError("Headless child should not request a Slack client")

    monkeypatch.setattr(slack_delivery, "slack_client_for_run", fail_client_for_run)

    assert await runner._settle_slack_origin_run_async(object(), run) is None


async def test_runner_posts_typed_failure_for_headless_slack_monitor(monkeypatch):
    from brain.systems.runs.cortex import runner
    from brain.systems.runs.failures import UPSTREAM_FAILED_RUN_MESSAGE

    run = SimpleNamespace(
        id=81,
        parent_run_id=None,
        root_run_id=81,
        thread_id="slack:T789:C456:1716900000.000100",
        status="failed",
        org_id="org-1",
        user_id="user-1",
        target_ref={
            "kind": "slack_message",
            "slack_trigger": {
                "channel_id": "C456",
                "channel_type": "channel",
                "message_ts": "1716900000.000100",
                "thread_ts": "1716900000.000100",
                "response_target": {
                    "channel_id": "C456",
                    "thread_ts": "1716900000.000100",
                },
            },
        },
        metadata_={
            "origin": "slack_channel_monitor",
            "originating_surface": "slack",
            "final_answer_target_surface": "headless",
            "headless": True,
            "failure": {"category": "upstream"},
        },
    )
    final_artifact = SimpleNamespace(
        id=104,
        run_id=81,
        artifact_type="final_answer",
        text="peer closed connection without sending complete message body (incomplete chunked read)",
    )
    earlier_progress_reply = SimpleNamespace(
        payload={
            "tool_name": "post_slack_reply",
            "args": {"body": "I’m checking that now."},
            "result": '{"ok": true, "ts": "1716900001.000200"}',
        }
    )
    calls = []

    class FakeSession:
        def __init__(self):
            self.scalar_calls = 0

        async def scalars(self, _stmt):
            self.scalar_calls += 1
            if self.scalar_calls == 1:
                return _ScalarRows([final_artifact])
            return _ScalarRows([earlier_progress_reply])

    class FakeSlackClient:
        async def post_message(self, **kwargs):
            calls.append(kwargs)
            return {"ok": True, "channel": kwargs["channel"], "ts": "1716900400.000500"}

        async def set_assistant_status(self, **_kwargs):
            return {"ok": True}

    async def fake_client_for_run(run_arg):
        assert run_arg is run
        return FakeSlackClient()

    monkeypatch.setattr(slack_delivery, "slack_client_for_run", fake_client_for_run)
    deferral_recorder = AsyncMock(
        return_value=(
            SimpleNamespace(),
            SimpleNamespace(id=3, state="pending"),
            True,
        )
    )
    monkeypatch.setattr(
        "brain.systems.runs.run_deferrals.record_run_deferral",
        deferral_recorder,
    )
    monkeypatch.setattr(
        "brain.systems.runs.obligation_notices.schedule_post_commit_notice_delivery",
        lambda *_args, **_kwargs: False,
    )

    session = FakeSession()
    result = await runner._settle_slack_origin_run_async(session, run)

    assert result["surface"] == "slack"
    assert result["queued"] is True
    assert session.scalar_calls == 0
    assert calls == []
    assert deferral_recorder.await_args.kwargs["deferral_text"] == UPSTREAM_FAILED_RUN_MESSAGE


async def test_runner_does_not_mirror_after_same_run_ai_timeline_tool_message():
    from sqlalchemy.sql import visitors

    from brain.systems.runs.cortex.runner import _settle_idea_for_terminal_root_run_async
    from brain.platform.db.models.agent_run import AgentRunRow
    from brain.platform.db.models.idea import Idea, IdeaThread

    idea_id = str(uuid.uuid4())
    run = SimpleNamespace(id=48, parent_run_id=None, thread_id=idea_id, status="completed")
    idea = SimpleNamespace(id=idea_id, status="unread_reply", updated_at=None)
    final_artifact = SimpleNamespace(id=103, run_id=48, artifact_type="final_answer", text="Already posted")
    existing_message = SimpleNamespace(
        message_type="message",
        metadata_={"created_by_run_id": 48, "surface": "ai_timeline"},
    )
    added = []
    scalar_calls = 0

    def _filters_on_message_type(stmt) -> bool:
        found = False

        def visit_binary(binary):
            nonlocal found
            if "message_type" in str(getattr(binary, "left", "")):
                found = True

        visitors.traverse(stmt, {}, {"binary": visit_binary})
        return found

    class FakeSession:
        async def get(self, model, key):
            if model is AgentRunRow and int(key) == 48:
                return run
            if model is Idea and str(key) == idea_id:
                return idea
            return None

        def add(self, obj):
            added.append(obj)

        async def scalars(self, stmt):
            nonlocal scalar_calls
            scalar_calls += 1
            if scalar_calls == 1:
                return _ScalarRows([final_artifact])
            if scalar_calls == 2 and not _filters_on_message_type(stmt):
                return _ScalarRows([existing_message])
            return _ScalarRows([])

        async def flush(self):
            pass

    assert await _settle_idea_for_terminal_root_run_async(FakeSession(), 48) is None
    assert not any(isinstance(obj, IdeaThread) for obj in added)


async def test_runner_does_not_settle_child_run_idea_status():
    from brain.systems.runs.cortex.runner import _settle_idea_for_terminal_root_run_async
    from brain.platform.db.models.agent_run import AgentRunRow

    run = SimpleNamespace(id=43, parent_run_id=42, thread_id="idea-1", status="completed")

    class FakeSession:
        async def get(self, model, key):
            return run if model is AgentRunRow else None

    assert await _settle_idea_for_terminal_root_run_async(FakeSession(), 43) is None


async def test_runner_does_not_settle_non_idea_thread_id():
    from brain.systems.runs.cortex.runner import _settle_idea_for_terminal_root_run_async
    from brain.platform.db.models.agent_run import AgentRunRow
    from brain.platform.db.models.idea import Idea

    run = SimpleNamespace(
        id=44,
        parent_run_id=None,
        thread_id="external-agent:conn-1:ask-1",
        status="completed",
    )

    class FakeSession:
        async def get(self, model, key):
            if model is AgentRunRow and int(key) == 44:
                return run
            if model is Idea:
                raise AssertionError("synthetic external-agent thread id must not query ideas")
            return None

    assert await _settle_idea_for_terminal_root_run_async(FakeSession(), 44) is None


async def test_runner_settles_discussion_origin_run_into_discussion_not_timeline(monkeypatch):
    from brain.systems.runs.cortex.runner import _settle_terminal_root_run_async
    from brain.platform.db.models.agent_run import AgentRunRow
    from brain.platform.db.models.idea import IdeaThread, ThreadDiscussionComment

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
        id=46,
        parent_run_id=None,
        thread_id="thread-discussion:idea-1",
        status="completed",
        org_id="org-1",
        target_ref={
            "kind": "thread_discussion",
            "idea_id": "idea-1",
            "parent_thread_id": "idea-1",
            "discussion_trigger": discussion_trigger,
        },
        metadata_={"originating_surface": "thread_discussion"},
    )
    final_artifact = SimpleNamespace(
        id=101,
        run_id=46,
        artifact_type="final_answer",
        text="Yes, I can see this Discussion comment.",
    )
    added = []
    published = []
    scalar_calls = 0

    class FakeSession:
        async def get(self, model, key):
            if model is AgentRunRow and int(key) == 46:
                return run
            raise AssertionError("Discussion-origin settlement must not load the AI Timeline Thread")

        def add(self, obj):
            if isinstance(obj, ThreadDiscussionComment):
                obj.id = 8
            added.append(obj)

        async def scalars(self, stmt):
            nonlocal scalar_calls
            scalar_calls += 1
            if scalar_calls == 1:
                return _ScalarRows([])
            return _ScalarRows([final_artifact])

        async def flush(self):
            pass

    monkeypatch.setattr(
        "brain.systems.runs.cortex.runner.publish_safe",
        lambda event, payload: published.append((event, payload)),
    )

    payload = await _settle_terminal_root_run_async(FakeSession(), 46)

    assert payload == {
        "surface": "thread_discussion",
        "idea_id": "idea-1",
        "run_id": 46,
        "comment_id": 8,
    }
    assert not any(isinstance(obj, IdeaThread) for obj in added)
    comment = next(obj for obj in added if isinstance(obj, ThreadDiscussionComment))
    assert comment.thread_id == "idea-1"
    assert comment.author_kind == "illo"
    assert comment.body == "Yes, I can see this Discussion comment."
    assert comment.metadata_ == {
        "source": "agent_run_final_answer",
        "surface": "thread_discussion",
        "created_by_run_id": 46,
        "artifact_id": 101,
        "reply_to_comment_id": 7,
    }
    assert published[0][0] == "thread_discussion_comment"
    assert published[0][1]["idea_id"] == "idea-1"


async def test_runner_carries_same_run_discussion_attachments_into_timeline_final_answer(monkeypatch):
    from brain.systems.runs.cortex.runner import _settle_idea_for_terminal_root_run_async
    from brain.systems.cortex.thought_lifecycle import TerminalRunSettlementCommand
    from brain.platform.db.models.agent_run import AgentRunRow
    from brain.platform.db.models.idea import Idea

    thread_id = "550e8400-e29b-41d4-a716-446655440000"
    run = SimpleNamespace(
        id=47,
        parent_run_id=None,
        thread_id=thread_id,
        status="completed",
        target_ref={"originating_surface": "ai_timeline"},
        metadata_={},
    )
    idea = SimpleNamespace(
        id=thread_id,
        status="working",
        org_id="org-1",
        user_id="owner-1",
        agent_details=None,
    )
    final_artifact = SimpleNamespace(
        id=102,
        run_id=47,
        artifact_type="final_answer",
        text="Done - I attached both PNGs in the thread.",
    )
    attachment = {
        "url": f"/static/uploads/thread-assets/{thread_id}/diagram.png",
        "kind": "image",
        "content_type": "image/png",
    }
    same_run_comment = SimpleNamespace(
        id=28,
        thread_id=thread_id,
        author_kind="illo",
        body="Corrected version",
        attachments=[attachment],
        metadata_={"created_by_run_id": 47},
    )
    scalar_calls = 0
    captured_command = None

    class FakeSession:
        async def get(self, model, key):
            if model is AgentRunRow and int(key) == 47:
                return run
            if model is Idea and str(key) == thread_id:
                return idea
            return None

        async def scalars(self, _stmt):
            nonlocal scalar_calls
            scalar_calls += 1
            if scalar_calls == 1:
                return _ScalarRows([final_artifact])
            if scalar_calls == 2:
                return _ScalarRows([])
            if scalar_calls == 3:
                return _ScalarRows([same_run_comment])
            return _ScalarRows([])

    async def fake_settle_terminal_run(_session, *, idea, command, publish=None):
        nonlocal captured_command
        captured_command = command
        return SimpleNamespace(status_change={"ok": True})

    monkeypatch.setattr("brain.systems.runs.cortex.runner.settle_terminal_run", fake_settle_terminal_run)

    payload = await _settle_idea_for_terminal_root_run_async(FakeSession(), 47)

    assert payload == {"ok": True}
    assert isinstance(captured_command, TerminalRunSettlementCommand)
    assert captured_command.attachments == [attachment]


def _cycle_run_message_with_prompt(prompt: str) -> str:
    from brain.platform.db.models.cycle import Cycle, CycleRun
    from brain.platform.db.models.idea import Idea
    from brain.systems.cycles.prompts import cycle_run_message

    idea = Idea()
    idea.id = "idea-mission"
    idea.title = "Mission Budget"

    cycle = Cycle()
    cycle.id = 9
    cycle.user_id = "user-1"
    cycle.org_id = "org-1"
    cycle.name = "Mission Budget"
    cycle.prompt = prompt
    cycle.model_override = None
    cycle.thinking_override = None

    run = CycleRun()
    run.id = 21
    run.cycle_id = 9
    run.scheduled_for = datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc)
    run.guidance_snapshot = []
    run.output_targets_snapshot = []
    run.context_snapshot = {}

    return cycle_run_message(idea, cycle, run)


def test_cycle_run_message_carries_full_mission_past_legacy_2000_cap():
    prompt = "z" * 8_000

    message = _cycle_run_message_with_prompt(prompt)

    assert prompt in message
    assert "[Cycle mission truncated for launch" not in message


def test_cycle_run_message_truncates_oversized_mission_loudly():
    from brain.systems.cycles.prompts import _MISSION_SEED_MAX_CHARS

    prompt = "z" * (_MISSION_SEED_MAX_CHARS + 500)

    message = _cycle_run_message_with_prompt(prompt)

    assert "z" * _MISSION_SEED_MAX_CHARS in message
    assert prompt not in message
    assert "[Cycle mission truncated for launch: 500 chars omitted." in message


def test_truncate_tool_result_text_appends_degraded_evidence_note(monkeypatch):
    from brain.systems.runs.direct_loop import tool_execution

    monkeypatch.setattr(tool_execution, "output_budget_chars_for_tool", lambda name: 2_000)

    result = tool_execution.truncate_tool_result_text("manage_domain", "x" * 5_000)

    assert len(result) <= 2_000
    assert "[System: output exceeded this tool's visible budget" in result
    assert result.endswith("or pagination.]")
    assert "of 5000 chars shown" in result
    assert "chars truncated by tool output budget" in result


def test_truncate_tool_result_text_under_budget_is_unchanged(monkeypatch):
    from brain.systems.runs.direct_loop import tool_execution

    monkeypatch.setattr(tool_execution, "output_budget_chars_for_tool", lambda name: 2_000)

    text = "x" * 1_999

    assert tool_execution.truncate_tool_result_text("manage_domain", text) == text


def test_truncate_tool_result_text_tiny_budget_skips_note(monkeypatch):
    from brain.systems.runs.direct_loop import tool_execution

    monkeypatch.setattr(tool_execution, "output_budget_chars_for_tool", lambda name: 250)

    result = tool_execution.truncate_tool_result_text("manage_domain", "x" * 5_000)

    assert len(result) <= 250
    assert "[System:" not in result
    assert "chars truncated by tool output budget" in result


async def test_runtime_tool_executor_persists_full_result_refs_beside_preview():
    """Big mutating results: the durable event keeps a 1000-char preview but
    result_refs is extracted from the FULL result (illo-dev finding,
    2026-07-16 — a created tracker record was invisible to attribution);
    the live stream never carries the backend-only refs channel."""
    import json as _json

    from brain.systems.runs.tools import AsyncRunToolExecutor, ToolExecution

    runtime = _runtime("worker")
    executor = AsyncRunToolExecutor(runtime.store, stream=runtime.stream)

    big_result = _json.dumps({"record": {"id": 1823, "domain_id": 30, "pad": "x" * 5000}})
    await executor.execute(
        42,
        ToolExecution(
            name="manage_domain",
            args={"action": "create_record"},
            handler=lambda **kwargs: big_result,
        ),
        root_run_id=42,
    )

    completed = next(e for e in runtime.store.events if e.event_type == "run.tool_completed")
    assert len(completed.payload["result"]) == 1000  # preview stays bounded
    assert {"kind": "domain_record", "id": "1823", "source": "manage_domain"} in (
        completed.payload["result_refs"]
    )
    streamed = [p for t, p in runtime.stream.messages if t == "run.tool_completed"]
    assert streamed and all("result_refs" not in p for p in streamed)


async def test_public_projection_never_carries_result_refs():
    from brain.systems.runs.presentation import public_tool_event_payload

    public = public_tool_event_payload(
        {
            "tool_name": "manage_domain",
            "args": {"action": "create_record"},
            "result": "{\"truncated",
            "result_refs": [{"kind": "domain_record", "id": "sk-live-oops", "source": "x"}],
        },
        "run.tool_completed",
    )
    assert "result_refs" not in public
