from __future__ import annotations


def _names(tools: list[dict]) -> set[str]:
    return {str(tool["name"]) for tool in tools}


def test_every_exposed_tool_has_registry_metadata():
    from brain.systems.runs.tool_definitions import COORDINATOR_TOOLS, WORKER_TOOLS
    from brain.systems.runs.tool_catalog.registry import all_tool_registrations
    from brain.systems.tools.handlers import EXTENDED_TOOLS

    exposed = (
        _names(COORDINATOR_TOOLS)
        | _names(WORKER_TOOLS)
        | _names(EXTENDED_TOOLS)
    )
    registered = set(all_tool_registrations())

    assert exposed <= registered


def test_registry_role_membership_matches_current_tool_lists():
    from brain.systems.runs.tool_definitions import COORDINATOR_TOOLS, WORKER_TOOLS
    from brain.systems.runs.tool_catalog.registry import all_tool_registrations
    from brain.systems.tools.handlers import EXTENDED_TOOLS

    registrations = all_tool_registrations()
    coordinator = {
        name for name, registration in registrations.items()
        if "coordinator" in registration.availability
    }
    worker = {
        name for name, registration in registrations.items()
        if "worker" in registration.availability
    }

    assert coordinator == _names(COORDINATOR_TOOLS) | _names(EXTENDED_TOOLS)
    assert worker == _names(WORKER_TOOLS) | _names(EXTENDED_TOOLS)


def test_handler_covered_runtime_tools_are_registered():
    from brain.systems.runs.tool_handlers import _get_tool_handlers
    from brain.systems.runs.tool_catalog.registry import all_tool_registrations

    registrations = all_tool_registrations()
    handlers = _get_tool_handlers()

    assert set(handlers) <= set(registrations)
    for name, registration in registrations.items():
        if registration.toolset != "pipeline":
            assert name in handlers, f"Registered runtime tool lacks handler: {name}"


def test_dormant_skill_authoring_tools_are_not_registered_for_runtime():
    from brain.systems.runs.tool_handlers import _get_tool_handlers
    from brain.systems.runs.tool_catalog.registry import all_tool_registrations

    dormant = {"create_skill", "manage_skill_asset", "flag_skill_gap"}

    assert dormant.isdisjoint(all_tool_registrations())
    assert dormant.isdisjoint(_get_tool_handlers())


def test_workspace_data_tool_is_read_only_agent_run_surface():
    from brain.systems.runs.tool_handlers import _get_tool_handlers
    from brain.systems.runs.tool_catalog.registry import context_route_tool_names, get_tool_registration

    registration = get_tool_registration("query_workspace_data")

    assert registration is not None
    assert registration.permission == "read_activity"
    assert registration.side_effect_class == "read_only"
    assert registration.evidence_emitter is True
    assert registration.context_route is not None
    assert "team activity" in registration.context_route.domains
    assert "query_workspace_data" in context_route_tool_names()
    assert "query_workspace_data" in _get_tool_handlers()


def test_context_route_surface_is_registry_driven():
    from brain.systems.runs.tool_catalog.registry import context_route_payload, context_route_tool_names

    names = context_route_tool_names()
    routes = {route["name"]: route for route in context_route_payload()}

    assert names == {"brain_recall", "my_activity", "query_workspace_data", "read_thread_messages", "runtime_settings"}
    assert set(routes) == names
    assert "broad" in routes["brain_recall"]["scopes"]
    assert "thread transcript" in routes["read_thread_messages"]["domains"]
    assert routes["query_workspace_data"]["empty_result_policy"] == "answer_honestly"
    assert "workspace records" in routes["query_workspace_data"]["domains"]


def test_workspace_activity_question_requires_workspace_data():
    from brain.systems.runs.introspection import required_introspection_tool

    tool, message = required_introspection_tool("Hey illo what is Alex working on?")

    assert tool == "query_workspace_data"
    assert message is not None
    assert "current workspace/team activity" in message


def test_memory_question_does_not_force_workspace_data():
    from brain.systems.runs.introspection import required_introspection_tool

    assert required_introspection_tool("What do you remember about Redis?") == (None, None)


def test_workspace_data_sources_are_adapter_registered():
    from brain.systems.runs.tool_catalog.handlers.workspace_data import _normalize_sources, _source_adapters

    adapters = _source_adapters()

    assert "runs" in adapters
    assert "memories" in adapters
    assert adapters["memories"].db_backed is False
    assert "memories" not in _normalize_sources(None)
    assert _normalize_sources(["memory"]) == ["memories"]
    assert _normalize_sources(["activity"]) == [
        "runs",
        "threads",
        "ideas",
        "tool_calls",
        "domain_events",
        "workspace_apps",
    ]
    assert _normalize_sources(["apps"]) == ["workspace_apps", "app_state"]


def test_side_effecting_tools_declare_action_metadata():
    from brain.systems.runs.tool_catalog.registry import all_tool_registrations

    for name, registration in all_tool_registrations().items():
        if registration.side_effect_class == "read_only":
            continue
        assert registration.risk_class in {"low", "medium", "high"}, name
        assert registration.reversibility not in {"", "none"}, name
        assert registration.expected_effect, name


def test_registry_parallel_safe_names_match_expected_batch_surface():
    from brain.systems.runs.tool_catalog.registry import parallel_safe_tool_names

    assert parallel_safe_tool_names(scope="batch") == frozenset({
        "build_implementation_map",
        "file_summary",
        "list_files",
        "project_context",
        "read_file",
        "read_thread_messages",
        "search_files",
        "semantic_search",
        "trace_symbol",
        "web_fetch",
        "web_search",
    })
    assert "write_file" not in parallel_safe_tool_names(scope="batch")
    assert "edit_file" not in parallel_safe_tool_names(scope="batch")
    assert "exec_command" not in parallel_safe_tool_names(scope="batch")


def test_action_policy_comes_from_registry_metadata():
    from brain.systems.runs.tool_catalog.registry import action_manifest_tool_names, action_policy_for_tool

    assert "manage_cycle" in action_manifest_tool_names()
    assert "manage_cron_job" not in action_manifest_tool_names()
    assert action_policy_for_tool("manage_cycle", kwargs={"action": "list"}) is None

    policy = action_policy_for_tool("manage_cycle", kwargs={"action": "create"})
    assert policy == {
        "risk": "high",
        "reversibility": "reversible",
        "expected_effect": "mutate a scheduled cycle",
    }

    assert action_policy_for_tool(
        "exec_command",
        kwargs={"command": "git push origin main"},
    )["risk"] == "high"
    assert action_policy_for_tool(
        "browser_snapshot",
        kwargs={"persist": False},
    ) is None


def test_tool_result_truncation_uses_registry_output_budget():
    from brain.systems.runs.direct_loop.tool_execution import PendingToolCall, resolve_tool_call
    from brain.systems.runs.tool_catalog.registry import output_budget_chars_for_tool

    budget = output_budget_chars_for_tool("brain_recall")
    request = PendingToolCall(
        block_id="call-1",
        tool_name="brain_recall",
        tool_input={},
        handler=lambda: {"memories": ["x" * (budget * 3)]},
    )

    resolved = resolve_tool_call(request)

    assert len(resolved.result_text) <= budget
    assert "truncated by tool output budget" in resolved.result_text
    assert resolved.result_text.startswith("{")
