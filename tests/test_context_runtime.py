from __future__ import annotations


def _pack():
    from brain.systems.context.runtime import ContextRuntime

    return ContextRuntime().compile_run_pack(
        task="Fix uploads",
        idea_id="idea-1",
        run_id=12,
        identity="identity facts",
        working_memory="previous work",
        thread_context="user: uploads fail",
        user_context={"id": "user-1", "org_id": "org-1"},
        selected_skill="debug",
        skill={"name": "debug", "procedure": "Reproduce first"},
        memories=[{"id": 1, "type": "lesson", "content": "Uploads once failed because limits were low."}],
        guardrails=["do not skip tests"],
        coordinator_instructions="## Coordinator Only\nUse the AgentRun graph.",
        compiled_at="2026-04-24T12:00:00+00:00",
    )


def test_context_runtime_wraps_pack_compilation_and_rendering():
    from brain.systems.context.runtime import ContextRuntime

    runtime = ContextRuntime()
    pack = _pack()
    render = runtime.render_prompt(pack, role="coordinator")

    assert pack["runtime"]["compiler"] == "context-runtime-v1"
    assert render.context_pack_digest == pack["digest"]
    assert render.source_context_pack_digest == pack["digest"]
    assert "## Context Pack" in render.prompt
    assert "Uploads once failed" in render.prompt
    assert "Use the AgentRun graph" in render.prompt
    assert "thread_summary" in render.rendered_sections
    assert render.to_metadata()["context_pack_digest"] == render.context_pack_digest


def test_worker_context_is_reduced_and_records_omissions():
    from brain.systems.context.schema import ContextPack
    from brain.systems.context.runtime import ContextRuntime

    pack = _pack()
    render = ContextRuntime().render_worker_context(
        pack,
        worker_id="worker-1",
        node_id="node-1",
        skill_name="debug",
    )

    assert render.role == "worker"
    assert render.source_context_pack_digest == pack["digest"]
    assert render.context_pack_digest != pack["digest"]
    assert ContextPack.model_validate(render.context_pack).render_role == "worker"
    assert "Uploads once failed" in render.prompt
    assert "Use the AgentRun graph" not in render.prompt
    assert "tool_permissions" not in render.rendered_sections
    omitted = {section["name"]: section["reason"] for section in render.omitted_sections}
    assert "budget" in omitted
    assert "tool_permissions" in omitted
    metadata = render.to_metadata()
    assert metadata["worker_id"] == "worker-1"
    assert metadata["node_id"] == "node-1"


def test_context_compaction_preserves_tool_pairs_and_reports_provenance():
    from brain.systems.context.compaction import compact_session_messages

    messages = [{"role": "user", "content": "start"}]
    for index in range(24):
        if index == 8:
            messages.append({
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "tool-1", "name": "read_file", "input": {}}],
            })
            messages.append({
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "tool-1", "content": "ok"}],
            })
        else:
            messages.append({"role": "assistant", "content": [{"type": "text", "text": f"reply {index}"}]})
            messages.append({"role": "user", "content": f"message {index}"})

    compacted, report = compact_session_messages(messages, max_messages=18, session_id="session-1")

    use_ids = {
        block.get("id")
        for msg in compacted
        for block in (msg.get("content") if isinstance(msg.get("content"), list) else [])
        if isinstance(block, dict) and block.get("type") == "tool_use"
    }
    result_ids = {
        block.get("tool_use_id")
        for msg in compacted
        for block in (msg.get("content") if isinstance(msg.get("content"), list) else [])
        if isinstance(block, dict) and block.get("type") == "tool_result"
    }
    assert use_ids == result_ids
    assert report.omitted_count > 0
    assert report.provenance["summary_source"].endswith("_summarize_trimmed_messages")
    assert report.provenance["tool_pair_safe"] is True
    assert any(
        isinstance(msg.get("content"), str) and "ContextRuntime" in msg["content"]
        for msg in compacted
    )


def test_token_budget_compaction_uses_estimated_active_context():
    from brain.systems.context.compaction import (
        compact_session_messages_to_token_budget,
        estimate_session_tokens,
    )

    messages = [{"role": "user", "content": "start"}]
    for index in range(18):
        messages.append({
            "role": "assistant",
            "content": [{"type": "text", "text": "reply " + ("x" * 600)}],
        })
        messages.append({"role": "user", "content": "message " + ("y" * 600)})

    before = estimate_session_tokens(messages, system="system " * 100)
    compacted, report = compact_session_messages_to_token_budget(
        messages,
        token_limit=1200,
        target_tokens=900,
        session_id="token-session",
        system="system " * 100,
        max_messages=18,
        min_messages=6,
    )
    after = estimate_session_tokens(compacted, system="system " * 100)

    assert before > 1200
    assert after < before
    assert report.strategy == "token_budget_preserve_tool_pairs"
    assert report.provenance["original_estimated_tokens"] == before
    assert report.provenance["final_estimated_tokens"] == after
    assert any(
        isinstance(msg.get("content"), str) and "earlier messages compacted" in msg["content"]
        for msg in compacted
    )


def test_model_context_budget_is_model_and_provider_aware(monkeypatch):
    from brain.systems.context.budget import resolve_model_context_budget

    for name in (
        "AGENT_MODEL_CONTEXT_WINDOW_TOKENS",
        "AGENT_AUTO_COMPACT_TOKEN_LIMIT",
        "AGENT_AUTO_COMPACT_TARGET_TOKENS",
        "AGENT_CONTEXT_RESERVED_OUTPUT_TOKENS",
        "AGENT_CONTEXT_RESERVED_REASONING_TOKENS",
        "AGENT_CONTEXT_RESERVED_TOOL_TOKENS",
    ):
        monkeypatch.delenv(name, raising=False)

    openai_budget = resolve_model_context_budget(
        model="gpt-5.5",
        provider="openai",
        reasoning_effort="high",
        max_output_tokens=32_768,
        tools=[{"name": "read_file"}, {"name": "write_file"}],
    )
    anthropic_budget = resolve_model_context_budget(
        model="claude-sonnet-5",
        provider="anthropic",
        reasoning_effort="medium",
        max_output_tokens=16_384,
        tools=[],
    )

    assert openai_budget.context_window_tokens != anthropic_budget.context_window_tokens
    assert openai_budget.effective_input_limit_tokens < openai_budget.context_window_tokens
    assert openai_budget.auto_compact_threshold_tokens <= openai_budget.effective_input_limit_tokens
    assert openai_budget.target_tokens < openai_budget.auto_compact_threshold_tokens
    assert openai_budget.reserved_tool_tokens > 0


def test_context_admission_floor_includes_canonical_checkpoint_and_healthy_path(monkeypatch):
    from brain.systems.context.compaction import estimate_session_tokens
    from brain.systems.context.window_policy import ContextWindowPolicy

    monkeypatch.setenv("AGENT_MODEL_CONTEXT_WINDOW_TOKENS", "10000")
    monkeypatch.setenv("AGENT_AUTO_COMPACT_TOKEN_LIMIT", "7000")
    monkeypatch.setenv("AGENT_CONTEXT_RESERVED_OUTPUT_TOKENS", "0")
    monkeypatch.setenv("AGENT_CONTEXT_RESERVED_REASONING_TOKENS", "0")
    monkeypatch.setenv("AGENT_CONTEXT_RESERVED_TOOL_TOKENS", "0")
    monkeypatch.setenv("AGENT_CONTEXT_SAFETY_MARGIN_TOKENS", "0")
    messages = [
        {"role": "user", "content": f"message {index} " + ("x" * 80)}
        for index in range(8)
    ]
    system = "healthy system prompt"
    tools = [{"name": "read_file"}]

    policy = ContextWindowPolicy.resolve(
        model="gpt-5.5",
        provider="openai",
        reasoning_effort="low",
        max_output_tokens=0,
        tools=tools,
    )
    admission = policy.admit(
        messages,
        system=system,
        tools=tools,
        session_id="admission-test",
    )

    retained_raw_edges = messages[:2] + messages[-1:]
    assert admission.floor_tokens > estimate_session_tokens(
        retained_raw_edges,
        system=system,
        tools=tools,
    )
    assert policy.admits(admission.floor_tokens)
    assert admission.tool_count == 1
    assert admission.min_messages == policy.min_messages


def test_context_window_threshold_is_an_inclusive_admission_boundary():
    from brain.systems.context.budget import ModelContextBudget
    from brain.systems.context.window_policy import ContextWindowPolicy

    budget = ModelContextBudget(
        provider="openai",
        model="gpt-5.5",
        context_window_tokens=1_000,
        reserved_output_tokens=0,
        reserved_reasoning_tokens=0,
        reserved_tool_tokens=0,
        safety_margin_tokens=0,
        effective_input_limit_tokens=500,
        auto_compact_threshold_tokens=500,
        target_tokens=350,
        emergency_target_tokens=250,
    )
    policy = ContextWindowPolicy(budget)

    assert policy.admits(500) is True
    assert policy.requires_compaction(500) is False
    assert policy.admits(501) is False
    assert policy.requires_compaction(501) is True


def test_structured_checkpoint_compaction_uses_injected_semantic_compactor():
    from brain.systems.context.semantic_compaction import plan_session_compaction

    messages = [{"role": "user", "content": "Build the compaction harness cleanly."}]
    for index in range(16):
        messages.append({"role": "assistant", "content": [{"type": "text", "text": f"work note {index}"}]})
        messages.append({"role": "user", "content": f"follow up {index}"})
    messages.append({"role": "user", "content": "Latest raw request: keep the harness modular."})

    def semantic_compactor(omitted, context):
        assert omitted
        assert "schema" in context
        return {
            "active_objective": "Build modular context compaction.",
            "user_constraints": ["Keep the harness easy to iterate on."],
            "current_plan": ["Separate budget policy from checkpoint rendering."],
            "recent_user_intent": "Latest raw request: keep the harness modular.",
            "verification_status": "not run",
        }

    plan = plan_session_compaction(
        messages,
        token_limit=10,
        target_tokens=10,
        session_id="semantic-session",
        phase="test",
        max_messages=12,
        min_messages=4,
        force=True,
        semantic_compactor=semantic_compactor,
    )
    compacted, report = plan.messages, plan.report

    checkpoint_text = "\n".join(
        msg.get("content", "")
        for msg in compacted
        if isinstance(msg.get("content"), str)
    )
    assert report.strategy == "semantic_checkpoint"
    assert report.provenance["checkpoint_source"] == "semantic_compactor"
    assert "Build modular context compaction" in checkpoint_text
    assert "Latest raw request: keep the harness modular." == compacted[-1]["content"]


def test_structured_checkpoint_falls_back_and_retains_constraints():
    from brain.systems.context.semantic_compaction import plan_session_compaction

    messages = [
        {"role": "user", "content": "Never edit billing.py. Must preserve tool-call boundaries."},
    ]
    for index in range(14):
        messages.append({"role": "assistant", "content": [{"type": "text", "text": f"step {index}"}]})
        messages.append({"role": "user", "content": f"message {index}"})
    messages.append({"role": "user", "content": "Latest raw request: retry after overflow."})

    def broken_compactor(_omitted, _context):
        raise RuntimeError("summary model unavailable")

    plan = plan_session_compaction(
        messages,
        token_limit=10,
        target_tokens=10,
        session_id="fallback-session",
        phase="test",
        max_messages=10,
        min_messages=4,
        force=True,
        semantic_compactor=broken_compactor,
    )
    compacted, report = plan.messages, plan.report

    checkpoint_text = "\n".join(
        msg.get("content", "")
        for msg in compacted
        if isinstance(msg.get("content"), str)
    )
    assert report.strategy == "structured_checkpoint_fallback"
    assert report.provenance["semantic_fallback_error"].startswith("RuntimeError")
    assert "Never edit billing.py" in checkpoint_text
    assert compacted[-1]["content"] == "Latest raw request: retry after overflow."


def test_structured_checkpoint_fallback_uses_latest_user_intent_as_objective():
    import json

    from brain.systems.context.semantic_compaction import plan_session_compaction

    messages = [{"role": "user", "content": "First stale question: which project is attached?"}]
    for index in range(12):
        messages.append({"role": "assistant", "content": [{"type": "text", "text": f"old answer {index}"}]})
        messages.append({"role": "user", "content": f"old follow-up {index}"})
    messages.append({"role": "user", "content": "Latest raw request: explain the GitHub PR blocker."})

    def broken_compactor(_omitted, _context):
        raise RuntimeError("summary model unavailable")

    plan = plan_session_compaction(
        messages,
        token_limit=10,
        target_tokens=10,
        session_id="latest-intent-session",
        phase="test",
        max_messages=10,
        min_messages=4,
        force=True,
        semantic_compactor=broken_compactor,
    )
    compacted, report = plan.messages, plan.report

    checkpoint_text = next(
        msg["content"]
        for msg in compacted
        if isinstance(msg.get("content"), str) and "Context compaction checkpoint" in msg["content"]
    )
    payload = json.loads(checkpoint_text[checkpoint_text.find("{"):checkpoint_text.rfind("}") + 1])

    assert report.strategy == "structured_checkpoint_fallback"
    assert payload["active_objective"] == "Latest raw request: explain the GitHub PR blocker."
    assert payload["recent_user_intent"] == "Latest raw request: explain the GitHub PR blocker."


def test_structured_checkpoint_fallback_ignores_tool_results_as_latest_intent():
    from brain.systems.context.semantic_compaction import deterministic_checkpoint_from_messages

    checkpoint = deterministic_checkpoint_from_messages(
        [
            {"role": "user", "content": "Latest human request: finish the GitHub diagnosis."},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "tool-1", "name": "read_file", "input": {}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tool-1", "content": "large file output"}]},
        ],
        recent_messages=[
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tool-2", "content": "newer tool output"}]},
        ],
        session_id="tool-result-intent-session",
    )

    assert checkpoint.active_objective == "Latest human request: finish the GitHub diagnosis."
    assert checkpoint.recent_user_intent == "Latest human request: finish the GitHub diagnosis."


def test_thread_handoff_builds_startup_context_with_recent_raw_messages():
    from brain.systems.context.thread_handoff import (
        build_thread_handoff,
        build_thread_handoff_context_messages,
    )

    messages = [{"role": "user", "content": "Start durable handoff work. Never drop exact constraints."}]
    for index in range(20):
        messages.append({"role": "assistant", "content": [{"type": "text", "text": f"completed step {index}"}]})
        messages.append({"role": "user", "content": f"follow up {index}"})
    messages.append({"role": "user", "content": "Latest exact request should stay raw."})

    handoff, error = build_thread_handoff(
        previous_handoff=None,
        messages_since=messages[:-6],
        total_message_count=len(messages) - 6,
        session_id="handoff-session",
    )
    startup = build_thread_handoff_context_messages(
        messages,
        handoff=handoff,
        max_recent_messages=8,
    )

    assert error is None
    assert startup[0]["role"] == "user"
    assert "Durable thread handoff summary" in startup[0]["content"]
    assert len(startup) < len(messages)
    assert startup[-1]["content"] == "Latest exact request should stay raw."
    assert "read_thread_messages" in startup[0]["content"]


def test_thread_handoff_fallback_uses_latest_user_intent_as_objective():
    from brain.systems.context.thread_handoff import build_thread_handoff

    messages = [{"role": "user", "content": "First stale question: what context is connected?"}]
    for index in range(10):
        messages.append({"role": "assistant", "content": [{"type": "text", "text": f"context answer {index}"}]})
        messages.append({"role": "user", "content": f"follow-up {index}"})
    messages.append({"role": "user", "content": "Latest raw request: what exact GitHub setup do you need?"})

    handoff, error = build_thread_handoff(
        previous_handoff=None,
        messages_since=messages,
        total_message_count=len(messages),
        session_id="handoff-latest-intent-session",
    )
    payload = handoff.to_payload()

    assert error is None
    assert payload["checkpoint"]["active_objective"] == "Latest raw request: what exact GitHub setup do you need?"
    assert payload["checkpoint"]["recent_user_intent"] == "Latest raw request: what exact GitHub setup do you need?"


def test_thread_handoff_incrementally_carries_previous_summary():
    from brain.systems.context.thread_handoff import build_thread_handoff

    first_messages = [
        {"role": "user", "content": "Must keep the acceptance contract."},
        {"role": "assistant", "content": [{"type": "text", "text": "Implemented phase one."}]},
    ]
    first, _ = build_thread_handoff(
        previous_handoff=None,
        messages_since=first_messages,
        total_message_count=len(first_messages),
        session_id="incremental-session",
    )
    second, _ = build_thread_handoff(
        previous_handoff=first,
        messages_since=[
            {"role": "user", "content": "Now add the raw-message retrieval tool."},
            {"role": "assistant", "content": [{"type": "text", "text": "Added read_thread_messages."}]},
        ],
        total_message_count=4,
        session_id="incremental-session",
    )

    payload = second.to_payload()
    assert payload["message_count"] == 4
    assert payload["previous_message_count"] == 2
    assert payload["metadata"]["previous_handoff_digest"] == first.digest
