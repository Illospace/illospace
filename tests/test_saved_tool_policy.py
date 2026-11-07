"""Saved policies apply to every runtime and remain additive at call time."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from brain.systems.runs.direct_loop.loop_control import RunControlPolicy
from brain.systems.runs.execution_context import (
    bind_agent_context,
    current_agent_context,
    snapshot_agent_context,
)
from brain.systems.runs.tool_policy import (
    apply_loaded_skill_policy,
    current_policy_metadata,
    disabled_tool_names_from_metadata,
    merge_tool_policies,
    policy_skill_names,
    resolve_effective_tool_policy,
)


def policy(*names, source="organization", skill=None):
    detail = {"source": source}
    if skill:
        detail["skill"] = skill
    return {
        "disabled_tools": sorted(names),
        "disabled_tool_sources": {name: detail for name in names},
    }


def row(*names, scope="organization", key=""):
    return SimpleNamespace(scope_type=scope, scope_key=key, disabled_tools=list(names))


@pytest.mark.asyncio
async def test_resolution_unions_all_sources_and_rejects_allow_overrides():
    metadata = {
        "tool_policy": {
            "disabled_tools": ["run_tool"],
            "enabled_tools": ["exec_command"],
        },
        "execution_provenance": {
            "tool_policy": policy("inherited", source="skill", skill="inherited-skill")
        },
        "launch_envelope": {"skills": [{"name": "review"}]},
        "skill_name": " /coordinate ",
        "slash_skill_names": ["/inspect"],
    }
    loader = AsyncMock(
        return_value=[
            row("exec_command"),
            row("run_script", scope="skill", key="review"),
        ]
    )
    disabled, sources = await resolve_effective_tool_policy(
        "org-1", policy_skill_names(metadata), metadata, policy_loader=loader
    )
    assert disabled == {"run_tool", "exec_command", "run_script", "inherited"}
    assert sources["exec_command"] == {"source": "organization"}
    assert sources["run_script"] == {"source": "skill", "skill": "review"}
    assert sources["run_tool"] == {"source": "run"}
    assert loader.await_args.args == ("org-1", {"review", "coordinate", "inspect"})
    assert "enabled_tools" not in metadata["tool_policy"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "origin,org_context",
    [
        ("interactive", "direct"),
        ("scheduler", "metadata"),
        ("cycle", "execution_provenance"),
        ("slack", "runtime_envelope"),
        ("github", "metadata"),
        ("illo_submit", "execution_provenance"),
        ("worker", "direct"),
    ],
)
async def test_every_direct_agent_origin_resolves_before_the_tool_surface(
    monkeypatch, origin, org_context
):
    import brain.systems.runs.direct_agent as agent

    loader = AsyncMock(return_value=[row("exec_command")])
    monkeypatch.setattr(
        "brain.systems.runs.tool_policy_store.load_tool_policies", loader
    )
    metadata = {"origin": origin, "tool_policy": {"enabled_tools": ["exec_command"]}}
    kwargs = {}
    if org_context == "direct":
        kwargs["org_id"] = "org-1"
    elif org_context == "metadata":
        metadata["org_id"] = "org-1"
    else:
        metadata[org_context] = {"org_id": "org-1"}
    captured = {}
    original = agent._apply_tool_policy

    def capture(tools, handlers, resolved):
        captured["surface"] = original(tools, handlers, resolved)
        captured["policy"] = resolved["tool_policy"]
        raise RuntimeError("surface captured")

    monkeypatch.setattr(agent, "_apply_tool_policy", capture)
    with pytest.raises(RuntimeError, match="surface captured"):
        await agent.run_agent_async(
            "Inspect this",
            tools=[{"name": "exec_command"}, {"name": "read_file"}],
            tool_handlers={"exec_command": lambda: None, "read_file": lambda: None},
            metadata=metadata,
            persist_session=False,
            **kwargs,
        )
    tools, handlers = captured["surface"]
    assert tools == [{"name": "read_file"}]
    assert set(handlers) == {"read_file"}
    assert captured["policy"]["disabled_tool_sources"]["exec_command"] == {
        "source": "organization"
    }
    loader.assert_awaited_once()


@pytest.mark.asyncio
async def test_empty_policy_preserves_tool_surface_and_no_org_skips_database():
    from brain.systems.runs.direct_agent import _apply_tool_policy

    metadata = {}
    loader = AsyncMock()
    assert await resolve_effective_tool_policy(
        None, [], metadata, policy_loader=loader
    ) == (set(), {})
    loader.assert_not_awaited()
    tools, handlers = [{"name": "exec_command"}], {"exec_command": lambda: None}
    assert _apply_tool_policy(tools, handlers, metadata) == (tools, handlers)
    loader.return_value = []
    await resolve_effective_tool_policy("org-1", [], metadata, policy_loader=loader)
    assert _apply_tool_policy(tools, handlers, metadata) == (tools, handlers)


@pytest.mark.asyncio
async def test_lookup_failure_stops_run_resolution():
    with pytest.raises(RuntimeError, match="unavailable"):
        await resolve_effective_tool_policy(
            "org-1",
            [],
            {},
            policy_loader=AsyncMock(side_effect=RuntimeError("unavailable")),
        )


@pytest.mark.parametrize("async_mode", [False, True])
def test_policy_disabled_call_returns_error_without_handler(async_mode):
    from tests.test_tool_execution import _execute_async, _execute_sync, _response

    calls = []
    metadata = {"tool_policy": policy("exec_command")}
    control = RunControlPolicy(policy_metadata=metadata)
    args = (
        _response(("blocked", "exec_command", {})),
        {"exec_command": lambda: calls.append(True)},
    )
    result = (
        asyncio.run(_execute_async(*args, loop_control=control))
        if async_mode
        else _execute_sync(*args, loop_control=control)
    )
    assert calls == []
    assert result.tool_results[0]["is_error"] is True
    assert result.tool_results[0]["content"] == (
        "Tool 'exec_command' is disabled by organization policy. "
        "Tell the user what you cannot do and offer to file a ticket. Do not look for a workaround."
    )


@pytest.mark.asyncio
async def test_mid_run_skill_policy_survives_cloned_context_and_blocks_next_call(
    monkeypatch,
):
    from tests.test_tool_execution import _execute_async, _response

    loader = AsyncMock(return_value=[row("exec_command", scope="skill", key="review")])
    monkeypatch.setattr(
        "brain.systems.runs.tool_policy_store.load_tool_policies", loader
    )
    metadata = {"org_id": "org-1"}
    control = RunControlPolicy(policy_metadata=metadata)
    calls = []
    with bind_agent_context(
        org_id="org-1", execution_metadata=metadata, loop_control=control
    ):
        cloned = snapshot_agent_context()
        with bind_agent_context(cloned):
            await apply_loaded_skill_policy("review", current_agent_context())
        result = await _execute_async(
            _response(("blocked", "exec_command", {})),
            {"exec_command": lambda: calls.append(True)},
            agent_context=current_agent_context(),
            loop_control=control,
        )
    assert calls == []
    assert "disabled by skill policy (review)" in result.tool_results[0]["content"]
    assert metadata["tool_policy"]["disabled_tools"] == ["exec_command"]


@pytest.mark.asyncio
async def test_concurrent_skill_loads_do_not_drop_disabled_names(monkeypatch):
    async def loader(_org_id, names):
        await asyncio.sleep(0)
        return [row(f"tool_{name}", scope="skill", key=name) for name in names]

    monkeypatch.setattr(
        "brain.systems.runs.tool_policy_store.load_tool_policies", loader
    )
    metadata = {"org_id": "org-1"}
    with bind_agent_context(
        org_id="org-1",
        execution_metadata=metadata,
        loop_control=RunControlPolicy(policy_metadata=metadata),
    ):
        await asyncio.gather(
            *(
                apply_loaded_skill_policy(name, current_agent_context())
                for name in ("first", "second")
            )
        )
    assert disabled_tool_names_from_metadata(metadata) == {"tool_first", "tool_second"}


@pytest.mark.asyncio
async def test_threaded_tool_contexts_cannot_overwrite_another_disabled_list(
    monkeypatch,
):
    import time
    import brain.systems.runs.tool_policy as policies

    merge = policies.merge_tool_policies

    def slow_merge(*values):
        result = merge(*values)
        time.sleep(0.01)
        return result

    monkeypatch.setattr(policies, "merge_tool_policies", slow_merge)
    metadata = {}
    control = RunControlPolicy(policy_metadata=metadata)
    await asyncio.gather(
        *(
            asyncio.to_thread(
                current_policy_metadata,
                {
                    "loop_control": control,
                    "execution_metadata": {"tool_policy": policy(name)},
                },
            )
            for name in ("exec_command", "run_script")
        )
    )
    assert disabled_tool_names_from_metadata(metadata) == {"exec_command", "run_script"}


def test_parallel_tool_batch_cannot_dispatch_a_disabled_tool(tmp_path):
    from brain.systems.runs.tool_surface import build_tool_handlers

    handlers = build_tool_handlers(workspace_root=str(tmp_path))
    with bind_agent_context(execution_metadata={"tool_policy": policy("read_file")}):
        result = handlers["parallel_tool_batch"](
            operations=[{"tool_name": "read_file", "args": {"path": "missing"}}]
        )
    assert "disabled by organization policy" in result["error"]


def test_read_file_redirect_cannot_dispatch_a_disabled_summary_tool(tmp_path):
    from brain.systems.runs.direct_loop.tool_execution import run_tool_awaitable
    from brain.systems.runs.tool_surface import build_tool_handlers

    (tmp_path / "large.txt").write_text("long line\n" * 10)
    handlers = build_tool_handlers(
        workspace_root=str(tmp_path),
        reader_policy={"enabled": True, "large_file_lines": 1},
    )
    with bind_agent_context(
        execution_metadata={
            "tool_policy": policy(
                "summarize_file_for_task", source="skill", skill="review"
            )
        }
    ):
        result = run_tool_awaitable(handlers["read_file"](path="large.txt"))
    assert "disabled by skill policy (review)" in result["error"]


def test_capabilities_and_self_context_explain_each_disabled_tool():
    from brain.systems.runs.tool_catalog.handlers.capabilities import (
        _handle_read_capabilities,
    )
    from brain.systems.runs.tool_catalog.handlers.self_context import (
        _handle_read_self_context,
    )

    metadata = {
        "tool_policy": merge_tool_policies(
            policy("exec_command"),
            policy("run_script", source="skill", skill="review"),
            policy("future_tool"),
        )
    }
    with bind_agent_context(execution_metadata=metadata):
        capabilities = json.loads(_handle_read_capabilities(detail_level="summary"))
        context = json.loads(
            _handle_read_self_context(include_paths=False, include_git=False)
        )
    details = {
        name: value
        for cap in capabilities["capabilities"]
        for name, value in cap.get("unavailable_tool_details", {}).items()
    }
    assert details["exec_command"]["reason"] == "disabled by organization policy"
    assert details["run_script"]["reason"] == "disabled by skill policy (review)"
    assert "future_tool" in details
    assert (
        context["source_inspection"]["tools"]["exec_command"]["available_after_policy"]
        is False
    )
    assert (
        context["unavailable_tools"]["run_script"]["reason"]
        == details["run_script"]["reason"]
    )
    assert "do not look for a workaround" in details["exec_command"]["guidance"]


@pytest.mark.asyncio
async def test_spawn_worker_inherits_full_policy_with_sources(monkeypatch):
    from tests.test_agent_run_runtime import _captured_spawn_worker

    _, captured, _ = await _captured_spawn_worker(
        monkeypatch,
        parent_model_policy={"model": "openai/gpt-5.6-sol", "thinking": "high"},
        execution_metadata={
            "tool_policy": merge_tool_policies(
                policy("exec_command"),
                policy("run_script", source="skill", skill="review"),
            )
        },
        tool_policy={
            "disabled_tools": ["read_file"],
            "enabled_tools": ["exec_command", "run_script"],
        },
    )
    child_metadata = captured["metadata"]
    assert disabled_tool_names_from_metadata(child_metadata) >= {
        "exec_command",
        "run_script",
        "read_file",
    }
    assert child_metadata["tool_policy"]["disabled_tool_sources"]["run_script"] == {
        "source": "skill",
        "skill": "review",
    }


@pytest.mark.asyncio
async def test_fast_recipe_filters_policy_before_invocation(monkeypatch):
    from tests.test_agent_run_runtime import _runtime
    from brain.systems.runs.recipes.fast import FastRecipe

    loader = AsyncMock(return_value=[row("exec_command")])
    monkeypatch.setattr(
        "brain.systems.runs.tool_policy_store.load_tool_policies", loader
    )
    monkeypatch.setattr(
        "brain.systems.runs.recipes.fast.build_agent_tools",
        lambda role: [{"name": "exec_command"}, {"name": "read_file"}],
    )
    monkeypatch.setattr(
        "brain.systems.runs.recipes.fast.build_tool_handlers",
        lambda **kwargs: {"exec_command": lambda: None, "read_file": lambda: None},
    )
    captured = {}

    async def invoke(spec):
        captured["spec"] = spec
        return SimpleNamespace(output="done", success=True, error=None)

    monkeypatch.setattr(
        "brain.systems.runs.recipes.fast.invoke_direct_agent_async", invoke
    )
    runtime = _runtime("fast")
    await FastRecipe().execute(runtime)
    assert [tool["name"] for tool in captured["spec"].tools] == ["read_file"]
    assert set(captured["spec"].tool_handlers) == {"read_file"}
    assert captured["spec"].metadata["tool_policy"]["disabled_tool_sources"][
        "exec_command"
    ] == {"source": "organization"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool_name,tool_input",
    [
        ("skill_view", {"name": "review", "section": "procedure"}),
        ("skill_asset", {"name": "review", "path": "guide.md"}),
    ],
)
async def test_skill_load_tools_latch_policy_before_returning_content(
    monkeypatch, tool_name, tool_input
):
    import brain.app.mcp.server as server
    from brain.systems.runs.direct_loop.tool_execution import async_invoke_tool_handler
    from brain.systems.runs.tool_surface import build_tool_handlers

    loader = AsyncMock(return_value=[row("exec_command", scope="skill", key="review")])
    monkeypatch.setattr(
        "brain.systems.runs.tool_policy_store.load_tool_policies", loader
    )
    monkeypatch.setattr(
        server,
        f"async_tool_{tool_name}",
        AsyncMock(return_value={"name": "review", "content": "procedure"}),
    )
    handlers = build_tool_handlers(workspace_root=None)
    metadata = {"org_id": "org-1"}
    with bind_agent_context(
        org_id="org-1",
        execution_metadata=metadata,
        loop_control=RunControlPolicy(policy_metadata=metadata),
    ):
        result = await async_invoke_tool_handler(handlers[tool_name], tool_input)
        assert "procedure" in json.dumps(result)
        assert disabled_tool_names_from_metadata(
            current_policy_metadata(current_agent_context())
        ) == {"exec_command"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action,new_name",
    [
        ("get", None),
        ("get_asset", None),
        ("update", None),
        ("edit", None),
        ("convert_to_bundle", None),
        ("update", "renamed-review"),
    ],
)
async def test_manage_skill_content_loads_latch_policy(monkeypatch, action, new_name):
    from brain.systems.runs.tool_catalog.handlers.skills import _handle_manage_skill

    skill = SimpleNamespace(id=1, name="review", procedure="Skill procedure")

    async def update_skill(_id, **updates):
        if updates.get("name"):
            skill.name = updates["name"]
        return skill

    repo = SimpleNamespace(
        a_get_by_name_or_raise=AsyncMock(return_value=skill),
        a_update_full=AsyncMock(side_effect=update_skill),
    )
    asset = SimpleNamespace(path="guide.md", content_text="Skill procedure")
    service = SimpleNamespace(
        ensure_skill_bundle=AsyncMock(return_value=skill),
        _load_package_rows=AsyncMock(
            return_value=(None, SimpleNamespace(id=1), None, [asset])
        ),
    )
    uow = MagicMock()
    uow.__aenter__ = AsyncMock(return_value=SimpleNamespace(session=object()))
    uow.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(
        "brain.platform.db.repositories.unit_of_work.UnitOfWork", lambda: uow
    )
    monkeypatch.setattr(
        "brain.platform.db.repositories.skills.SkillRepository", lambda session: repo
    )
    monkeypatch.setattr(
        "brain.platform.db.repositories.skill_bundles.SkillBundleRepository",
        lambda session: object(),
    )
    monkeypatch.setattr(
        "brain.platform.db.services.skill_bundle_io.AsyncSkillBundleIOService",
        lambda *args: service,
    )

    async def load_policy(_org, names):
        policies = [row("exec_command", scope="skill", key="review")]
        if "renamed-review" in names:
            policies.append(row("run_script", scope="skill", key="renamed-review"))
        return policies

    monkeypatch.setattr(
        "brain.systems.runs.tool_policy_store.load_tool_policies", load_policy
    )
    metadata = {"org_id": "org-1"}
    with bind_agent_context(
        org_id="org-1",
        execution_metadata=metadata,
        loop_control=RunControlPolicy(policy_metadata=metadata),
    ):
        result = json.loads(
            await _handle_manage_skill(
                action=action,
                skill_name="review",
                name=new_name,
                path="guide.md",
                description="Updated description",
            )
        )
    assert result["ok"] is True
    assert "Skill procedure" in json.dumps(result)
    assert disabled_tool_names_from_metadata(metadata) == (
        {"exec_command", "run_script"} if new_name else {"exec_command"}
    )
    assert metadata["tool_policy"]["disabled_tool_sources"]["exec_command"] == {
        "source": "skill",
        "skill": "review",
    }


@pytest.mark.asyncio
async def test_predict_rlm_wrapper_blocks_tool_disabled_after_it_was_built():
    from brain.systems.runs.predict_rlm_backend import build_predict_rlm_tools

    calls = []
    metadata = {}
    control = RunControlPolicy(policy_metadata=metadata)
    tools = build_predict_rlm_tools(
        tool_definitions=[
            {
                "name": "exec_command",
                "input_schema": {"type": "object", "properties": {}},
            }
        ],
        tool_handlers={"exec_command": lambda: calls.append(True)},
        on_tool_call=None,
        run_id=None,
        idea_id=None,
        tool_call_source="worker",
        threadlocal_context={"execution_metadata": metadata, "loop_control": control},
    )
    metadata["tool_policy"] = policy("exec_command")
    result = await tools["exec_command"]()
    assert calls == []
    assert "disabled by organization policy" in result
