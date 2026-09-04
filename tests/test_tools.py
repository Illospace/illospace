"""Tests for core/tools.py — extended agent tools."""

import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), *([".."] * 1))))


class TestToolDefinitions:
    """Test extended tool definitions."""

    @pytest.mark.asyncio
    async def test_reader_uses_luna_without_requiring_astra_access(self):
        from brain.systems.tools.handlers import _reader_completion

        llm = MagicMock(provider="openai")
        llm.build_request_headers.return_value = {}
        provider = MagicMock()

        def create(request):
            assert request.model == "gpt-5.6-luna"
            return MagicMock(content=[MagicMock(type="text", text='{"answer":"file evidence"}')])

        provider.create.side_effect = create
        with (
            patch("brain.platform.providers.model_policy.resolve_default_provider", return_value="openai"),
            patch("brain.platform.integrations.completions.async_resolve_llm_client", new=AsyncMock(return_value=llm)) as resolve,
            patch("brain.platform.integrations.completions.get_provider", return_value=provider),
            patch("brain.systems.runs.direct_loop.telemetry.async_record_api_call", new=AsyncMock()) as record,
        ):
            result = await _reader_completion("Read this file", user_id="user-1", org_id="org-1")

        assert result == {"answer": "file evidence", "model": "openai/gpt-5.6-luna"}
        assert resolve.await_args.kwargs["auth_mode"] == "chatgpt"
        assert record.await_args.kwargs["model"] == "openai/gpt-5.6-luna"

    def test_extended_tools_defined(self):
        from brain.systems.tools.handlers import EXTENDED_TOOLS
        names = [t["name"] for t in EXTENDED_TOOLS]
        assert "semantic_search" in names
        assert "file_summary" in names
        assert "test_runner" in names
        assert "project_context" in names
        assert "summarize_file_for_task" in names
        assert "summarize_files_for_task" in names
        assert "trace_symbol" in names
        assert "build_implementation_map" in names


    @pytest.mark.asyncio
    async def test_reader_subcall_uses_async_completion_with_run_identity(self):
        from brain.systems.runs.execution_context import bind_agent_context
        from brain.systems.tools.handlers import _reader_completion

        response = '{"answer":"stored Codex auth works"}'
        async_completion = AsyncMock(return_value=response)
        record_api_call = AsyncMock()

        with (
            patch(
                "brain.platform.integrations.completions.async_simple_text_completion",
                new=async_completion,
            ),
            patch(
                "brain.platform.integrations.completions.simple_text_completion",
                side_effect=RuntimeError(
                    "No OpenAI auth found. user Codex subscription credentials require "
                    "async_resolve_llm_client."
                ),
            ) as sync_completion,
            patch(
                "brain.systems.runs.direct_loop.telemetry.async_record_api_call",
                new=record_api_call,
            ),
            patch(
                "brain.systems.tools.handlers._reader_model",
                return_value="openai/gpt-5.6-sol",
            ),
            bind_agent_context(
                session_id="agent-run-7",
                run=SimpleNamespace(run_id=7),
            ),
        ):
            result = await _reader_completion(
                "find the auth boundary",
                user_id="user-1",
                org_id="org-1",
            )

        assert result == {"answer": "stored Codex auth works", "model": "openai/gpt-5.6-sol"}
        async_completion.assert_awaited_once()
        assert async_completion.await_args.kwargs["user_id"] == "user-1"
        assert async_completion.await_args.kwargs["org_id"] == "org-1"
        sync_completion.assert_not_called()
        record_api_call.assert_awaited_once()
        assert record_api_call.await_args.kwargs["status"] == "success"
        assert record_api_call.await_args.kwargs["error"] is None

    def test_all_tools_have_schema(self):
        from brain.systems.tools.handlers import EXTENDED_TOOLS
        for tool in EXTENDED_TOOLS:
            assert "input_schema" in tool, f"{tool['name']} missing schema"
            assert tool["input_schema"]["type"] == "object"

    def test_handlers_match_definitions(self):
        from brain.systems.tools.handlers import EXTENDED_TOOLS, get_extended_handlers
        handlers = get_extended_handlers()
        for tool in EXTENDED_TOOLS:
            assert tool["name"] in handlers, f"No handler for {tool['name']}"


class TestFileSummary:
    """Test file_summary tool."""

    def test_summary_python_file(self, tmp_path):
        from brain.systems.tools.handlers import handle_file_summary
        # Create a test Python file
        py_file = tmp_path / "test_module.py"
        py_file.write_text('''"""A test module."""

import os
from pathlib import Path

class MyClass:
    """A class."""
    def method_one(self):
        pass
    def method_two(self):
        pass

def helper_function():
    pass
''')
        with patch("brain.systems.tools.handlers.WORKSPACE_ROOT", str(tmp_path)):
            result = handle_file_summary(str(py_file))

        assert result["line_count"] > 0
        assert result["extension"] == ".py"
        assert "MyClass" in [c["name"] for c in result["classes"]]
        assert "helper_function" in [f["name"] for f in result["functions"]]
        assert len(result["classes"][0]["methods"]) == 2
        assert "A test module." in result.get("docstring", "")

    def test_summary_nonexistent_file(self, tmp_path):
        from brain.systems.tools.handlers import handle_file_summary
        with patch("brain.systems.tools.handlers.WORKSPACE_ROOT", str(tmp_path)):
            result = handle_file_summary(str(tmp_path / "missing.py"))
        assert "error" in result

    def test_summary_non_python(self, tmp_path):
        from brain.systems.tools.handlers import handle_file_summary
        txt_file = tmp_path / "readme.txt"
        txt_file.write_text("Hello world\nLine 2\n")
        with patch("brain.systems.tools.handlers.WORKSPACE_ROOT", str(tmp_path)):
            result = handle_file_summary(str(txt_file))
        assert result["line_count"] == 2
        assert "preview" in result


class TestTestRunner:
    """Test test_runner tool."""

    def test_runner_success(self):
        from brain.systems.tools.handlers import handle_test_runner
        result = handle_test_runner("tests/test_cognition.py::TestSimpleTaskHeuristic::test_simple_tasks")
        assert result["success"] is True
        assert result["exit_code"] == 0

    def test_runner_with_pattern(self):
        from brain.systems.tools.handlers import handle_test_runner
        result = handle_test_runner("tests/test_cognition.py", pattern="test_simple_tasks")
        assert result["success"] is True

    def test_runner_failure_details(self):
        from brain.systems.tools.handlers import handle_test_runner
        result = handle_test_runner("tests/nonexistent_test.py")
        assert result["success"] is False


class TestProjectContext:
    """Test project_context tool."""

    def test_detects_python_project(self):
        from brain.systems.tools.handlers import handle_project_context
        result = handle_project_context()
        assert result["type"] == "python"
        assert len(result["directories"]) > 0

    def test_has_recent_commits(self):
        from brain.systems.tools.handlers import handle_project_context
        result = handle_project_context()
        assert "recent_commits" in result
        assert len(result["recent_commits"]) > 0


class TestSemanticSearch:
    """Test semantic_search tool."""

    @patch("brain.app.mcp.server.async_tool_brain_recall", new_callable=AsyncMock)
    async def test_memory_search(self, mock_recall):
        from brain.systems.tools.handlers import handle_semantic_search
        mock_recall.return_value = {
            "count": 1,
            "memories": [{"content": "Redis timeout fix", "type": "lesson", "similarity": 0.8}],
        }
        result = await handle_semantic_search("redis issues", scope="memories")
        assert result["count"] >= 1
        assert result["results"][0]["source"] == "memory"

    async def test_search_returns_results_structure(self):
        from brain.systems.tools.handlers import handle_semantic_search
        # Even if backends fail, should return valid structure
        result = await handle_semantic_search("test query", scope="both", limit=3)
        assert "results" in result
        assert "count" in result
        assert isinstance(result["results"], list)


class TestIntegration:
    """Test tools integration with agent."""

    def test_get_tools_with_extended(self):
        from brain.systems.runs.direct_agent import get_tools_with_extended, WORKER_TOOLS
        extended = get_tools_with_extended(WORKER_TOOLS)
        names = [t["name"] for t in extended]
        # Should have base tools
        assert "read_file" in names
        assert "parallel_tool_batch" in names
        assert "brain_recall" in names
        # Should have extended tools
        assert "semantic_search" in names
        assert "file_summary" in names
        assert "summarize_file_for_task" in names
        assert "summarize_files_for_task" in names
        assert "trace_symbol" in names
        assert "build_implementation_map" in names

    def test_handlers_include_extended(self):
        from brain.systems.runs.direct_agent import _get_tool_handlers
        handlers = _get_tool_handlers()
        assert "manage_inbound" in handlers
        assert "parallel_tool_batch" in handlers
        assert "semantic_search" in handlers
        assert "file_summary" in handlers
        assert "test_runner" in handlers
        assert "project_context" in handlers
        assert "summarize_file_for_task" in handlers
        assert "summarize_files_for_task" in handlers
        assert "trace_symbol" in handlers
        assert "build_implementation_map" in handlers

    def test_illo_can_see_inbound_configuration_tool(self):
        from brain.systems.runs.direct_agent import COORDINATOR_TOOLS, WORKER_TOOLS
        from brain.systems.runs.tool_catalog.registry import action_policy_for_tool

        coordinator_names = [tool["name"] for tool in COORDINATOR_TOOLS]
        worker_names = [tool["name"] for tool in WORKER_TOOLS]

        assert "manage_inbound" in coordinator_names
        assert "manage_inbound" in worker_names
        assert action_policy_for_tool("manage_inbound", kwargs={"action": "list_connections"}) is None
        assert action_policy_for_tool("manage_inbound", kwargs={"action": "replay_events"}) is None
        assert action_policy_for_tool("manage_inbound", kwargs={"action": "get_source_card"}) is None
        assert action_policy_for_tool("manage_inbound", kwargs={"action": "refresh_source_card"}) == {
            "risk": "high",
            "reversibility": "variable",
            "expected_effect": "refresh persisted inbound source-card metadata",
        }
        assert action_policy_for_tool("manage_inbound", kwargs={"action": "mint_token"}) == {
            "risk": "high",
            "reversibility": "variable",
            "expected_effect": "mint a scoped source token for inbound signal submission",
        }

    async def test_brain_recall_handler_injects_agent_context(self):
        from brain.systems.runs.execution_context import AgentExecutionContext, bind_agent_context
        from brain.systems.runs.tool_handlers import _get_tool_handlers

        captured = {}

        async def fake_recall(**kwargs):
            captured.update(kwargs)
            return {"memories": []}

        with patch("brain.app.mcp.server.async_tool_brain_recall", side_effect=fake_recall):
            handlers = _get_tool_handlers()
            with bind_agent_context(
                AgentExecutionContext(
                    user_id="user-1",
                    org_id="org-1",
                    run=SimpleNamespace(run_id=42),
                )
            ):
                result = await handlers["brain_recall"](query="deployment notes")

        assert result == {"memories": []}
        assert captured["query"] == "deployment notes"
        assert captured["user_id"] == "user-1"
        assert captured["org_id"] == "org-1"
        assert captured["run_id"] == 42

    def test_extended_tools_default_to_current_workspace_root(self, tmp_path):
        from brain.systems.runs.tool_handlers import _get_tool_handlers, _agent_context

        backend = tmp_path / "backend"
        backend.mkdir()
        (backend / "requirements.txt").write_text("pytest\n")

        _agent_context.workspace_root = str(backend)
        try:
            handlers = _get_tool_handlers()
            result = handlers["project_context"]()
        finally:
            _agent_context.workspace_root = None

        assert result["root"] == str(backend)
        assert result["type"] == "python"

    @patch("brain.systems.tools.handlers.subprocess.run")
    def test_test_runner_uses_current_workspace_root_hint(self, mock_run, tmp_path):
        from brain.systems.runs.tool_handlers import _get_tool_handlers, _agent_context

        backend = tmp_path / "backend"
        backend.mkdir()
        _agent_context.workspace_root = str(backend)
        mock_run.return_value = SimpleNamespace(returncode=0, stdout="1 passed\n", stderr="")

        try:
            handlers = _get_tool_handlers()
            result = handlers["test_runner"]("tests/test_smoke.py")
        finally:
            _agent_context.workspace_root = None

        assert result["success"] is True
        assert mock_run.call_args.kwargs["cwd"] == str(backend)


class TestPredictiveReading:
    """Test predictive reading tools."""

    async def test_summarize_file_for_task_fallback(self, tmp_path):
        from brain.systems.tools.handlers import handle_summarize_file_for_task

        py_file = tmp_path / "service.py"
        body = ['import json', '']
        body.extend(
            [
                f"def helper_{i}():",
                f"    return {i}",
                ""
            ]
            for i in range(40)
        )
        flattened = []
        for group in body:
            if isinstance(group, list):
                flattened.extend(group)
            else:
                flattened.append(group)
        flattened.extend([
            "def route_with_confidence(branch, title, body):",
            '    """Route a branch based on body contents."""',
            '    if "bug" in body:',
            '        return {"route": "fix", "confidence": 0.9}',
            '    return {"route": "review", "confidence": 0.6}',
        ])
        py_file.write_text("\n".join(flattened))

        with patch("brain.systems.tools.handlers.WORKSPACE_ROOT", str(tmp_path)):
            with patch("brain.systems.tools.handlers._reader_completion", new=AsyncMock(return_value=None)):
                result = await handle_summarize_file_for_task(
                    str(py_file),
                    "Where is route_with_confidence defined and what does it return?",
                )

        assert result["path"] == str(py_file)
        assert result["model"] == "deterministic-multihop-fallback"
        assert "route_with_confidence" in result["key_symbols"]
        assert isinstance(result["citations"], list)
        assert result["confidence"] > 0
        assert len(result["relevant_ranges"]) >= 1

    async def test_summarize_file_for_task_can_skip_llm_reader(self, tmp_path):
        from brain.systems.tools.handlers import handle_summarize_file_for_task

        py_file = tmp_path / "service.py"
        py_file.write_text(
            """def route_with_confidence(body):
    if "bug" in body:
        return {"route": "fix", "confidence": 0.9}
    return {"route": "review", "confidence": 0.6}
"""
        )

        with patch("brain.systems.tools.handlers.WORKSPACE_ROOT", str(tmp_path)):
            with patch("brain.systems.tools.handlers._reader_completion", new_callable=AsyncMock) as mock_reader:
                result = await handle_summarize_file_for_task(
                    str(py_file),
                    "What does route_with_confidence return?",
                    allow_llm=False,
                )

        mock_reader.assert_not_called()
        assert result["model"] == "deterministic-multihop-fallback"
        assert "route_with_confidence" in result["key_symbols"]

    async def test_summarize_file_for_task_threads_agent_identity(self, tmp_path):
        from brain.systems.runs.execution_context import _agent_context
        from brain.systems.runs.tool_handlers import _get_tool_handlers

        py_file = tmp_path / "service.py"
        py_file.write_text("def route_with_confidence():\n    return 0.9\n")
        previous = vars(_agent_context).copy()
        for key in list(vars(_agent_context).keys()):
            delattr(_agent_context, key)
        _agent_context.user_id = "user-123"
        _agent_context.org_id = "org-456"

        try:
            handlers = _get_tool_handlers(workspace_root=str(tmp_path))
            with patch("brain.systems.tools.handlers._reader_completion", new=AsyncMock(return_value=None)) as mock_reader:
                await handlers["summarize_file_for_task"](
                    str(py_file),
                    "Where is route_with_confidence defined?",
                )
        finally:
            for key in list(vars(_agent_context).keys()):
                delattr(_agent_context, key)
            for key, value in previous.items():
                setattr(_agent_context, key, value)

        mock_reader.assert_called_once()
        assert mock_reader.call_args.kwargs["user_id"] == "user-123"
        assert mock_reader.call_args.kwargs["org_id"] == "org-456"

    async def test_summarize_files_for_task_threads_metadata_identity(self, tmp_path):
        from brain.systems.runs.execution_context import _agent_context
        from brain.systems.runs.tool_handlers import _get_tool_handlers

        py_file = tmp_path / "service.py"
        py_file.write_text("def create_child_run():\n    return 'ok'\n")
        previous = vars(_agent_context).copy()
        for key in list(vars(_agent_context).keys()):
            delattr(_agent_context, key)
        _agent_context.execution_metadata = {"user_id": "metadata-user", "org_id": "metadata-org"}

        try:
            handlers = _get_tool_handlers(workspace_root=str(tmp_path))
            with patch("brain.systems.tools.handlers._reader_completion", new=AsyncMock(return_value=None)) as mock_reader:
                await handlers["summarize_files_for_task"](
                    [str(py_file)],
                    "Which file owns create_child_run?",
                )
        finally:
            for key in list(vars(_agent_context).keys()):
                delattr(_agent_context, key)
            for key, value in previous.items():
                setattr(_agent_context, key, value)

        mock_reader.assert_called_once()
        assert mock_reader.call_args.kwargs["user_id"] == "metadata-user"
        assert mock_reader.call_args.kwargs["org_id"] == "metadata-org"

    async def test_summarize_files_for_task_fallback(self, tmp_path):
        from brain.systems.tools.handlers import handle_summarize_files_for_task

        a = tmp_path / "a.py"
        b = tmp_path / "b.py"
        a.write_text(
            '''def build_agent_invocation():
    return "invocation"
'''
        )
        b.write_text(
            '''def create_child_run():
    return build_agent_invocation()
'''
        )

        with patch("brain.systems.tools.handlers.WORKSPACE_ROOT", str(tmp_path)):
            with patch("brain.systems.tools.handlers._reader_completion", new=AsyncMock(return_value=None)):
                result = await handle_summarize_files_for_task(
                    [str(a), str(b)],
                    "Which file owns create_child_run and invocation setup?",
                )

        assert result["model"] == "deterministic-implementation-map"
        assert result["file_count"] == 2
        assert len(result["files_ranked"]) == 2
        assert any(item["path"] == str(b) for item in result["files_ranked"])
        assert "implementation_map" in result

    async def test_summarize_files_for_task_llm_path(self, tmp_path):
        from brain.systems.tools.handlers import handle_summarize_files_for_task

        file_path = tmp_path / "workers.py"
        file_path.write_text(
            '''def create_child_run():
    return "ok"
'''
        )

        llm_payload = {
            "answer": "children.py owns child run creation",
            "files_ranked": [{"path": str(file_path), "relevance": 0.95, "why": "contains create_child_run"}],
            "cross_file_findings": ["create_child_run defined here"],
            "open_questions": [],
            "citations": [{"path": str(file_path), "start_line": 1, "end_line": 2, "reason": "create_child_run"}],
            "confidence": 0.88,
        }

        with patch("brain.systems.tools.handlers.WORKSPACE_ROOT", str(tmp_path)):
            with patch("brain.systems.tools.handlers._reader_completion", new=AsyncMock(return_value=llm_payload)):
                with patch("brain.systems.tools.handlers._reader_model", return_value="openai/gpt-5.6-luna"):
                    result = await handle_summarize_files_for_task(
                        [str(file_path)],
                        "Which file owns child run creation?",
                    )

        assert result["model"] == "openai/gpt-5.6-luna"
        assert result["confidence"] == 0.88
        assert result["files_ranked"][0]["path"] == str(file_path)

    def test_read_file_auto_redirects_under_reader_policy(self, tmp_path):
        from brain.systems.runs.tool_handlers import _get_tool_handlers

        target = tmp_path / "large_module.py"
        body = "\n".join(f"def fn_{i}():\n    return {i}\n" for i in range(180))
        target.write_text(body)

        with patch("brain.systems.tools.handlers.WORKSPACE_ROOT", str(tmp_path)):
            handlers = _get_tool_handlers(
                workspace_root=str(tmp_path),
                reader_policy={
                    "enabled": True,
                    "large_file_lines": 100,
                    "large_file_chars": 2000,
                    "task_hint": "implement a change in the right part of this file",
                    "focus": "behavior",
                },
            )
            summary_mock = MagicMock(
                return_value={
                    "answer": "summary",
                    "key_symbols": ["fn_1"],
                    "citations": [{"path": str(target), "start_line": 1, "end_line": 3, "reason": "fn_1"}],
                    "confidence": 0.7,
                }
            )
            with patch.dict(
                handlers,
                {
                    "summarize_file_for_task": summary_mock
                },
                clear=False,
            ):
                result = handlers["read_file"](str(target))

        summary_mock.assert_called_once_with(
            path=str(target),
            question="For task 'implement a change in the right part of this file', summarize the file's relevant responsibilities, key symbols, likely edit areas, and risks.",
            focus="behavior",
            allow_llm=False,
        )
        assert result["redirected"] is True
        assert "summary" in result["summary"]["answer"]
        assert result["total_lines"] > 100

    def test_read_file_with_line_range_bypasses_redirect(self, tmp_path):
        from brain.systems.runs.tool_handlers import _get_tool_handlers

        target = tmp_path / "large_module.py"
        body = "\n".join(f"line_{i}" for i in range(300))
        target.write_text(body)

        with patch("brain.systems.tools.handlers.WORKSPACE_ROOT", str(tmp_path)):
            handlers = _get_tool_handlers(
                workspace_root=str(tmp_path),
                reader_policy={
                    "enabled": True,
                    "large_file_lines": 100,
                    "large_file_chars": 2000,
                    "task_hint": "debug this file",
                    "focus": "behavior",
                },
            )
            result = handlers["read_file"](str(target), start_line=10, end_line=20)

        assert "content" in result
        assert result["total_lines"] == 300

    def test_parallel_tool_batch_executes_safe_operations(self, tmp_path):
        from brain.systems.runs.tool_handlers import _get_tool_handlers

        target = tmp_path / "example.py"
        target.write_text("def target():\n    return 1\n")

        handlers = _get_tool_handlers(workspace_root=str(tmp_path))
        result = handlers["parallel_tool_batch"](
            operations=[
                {"tool_name": "read_file", "args": {"path": str(target), "start_line": 1, "end_line": 2}},
                {"tool_name": "search_files", "args": {"pattern": "target", "path": str(tmp_path)}},
                {"tool_name": "list_files", "args": {"pattern": "**/*.py", "path": str(tmp_path)}},
            ],
            max_parallel=2,
        )

        assert result["completed"] == 3
        assert result["failed"] == 0
        assert result["max_parallel"] == 2
        assert result["results"][0]["tool_name"] == "read_file"
        assert "content" in result["results"][0]["result"]
        assert result["results"][1]["tool_name"] == "search_files"
        assert result["results"][2]["tool_name"] == "list_files"

    def test_parallel_tool_batch_rejects_unsafe_tools(self):
        from brain.systems.runs.tool_handlers import _get_tool_handlers

        handlers = _get_tool_handlers()
        result = handlers["parallel_tool_batch"](
            operations=[{"tool_name": "exec_command", "args": {"command": "pwd"}}]
        )

        assert "error" in result
        assert "not allowed" in result["error"]


class TestTraceSymbol:
    """Test deterministic symbol tracing."""

    def test_trace_symbol_finds_definition_and_reference(self, tmp_path):
        from brain.systems.tools.handlers import handle_trace_symbol

        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "a.py").write_text(
            '''def build_agent_invocation():
    return "ok"
'''
        )
        (pkg / "b.py").write_text(
            '''from pkg.a import build_agent_invocation

def create_child_run():
    return build_agent_invocation()
'''
        )

        with patch("brain.systems.tools.handlers.WORKSPACE_ROOT", str(tmp_path)):
            result = handle_trace_symbol("build_agent_invocation")

        assert result["symbol"] == "build_agent_invocation"
        assert any(item["path"] == "pkg/a.py" for item in result["definitions"])
        assert any(item["path"] == "pkg/b.py" for item in result["references"])
        assert result["count"] >= 2


class TestImplementationMap:
    """Test task-scoped implementation maps."""

    def test_build_implementation_map_ranks_relevant_files(self, tmp_path):
        from brain.systems.tools.handlers import handle_build_implementation_map

        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "workers.py").write_text(
            '''from pkg.invocation import build_agent_invocation

def create_child_run():
    return build_agent_invocation()
'''
        )
        (pkg / "invocation.py").write_text(
            '''def build_agent_invocation():
    return {"role": "worker"}
'''
        )
        (pkg / "misc.py").write_text(
            '''def unrelated():
    return "noise"
'''
        )

        with patch("brain.systems.tools.handlers.WORKSPACE_ROOT", str(tmp_path)):
            result = handle_build_implementation_map(
                "How does create_child_run connect to build_agent_invocation?",
                paths=["pkg/workers.py", "pkg/invocation.py", "pkg/misc.py"],
            )

        assert result["file_count"] >= 2
        assert result["files_ranked"][0]["path"] in {"pkg/workers.py", "pkg/invocation.py"}
        assert any(edge["kind"] == "import" for edge in result["edges"])
        assert any(zone["path"] == "pkg/workers.py" for zone in result["likely_edit_zones"])
