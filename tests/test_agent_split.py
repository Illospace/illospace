"""Tests for agent module split — verifies all public API contracts survive refactoring.

These tests lock down every import path that production code uses, plus
internal helpers that test_agent.py relies on. If the refactor breaks any
import, these tests catch it immediately.
"""

import importlib
import inspect
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


# ── 1. Public API: every symbol that production code imports ──────────


class TestPublicImports:
    """Every from brain.systems.runs.direct_agent import X used in production must work."""

    def test_import_run_agent(self):
        from brain.systems.runs.direct_agent import run_agent
        assert callable(run_agent)

    def test_import_call_llm(self):
        from brain.systems.runs.direct_agent import call_llm
        assert callable(call_llm)

    def test_import_brain_tools(self):
        from brain.systems.runs.direct_agent import BRAIN_TOOLS
        assert isinstance(BRAIN_TOOLS, list)
        assert len(BRAIN_TOOLS) > 0

    def test_import_exec_tools(self):
        from brain.systems.runs.direct_agent import EXEC_TOOLS
        assert isinstance(EXEC_TOOLS, list)
        assert len(EXEC_TOOLS) > 0

    def test_import_worker_tools(self):
        from brain.systems.runs.direct_agent import WORKER_TOOLS
        assert isinstance(WORKER_TOOLS, list)

    def test_import_coordinator_tools(self):
        from brain.systems.runs.direct_agent import COORDINATOR_TOOLS
        assert isinstance(COORDINATOR_TOOLS, list)

    def test_import_agent_result(self):
        from brain.systems.runs.direct_agent import AgentResult
        field_names = {f.name for f in AgentResult.__dataclass_fields__.values()}
        assert "output" in field_names
        assert "success" in field_names

    def test_import_get_tools_with_extended(self):
        from brain.systems.runs.direct_agent import get_tools_with_extended
        assert callable(get_tools_with_extended)

    def test_import_agent_context(self):
        from brain.systems.runs.direct_agent import _agent_context
        assert _agent_context is not None

    def test_import_get_tool_handlers(self):
        from brain.systems.runs.direct_agent import _get_tool_handlers
        assert callable(_get_tool_handlers)


# ── 2. Internal helpers used by test_agent.py ─────────────────────────


class TestInternalImports:
    """Internal symbols that test_agent.py imports."""

    def test_import_normalize_model(self):
        from brain.systems.runs.direct_agent import _normalize_model
        assert callable(_normalize_model)

    def test_import_strip_thinking(self):
        from brain.systems.runs.direct_agent import _strip_thinking_from_messages
        assert callable(_strip_thinking_from_messages)

    def test_import_extract_text(self):
        from brain.systems.runs.direct_agent import _extract_text
        assert callable(_extract_text)

    def test_import_load_session(self):
        from brain.systems.runs.direct_agent import _load_session
        assert callable(_load_session)

    def test_import_save_session(self):
        from brain.systems.runs.direct_agent import _save_session
        assert callable(_save_session)

    def test_import_sanitize_tool_pairs(self):
        from brain.systems.runs.direct_agent import _sanitize_tool_pairs
        assert callable(_sanitize_tool_pairs)

    def test_import_brain_tool_names(self):
        from brain.systems.runs.direct_agent import _BRAIN_TOOL_NAMES
        assert isinstance(_BRAIN_TOOL_NAMES, frozenset)

    def test_import_gated_tool_names(self):
        from brain.systems.runs.direct_agent import _GATED_TOOL_NAMES
        assert isinstance(_GATED_TOOL_NAMES, frozenset)

    def test_import_agent_loop_state(self):
        from brain.systems.runs.direct_agent import AgentLoopState
        assert AgentLoopState.__name__ == "AgentLoopState"


# ── 3. Tool definition contracts ──────────────────────────────────────


class TestToolDefinitionContracts:
    """Tool schemas must have the expected shape after refactoring."""

    def test_brain_tools_have_required_fields(self):
        from brain.systems.runs.direct_agent import BRAIN_TOOLS
        for tool in BRAIN_TOOLS:
            assert "name" in tool, f"Tool missing 'name': {tool}"
            assert "description" in tool, f"Tool {tool['name']} missing 'description'"
            assert "input_schema" in tool, f"Tool {tool['name']} missing 'input_schema'"

    def test_exec_tools_have_required_fields(self):
        from brain.systems.runs.direct_agent import EXEC_TOOLS
        for tool in EXEC_TOOLS:
            assert "name" in tool
            assert "input_schema" in tool

    def test_brain_tool_names_match(self):
        from brain.systems.runs.direct_agent import BRAIN_TOOLS
        expected = {
            "brain_recall",
            "brain_guardrails",
            "brain_skills",
            "skill_view",
            "manage_skill",
            "skill_asset",
            "brain_encode",
            "memory_reconstruct",
            "memory_ingest_source",
            "memory_link",
            "memory_supersede",
            "memory_archive",
            "vault_inventory",
            "brain_vault",
            "vault_secret_prompt",
            "runtime_settings",
            "read_self_context",
            "read_capabilities",
            "transcribe_audio_attachment",
            "read_thread_messages",
            "query_workspace_data",
            "read_workspace_overview",
            "read_team_activity",
            "read_project_contexts",
            "read_team_members",
            "read_workspace_records",
            "read_cycles",
            "read_workspace_apps",
            "manage_cycle",
        }
        actual = {t["name"] for t in BRAIN_TOOLS}
        assert actual == expected

    def test_exec_tool_names_match(self):
        from brain.systems.runs.direct_agent import EXEC_TOOLS
        expected = {
            "exec_command",
            "read_file",
            "write_file",
            "edit_file",
            "search_files",
            "list_files",
            "run_script",
            "parallel_tool_batch",
            "web_search",
            "web_fetch",
            "parallel_tool_batch",
        }
        actual = {t["name"] for t in EXEC_TOOLS}
        assert actual == expected

    def test_coordinator_tools_superset_of_worker(self):
        from brain.systems.runs.direct_agent import COORDINATOR_TOOLS, WORKER_TOOLS
        coord_names = {t["name"] for t in COORDINATOR_TOOLS}
        worker_names = {t["name"] for t in WORKER_TOOLS}
        assert worker_names.issubset(coord_names)

    def test_coordinator_tools_include_cortex_and_activity_tools(self):
        from brain.systems.runs.direct_agent import COORDINATOR_TOOLS
        names = {t["name"] for t in COORDINATOR_TOOLS}
        assert "cortex_reply" in names
        assert "cortex_visual_reply" in names
        assert "my_activity" in names

    def test_skill_authoring_uses_umbrella_tool(self):
        from brain.systems.runs.direct_agent import COORDINATOR_TOOLS, WORKER_TOOLS
        names = {t["name"] for t in COORDINATOR_TOOLS + WORKER_TOOLS}
        assert "manage_skill" in names
        assert "create_skill" not in names
        assert "manage_skill_asset" not in names
        assert "flag_skill_gap" not in names


# ── 4. Handler contracts ──────────────────────────────────────────────


class TestHandlerContracts:
    """Tool handlers must return dicts and cover all tool names."""

    def test_get_tool_handlers_returns_dict(self):
        from brain.systems.runs.direct_agent import _get_tool_handlers
        handlers = _get_tool_handlers()
        assert isinstance(handlers, dict)

    def test_handlers_cover_brain_tools(self):
        from brain.systems.runs.direct_agent import _get_tool_handlers, BRAIN_TOOLS
        handlers = _get_tool_handlers()
        for tool in BRAIN_TOOLS:
            assert tool["name"] in handlers, f"No handler for brain tool '{tool['name']}'"

    def test_handlers_cover_exec_tools(self):
        from brain.systems.runs.direct_agent import _get_tool_handlers, EXEC_TOOLS
        handlers = _get_tool_handlers()
        for tool in EXEC_TOOLS:
            assert tool["name"] in handlers, f"No handler for exec tool '{tool['name']}'"

    def test_handlers_are_callable(self):
        from brain.systems.runs.direct_agent import _get_tool_handlers
        handlers = _get_tool_handlers()
        for name, handler in handlers.items():
            assert callable(handler), f"Handler '{name}' is not callable"


# ── 5. Behavioral contracts ───────────────────────────────────────────


class TestBehavioralContracts:
    """Key behaviors must survive the refactor."""

    def test_normalize_model_strips_prefix(self):
        from brain.systems.runs.direct_agent import _normalize_model
        assert _normalize_model("anthropic/claude-sonnet-4-6") == "claude-sonnet-4-6"
        assert _normalize_model("claude-sonnet-4-6") == "claude-sonnet-4-6"

    def test_strip_thinking_removes_thinking_blocks(self):
        from brain.systems.runs.direct_agent import _strip_thinking_from_messages
        messages = [
            {"role": "assistant", "content": [
                {"type": "thinking", "thinking": "hmm"},
                {"type": "text", "text": "hello"},
            ]},
        ]
        result = _strip_thinking_from_messages(messages)
        for msg in result:
            if isinstance(msg.get("content"), list):
                for block in msg["content"]:
                    assert block.get("type") != "thinking"

    def test_extract_text_from_assistant(self):
        from brain.systems.runs.direct_agent import _extract_text
        messages = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": [{"type": "text", "text": "hello back"}]},
        ]
        assert _extract_text(messages) == "hello back"

    def test_agent_result_dataclass(self):
        from brain.systems.runs.direct_agent import AgentResult
        r = AgentResult(output="test", success=True, session_id="s1")
        assert r.output == "test"
        assert r.tokens_input == 0
        assert r.tool_calls == []

    def test_brain_gate_sets(self):
        from brain.systems.runs.direct_agent import _BRAIN_TOOL_NAMES, _GATED_TOOL_NAMES
        assert "brain_recall" in _BRAIN_TOOL_NAMES
        assert "write_file" in _GATED_TOOL_NAMES
        assert "cortex_reply" in _GATED_TOOL_NAMES

    def test_sanitize_tool_pairs_no_orphans(self):
        from brain.systems.runs.direct_agent import _sanitize_tool_pairs
        messages = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": [{"type": "text", "text": "hello"}]},
        ]
        result = _sanitize_tool_pairs(messages)
        assert len(result) == 2


# ── 6. run_agent signature contract ──────────────────────────────────


class TestRunAgentSignature:
    """run_agent must accept all the kwargs that callers use."""

    def test_run_agent_signature(self):
        from brain.systems.runs.direct_agent import run_agent
        sig = inspect.signature(run_agent)
        expected_params = {
            "message", "system_prompt", "session_id", "model", "thinking",
            "tools", "tool_handlers", "max_turns",
            "cache_system_prompt", "persist_session", "on_tool_call",
            "workspace_root", "brain_context_preloaded", "run_id",
            "cancel_event", "on_stream_activity",
            "user_id", "skip_harvest",
        }
        actual_params = set(sig.parameters.keys())
        assert expected_params.issubset(actual_params), (
            f"Missing params: {expected_params - actual_params}"
        )


# ── 7. Runtime extraction contracts ──────────────────────────────────


class TestRuntimeExtractionContracts:
    """New runtime collaborators stay importable and behavior-preserving."""

    def test_agent_loop_state_tracks_loop_mutables(self):
        from brain.systems.runs.direct_loop.gates import GateState
        from brain.systems.runs.direct_loop.result import TokenAccumulator
        from brain.systems.runs.direct_loop.state import AgentLoopState

        state = AgentLoopState(operation_type="worker", metadata={"role": "worker"})
        state.messages.append({"role": "user", "content": "hi"})
        state.recent_calls.append("read_file:{}")
        state.tool_calls_made.append("read_file")

        assert state.messages == [{"role": "user", "content": "hi"}]
        assert isinstance(state.gates, GateState)
        assert isinstance(state.tokens, TokenAccumulator)
        assert state.recent_calls == ["read_file:{}"]
        assert state.tool_calls_made == ["read_file"]
        assert state.operation_type == "worker"
        assert state.metadata == {"role": "worker"}

    def test_loop_control_helpers_are_runtime_owned_and_reexported(self):
        from brain.systems.runs.direct_agent import _detect_stuck_loop as agent_detect_stuck_loop
        from brain.systems.runs.direct_agent import _inject_nudges as agent_inject_nudges
        from brain.systems.runs.direct_loop.loop_control import _detect_stuck_loop, _inject_nudges

        assert agent_detect_stuck_loop is _detect_stuck_loop
        assert agent_inject_nudges is _inject_nudges

        messages = []
        assert _detect_stuck_loop(["read_file:{}"] * 5, "session-1", messages)
        # The fabricated stuck message must not masquerade as model output.
        assert messages[-1]["role"] == "user"
        assert "stuck in a loop" in messages[-1]["content"][0]["text"]

    def test_inject_nudges_returns_only_durable_reminder(self):
        from brain.systems.runs.direct_loop.loop_control import _inject_nudges

        # Below the warn threshold: nothing to say.
        assert _inject_nudges(["read_file:{}"]) is None

        # 3 identical calls: durable `cd` reminder, no strategy second-guessing.
        message = _inject_nudges(["read_file:{}"] * 3)
        assert message is not None
        assert message["role"] == "user"
        assert "cd` does not persist" in message["content"]
        assert "different strategy" not in message["content"]
        assert "run_script" not in message["content"]
        assert "adding NEW information" not in message["content"]

        # Non-repeating calls: no reminder.
        assert _inject_nudges(["read_file:{}", "write_file:{}", "list_files:{}"]) is None

    def test_session_effects_apply_harvest_and_save(self):
        from brain.systems.runs.direct_loop.session_effects import apply_agent_session_side_effects

        calls = []
        tokens = SimpleNamespace(input=10, output=5, cache_read=2, cache_creation=1)
        messages = [{"role": "user", "content": "hello"}]

        def harvest(session_id, harvested_messages, **kwargs):
            calls.append(("harvest", session_id, harvested_messages, kwargs))

        def save(session_id, saved_messages, system_prompt, *token_args):
            calls.append(("save", session_id, saved_messages, system_prompt, token_args))

        def memory_org(user_id):
            calls.append(("memory_org", user_id))
            return "org-from-memory"

        effective_org_id = apply_agent_session_side_effects(
            session_id="session-1",
            messages=messages,
            output="A routine file-change result long enough to exercise session side effects.",
            system_prompt="system",
            tokens=tokens,
            tool_calls_made=["write_file"],
            user_id="user-1",
            metadata={"org_id": "org-from-metadata"},
            agent_context=SimpleNamespace(org_id="org-from-agent-run"),
            idea_id="idea-1",
            run_id=42,
            memory_org_for_user=memory_org,
            harvest_session=harvest,
            save_session=save,
        )

        assert effective_org_id == "org-from-agent-run"
        assert [call[0] for call in calls] == ["harvest", "save"]
        assert calls[0][3]["org_id"] == "org-from-agent-run"
        assert calls[1][4] == (10, 5, 2, 1)
