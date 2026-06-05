"""Focused tests for the new AgentRun runtime recipes and projections."""

from __future__ import annotations

import asyncio
import inspect
import json
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest


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


class _Store:
    def __init__(self):
        self.events = []
        self.artifacts = []
        self.created_requests = []
        self.runs = {}
        self.children_by_step = {}
        self.completed_steps = {}
        self.statuses = {}
        self.session = self

    def append_event(self, event):
        self.events.append(event)
        return _AwaitableValue(event)

    def append_artifact(self, artifact):
        self.artifacts.append(artifact)
        return _AwaitableValue(artifact)

    def create_run(self, request):
        from brain.systems.runs.domain import AgentRun
        from brain.systems.runs.ids import trace_id_for_run_id
        from brain.systems.runs.status import RunStatus

        run_id = 100 + len(self.created_requests)
        run = AgentRun(
            id=run_id,
            trace_id=trace_id_for_run_id(run_id),
            thread_id=request.thread_id,
            input_message=request.message,
            profile=request.normalized_profile,
            recipe=request.normalized_recipe,
            status=RunStatus.QUEUED,
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
    ):
        from brain.systems.runs.domain import AgentRunRequest
        from brain.systems.runs.events import run_event

        if step_key and step_key in self.children_by_step:
            return self.children_by_step[step_key]
        metadata_payload = dict(metadata or {})
        if step_key:
            metadata_payload["parent_step_key"] = step_key
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
            )
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

    def step_completed(self, run_id, step_key):
        return _AwaitableValue(step_key in self.completed_steps.get(run_id, {}))

    def step_result(self, run_id, step_key):
        return _AwaitableValue(self.completed_steps.get(run_id, {}).get(step_key))

    def start_step(self, run_id, step_key):
        from brain.systems.runs.events import run_event

        self.append_event(run_event(run_id, "run.step_started", {"step": step_key, "step_key": step_key}, root_run_id=42))
        return _AwaitableValue(None)

    def complete_step(self, run_id, step_key, result=None):
        from brain.systems.runs.events import run_event

        self.completed_steps.setdefault(run_id, {})[step_key] = result
        self.append_event(
            run_event(
                run_id,
                "run.step_completed",
                {"step": step_key, "step_key": step_key, "result": result},
                root_run_id=42,
            )
        )
        return _AwaitableValue(result)

    def fail_step(self, run_id, step_key, error):
        from brain.systems.runs.events import run_event

        self.append_event(
            run_event(run_id, "run.step_failed", {"step": step_key, "step_key": step_key, "error": error}, root_run_id=42)
        )
        return _AwaitableValue(None)

    def skip_step(self, run_id, step_key):
        from brain.systems.runs.events import run_event

        self.append_event(run_event(run_id, "run.step_skipped", {"step": step_key, "step_key": step_key}, root_run_id=42))
        return _AwaitableValue(None)

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


def _stub_phase_reviews(monkeypatch, decisions_by_node=None):
    decisions_by_node = decisions_by_node or {}

    async def fake_phase_review(spec):
        payload = json.loads(spec.message)
        node_id = payload["completed_phase"]["node"]["id"]
        decision = decisions_by_node.get(node_id) or {"summary": "Plan still fits.", "revisions": []}
        return SimpleNamespace(output=json.dumps(decision), success=True)

    monkeypatch.setattr("brain.systems.runs.recipes.phase_barrier.invoke_direct_agent_async", fake_phase_review)
    _stub_deep_synthesis(monkeypatch)


def _stub_deep_synthesis(monkeypatch, output="Deep completed using native AgentRun workers."):
    async def fake_deep_synthesis(spec):
        return SimpleNamespace(output=output, success=True)

    monkeypatch.setattr("brain.systems.runs.recipes.deep.invoke_direct_agent_async", fake_deep_synthesis)


def _runtime(recipe: str = "fast", *, message: str = "Read the README", store=None, workspace_ref=None):
    from brain.systems.runs.context import RunContextLoader
    from brain.systems.runs.domain import AgentRun, AgentRunRequest, RunProfile, RunRecipe
    from brain.systems.runs.engine import RunRuntime
    from brain.systems.runs.status import RunStatus
    from brain.systems.runs.steering import SteeringInbox, SteeringMessage

    recipe_enum = {
        "fast": RunRecipe.FAST,
        "deep": RunRecipe.DEEP,
        "worker": RunRecipe.WORKER,
    }[recipe]
    profile = RunProfile.FAST if recipe == "fast" else RunProfile.DEEP
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
        model_policy={"model": "openai/gpt-5.4", "thinking": "high"},
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
    assert "spawn_worker" in captured["spec"].system_prompt
    assert "headless=true" in captured["spec"].system_prompt
    assert "write one brief task-specific assistant sentence" in captured["spec"].system_prompt
    assert "## Agent Profile" in captured["spec"].system_prompt
    assert captured["spec"].metadata["max_parallel_tool_calls"] == 4
    assert "Final Reply Presenter" in captured["spec"].system_prompt
    assert "When the user only confirms, corrects, asks yes/no" in captured["spec"].system_prompt
    assert "config snippets, caveats, or next steps" in captured["spec"].system_prompt
    assert _stream_has(runtime.stream.messages, "run.text_delta", {"delta": "README contents", "run_id": 42})
    assert any(event.event_type == "run.activity" and event.payload["label"] == "Reading files" for event in runtime.store.events)
    assert any(event.event_type == "run.tool_completed" and event.payload["tool_name"] == "read_file" for event in runtime.store.events)
    assert any(_artifact_type(artifact) == "file_observation" for artifact in runtime.store.artifacts)


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
    assert prompt.index("## Agent Soul") < prompt.index("## Agent Profile")
    assert prompt.index("## Agent Profile") < prompt.index("## Fast Runtime Recipe")


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


def test_workspace_root_from_ref_uses_thread_project_context_snapshot():
    from brain.systems.runs.recipes.shared import project_runtime_workspace_from_ref, workspace_root_from_ref

    assert workspace_root_from_ref({"workspace_root": "/tmp/direct"}) == "/tmp/direct"
    assert workspace_root_from_ref(
        {
            "project_context_snapshot": {
                "resources": [{"kind": "repo", "path": "/tmp/ideas/idea-1/project/repo"}],
            }
        }
    ) == "/tmp/ideas/idea-1/project/repo"
    assert (
        workspace_root_from_ref(
            {
                "workspace_root": "/tmp/ideas/idea-1/uploads/agent.md",
                "resolved_workspace_root": "/tmp/ideas/idea-1/uploads/agent.md",
                "project_context_snapshot": {
                    "resources": [{"kind": "file", "path": "/tmp/ideas/idea-1/uploads/agent.md"}],
                    "permission_scope": {"allowed_paths": ["/tmp/ideas/idea-1/uploads/agent.md"]},
                },
                "project_context_permission_scope": {"allowed_paths": ["/tmp/ideas/idea-1/uploads/agent.md"]},
            }
        )
        is None
    )
    assert workspace_root_from_ref(
        {
            "project_context_permission_scope": {
                "allowed_paths": ["/tmp/ideas/idea-1/project/repo"],
            }
        }
    ) == "/tmp/ideas/idea-1/project/repo"
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


async def test_spawn_worker_handler_uses_child_run_store_path_for_headless(monkeypatch):
    from brain.systems.runs.domain import RunProfile, RunRecipe
    from brain.systems.runs.execution_context import bind_agent_context
    import brain.systems.runs.tool_catalog.handlers.workers as worker_handlers

    parent = SimpleNamespace(
        id=42,
        org_id="org-1",
        user_id="user-1",
        root_run_id=42,
        profile=RunProfile.FAST,
        target_ref={"kind": "cortex_idea"},
        workspace_ref={"workspace_root": "/tmp/work"},
        model_policy={"tier": "high"},
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

        async def create_child_run(self, parent_arg, **kwargs):
            assert parent_arg is parent
            create_kwargs.update(kwargs)
            return child

        async def append_event(self, event):
            events.append(event)

    monkeypatch.setattr(worker_handlers, "UnitOfWork", _Uow)
    monkeypatch.setattr(worker_handlers, "AsyncAgentRunStore", _Store)

    with bind_agent_context(run=SimpleNamespace(id=parent.id)):
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
    assert create_kwargs["thread_id"].startswith("headless-worker:42:")
    assert create_kwargs["metadata"]["headless"] is True
    assert set(create_kwargs["metadata"]["tool_policy"]["disabled_tools"]) >= {
        "manage_cycle",
        "cortex_reply",
        "post_ai_timeline_message",
    }
    assert [event.event_type for event in events] == ["run.worker_spawned"]


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

        async def create_child_run(self, parent_arg, **kwargs):
            assert parent_arg is current_parent
            created.update(kwargs)
            return child

        async def append_event(self, event):
            assert event.run_id == current_parent.id

    monkeypatch.setattr(worker_handlers, "UnitOfWork", _Uow)
    monkeypatch.setattr(worker_handlers, "AsyncAgentRunStore", _Store)

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


async def test_deep_recipe_uses_native_child_runs(monkeypatch):
    import brain.systems.runs.recipes.deep as deep_module
    from brain.systems.runs.domain import AgentRunArtifact
    from brain.systems.runs.recipes.deep import DeepRecipe
    from brain.systems.runs.status import RunStatus

    assert "run_coordinator" not in inspect.getsource(deep_module)
    _stub_phase_reviews(monkeypatch)

    executed = []

    class _ChildEngine:
        def __init__(self, session, **kwargs):
            self.store = session

        async def run_existing(self, run_id):
            executed.append(run_id)
            run = self.store.runs[run_id]
            artifact_type = "worker_result" if str(getattr(run.recipe, "value", run.recipe)) == "worker" else "final_answer"
            self.store.append_artifact(
                AgentRunArtifact(
                    run_id=run_id,
                    root_run_id=42,
                    artifact_type=artifact_type,
                    title="Worker result" if artifact_type == "worker_result" else "Final answer",
                    text=f"child {run_id} evidence",
                )
            )
            return SimpleNamespace(status=RunStatus.COMPLETED, id=run.id)

    store = _Store()
    runtime = _runtime("deep", message="Implement the README cleanup and verify the result.", store=store)
    runtime.engine = _ChildEngine(store)
    result = await DeepRecipe().execute(runtime)

    worker_requests = [
        request
        for request in store.created_requests
        if str(getattr(request.recipe, "value", request.recipe)) == "worker"
    ]
    assert len(worker_requests) == 2
    assert [request.parent_run_id for request in worker_requests] == [42, 42]
    assert executed == [100, 101, 102]
    assert result.status.value == "completed"
    assert "Deep completed using native AgentRun workers." in result.output
    assert any(event.event_type == "run.worker_planned" for event in store.events)
    assert any(event.event_type == "run.worker_started" for event in store.events)
    assert any(event.event_type == "run.worker_completed" for event in store.events)
    assert any(event.event_type == "run.step_completed" and event.payload["step"] == "verify" for event in store.events)
    assert any(_artifact_type(artifact) == "scout_handoff" for artifact in store.artifacts)
    deep_plan = next(artifact for artifact in store.artifacts if _artifact_type(artifact) == "deep_plan")
    def _wave_nodes(wave):
        return wave["nodes"] if isinstance(wave, dict) else wave

    assert [sorted(_wave_nodes(wave)) for wave in deep_plan.payload["waves"]] == [
        ["scout"],
        ["execute", "investigate"],
        ["verify"],
        ["synthesize"],
    ]
    assert not any(_artifact_type(artifact) == "worker_bundle" for artifact in store.artifacts)
    verification = [artifact for artifact in store.artifacts if _artifact_type(artifact) == "verifier_evidence"]
    assert verification
    assert verification[-1].payload["passed"] is True
    reviewed_nodes = [
        event.payload.get("completed_node_id")
        for event in store.events
        if event.event_type == "run.phase_review_started"
    ]
    assert "investigate" in reviewed_nodes
    assert "execute" in reviewed_nodes
    worker_completion_positions = [
        index
        for index, event in enumerate(store.events)
        if event.event_type == "run.worker_completed"
        and event.payload.get("node_id") in {"investigate", "execute"}
    ]
    worker_review_positions = [
        index
        for index, event in enumerate(store.events)
        if event.event_type == "run.phase_review_started"
        and event.payload.get("completed_node_id") in {"investigate", "execute"}
    ]
    assert max(worker_completion_positions) < min(worker_review_positions)


async def test_deep_coordinator_synthesis_uses_soul_and_owns_final_answer(monkeypatch):
    from brain.systems.runs.domain import AgentRunArtifact
    from brain.systems.runs.recipes.deep import DeepRecipe
    from brain.systems.runs.status import RunStatus

    _stub_phase_reviews(monkeypatch)
    monkeypatch.setattr(
        "brain.systems.runs.recipes.deep.soul_prompt_section",
        lambda: "## Agent Soul\nUse the coordinator voice.",
    )
    monkeypatch.setattr(
        "brain.systems.runs.recipes.deep.agent_profile_prompt_section",
        lambda: "## Agent Profile\nUse the final reply presenter.",
    )
    captured = {}

    async def fake_deep_synthesis(spec):
        captured["spec"] = spec
        return SimpleNamespace(output="Coordinator final answer.", success=True)

    monkeypatch.setattr("brain.systems.runs.recipes.deep.invoke_direct_agent_async", fake_deep_synthesis)

    class _ChildEngine:
        def __init__(self, session, **kwargs):
            self.store = session

        async def run_existing(self, run_id):
            run = self.store.runs[run_id]
            artifact_type = "worker_result" if str(getattr(run.recipe, "value", run.recipe)) == "worker" else "final_answer"
            self.store.append_artifact(
                AgentRunArtifact(
                    run_id=run_id,
                    root_run_id=42,
                    artifact_type=artifact_type,
                    title="Worker result" if artifact_type == "worker_result" else "Scout result",
                    text=f"child {run_id} evidence",
                )
            )
            return SimpleNamespace(status=RunStatus.COMPLETED, id=run.id)

    request_message = "Implement the README cleanup and verify the result."
    store = _Store()
    runtime = _runtime("deep", message=request_message, store=store)
    runtime.engine = _ChildEngine(store)

    result = await DeepRecipe().execute(runtime)

    assert result.output == "Coordinator final answer."
    spec = captured["spec"]
    assert spec.tool_call_source == "coordinator"
    assert spec.tools == []
    assert spec.persist_session is False
    assert "## Agent Soul\nUse the coordinator voice." in spec.system_prompt
    assert "## Agent Profile\nUse the final reply presenter." in spec.system_prompt
    assert spec.system_prompt.index("## Agent Soul") < spec.system_prompt.index("## Agent Profile")
    assert spec.system_prompt.index("## Agent Profile") < spec.system_prompt.index("## Deep Coordinator Mode")
    assert "## Deep Coordinator Mode" in spec.system_prompt
    payload = json.loads(spec.message)
    assert payload["task"] == request_message
    assert "child 101 evidence" in spec.message
    assert any(event.event_type == "run.coordinator_synthesis_started" for event in store.events)
    assert any(event.event_type == "run.coordinator_synthesis_completed" for event in store.events)


async def test_deep_recipe_failed_verification_returns_failed_status(monkeypatch):
    import brain.systems.runs.recipes.deep as deep_module
    from brain.systems.runs.domain import AgentRunArtifact
    from brain.systems.runs.recipes.deep import DeepRecipe
    from brain.systems.runs.status import RunStatus
    _stub_phase_reviews(monkeypatch)

    class _ChildEngine:
        def __init__(self, session, **kwargs):
            self.store = session

        async def run_existing(self, run_id):
            self.store.append_artifact(
                AgentRunArtifact(
                    run_id=run_id,
                    root_run_id=42,
                    artifact_type="final_answer",
                    title="Final answer",
                    text="",
                )
            )
            return SimpleNamespace(status=RunStatus.FAILED, id=run_id)

    store = _Store()
    runtime = _runtime("deep", message="Implement the README cleanup and verify the result.", store=store)
    runtime.engine = _ChildEngine(store)
    result = await DeepRecipe().execute(runtime)

    assert result.status.value == "failed"
    assert result.output.startswith("Deep verification failed:")
    verification = [artifact for artifact in store.artifacts if _artifact_type(artifact) == "verifier_evidence"]
    assert verification[-1].payload["passed"] is False
    assert "worker run(s) failed" in verification[-1].payload["warning"]
    assert any(event.event_type == "run.verification_failed" for event in store.events)


async def test_deep_recipe_verifies_required_worker_artifacts_from_child_runs(monkeypatch):
    from brain.systems.runs.domain import AgentRunArtifact
    from brain.systems.runs.recipes.deep import DeepRecipe
    from brain.systems.runs.status import RunStatus
    _stub_phase_reviews(monkeypatch)

    class _ChildEngine:
        def __init__(self, session, **kwargs):
            self.store = session

        async def run_existing(self, run_id):
            run = self.store.runs[run_id]
            artifact_type = "worker_result" if str(getattr(run.recipe, "value", run.recipe)) == "worker" else "final_answer"
            self.store.append_artifact(
                AgentRunArtifact(
                    run_id=run_id,
                    root_run_id=42,
                    artifact_type=artifact_type,
                    title="Worker result" if artifact_type == "worker_result" else "Final answer",
                    text=f"child {run_id} evidence",
                )
            )
            return SimpleNamespace(status=RunStatus.COMPLETED, id=run.id)

    store = _Store()
    runtime = _runtime("deep", message="Run the focused verification command.", store=store)
    runtime.request = replace(
        runtime.request,
        metadata={
            "deep_workers": [
                {
                    "role": "verify",
                    "objective": "Run the focused verification command.",
                    "expected_artifacts": ["command_output"],
                }
            ]
        },
    )
    runtime.engine = _ChildEngine(store)

    result = await DeepRecipe().execute(runtime)

    assert result.status.value == "failed"
    verification = [artifact for artifact in store.artifacts if _artifact_type(artifact) == "verifier_evidence"]
    assert verification[-1].payload["passed"] is False
    missing = verification[-1].payload["details"]["missing_required_evidence"]
    assert missing[0]["requirement"]["artifact_type"] == "command_output"


async def test_deep_phase_barrier_revises_pending_execute_assignment_from_investigate_output(monkeypatch):
    from brain.systems.runs.domain import AgentRunArtifact
    from brain.systems.runs.recipes.deep import DeepRecipe
    from brain.systems.runs.status import RunStatus

    request_message = "Clean up setup docs."
    original_execute_objective = (
        "Do the primary scoped work for this request and report concrete results: "
        f"{request_message}"
    )
    revised_execute_objective = "Only update README.md setup steps; leave API docs untouched."
    revised_execute_message = "Investigation found the setup drift is isolated to README.md."

    _stub_phase_reviews(
        monkeypatch,
        {
            "investigate": {
                "summary": "Narrow execute after investigation.",
                "revisions": [
                    {
                        "node_id": "execute",
                        "objective": revised_execute_objective,
                        "message": revised_execute_message,
                        "reason": "Narrow execute after investigation.",
                    }
                ],
            }
        },
    )

    class _ChildEngine:
        def __init__(self, session, **kwargs):
            self.store = session

        async def run_existing(self, run_id):
            run = self.store.runs[run_id]
            metadata = dict(run.metadata or {})
            role = str(metadata.get("worker_role") or "")
            if role == "investigate":
                self.store.append_artifact(
                    AgentRunArtifact(
                        run_id=run_id,
                        root_run_id=42,
                        artifact_type="worker_result",
                        title="Investigation result",
                        text=(
                            "The setup drift is isolated to README.md. "
                            "The execute worker should avoid API docs."
                        ),
                        payload={
                            "phase_output": {
                                "summary": "README.md is the only file that needs work.",
                                "evidence": ["README.md contains stale setup steps."],
                            },
                            "downstream_revisions": [
                                {
                                    "node_id": "execute",
                                    "objective": revised_execute_objective,
                                    "message": revised_execute_message,
                                    "reason": "Narrow execute after investigation.",
                                }
                            ],
                        },
                    )
                )
            elif role == "execute":
                self.store.append_artifact(
                    AgentRunArtifact(
                        run_id=run_id,
                        root_run_id=42,
                        artifact_type="worker_result",
                        title="Execute result",
                        text=f"Executed objective: {metadata['worker_assignment']['objective']}",
                    )
                )
            else:
                self.store.append_artifact(
                    AgentRunArtifact(
                        run_id=run_id,
                        root_run_id=42,
                        artifact_type="final_answer",
                        title="Scout result",
                        text="Scout completed.",
                    )
                )
            return SimpleNamespace(status=RunStatus.COMPLETED, id=run_id)

    store = _Store()
    runtime = _runtime("deep", message=request_message, store=store)
    runtime.request = replace(
        runtime.request,
        metadata={
            "deep_workers": [
                {
                    "id": "investigate",
                    "role": "investigate",
                    "objective": f"Gather context, constraints, and evidence for this request: {request_message}",
                },
                {
                    "id": "execute",
                    "role": "execute",
                    "objective": original_execute_objective,
                    "depends_on": ["investigate"],
                },
            ]
        },
    )
    runtime.engine = _ChildEngine(store)

    result = await DeepRecipe().execute(runtime)

    assert result.status.value == "completed"
    execute_request = _child_request_for_node(store, "execute")
    assert execute_request.metadata["worker_assignment"]["objective"] == revised_execute_objective
    assert revised_execute_objective in execute_request.message
    assert revised_execute_message in execute_request.message
    assert original_execute_objective not in execute_request.message

    assert any(
        event.event_type == "run.phase_review_started"
        and event.payload.get("completed_node_id") == "investigate"
        for event in store.events
    )
    assert any(
        event.event_type == "run.plan_revised"
        and event.payload.get("after_node_id") == "investigate"
        and event.payload.get("updated_node_ids") == ["execute"]
        for event in store.events
    )
    phase_outputs = [artifact for artifact in store.artifacts if _artifact_type(artifact) == "phase_result"]
    investigate_phase = next(artifact for artifact in phase_outputs if artifact.payload["node_id"] == "investigate")
    assert investigate_phase.payload["node"]["status"] == "completed"
    assert investigate_phase.payload["node"]["run_id"] == 101
    assert investigate_phase.payload["result"]["artifacts"][0]["payload"]["phase_output"]["summary"] == "README.md is the only file that needs work."
    revisions = [artifact for artifact in store.artifacts if _artifact_type(artifact) == "deep_plan_revision"]
    applied = revisions[-1].payload["applied"]
    assert applied[0]["after"]["assignment"]["objective"] == revised_execute_objective


async def test_deep_phase_barrier_revises_only_pending_downstream_worker_nodes(monkeypatch):
    from brain.systems.runs.domain import AgentRunArtifact
    from brain.systems.runs.recipes.deep import DeepRecipe
    from brain.systems.runs.status import RunStatus

    request_message = "Fix setup docs."
    original_investigate_objective = (
        "Gather context, constraints, and evidence for this request: "
        f"{request_message}"
    )
    forbidden_completed_objective = "Rerun investigation with a different scope."
    revised_execute_objective = "Patch only the setup command block discovered by investigate."

    _stub_phase_reviews(
        monkeypatch,
        {
            "investigate": {
                "summary": "Use investigation to narrow execute only.",
                "revisions": [
                    {
                        "node_id": "investigate",
                        "objective": forbidden_completed_objective,
                        "reason": "This completed node must not be replaced.",
                    },
                    {
                        "node_id": "execute",
                        "objective": revised_execute_objective,
                        "reason": "Pending downstream worker can be narrowed.",
                    },
                ],
            }
        },
    )

    class _ChildEngine:
        def __init__(self, session, **kwargs):
            self.store = session
            self.executed = []

        async def run_existing(self, run_id):
            self.executed.append(run_id)
            run = self.store.runs[run_id]
            metadata = dict(run.metadata or {})
            role = str(metadata.get("worker_role") or "")
            if role == "investigate":
                self.store.append_artifact(
                    AgentRunArtifact(
                        run_id=run_id,
                        root_run_id=42,
                        artifact_type="worker_result",
                        title="Investigation result",
                        text="Investigation completed with enough evidence to narrow execute.",
                        payload={
                            "phase_output": {"summary": "Setup command block is stale."},
                            "downstream_revisions": [
                                {
                                    "node_id": "investigate",
                                    "objective": forbidden_completed_objective,
                                    "reason": "This completed node must not be replaced.",
                                },
                                {
                                    "node_id": "execute",
                                    "objective": revised_execute_objective,
                                    "reason": "Pending downstream worker can be narrowed.",
                                },
                            ],
                        },
                    )
                )
            elif role == "execute":
                self.store.append_artifact(
                    AgentRunArtifact(
                        run_id=run_id,
                        root_run_id=42,
                        artifact_type="worker_result",
                        title="Execute result",
                        text=f"Executed objective: {metadata['worker_assignment']['objective']}",
                    )
                )
            else:
                self.store.append_artifact(
                    AgentRunArtifact(
                        run_id=run_id,
                        root_run_id=42,
                        artifact_type="final_answer",
                        title="Scout result",
                        text="Scout completed.",
                    )
                )
            return SimpleNamespace(status=RunStatus.COMPLETED, id=run_id)

    store = _Store()
    runtime = _runtime("deep", message=request_message, store=store)
    runtime.request = replace(
        runtime.request,
        metadata={
            "deep_workers": [
                {
                    "id": "investigate",
                    "role": "investigate",
                    "objective": original_investigate_objective,
                },
                {
                    "id": "execute",
                    "role": "execute",
                    "objective": "Patch the setup docs.",
                    "depends_on": ["investigate"],
                },
            ]
        },
    )
    child_engine = _ChildEngine(store)
    runtime.engine = child_engine

    result = await DeepRecipe().execute(runtime)

    assert result.status.value == "completed"
    investigate_requests = _child_requests_for_node(store, "investigate")
    assert len(investigate_requests) == 1
    assert investigate_requests[0].metadata["worker_assignment"]["objective"] == original_investigate_objective
    assert forbidden_completed_objective not in investigate_requests[0].message

    execute_request = _child_request_for_node(store, "execute")
    assert execute_request.metadata["worker_assignment"]["objective"] == revised_execute_objective
    assert revised_execute_objective in execute_request.message

    revision_events = [event for event in store.events if event.event_type == "run.plan_revised"]
    assert revision_events
    assert revision_events[-1].payload["updated_node_ids"] == ["execute"]
    assert revision_events[-1].payload["ignored_node_ids"] == ["investigate"]
    assert child_engine.executed == [100, 101, 102]


def _child_requests_for_node(store, node_id):
    return [
        request
        for request in store.created_requests
        if request.parent_run_id == 42 and request.metadata.get("parent_node_id") == node_id
    ]


def _child_request_for_node(store, node_id):
    requests = _child_requests_for_node(store, node_id)
    assert len(requests) == 1
    return requests[0]


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

    monkeypatch.setattr("brain.systems.vault.agent_access.read_agent_secret_for_runtime", fake_runtime_secret_read)

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


@pytest.mark.asyncio
async def test_direct_loop_returns_timeout_as_tool_error(monkeypatch):
    from brain.systems.runs.direct_loop.tool_execution import PendingToolCall, async_resolve_tool_call

    monkeypatch.setenv("AGENT_TOOL_TIMEOUT_SECONDS", "0.02")

    async def slow_handler():
        await asyncio.sleep(0.2)
        return {"ok": True}

    resolved = await async_resolve_tool_call(PendingToolCall("tool-1", "slow_tool", {}, slow_handler))

    assert resolved.is_error is True
    assert "slow_tool" in resolved.result_text
    assert "timed out" in resolved.result_text


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
        return SimpleNamespace(output="README setup documented", success=True, error=None)

    monkeypatch.setattr("brain.systems.runs.recipes.workers.build_agent_tools", lambda role: [{"name": "read_file"}])
    monkeypatch.setattr(
        "brain.systems.runs.recipes.workers.build_tool_handlers",
        lambda **kwargs: {"read_file": lambda **tool_args: {"content": "setup steps", "path": tool_args["path"]}},
    )
    monkeypatch.setattr("brain.systems.runs.recipes.workers.invoke_direct_agent_async", fake_invoke)

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
        model_policy={"tier": "high", "thinking": "high"},
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
    assert payload["model_policy"] == {"tier": "high", "thinking": "high"}


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


async def test_work_intake_keeps_fast_high_intelligence_by_default():

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
    assert request.model_policy == {"tier": "high", "thinking": "high"}
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


async def test_work_intake_applies_intelligence_and_effort_overrides():
    session = _async_work_intake_session(SimpleNamespace(id="idea-1", org_id="org-1", user_id="u1", title="Thread"))

    request = await _build_cortex_intake_run_request(
        session,
        idea_id="idea-1",
        event="thread_reply",
        message="Use cheaper settings",
        user_id="u1",
        metadata={"execution_profile": "fast", "model_tier": "medium", "effort": "xhigh"},
    )

    assert request.profile == "fast"
    assert request.recipe == "fast"
    assert request.model_policy == {"tier": "medium", "thinking": "xhigh"}


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
        "tier": "high",
        "thinking": "high",
        "model": "openai/gpt-5.5",
        "provider": "openai",
    }


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
