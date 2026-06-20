from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]

WORK_INTAKE_MODULE = "brain/systems/runs/work_intake.py"

PRODUCT_RUN_PRODUCERS = {
    "brain/app/triggers/router.py",
    "brain/app/api/routers/cortex/_misc.py",
    "brain/systems/cycles/service.py",
    "brain/systems/external_agents/service.py",
    "brain/systems/inbound/service.py",
    "brain/systems/runs/cortex/__init__.py",
    "brain/systems/runs/tool_catalog/handlers/ideas.py",
}

EXPECTED_WORK_INTAKE_API = {
    "WorkIntakeEvent",
    "WorkIntakeActor",
    "WorkIntakeTarget",
    "WorkIntakePolicy",
    "build_agent_run_request",
    "admit_work",
}

REMOVED_WORK_INTAKE_BYPASSES = {
    "build_chat_agent_run_request",
    "build_cortex_agent_run_request",
    "build_cortex_run_admission_kwargs",
    "build_run_admission_request",
}


class _Session:
    async def get(self, _model, _idea_id):
        return SimpleNamespace(
            id="idea-1",
            title="Launch",
            org_id="org-1",
            user_id="owner-1",
            agent_details={
                "project_context": {
                    "selected_profile_name": "Repo",
                    "resources": [{"kind": "folder", "path": "/workspace/repo"}],
                }
            },
        )

    async def scalars(self, *_args, **_kwargs):
        return SimpleNamespace(first=lambda: None)


def _tree(path: str) -> ast.AST:
    return ast.parse((ROOT / path).read_text(encoding="utf-8"))


def _function_names(tree: ast.AST) -> set[str]:
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }


def _call_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def _imported_names(path: str) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.Import):
            names.update(alias.asname or alias.name.rsplit(".", 1)[-1] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.update(alias.asname or alias.name for alias in node.names)
    return names


def _run_creation_violations(path: str) -> list[str]:
    tree = _tree(path)
    violations: list[str] = []
    forbidden_imports = {
        "AgentRunRequest",
        "RunAdmissionRequest",
        "AsyncAgentRunStore",
        "async_admit_run",
    }
    for name in sorted(_imported_names(path) & forbidden_imports):
        violations.append(f"{path} imports {name}")

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node)
        if name in {"AgentRunRequest", "RunAdmissionRequest", "async_admit_run", "create_run"}:
            violations.append(f"{path}:{node.lineno} calls {name}")
    return violations


def test_work_intake_exposes_single_product_event_api():
    function_names = _function_names(_tree(WORK_INTAKE_MODULE))
    missing = EXPECTED_WORK_INTAKE_API - function_names

    assert missing == set()
    assert REMOVED_WORK_INTAKE_BYPASSES.isdisjoint(function_names)


def test_cortex_thread_binding_compatibility_module_is_removed():
    assert not (ROOT / "brain/systems/runs/cortex/thread_binding.py").exists()


@pytest.mark.asyncio
async def test_cycle_tool_and_external_agent_events_share_the_same_intake_policy(monkeypatch):
    from brain.systems.runs.domain import RunProfile, RunRecipe
    from brain.systems.runs.work_intake import WorkIntakeEvent, build_agent_run_request

    async def fake_thread_context(*_args, **_kwargs):
        return {"formatted": "Earlier thread context"}

    monkeypatch.setattr(
        "brain.systems.runs.work_intake.async_build_agent_visible_thread_context",
        fake_thread_context,
    )

    common = {
        "org_id": "org-1",
        "actor": {"id": "owner-1", "org_id": "org-1", "internal": False},
        "target": {"kind": "cortex_idea", "idea_id": "idea-1"},
    }
    events = [
        WorkIntakeEvent(
            source="cycle",
            event_type="cycle.due_run",
            payload={
                "message": "Run the scheduled check.",
                "metadata": {"execution_profile": "deep", "model": "openai/gpt-5.4"},
            },
            policy={"priority": 4, "idempotency_key": "cycle:run-1"},
            **common,
        ),
        WorkIntakeEvent(
            source="tool",
            event_type="tool.idea_created",
            payload={
                "message": '[Idea: "Launch" | idea-1]\n\nStart this thought.',
                "metadata": {"created_by_tool": "manage_idea", "thinking": "xhigh"},
            },
            policy={"producer": "agent_tool", "idempotency_key": "tool:create:idea-1"},
            **common,
        ),
        WorkIntakeEvent(
            source="external_agent",
            event_type="external_agent.submitted",
            payload={
                "message": "Summarize the external agent submission.",
                "metadata": {"model_provider": "anthropic"},
            },
            policy={"producer": "external_agent", "idempotency_key": "external:task-1"},
            **common,
        ),
    ]

    requests = [
        await build_agent_run_request(_Session(), event)
        for event in events
    ]

    assert {request.thread_id for request in requests} == {"idea-1"}
    assert {request.target_ref["kind"] for request in requests} == {"cortex_idea"}
    assert {request.user_id for request in requests} == {"owner-1"}
    assert requests[0].profile == RunProfile.DEEP
    assert requests[0].recipe == RunRecipe.DEEP
    assert requests[0].model_policy["model"] == "openai/gpt-5.4"
    assert requests[1].model_policy["thinking"] == "xhigh"
    assert requests[2].model_policy["provider"] == "anthropic"
    assert [request.metadata["idempotency_key"] for request in requests] == [
        "cycle:run-1",
        "tool:create:idea-1",
        "external:task-1",
    ]
    assert all(request.metadata["thread_context"]["formatted"] == "Earlier thread context" for request in requests)
    assert all(request.metadata["work_intake"]["source"] == event.source for request, event in zip(requests, events))


def test_product_run_producers_do_not_construct_or_admit_runs_directly():
    violations: list[str] = []
    for path in PRODUCT_RUN_PRODUCERS:
        violations.extend(_run_creation_violations(path))

    assert violations == []


def test_work_intake_owns_run_profile_recipe_model_and_project_context_selection():
    forbidden_policy_names = {
        "model_policy_from_metadata",
        "profile_from_metadata",
        "recipe_for_profile",
        "_model_policy_from_metadata",
        "_profile_from_metadata",
        "_recipe_for_profile",
        "validated_project_context_snapshot",
        "async_build_agent_visible_thread_context",
    }
    violations: list[str] = []

    for path in PRODUCT_RUN_PRODUCERS:
        if path == "brain/systems/runs/cortex/__init__.py":
            continue
        imported = _imported_names(path) & forbidden_policy_names
        if imported:
            violations.append(f"{path} imports policy/context internals: {sorted(imported)}")
        defined = _function_names(_tree(path)) & forbidden_policy_names
        if defined:
            violations.append(f"{path} defines policy/context internals: {sorted(defined)}")

    assert violations == []


@pytest.mark.asyncio
async def test_external_headless_ask_uses_async_work_intake_policy():
    from brain.systems.runs.work_intake import WorkIntakeEvent, build_agent_run_request

    request = await build_agent_run_request(
        _Session(),
        WorkIntakeEvent(
            source="external_agent",
            event_type="external_agent.headless_ask",
            org_id="org-1",
            actor={"id": "owner-1", "org_id": "org-1"},
            target={
                "kind": "external_agent_headless_ask",
                "external_agent_connection_id": "conn-1",
                "external_agent_task_id": "task-1",
                "thread_id": "external-agent:conn-1:task-1",
            },
            payload={
                "message": "Answer the personal agent.",
                "workspace_ref": {"source": "external_agent_bridge", "mode": "headless"},
                "model_policy": {"model": "openai/gpt-5.4", "thinking": "medium"},
                "metadata": {
                    "execution_profile": "fast",
                    "recipe": "fast",
                    "tool_policy": {"blocked_tools": ["manage_idea"]},
                },
            },
            policy={"producer": "external_agent", "idempotency_key": "ask:task-1"},
        ),
    )

    assert request.thread_id == "external-agent:conn-1:task-1"
    assert request.user_id == "owner-1"
    assert request.target_ref["kind"] == "external_agent_headless_ask"
    assert request.workspace_ref == {"source": "external_agent_bridge", "mode": "headless"}
    assert request.model_policy == {"model": "openai/gpt-5.4", "thinking": "medium"}
    assert request.metadata["producer"] == "external_agent"
    assert request.metadata["idempotency_key"] == "ask:task-1"
    assert request.metadata["tool_policy"]["blocked_tools"] == ["manage_idea"]
    assert request.metadata["work_intake"]["source"] == "external_agent"


@pytest.mark.asyncio
async def test_admit_work_is_the_only_public_agent_run_creation_boundary(monkeypatch):
    from brain.systems.runs.work_intake import WorkIntakeEvent, admit_work

    created_requests = []

    class _Store:
        def __init__(self, _session):
            pass

        async def create_run(self, request):
            created_requests.append(request)
            return SimpleNamespace(id=42)

    monkeypatch.setattr("brain.systems.runs.work_intake.AsyncAgentRunStore", _Store)

    result = await admit_work(
        _Session(),
        WorkIntakeEvent(
            source="cortex",
            event_type="cortex.thread_reply",
            org_id="org-1",
            actor={"id": "owner-1", "org_id": "org-1", "internal": False},
            target={"kind": "cortex_idea", "idea_id": "idea-1"},
            payload={"message": "@illo continue", "metadata": {"execution_profile": "fast"}},
            policy={"idempotency_key": "cortex:thread:1", "priority": 2},
        ),
    )

    assert result.ok is True
    assert result.run_id == 42
    assert created_requests[0].metadata["source"] == "cortex"
    assert created_requests[0].metadata["producer"] == "work_intake"
