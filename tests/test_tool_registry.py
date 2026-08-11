from __future__ import annotations

from datetime import timedelta

import pytest


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


def test_every_registered_tool_declares_an_explicit_side_effect_class():
    from brain.systems.runs.tool_catalog.registry import _STATIC_METADATA, all_tool_registrations

    assert {
        name
        for name in all_tool_registrations()
        if not _STATIC_METADATA.get(name, {}).get("side_effect_class")
    } == set()


def test_unknown_tool_side_effect_defaults_to_write():
    from brain.systems.runs.tool_catalog.metadata import ToolSideEffectClass
    from brain.systems.runs.tool_catalog.registry import side_effect_class_for_tool

    assert side_effect_class_for_tool("not_registered") is ToolSideEffectClass.WRITE


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


def test_create_github_issue_tool_is_registered_and_exposed():
    from brain.systems.runs.tool_definitions import COORDINATOR_TOOLS, WORKER_TOOLS
    from brain.systems.runs.tool_handlers import _get_tool_handlers
    from brain.systems.runs.tool_catalog.registry import get_tool_registration

    name = "create_github_issue"
    assert name in _names(COORDINATOR_TOOLS)
    assert name in _names(WORKER_TOOLS)
    assert name in _get_tool_handlers()

    registration = get_tool_registration(name)
    assert registration is not None
    assert registration.side_effect_class != "read_only"
    assert registration.action_manifest is True
    assert registration.risk_class == "high"


def test_github_sub_issue_tools_are_registered_and_exposed():
    from brain.systems.runs.tool_definitions import COORDINATOR_TOOLS, WORKER_TOOLS
    from brain.systems.runs.tool_handlers import _get_tool_handlers
    from brain.systems.runs.tool_catalog.registry import get_tool_registration

    handlers = _get_tool_handlers()
    for name in (
        "add_github_sub_issue",
        "remove_github_sub_issue",
        "list_github_sub_issues",
    ):
        assert name in _names(COORDINATOR_TOOLS)
        assert name in _names(WORKER_TOOLS)
        assert name in handlers

    for name in ("add_github_sub_issue", "remove_github_sub_issue"):
        registration = get_tool_registration(name)
        assert registration is not None
        assert registration.permission == "write_workspace"
        assert registration.risk_class == "high"
        assert registration.reversibility == "reversible"
        assert registration.action_manifest is True
        assert registration.output_budget_chars == 8_000

    read_registration = get_tool_registration("list_github_sub_issues")
    assert read_registration is not None
    assert read_registration.side_effect_class == "read_only"
    assert read_registration.output_budget_chars == 18_000


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


def test_manage_runtime_preferences_tool_is_registered_and_audited():
    import inspect

    from brain.systems.runs.tool_definitions import COORDINATOR_TOOLS, WORKER_TOOLS
    from brain.systems.runs.tool_handlers import _get_tool_handlers
    from brain.systems.runs.tool_catalog.registry import get_tool_registration

    name = "manage_runtime_preferences"
    assert name in _names(COORDINATOR_TOOLS)
    assert name not in _names(WORKER_TOOLS)
    handler = _get_tool_handlers()[name]
    assert set(inspect.signature(handler).parameters) == {
        "action",
        "setting",
        "value",
    }

    registration = get_tool_registration(name)
    assert registration is not None
    assert [role.value for role in registration.availability] == ["coordinator"]
    assert registration.permission == "manage_runtime"
    assert registration.side_effect_class == "runtime_configuration"
    assert registration.reversibility == "reversible"
    assert registration.action_manifest is True


def test_manage_storage_policy_tool_is_registered_and_audited():
    import inspect

    from brain.systems.runs.tool_definitions import COORDINATOR_TOOLS, WORKER_TOOLS
    from brain.systems.runs.tool_handlers import _get_tool_handlers
    from brain.systems.runs.tool_catalog.registry import get_tool_registration

    name = "manage_storage_policy"
    assert name in _names(COORDINATOR_TOOLS)
    assert name not in _names(WORKER_TOOLS)
    handler = _get_tool_handlers()[name]
    assert set(inspect.signature(handler).parameters) == {
        "action",
        "policy_id",
        "rationale",
        "limit",
        "storage_values",
    }
    assert "__signature__" not in vars(handler)

    registration = get_tool_registration(name)
    assert registration is not None
    assert [role.value for role in registration.availability] == ["coordinator"]
    assert registration.permission == "manage_runtime"
    assert registration.side_effect_class == "runtime_configuration"
    assert registration.reversibility == "reversible"
    assert registration.action_manifest is True


@pytest.mark.asyncio
async def test_manage_storage_policy_handler_binds_the_derived_patch(monkeypatch):
    from brain.systems import storage_policy
    from brain.systems.runs.tool_handlers import _get_tool_handlers

    captured = {}

    class FakeUnitOfWork:
        session = object()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    async def fake_manage_storage_policy(session, **kwargs):
        captured.update(session=session, **kwargs)
        return {"ok": True}

    monkeypatch.setattr(
        "brain.platform.db.repositories.unit_of_work.UnitOfWork",
        FakeUnitOfWork,
    )
    monkeypatch.setattr(
        storage_policy,
        "async_manage_storage_policy",
        fake_manage_storage_policy,
    )
    handler = _get_tool_handlers()["manage_storage_policy"]

    result = await handler(
        action="update",
        canvas_quiet_hours=12,
        rationale="Exercise the typed boundary",
    )

    assert result == {"ok": True}
    assert captured["patch"] == storage_policy.StoragePolicyPatch(
        canvas_quiet_period=timedelta(hours=12)
    )
    assert "canvas_quiet_hours" not in captured

    with pytest.raises(TypeError, match="Unexpected storage policy fields: unknown"):
        await handler(unknown=True)


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
        "search_knowledge",
    }
    assert set(routes) == names
    assert "broad" in routes["brain_recall"]["scopes"]
    assert "thread transcript" in routes["read_thread_messages"]["domains"]
    assert routes["query_workspace_data"]["empty_result_policy"] == "answer_honestly"
    assert "workspace records" in routes["query_workspace_data"]["domains"]
    assert "setup" in routes["read_capabilities"]["domains"]
    assert "source code" in routes["read_self_context"]["domains"]
    assert "workspace setup" in routes["read_workspace_overview"]["domains"]


def test_my_activity_is_available_to_workers():
    from brain.systems.runs.tool_catalog.registry import get_tool_registration
    from brain.systems.runs.tool_definitions import WORKER_TOOLS

    assert "my_activity" in _names(WORKER_TOOLS)
    registration = get_tool_registration("my_activity")
    assert registration is not None
    assert {role.value for role in registration.availability} == {
        "coordinator",
        "worker",
    }


def test_workspace_activity_question_is_not_force_routed():
    # No heuristic forcing remains: "what is X working on" is answered by the model
    # calling read_team_activity voluntarily, not by an end-of-turn forced detour.
    from brain.systems.runs.introspection import required_introspection_tool

    assert required_introspection_tool("Hey illo what is Alex working on?") == (None, None)


def test_capability_question_is_not_force_routed():
    # read_capabilities is a self-description tool; force-routing it at end-of-turn hijacked
    # completed work (issue #249 recurrences). A genuine "what can you do" question is
    # answered by the model calling read_capabilities voluntarily, not by forcing.
    from brain.systems.runs.introspection import required_introspection_tool

    assert required_introspection_tool(
        "Hey Illo, help me understand what you can do to help me."
    ) == (None, None)


def test_source_identity_question_is_not_force_routed():
    # read_self_context is no longer force-routed: its "where … source" trigger also fires
    # inside ordinary work requests and hijacked the answer (issue #249). The model can
    # still call read_self_context voluntarily for a genuine identity question.
    from brain.systems.runs.introspection import required_introspection_tool

    assert required_introspection_tool("Where is your source code installed?") == (None, None)


def test_where_source_across_sentences_does_not_require_self_context():
    # Regression for issue #249: an ordinary task that happens to contain "where ..." and a
    # "source"/"code" token (here the coordination wrapper's "Source metadata:" header) must
    # never be routed to a self-description tool. read_self_context is no longer force-routed
    # at all, so this holds unconditionally.
    from brain.systems.runs.introspection import required_introspection_tool

    message = (
        "Post-deploy SEO health check for uwear.ai. Confirm the 301 redirects resolve and "
        "call out where it's too soon to tell vs. where there's a real signal. Reply back "
        'with the findings. Source metadata: {"repo": "uwear-website"}'
    )

    assert required_introspection_tool(message) == (None, None)


def test_setup_request_is_not_force_routed_to_capabilities():
    # "set you up in our Slack" is a work request, not a request for a capability listing.
    # Force-routing read_capabilities here is exactly what hijacked answers; it no longer does.
    from brain.systems.runs.introspection import required_introspection_tool

    assert required_introspection_tool(
        "Hi Illo, I would like to set you up in our Slack."
    ) == (None, None)


def test_named_setup_request_is_not_force_routed_to_capabilities():
    from brain.systems.runs.introspection import required_introspection_tool

    assert required_introspection_tool("Help me set up Slack for the team.") == (None, None)


def test_work_request_that_mentions_skill_and_cycle_does_not_require_capabilities():
    from brain.systems.runs.introspection import required_introspection_tool

    message = (
        "I want to create un dropshipping store where we will add trend products on the hot topics "
        "we can find on the news/reddit. Idea is a collection last only something short like 2 weeks. "
        "can you have a skill and a cyle running every week to propose trendy or funny/popular design ?"
    )

    assert required_introspection_tool(message) == (None, None)


def test_run_introspection_uses_current_human_message_over_thread_wrapper():
    # message_for_required_introspection extracts the operator's latest message from a
    # decorated thread wrapper, and prefers an explicit human_message metadata field.
    # (Nothing is force-routed anymore regardless of content — asserted for good measure.)
    from brain.systems.runs.introspection import (
        message_for_required_introspection,
        required_introspection_tool,
    )

    wrapped_message = (
        '[Idea: "what is Alex working on this week" | idea-1]\n\n'
        "where are the results ?"
    )

    assert message_for_required_introspection(wrapped_message) == "where are the results ?"

    selected = message_for_required_introspection(
        wrapped_message,
        {"human_message": "where are the results ?"},
    )

    assert selected == "where are the results ?"
    assert required_introspection_tool(wrapped_message) == (None, None)
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


def test_add_integration_action_is_not_force_routed_to_capabilities():
    from brain.systems.runs.introspection import required_introspection_tool

    assert required_introspection_tool("Add the Slack integration.") == (None, None)


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
    assert action_policy_for_tool(
        "manage_runtime_preferences",
        kwargs={"action": "get"},
    ) is None
    assert action_policy_for_tool(
        "manage_storage_policy",
        kwargs={"action": "history"},
    ) is None

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
        "manage_runtime_preferences",
        kwargs={"action": "set"},
    ) == {
        "risk": "medium",
        "reversibility": "reversible",
        "expected_effect": "inspect or persist a supported workspace preference",
    }
    assert action_policy_for_tool(
        "manage_storage_policy",
        kwargs={"action": "update"},
    ) == {
        "risk": "medium",
        "reversibility": "reversible",
        "expected_effect": "inspect or revise the installation-wide storage policy",
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
    assert "usage_summary" in properties["action"]["enum"]
    assert properties["days"]["minimum"] == 1
    assert properties["run_limit"]["maximum"] == 500
    timeout_variants = properties["timeout_seconds"]["oneOf"]
    assert timeout_variants[0] == {
        "type": "integer",
        "minimum": 60,
        "maximum": 14400,
    }
    assert timeout_variants[1] == {"type": "null"}
    # The definitions module mirrors these bounds as literals (import-leaf
    # rule); this pins them to the validator's owning constants.
    from brain.systems.cycles.common import (
        MAX_CYCLE_TIMEOUT_SECONDS,
        MIN_CYCLE_TIMEOUT_SECONDS,
    )

    assert timeout_variants[0]["minimum"] == MIN_CYCLE_TIMEOUT_SECONDS
    assert timeout_variants[0]["maximum"] == MAX_CYCLE_TIMEOUT_SECONDS
    assert "live per-Cycle agent-run deadline" in tool["description"]
    assert "add_guidance" in properties["action"]["enum"]
    assert "add_output_target" in properties["action"]["enum"]
    assert properties["run_kind"]["enum"] == [
        "scheduled_digest",
        "off_slot_material_alert",
    ]


def test_launch_handoff_tool_schema_offers_codex_and_claude():
    from brain.systems.runs.tool_definitions import COORDINATOR_TOOLS

    tool = next(tool for tool in COORDINATOR_TOOLS if tool["name"] == "create_launch_handoff")
    target_tool = tool["input_schema"]["properties"]["target_tool"]

    assert target_tool["enum"] == ["codex", "claude"]
    assert target_tool["default"] == "codex"


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


def test_slack_reaction_is_a_low_risk_audited_chat_action():
    from brain.systems.runs.actions import build_action_manifest
    from brain.systems.runs.tool_catalog.registry import get_tool_registration
    from brain.systems.runs.tool_handlers import _get_tool_handlers

    registration = get_tool_registration("react_to_slack_message")

    assert registration is not None
    assert registration.permission.value == "write_chat"
    assert registration.risk_class.value == "low"
    assert registration.side_effect_class.value == "chat_message"
    assert registration.reversibility.value == "reversible"
    assert registration.action_manifest is True
    assert "react_to_slack_message" in _get_tool_handlers()

    manifest = build_action_manifest(
        "react_to_slack_message",
        kwargs={"emoji": "thumbsup"},
    )

    assert manifest is not None
    assert manifest.target.to_payload() == {"emoji": "thumbsup"}

    targeted_manifest = build_action_manifest(
        "react_to_slack_message",
        kwargs={"emoji": "thumbsup"},
        context={
            "target_ref": {
                "slack_trigger": {
                    "channel_id": "C123",
                    "message_ts": "1716900000.000100",
                }
            }
        },
    )
    assert targeted_manifest.target.to_payload() == {
        "emoji": "thumbsup",
        "channel_id": "C123",
        "message_ts": "1716900000.000100",
    }
