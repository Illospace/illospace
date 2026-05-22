from __future__ import annotations

from types import SimpleNamespace


def test_run_envelope_payload_is_stable_and_trace_scoped():
    from brain.kernel.runtime.envelope import RunActor, RunActorKind, RunEnvelope, RunOrigin

    envelope = RunEnvelope(
        task="Ship the runtime boundary",
        origin="cortex_thread",
        actor=RunActor(kind="user", id="user-1"),
        org_id="org-1",
        user_id="user-1",
        run_id=42,
        idea_id="idea-1",
        contract={"type": "freeform"},
        provider_operation_type="coordinator",
        metadata={"org_id": "org-1"},
        session_id="coordinator-idea-1",
        model="claude-sonnet-4-6",
        tools=[{"name": "brain_recall"}],
    )

    payload = envelope.to_payload()

    assert envelope.origin is RunOrigin.CORTEX_THREAD
    assert envelope.actor.kind is RunActorKind.USER
    assert payload["trace_id"] == "run:42"
    assert payload["origin"] == "cortex_thread"
    assert payload["actor"]["kind"] == "user"
    assert payload["runtime"]["tool_names"] == ["brain_recall"]
    assert payload["digest"] == envelope.to_payload()["digest"]


def test_run_envelope_coerces_policy_blocks_without_changing_payload():
    from brain.kernel.runtime.envelope import (
        ContextPolicy,
        RunBudget,
        RunContract,
        RunEnvelope,
        TargetContext,
        ToolPolicy,
        WorkspacePolicy,
    )

    envelope = RunEnvelope(
        task="typed policies",
        origin="unknown-origin",
        contract={"type": "pr", "requirements": {"require_pr": True}},
        target_context=TargetContext({"repo": "illo-brain"}),
        workspace_policy={"root": "/tmp/workspace"},
        tool_policy={"allowed": ["read_file"]},
        context_policy={"sections": ["memory"]},
        budget={"max_turns": 3},
    )

    assert isinstance(envelope.contract, RunContract)
    assert isinstance(envelope.target_context, TargetContext)
    assert isinstance(envelope.workspace_policy, WorkspacePolicy)
    assert isinstance(envelope.tool_policy, ToolPolicy)
    assert isinstance(envelope.context_policy, ContextPolicy)
    assert isinstance(envelope.budget, RunBudget)

    payload = envelope.to_payload()
    assert payload["origin"] == "manual_api"
    assert payload["contract"] == {"type": "pr", "requirements": {"require_pr": True}}
    assert payload["target_context"] == {"repo": "illo-brain"}
    assert payload["workspace_policy"] == {"root": "/tmp/workspace"}
    assert payload["tool_policy"] == {"allowed": ["read_file"]}
    assert payload["context_policy"] == {"sections": ["memory"]}
    assert payload["budget"] == {"max_turns": 3}


def test_run_envelope_projects_to_run_agent_kwargs_with_metadata():
    from brain.kernel.runtime.envelope import RunEnvelope

    stream_delta = lambda _delta: None
    envelope = RunEnvelope.from_run_agent_kwargs(
        message="remember this",
        origin="manual_api",
        metadata={"provider_operation_type": "memory_extraction", "org_id": "org-1"},
        user_id="user-1",
        run_id=7,
        idea_id="idea-1",
        session_id="agent-1",
        model="openai/gpt-5.4",
        tools=[],
        persist_session=False,
        max_turns=3,
        on_stream_delta=stream_delta,
    )

    kwargs = envelope.to_run_agent_kwargs()

    assert kwargs["message"] == "remember this"
    assert kwargs["run_id"] == 7
    assert kwargs["persist_session"] is False
    assert kwargs["max_turns"] == 3
    assert kwargs["on_stream_delta"] is stream_delta
    assert kwargs["metadata"]["provider_operation_type"] == "memory_extraction"
    assert kwargs["metadata"]["runtime_trace_id"] == "run:7"
    assert kwargs["metadata"]["runtime_envelope"]["origin"] == "manual_api"


def test_agent_kernel_wraps_agent_result(monkeypatch):
    import brain.systems.runs.direct_agent as agent
    from brain.kernel.runtime.kernel import AgentKernel
    from brain.kernel.runtime.envelope import RunEnvelope

    captured = {}

    def fake_run_agent(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            output="done",
            success=True,
            session_id=kwargs["session_id"],
            tokens_input=11,
            tokens_output=5,
            tokens_cache_read=0,
            tokens_cache_creation=0,
            duration_sec=2,
            tool_calls=["brain_recall"],
            error=None,
        )

    monkeypatch.setattr(agent, "run_agent", fake_run_agent)

    envelope = RunEnvelope(
        task="do it",
        origin="worker_node",
        run_id=99,
        session_id="agent-run-worker",
        provider_operation_type="worker",
        tools=[],
    )
    result = AgentKernel().run(envelope)

    assert result.success is True
    assert result.output == "done"
    assert result.trace_id == "run:99"
    assert result.tool_calls == ("brain_recall",)
    assert captured["metadata"]["runtime_envelope_digest"] == envelope.digest
    assert captured["metadata"]["provider_operation_type"] == "worker"


def test_agent_invocation_enters_through_runtime_envelope(monkeypatch):
    import brain.systems.runs.direct_agent as agent
    from brain.systems.runs.invocation import build_direct_agent_invocation, invoke_direct_agent

    captured = {}

    def fake_run_agent(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            output="coordinated",
            success=True,
            session_id=kwargs["session_id"],
            tokens_input=0,
            tokens_output=0,
            tokens_cache_read=0,
            tokens_cache_creation=0,
            duration_sec=0,
            tool_calls=[],
            error=None,
        )

    monkeypatch.setattr(agent, "run_agent", fake_run_agent)

    spec = build_direct_agent_invocation(
        message="coordinate",
        session_id="agent-run-123",
        system_prompt="system",
        model="claude-sonnet-4-6",
        thinking="medium",
        idea_id="idea-1",
        run_id=123,
        user_id="user-1",
        org_id="org-1",
    )

    result = invoke_direct_agent(spec)

    assert result.output == "coordinated"
    assert captured["metadata"]["runtime_origin"] == "manual_api"
    assert captured["metadata"]["runtime_trace_id"] == "run:123"
    assert captured["metadata"]["org_id"] == "org-1"
    assert captured["metadata"]["runtime_envelope"]["org_id"] == "org-1"
    assert captured["metadata"]["provider_operation_type"] == "coordinator"
