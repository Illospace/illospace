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
                thread_id=parent.thread_id,
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


async def test_run_admission_marks_idea_working_without_worker_details():
    from brain.systems.runs.cortex import _a_mark_idea_working_for_run_admission
    from brain.platform.db.models.idea import Idea, IdeaStateLog

    idea = SimpleNamespace(id="idea-1", status="idle", updated_at=None, active_agents=7)
    added = []

    class FakeSession:
        async def get(self, model, key):
            return idea if model is Idea and key == "idea-1" else None

        def add(self, row):
            added.append(row)

        async def flush(self):
            return None

    payload = await _a_mark_idea_working_for_run_admission(FakeSession(), "idea-1", 123)

    assert payload == {
        "idea_id": "idea-1",
        "old_status": "idle",
        "new_status": "working",
        "run_id": 123,
        "changed": True,
    }
    assert idea.status == "working"
    assert idea.active_agents == 7
    assert isinstance(idea.updated_at, datetime)
    assert len(added) == 1
    assert isinstance(added[0], IdeaStateLog)
    assert added[0].from_state == "idle"
    assert added[0].to_state == "working"
    assert added[0].trigger == "agent_run_admitted"


async def test_run_admission_preserves_protected_idea_statuses():
    from brain.systems.runs.cortex import _a_mark_idea_working_for_run_admission
    from brain.platform.db.models.idea import Idea

    idea = SimpleNamespace(id="idea-1", status="archived", updated_at=None)
    added = []

    class FakeSession:
        async def get(self, model, key):
            return idea if model is Idea and key == "idea-1" else None

        def add(self, row):
            added.append(row)

        async def flush(self):
            return None

    payload = await _a_mark_idea_working_for_run_admission(FakeSession(), "idea-1", 123)

    assert payload is None
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
    assert "Move quickly, but keep senior engineering hygiene." not in captured["spec"].system_prompt
    assert "A `/skill` mention is an explicit skill command." not in captured["spec"].system_prompt
    assert _stream_has(runtime.stream.messages, "run.text_delta", {"delta": "README contents", "run_id": 42})
    assert any(event.event_type == "run.activity" and event.payload["label"] == "Reading files" for event in runtime.store.events)
    assert any(event.event_type == "run.tool_completed" and event.payload["tool_name"] == "read_file" for event in runtime.store.events)
    assert any(_artifact_type(artifact) == "file_observation" for artifact in runtime.store.artifacts)


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
                "required_response": "introduce_and_continue_setup",
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
    from brain.systems.runs.recipes.shared import workspace_root_from_ref

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
        lambda role: [{"name": "manage_cycle"}, {"name": "web_search"}],
    )
    monkeypatch.setattr(
        "brain.systems.runs.recipes.fast.build_tool_handlers",
        lambda **kwargs: {"manage_cycle": object(), "web_search": object()},
    )
    monkeypatch.setattr("brain.systems.runs.recipes.fast.invoke_direct_agent_async", fake_invoke)

    runtime = _runtime("fast")
    runtime.request = replace(
        runtime.request,
        metadata={"tool_policy": {"disabled_tools": ["manage_cycle"]}},
    )

    result = await FastRecipe().execute(runtime)

    assert result.status.value == "completed"
    assert [tool["name"] for tool in captured["spec"].tools] == ["web_search"]
    assert sorted(captured["spec"].tool_handlers) == ["web_search"]


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


def _async_thread_binding_session(idea, *, attachment=None):
    class _Session:
        async def get(self, model, idea_id):
            return idea

        async def scalars(self, stmt):
            return SimpleNamespace(first=lambda: attachment)

    return _Session()


async def test_thread_binding_keeps_fast_high_intelligence_by_default():
    from brain.systems.runs.cortex.thread_binding import a_build_run_request

    session = _async_thread_binding_session(SimpleNamespace(id="idea-1", org_id="org-1", user_id="u1", title="Thread"))

    request = await a_build_run_request(
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


async def test_thread_binding_prefers_cortex_trigger_actor_over_payload_user_id():
    from brain.systems.runs.cortex.thread_binding import a_build_run_request

    session = _async_thread_binding_session(
        SimpleNamespace(id="idea-1", org_id="org-1", user_id="u1", title="Thread")
    )

    request = await a_build_run_request(
        session,
        idea_id="idea-1",
        event="idea_created",
        message="Build a workspace app",
        user_id="service-user",
        metadata={
            "illo_trigger": {
                "actor": {
                    "id": "u1",
                    "org_id": "org-1",
                    "internal": False,
                    "principal_type": "human",
                }
            }
        },
    )

    assert request.user_id == "u1"


async def test_thread_binding_records_slash_skill_interest():
    from brain.systems.runs.cortex.thread_binding import a_build_run_request

    session = _async_thread_binding_session(SimpleNamespace(id="idea-1", org_id="org-1", user_id="u1", title="Thread"))

    request = await a_build_run_request(
        session,
        idea_id="idea-1",
        event="thread_reply",
        message="what does /debug do? also inspect /api/foo",
        user_id="u1",
        metadata={"run_profile": "fast"},
    )

    assert request.metadata["slash_skill_names"] == ["debug"]
    assert request.metadata["slash_skill_commands"][0]["token"] == "/debug"


async def test_thread_binding_inherits_project_context_from_idea():
    from brain.systems.runs.cortex.thread_binding import a_build_run_request

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
    session = _async_thread_binding_session(idea)

    request = await a_build_run_request(
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


async def test_thread_binding_skips_invalid_metadata_project_context_for_valid_idea_context():
    from brain.systems.runs.cortex.thread_binding import a_build_run_request

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
    session = _async_thread_binding_session(idea)

    request = await a_build_run_request(
        session,
        idea_id="idea-1",
        event="thread_reply",
        message="Try again",
        user_id="u1",
        metadata={"project_context": {"name": "Stale empty project", "resources": []}},
    )

    assert request.metadata["project_context"]["name"] == "Illospace"
    assert request.target_ref["project_context_snapshot"]["resources"][0]["name"] == "Illospace/illospace"
    assert request.metadata["project_context_validation_errors"] == [
        {
            "source": "metadata",
            "errors": ["project_context_snapshot.resources must contain at least one resource."],
        }
    ]


async def test_thread_binding_drops_invalid_legacy_project_context_when_no_valid_fallback():
    from brain.systems.runs.cortex.thread_binding import a_build_run_request

    idea = SimpleNamespace(
        id="idea-1",
        org_id="org-1",
        user_id="u1",
        title="Thread",
        agent_details={"project_context": {"name": "Legacy empty project", "resources": []}},
    )
    session = _async_thread_binding_session(idea)

    request = await a_build_run_request(
        session,
        idea_id="idea-1",
        event="thread_reply",
        message="Try again",
        user_id="u1",
        metadata={},
    )

    assert "project_context" not in request.metadata
    assert "project_context_snapshot" not in request.target_ref
    assert request.workspace_ref == {}
    assert request.metadata["project_context_validation_errors"] == [
        {
            "source": "idea",
            "errors": ["project_context_snapshot.resources must contain at least one resource."],
        }
    ]


async def test_thread_binding_falls_back_to_latest_project_attachment():
    from brain.systems.runs.cortex.thread_binding import a_build_run_request

    idea = SimpleNamespace(id="idea-1", org_id="org-1", user_id="u1", title="Thread", agent_details={})
    attachment = SimpleNamespace(
        snapshot={
            "name": "Attached Project",
            "resources": [{"type": "folder", "path": "attached/project"}],
        },
    )
    session = _async_thread_binding_session(idea, attachment=attachment)

    request = await a_build_run_request(
        session,
        idea_id="idea-1",
        event="thread_reply",
        message="Continue in the project",
        user_id="u1",
        metadata={},
    )

    assert request.metadata["project_context"]["name"] == "Attached Project"
    assert request.target_ref["project_context_snapshot"]["resources"][0]["path"] == "attached/project"


async def test_thread_binding_applies_intelligence_and_effort_overrides():
    from brain.systems.runs.cortex.thread_binding import a_build_run_request

    session = _async_thread_binding_session(SimpleNamespace(id="idea-1", org_id="org-1", user_id="u1", title="Thread"))

    request = await a_build_run_request(
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


async def test_thread_binding_applies_explicit_model_override():
    from brain.systems.runs.cortex.thread_binding import a_build_run_request

    session = _async_thread_binding_session(SimpleNamespace(id="idea-1", org_id="org-1", user_id="u1", title="Thread"))

    request = await a_build_run_request(
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
    existing_message = SimpleNamespace(metadata_={"run_id": 45})
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
