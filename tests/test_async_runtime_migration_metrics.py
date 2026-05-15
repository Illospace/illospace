from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _load_metrics_module():
    root = Path(__file__).resolve().parents[1]
    path = root / "scripts" / "async_runtime_migration_metrics.py"
    spec = importlib.util.spec_from_file_location("async_runtime_migration_metrics", path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_metric_counts_sync_mcp_facade_imports_and_calls(tmp_path):
    metrics = _load_metrics_module()
    path = tmp_path / "composition.py"
    path.write_text(
        '''
from brain.app.mcp.server import async_tool_brain_skills, tool_brain_recall, tool_brain_skills

async def build_handlers():
    plan = await async_tool_brain_skills("safe")
    recall = tool_brain_recall("unsafe")
    skills = tool_brain_skills("unsafe")
    return plan, recall, skills
''',
        encoding="utf-8",
    )

    matches = list(metrics.ast_matches(path, "brain/systems/runs/tool_catalog/handlers/composition.py", "systems"))
    categories = [match.category for match in matches]

    assert categories.count("sync_mcp_facade_import_refs") == 2
    assert categories.count("sync_mcp_facade_call_refs") == 2
    assert {match.symbol for match in matches if match.category == "sync_mcp_facade_import_refs"} == {
        "tool_brain_recall",
        "tool_brain_skills",
    }


def test_metric_counts_sync_mcp_facade_value_refs(tmp_path):
    metrics = _load_metrics_module()
    path = tmp_path / "composition.py"
    path.write_text(
        '''
from brain.app.mcp.server import tool_brain_guardrails, tool_brain_skills

def build_handlers():
    return {
        "brain_guardrails": tool_brain_guardrails,
        "brain_skills": wrap(tool_brain_skills),
    }
''',
        encoding="utf-8",
    )

    matches = list(metrics.ast_matches(path, "brain/systems/runs/tool_catalog/handlers/composition.py", "systems"))
    categories = [match.category for match in matches]

    assert categories.count("sync_mcp_facade_import_refs") == 2
    assert categories.count("sync_mcp_facade_value_refs") == 2


def test_metric_counts_mcp_server_sync_facade_definitions(tmp_path):
    metrics = _load_metrics_module()
    path = tmp_path / "server.py"
    path.write_text(
        '''
def tool_brain_skills(task):
    return _run_mcp_sync(async_tool_brain_skills(task))
''',
        encoding="utf-8",
    )

    matches = list(metrics.ast_matches(path, "brain/app/mcp/server.py", "app_runtime"))
    categories = [match.category for match in matches]

    assert categories.count("sync_mcp_facade_definition_refs") == 1
    assert categories.count("sync_mcp_runner_call_refs") == 1


def test_metric_counts_legacy_direct_agent_sync_surfaces(tmp_path):
    metrics = _load_metrics_module()
    path = tmp_path / "reader.py"
    path.write_text(
        '''
from brain.systems.runs.direct_agent import _invoke_tool_handler, _record_api_call
from brain.systems.runs.direct_loop.telemetry import record_api_call

async def run():
    _record_api_call(session_id="s")
    _invoke_tool_handler(lambda: None, {})
    record_api_call(session_id="s")
''',
        encoding="utf-8",
    )

    matches = list(metrics.ast_matches(path, "brain/systems/tools/handlers.py", "systems"))
    categories = [match.category for match in matches]

    assert categories.count("legacy_direct_agent_sync_import_refs") == 2
    assert categories.count("legacy_sync_telemetry_import_refs") == 1
    assert categories.count("legacy_direct_agent_sync_call_refs") == 2
    assert categories.count("legacy_sync_telemetry_call_refs") == 1


def test_metric_counts_legacy_direct_agent_sync_definitions(tmp_path):
    metrics = _load_metrics_module()
    path = tmp_path / "direct_agent.py"
    path.write_text(
        '''
def _invoke_tool_handler(handler, tool_input):
    return handler(**tool_input)
''',
        encoding="utf-8",
    )

    matches = list(metrics.ast_matches(path, "brain/systems/runs/direct_agent.py", "systems"))

    assert [match.category for match in matches] == ["legacy_direct_agent_sync_definition_refs"]
    assert matches[0].symbol == "_invoke_tool_handler"


def test_metric_counts_legacy_sync_telemetry_definitions(tmp_path):
    metrics = _load_metrics_module()
    path = tmp_path / "telemetry.py"
    path.write_text(
        '''
def record_api_call(**kwargs):
    return None
''',
        encoding="utf-8",
    )

    matches = list(metrics.ast_matches(path, "brain/systems/runs/direct_loop/telemetry.py", "systems"))

    assert [match.category for match in matches] == ["legacy_sync_telemetry_definition_refs"]
    assert matches[0].symbol == "record_api_call"


def test_metric_summary_sets_zero_targets():
    metrics = _load_metrics_module()
    matches = [
        metrics.Match(
            path="brain/systems/tools/handlers.py",
            line_number=10,
            category="sync_mcp_facade_call_refs",
            scope="systems",
            in_async_function=False,
            line="tool_brain_recall(query)",
            symbol="tool_brain_recall",
        )
    ]

    summary = metrics.summarize(matches, top=10)

    assert summary["metrics"]["async_runtime_migration_debt"] == 1
    assert summary["metrics"]["sync_mcp_facade_call_refs"] == 1
    assert all(target == 0 for target in summary["targets"].values())
