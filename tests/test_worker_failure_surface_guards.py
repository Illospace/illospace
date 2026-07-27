"""Source guards for worker diagnostics crossing public run surfaces."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _function(path: str, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"), filename=path)
    return next(
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
    )


def _call_name(node: ast.Call) -> str:
    return str(getattr(node.func, "attr", None) or getattr(node.func, "id", None) or "")


def _contains_direct_error_access(node: ast.AST) -> bool:
    for child in ast.walk(node):
        if isinstance(child, ast.Attribute) and child.attr == "error":
            return True
        if (
            isinstance(child, ast.Call)
            and _call_name(child) == "getattr"
            and len(child.args) >= 2
            and isinstance(child.args[1], ast.Constant)
            and child.args[1].value == "error"
        ):
            return True
    return False


def test_worker_public_output_sinks_do_not_receive_agent_error_directly():
    execute = _function("brain/systems/runs/recipes/workers.py", "execute")
    calls = [node for node in ast.walk(execute) if isinstance(node, ast.Call)]

    text_deltas = [node for node in calls if _call_name(node) == "text_delta"]
    assert text_deltas
    assert all(not _contains_direct_error_access(node) for node in text_deltas)
    assert {ast.unparse(node.args[0]) for node in text_deltas} == {"delta", "public_output"}

    artifact_call = next(node for node in calls if _call_name(node) == "worker_result_artifact")
    artifact_output = next(keyword.value for keyword in artifact_call.keywords if keyword.arg == "output")
    assert ast.unparse(artifact_output) == "public_output"
    assert not _contains_direct_error_access(artifact_output)


def test_cli_and_checker_public_error_values_use_safe_failure_projectors():
    cli = _function("brain/app/cli/agent_cli.py", "call_agent")
    returned_error_values = []
    for node in ast.walk(cli):
        if not isinstance(node, ast.Return) or not isinstance(node.value, ast.Dict):
            continue
        for key, value in zip(node.value.keys, node.value.values):
            if isinstance(key, ast.Constant) and key.value == "error":
                returned_error_values.append(value)

    assert returned_error_values
    assert all(not _contains_direct_error_access(value) for value in returned_error_values)
    assert not any(ast.unparse(value) in {"str(e)", "str(exc)"} for value in returned_error_values)

    checker = _function(
        "brain/systems/runs/tool_catalog/handlers/cortex_reply.py",
        "_build_final_reply_check_context",
    )
    checker_source = ast.unparse(checker)
    assert "worker_result.output or worker_result.error" not in checker_source
    assert "summary = _safe_failure_message(diagnostic)" in checker_source


@pytest.mark.parametrize(
    ("path", "function_name", "required_calls"),
    [
        (
            "brain/systems/runs/cortex/runner.py",
            "_latest_unmirrored_final_answer",
            {"_public_terminal_run_answer"},
        ),
        (
            "brain/systems/runs/cortex/runner.py",
            "_settle_thread_discussion_conversation_run_async",
            {"_public_terminal_run_answer"},
        ),
        (
            "brain/systems/runs/ui_events.py",
            "run_event_to_ui_message",
            {"_public_failure_for_event", "public_run_event_payload"},
        ),
        (
            "brain/systems/runs/cortex/read_models.py",
            "_debug_payload",
            {"public_run_debug_event_payload", "public_failed_run_artifact"},
        ),
        (
            "brain/systems/inbound/reconciliation.py",
            "reconcile_inbound_triage_run",
            {"public_run_failure"},
        ),
        (
            "brain/systems/inbound/admin.py",
            "serialize_receipt",
            {"_public_run_outcome"},
        ),
        (
            "brain/app/api/routers/agent_mcp.py",
            "_tool_get_result",
            {"public_run_failure"},
        ),
        (
            "brain/systems/runs/recipes/workers.py",
            "execute",
            {"public_run_failure", "worker_result_artifact"},
        ),
        (
            "brain/app/cli/agent_cli.py",
            "call_agent",
            {"_public_failure_message"},
        ),
    ],
)
def test_public_failure_sink_inventory_uses_safe_projectors(
    path,
    function_name,
    required_calls,
):
    function = _function(path, function_name)
    call_names = {
        _call_name(node)
        for node in ast.walk(function)
        if isinstance(node, ast.Call)
    }

    assert required_calls <= call_names


def test_timeline_and_discussion_sinks_never_read_final_answer_artifacts_directly():
    for function_name in (
        "_latest_unmirrored_final_answer",
        "_settle_thread_discussion_conversation_run_async",
    ):
        function = _function("brain/systems/runs/cortex/runner.py", function_name)
        call_names = {
            _call_name(node)
            for node in ast.walk(function)
            if isinstance(node, ast.Call)
        }
        assert "_latest_final_answer_artifact" not in call_names
