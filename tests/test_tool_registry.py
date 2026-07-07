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


def test_registered_tools_have_capability_coverage_or_explicit_exemption():
    from brain.systems.runs.capabilities import (
        CAPABILITY_COVERAGE_EXEMPT_TOOLS,
        first_party_capability_tool_names,
    )
    from brain.systems.runs.tool_catalog.registry import all_tool_registrations

    registered = set(all_tool_registrations())
    covered = first_party_capability_tool_names() | set(CAPABILITY_COVERAGE_EXEMPT_TOOLS)

    assert registered - covered == set()


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


def test_skill_authoring_umbrella_tool_is_registered_for_runtime():
    from brain.systems.runs.tool_handlers import _get_tool_handlers
    from brain.systems.runs.tool_catalog.registry import all_tool_registrations

    dormant = {"create_skill", "manage_skill_asset", "flag_skill_gap"}
    registrations = all_tool_registrations()
    handlers = _get_tool_handlers()

    assert dormant.isdisjoint(registrations)
    assert dormant.isdisjoint(handlers)
    assert "manage_skill" in registrations
    assert "manage_skill" in handlers


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
    assert "read_workspace_overview" in _get_tool_handlers()
    assert "read_team_activity" in _get_tool_handlers()


def test_capability_tool_is_read_only_agent_run_surface():
    from brain.systems.runs.tool_handlers import _get_tool_handlers
    from brain.systems.runs.tool_catalog.registry import context_route_tool_names, get_tool_registration

    self_registration = get_tool_registration("read_self_context")
    registration = get_tool_registration("read_capabilities")

    assert self_registration is not None
    assert self_registration.permission == "read_runtime"
    assert self_registration.side_effect_class == "read_only"
    assert self_registration.evidence_emitter is True
    assert "self context" in self_registration.context_route.domains
    assert "read_self_context" in context_route_tool_names()
    assert "read_self_context" in _get_tool_handlers()

    assert registration is not None
    assert registration.permission == "read_runtime"
    assert registration.side_effect_class == "read_only"
    assert registration.evidence_emitter is True
    assert registration.context_route is not None
    assert "capabilities" in registration.context_route.domains
    assert "read_capabilities" in context_route_tool_names()
    assert "read_capabilities" in _get_tool_handlers()


def test_thread_discussion_reply_tool_is_registered_and_exposed():
    from brain.systems.runs.tool_definitions import COORDINATOR_TOOLS, WORKER_TOOLS
    from brain.systems.runs.tool_handlers import _get_tool_handlers
    from brain.systems.runs.tool_catalog.registry import get_tool_registration

    name = "post_thread_discussion_reply"
    assert name in _names(COORDINATOR_TOOLS)
    assert name in _names(WORKER_TOOLS)
    assert name in _get_tool_handlers()

    registration = get_tool_registration(name)
    assert registration is not None
    assert registration.permission == "write_chat"
    assert registration.side_effect_class == "chat_message"
    assert registration.reversibility == "append_only"


def test_publish_thread_asset_tool_is_registered_and_exposed():
    from brain.systems.runs.tool_definitions import COORDINATOR_TOOLS, WORKER_TOOLS
    from brain.systems.runs.tool_handlers import _get_tool_handlers
    from brain.systems.runs.tool_catalog.registry import get_tool_registration

    name = "publish_thread_asset"
    assert name in _names(COORDINATOR_TOOLS)
    assert name in _names(WORKER_TOOLS)
    assert name in _get_tool_handlers()

    registration = get_tool_registration(name)
    assert registration is not None
    assert registration.permission == "write_workspace"
    assert registration.side_effect_class == "append_only"
    assert registration.reversibility == "append_only"


def test_publish_thread_artifact_tool_is_registered_and_exposed():
    from brain.systems.runs.tool_definitions import COORDINATOR_TOOLS, WORKER_TOOLS
    from brain.systems.runs.tool_handlers import _get_tool_handlers
    from brain.systems.runs.tool_catalog.registry import get_tool_registration

    name = "publish_thread_artifact"
    assert name in _names(COORDINATOR_TOOLS)
    assert name in _names(WORKER_TOOLS)
    assert name in _get_tool_handlers()

    registration = get_tool_registration(name)
    assert registration is not None
    assert registration.permission == "write_workspace_app"
    assert registration.side_effect_class == "workspace_app_management"
    assert registration.reversibility == "reversible_by_archive"


def test_manage_workspace_app_exposes_collaborative_artifact_runtime():
    from brain.systems.runs.tool_definitions import COORDINATOR_TOOLS
    from brain.systems.runs.tool_catalog.registry import action_policy_for_tool

    tool = next(item for item in COORDINATOR_TOOLS if item["name"] == "manage_workspace_app")
    action_enum = tool["input_schema"]["properties"]["action"]["enum"]

    assert "get_collaboration" in action_enum
    assert "list_events" in action_enum
    assert "append_event" in action_enum
    assert "window.illo.collab" in tool["input_schema"]["properties"]["source_code"]["description"]

    assert action_policy_for_tool("manage_workspace_app", kwargs={"action": "get_collaboration"}) is None
    assert action_policy_for_tool("manage_workspace_app", kwargs={"action": "list_events"}) is None
    assert action_policy_for_tool("manage_workspace_app", kwargs={"action": "append_event"}) is not None


def test_ai_timeline_message_tool_is_registered_and_exposed():
    from brain.systems.runs.tool_definitions import COORDINATOR_TOOLS, WORKER_TOOLS
    from brain.systems.runs.tool_handlers import _get_tool_handlers
    from brain.systems.runs.tool_catalog.registry import get_tool_registration

    name = "post_ai_timeline_message"
    assert name in _names(COORDINATOR_TOOLS)
    assert name in _names(WORKER_TOOLS)
    assert name in _get_tool_handlers()

    registration = get_tool_registration(name)
    assert registration is not None
    assert registration.permission == "write_chat"
    assert registration.side_effect_class == "chat_message"
    assert registration.reversibility == "append_only"


def test_transcribe_audio_attachment_tool_is_registered_and_exposed():
    from brain.systems.runs.tool_definitions import COORDINATOR_TOOLS, WORKER_TOOLS
    from brain.systems.runs.tool_handlers import _get_tool_handlers
    from brain.systems.runs.tool_catalog.registry import get_tool_registration

    name = "transcribe_audio_attachment"
    assert name in _names(COORDINATOR_TOOLS)
    assert name in _names(WORKER_TOOLS)
    assert name in _get_tool_handlers()

    registration = get_tool_registration(name)
    assert registration is not None
    assert registration.permission == "network_read"
    assert registration.side_effect_class == "read_only_external"
    assert registration.reversibility == "read_only_external"
    assert registration.evidence_emitter is True


def test_spawn_worker_tool_is_coordinator_only_and_registered():
    from brain.systems.runs.tool_definitions import COORDINATOR_TOOLS, WORKER_TOOLS
    from brain.systems.runs.tool_handlers import _get_tool_handlers
    from brain.systems.runs.tool_catalog.registry import get_tool_registration

    name = "spawn_worker"
    assert name in _names(COORDINATOR_TOOLS)
    assert name not in _names(WORKER_TOOLS)
    assert name in _get_tool_handlers()

    registration = get_tool_registration(name)
    assert registration is not None
    assert [role.value for role in registration.availability] == ["coordinator"]
    assert registration.permission == "spawn_worker"
    assert registration.side_effect_class == "run_spawn"
    assert registration.reversibility == "reversible"


def test_manage_deployment_tool_is_coordinator_only_and_registered():
    from brain.systems.runs.tool_definitions import COORDINATOR_TOOLS, WORKER_TOOLS
    from brain.systems.runs.tool_handlers import _get_tool_handlers
    from brain.systems.runs.tool_catalog.registry import get_tool_registration

    name = "manage_deployment"
    assert name in _names(COORDINATOR_TOOLS)
    assert name not in _names(WORKER_TOOLS)
    assert name in _get_tool_handlers()

    registration = get_tool_registration(name)
    assert registration is not None
    assert [role.value for role in registration.availability] == ["coordinator"]
    assert registration.permission == "manage_runtime"
    assert registration.side_effect_class == "deployment_management"
    assert registration.reversibility == "variable"


def test_manage_runtime_services_tool_is_coordinator_only_and_registered():
    from brain.systems.runs.tool_definitions import COORDINATOR_TOOLS, WORKER_TOOLS
    from brain.systems.runs.tool_handlers import _get_tool_handlers
    from brain.systems.runs.tool_catalog.registry import get_tool_registration

    name = "manage_runtime_services"
    assert name in _names(COORDINATOR_TOOLS)
    assert name not in _names(WORKER_TOOLS)
    assert name in _get_tool_handlers()

    registration = get_tool_registration(name)
    assert registration is not None
    assert [role.value for role in registration.availability] == ["coordinator"]
    assert registration.permission == "manage_runtime"
    assert registration.side_effect_class == "deployment_management"
    assert registration.reversibility == "variable"


def test_manage_workspace_tools_tool_is_coordinator_only_and_registered():
    from brain.systems.runs.tool_definitions import COORDINATOR_TOOLS, WORKER_TOOLS
    from brain.systems.runs.tool_handlers import _get_tool_handlers
    from brain.systems.runs.tool_catalog.registry import get_tool_registration

    name = "manage_workspace_tools"
    assert name in _names(COORDINATOR_TOOLS)
    assert name not in _names(WORKER_TOOLS)
    assert name in _get_tool_handlers()

    registration = get_tool_registration(name)
    assert registration is not None
    assert [role.value for role in registration.availability] == ["coordinator"]
    assert registration.permission == "manage_runtime"
    assert registration.side_effect_class == "workspace_tool_management"
    assert registration.reversibility == "variable"


def test_context_route_surface_is_registry_driven():
    from brain.systems.runs.tool_catalog.registry import context_route_payload, context_route_tool_names

    names = context_route_tool_names()
    routes = {route["name"]: route for route in context_route_payload()}

    assert names == {
        "brain_recall",
        "my_activity",
        "query_workspace_data",
        "read_capabilities",
        "read_cycles",
        "read_project_contexts",
        "read_self_context",
        "read_team_activity",
        "read_team_members",
        "read_thread_messages",
        "read_workspace_apps",
        "read_workspace_overview",
        "read_workspace_records",
        "runtime_settings",
    }
    assert set(routes) == names
    assert "broad" in routes["brain_recall"]["scopes"]
    assert "thread transcript" in routes["read_thread_messages"]["domains"]
    assert routes["query_workspace_data"]["empty_result_policy"] == "answer_honestly"
    assert "workspace records" in routes["query_workspace_data"]["domains"]
    assert "setup" in routes["read_capabilities"]["domains"]
    assert "source code" in routes["read_self_context"]["domains"]
    assert "workspace setup" in routes["read_workspace_overview"]["domains"]


def test_workspace_activity_question_requires_workspace_data():
    from brain.systems.runs.introspection import required_introspection_tool

    tool, message = required_introspection_tool("Hey illo what is Alex working on?")

    assert tool == "read_team_activity"
    assert message is not None
    assert "current workspace/team activity" in message


def test_ability_intro_requires_capabilities():
    from brain.systems.runs.introspection import required_introspection_tool

    tool, message = required_introspection_tool("Hey Illo, help me understand what you can do to help me.")

    assert tool == "read_capabilities"
    assert message is not None
    assert "capability/setup context" in message


def test_source_identity_question_requires_self_context():
    from brain.systems.runs.introspection import required_introspection_tool

    tool, message = required_introspection_tool("Where is your source code installed?")

    assert tool == "read_self_context"
    assert message is not None
    assert "identity/source/runtime context" in message


def test_where_source_across_sentences_does_not_require_self_context():
    # Regression for issue #249: a task's ordinary "where ..." must not bridge across a
    # sentence boundary to a "source"/"code" token elsewhere in the prompt (here the
    # coordination wrapper's "Source metadata:" header) and force read_self_context,
    # which then hijacks the final answer with a runtime self-description.
    from brain.systems.runs.introspection import (
        _looks_like_self_context_question,
        required_introspection_tool,
    )

    message = (
        "Post-deploy SEO health check for uwear.ai. Confirm the 301 redirects resolve and "
        "call out where it's too soon to tell vs. where there's a real signal. Reply back "
        'with the findings. Source metadata: {"repo": "uwear-website"}'
    )

    assert _looks_like_self_context_question(message) is False
    assert required_introspection_tool(message) == (None, None)


def test_capability_setup_question_requires_capabilities():
    from brain.systems.runs.introspection import required_introspection_tool

    tool, message = required_introspection_tool("Hi Illo, I would like to set you up in our Slack.")

    assert tool == "read_capabilities"
    assert message is not None
    assert "capability/setup context" in message


def test_named_capability_setup_question_requires_capabilities_without_agent_mention():
    from brain.systems.runs.introspection import required_introspection_tool

    tool, message = required_introspection_tool("Help me set up Slack for the team.")

    assert tool == "read_capabilities"
    assert message is not None
    assert "capability/setup context" in message


def test_work_request_that_mentions_skill_and_cycle_does_not_require_capabilities():
    from brain.systems.runs.introspection import required_introspection_tool

    message = (
        "I want to create un dropshipping store where we will add trend products on the hot topics "
        "we can find on the news/reddit. Idea is a collection last only something short like 2 weeks. "
        "can you have a skill and a cyle running every week to propose trendy or funny/popular design ?"
    )

    assert required_introspection_tool(message) == (None, None)


def test_run_introspection_uses_current_human_message_over_thread_wrapper():
    from brain.systems.runs.introspection import (
        message_for_required_introspection,
        required_introspection_tool,
    )

    wrapped_message = (
        '[Idea: "Help me set up Slack for the team" | idea-1]\n\n'
        "where are the results ?"
    )

    assert required_introspection_tool(wrapped_message)[0] == "read_capabilities"
    assert message_for_required_introspection(wrapped_message) == "where are the results ?"

    selected = message_for_required_introspection(
        wrapped_message,
        {"human_message": "where are the results ?"},
    )

    assert selected == "where are the results ?"
    assert required_introspection_tool(selected) == (None, None)


def test_thread_content_action_does_not_require_capabilities():
    from brain.systems.runs.introspection import required_introspection_tool

    tool, message = required_introspection_tool("Add JB's response to the thread.")

    assert tool is None
    assert message is None


def test_app_content_action_does_not_require_capabilities():
    from brain.systems.runs.introspection import required_introspection_tool

    tool, message = required_introspection_tool("Add JB's response to the app.")

    assert tool is None
    assert message is None


def test_add_integration_action_still_requires_capabilities():
    from brain.systems.runs.introspection import required_introspection_tool

    tool, message = required_introspection_tool("Add the Slack integration.")

    assert tool == "read_capabilities"
    assert message is not None
    assert "capability/setup context" in message


def test_memory_question_does_not_force_workspace_data():
    from brain.systems.runs.introspection import required_introspection_tool

    assert required_introspection_tool("What do you remember about Redis?") == (None, None)


def test_workspace_data_sources_are_adapter_registered():
    from brain.systems.runs.tool_catalog.handlers.workspace_data import _normalize_sources, _source_adapters

    adapters = _source_adapters()

    assert "runs" in adapters
    assert "team_members" in adapters
    assert "project_profiles" in adapters
    assert "project_attachments" in adapters
    assert "cycles" in adapters
    assert "cycle_runs" in adapters
    assert "memories" not in adapters
    assert "memories" not in _normalize_sources(None)
    assert _normalize_sources(["memory"]) == _normalize_sources(None)
    assert _normalize_sources(["activity"]) == [
        "runs",
        "threads",
        "ideas",
        "tool_calls",
        "project_attachments",
        "domain_events",
        "workspace_apps",
        "cycle_runs",
    ]
    assert _normalize_sources(["apps"]) == ["workspace_apps", "app_state"]
    assert _normalize_sources(["records"]) == ["domains", "domain_records", "domain_events"]
    assert _normalize_sources(["project_contexts"]) == ["project_profiles", "project_attachments"]
    assert _normalize_sources(["cycles"]) == ["cycles", "cycle_runs"]


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
    assert action_policy_for_tool("manage_skill", kwargs={"action": "get"}) is None

    policy = action_policy_for_tool("manage_cycle", kwargs={"action": "create"})
    assert policy == {
        "risk": "high",
        "reversibility": "reversible",
        "expected_effect": "mutate a scheduled cycle",
    }
    assert action_policy_for_tool("manage_skill", kwargs={"action": "create"}) == {
        "risk": "high",
        "reversibility": "variable",
        "expected_effect": "create a durable slash-routable skill",
    }

    assert action_policy_for_tool(
        "exec_command",
        kwargs={"command": "git push origin main"},
    )["risk"] == "high"
    assert action_policy_for_tool(
        "browser",
        kwargs={"action": "snapshot", "persist": False},
    ) is None


def test_manage_cycle_schema_matches_canonical_runtime_policy():
    from brain.systems.runs.tool_definitions import COORDINATOR_TOOLS

    tool = next(tool for tool in COORDINATOR_TOOLS if tool["name"] == "manage_cycle")
    properties = tool["input_schema"]["properties"]

    assert "execution_mode" not in properties
    assert "reopen_archived" not in properties
    assert "add_guidance" in properties["action"]["enum"]
    assert "add_output_target" in properties["action"]["enum"]


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
