"""GitHub issue edits must stay optional on the actual provider surface."""

from copy import deepcopy

import pytest


@pytest.mark.parametrize("role", ["coordinator", "worker"])
@pytest.mark.parametrize("provider", ["openai", "anthropic"])
def test_update_issue_required_fields_survive_provider_export(role, provider):
    from brain.platform.integrations.transports.anthropic import AnthropicMessagesTransport
    from brain.platform.integrations.transports.openai_responses import OpenAIResponsesTransport
    from brain.systems.runs.direct_loop.request import build_api_request
    from brain.systems.runs.tool_catalog.definitions.github import GITHUB_TOOLS
    from brain.systems.runs.tool_catalog.registry import get_tool_registration
    from brain.systems.runs.tool_definitions import COORDINATOR_TOOLS, WORKER_TOOLS

    name = "update_github_issue"
    declared = deepcopy(next(tool for tool in GITHUB_TOOLS if tool["name"] == name)["input_schema"])
    runtime_tools = COORDINATOR_TOOLS if role == "coordinator" else WORKER_TOOLS
    runtime = next(tool for tool in runtime_tools if tool["name"] == name)
    model = "gpt-5" if provider == "openai" else "claude-sonnet-4-5"
    request = build_api_request(
        model=model,
        messages=[{"role": "user", "content": "File the alert and assign its owner."}],
        max_tokens=1024,
        system=None,
        tools=runtime_tools,
        reasoning_effort=None,
        extra_headers=None,
        provider_name=provider,
        session_id="issue-export-contract",
        persist_session=True,
        cache_tools=True,
        operation_type="agent_run",
    )
    transport = OpenAIResponsesTransport() if provider == "openai" else AnthropicMessagesTransport()
    kwargs = transport.build_kwargs(request)
    exported = next(tool for tool in kwargs["tools"] if tool["name"] == name)
    exported_schema = exported["parameters" if provider == "openai" else "input_schema"]

    assert declared["required"] == ["repo", "issue_number"]
    assert get_tool_registration(name).schema == declared
    assert runtime["input_schema"] == declared
    assert exported_schema == declared
    print(f"{provider}/{role} update_github_issue exported required: {exported_schema['required']}")
