"""Check SDK message translation with the SDK's real message types."""

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, SystemMessage, TextBlock, ToolUseBlock

from brain import loop
from brain.errors import RunError
from brain.providers import BUILT_IN, Provider
from brain.tools import SmokeSession
from brain.wire.agent_sdk import translate


class ScriptedClient:
    def __init__(self, messages):
        self.messages = messages

    async def receive_response(self):
        for message in self.messages:
            yield message


ANTHROPIC = Provider(**{**BUILT_IN["anthropic"].wire_dict(), "api_key": "fake-key"})


async def test_catalog_rejects_a_builtin():
    client = ScriptedClient([SystemMessage(subtype="init", data={"tools": ["Bash"]})])
    with pytest.raises(RunError, match="outside Illo"):
        _ = [item async for item in translate(client)]


async def test_auth_error_does_not_echo_provider_text():
    client = ScriptedClient(
        [
            AssistantMessage(
                content=[TextBlock(text="private provider detail")],
                model="fake",
                error="authentication_failed",
            )
        ]
    )
    with pytest.raises(RunError) as error:
        _ = [item async for item in translate(client)]
    assert error.value.category == "auth"
    assert "private provider detail" not in str(error.value)


@pytest.mark.parametrize(
    "subtype,status,category",
    [
        ("error_max_turns", None, "limit"),
        ("success", 401, "auth"),
        ("error_during_execution", None, "runtime"),
    ],
)
async def test_sdk_result_error_categories(subtype, status, category):
    message = ResultMessage(
        subtype=subtype,
        duration_ms=1,
        duration_api_ms=1,
        is_error=True,
        num_turns=1,
        session_id="test",
        api_error_status=status,
        result="private provider detail",
    )
    events = [item async for item in translate(ScriptedClient([message]))]
    assert events[-1]["category"] == category
    assert "private provider detail" not in events[-1]["message"]


@pytest.mark.parametrize("after", [None, "text", "tool"])
async def test_sdk_transient_retry_observes_output(monkeypatch, after):
    opened, sleeps = [], []

    async def sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(loop, "sleep", sleep)
    output = {
        None: [],
        "text": [TextBlock(text="Partial answer.")],
        "tool": [ToolUseBlock(id="call_1", name="mcp__illo__lanes_search", input={})],
    }[after]

    async def request():
        opened.append(True)
        failed = len(opened) == 1
        messages = [
            AssistantMessage(model="fake", content=output if failed else [TextBlock(text="Ready.")]),
            ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=failed,
                          num_turns=1, session_id="fake", api_error_status=503 if failed else None),
        ]
        async for item in translate(ScriptedClient(messages)):
            yield item

    if after is None:
        events = [item async for item in loop.retry_transient(request)]
        assert sleeps == [2] and len(opened) == 2 and events[-1]["text"] == "Ready."
    else:
        with pytest.raises(RunError) as error:
            _ = [item async for item in loop.retry_transient(request)]
        assert error.value.category == "transient" and not sleeps and len(opened) == 1


async def test_sdk_smoke_rejects_two_tool_calls_with_the_same_message_id():
    messages = [
        AssistantMessage(
            model="fake", message_id="message_1",
            content=[ToolUseBlock(id=f"call_{n}", name="mcp__illo__smoke_step", input={"n": n})],
        )
        for n in (1, 2)
    ]
    with pytest.raises(RunError, match="one tool call in each response"):
        _ = [item async for item in translate(ScriptedClient(messages), smoke=SmokeSession())]


async def test_sdk_smoke_counts_three_separate_responses():
    session = SmokeSession()
    messages = []
    args = {}
    for step in range(1, 4):
        args = {**args, "n": step}
        messages.append(AssistantMessage(
            model="fake", message_id=f"message_{step}",
            content=[ToolUseBlock(id=f"call_{step}", name="mcp__illo__smoke_step", input=args)],
        ))
        result = await session.call_tool("smoke_step", args)
        args = {"token": result.content[0].text}
    messages.append(ResultMessage(
        subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
        num_turns=4, session_id="fake", stop_reason="end_turn", result="Ready.",
    ))
    events = [item async for item in translate(ScriptedClient(messages), smoke=session)]
    assert events[-1]["smoke_steps"] == 3
    assert events[-1]["stop"] == "end_turn"
    assert args["token"] not in str(events)


async def test_tool_limit_uses_one_separate_client_without_tools(run_request, tmp_path, monkeypatch):
    import json

    from claude_agent_sdk import ToolResultBlock, ToolUseBlock, UserMessage

    from brain.wire import agent_sdk as claude

    clients = []
    scripts = [
        [AssistantMessage(model="fake", content=[ToolUseBlock(
            id="c1", name="mcp__illo__lanes_search", input={"query": "release"})]),
         UserMessage(content=[ToolResultBlock(tool_use_id="c1", content="The release is ready.")]),
         ResultMessage(subtype="error_max_turns", duration_ms=1, duration_api_ms=1,
                       is_error=True, num_turns=2, session_id="fake", usage={"input_tokens": 2})],
        [AssistantMessage(model="fake", content=[TextBlock(text="The release is ready.")]),
         ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1,
                       is_error=False, num_turns=1, session_id="final", result="The release is ready.",
                       usage={"input_tokens": 3})],
    ]

    class Client(ScriptedClient):
        def __init__(self, options):
            super().__init__(scripts[len(clients)])
            self.options = options
            self.queries = []
            clients.append(self)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def query(self, text):
            self.queries.append(text)

    monkeypatch.setattr(claude, "ClaudeSDKClient", Client)
    run_request.max_tool_calls = 1
    events = [e async for e in claude.run(run_request, ANTHROPIC, tmp_path)]
    assert len(clients) == 2
    normal, final = clients
    assert normal.options.max_turns == 2
    assert final.options.max_turns == 1
    assert final.options.mcp_servers == {} and final.options.allowed_tools == []
    assert final.options.tools == [] and final.options.strict_mcp_config
    assert final.options.system_prompt == normal.options.system_prompt == run_request.system_prompt
    assert "Answer now" in final.queries[0] and "The release is ready." in final.queries[0]
    assert '<outside_data source="transcript">' in final.queries[0]
    assert "fake-run-token" not in json.dumps(final.queries)
    assert not [e for e in events if e["type"] == "error"]
    assert events[-1]["usage"] == {"input_tokens": 5, "limited": True}


@pytest.mark.parametrize("stage", ["first request", "last request"])
async def test_closed_run_closes_the_sdk_client_at_once(run_request, tmp_path, monkeypatch, stage):
    """A closed run must close its SDK client before the close returns, not at garbage collection."""
    from contextlib import aclosing

    from brain.wire import agent_sdk as claude

    opened, closed = [], []
    scripts = [
        [AssistantMessage(model="fake", content=[TextBlock(text="Reading the lanes.")]),
         ResultMessage(subtype="error_max_turns", duration_ms=1, duration_api_ms=1,
                       is_error=True, num_turns=2, session_id="fake", usage={})],
        [AssistantMessage(model="fake", content=[TextBlock(text="The release is ready.")]),
         ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
                       num_turns=1, session_id="final", result="The release is ready.", usage={})],
    ]

    class Client(ScriptedClient):
        def __init__(self, options):
            super().__init__(scripts[len(opened)])
            opened.append(self)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            closed.append(opened.index(self))

        async def query(self, text):
            pass

    monkeypatch.setattr(claude, "ClaudeSDKClient", Client)
    run_request.max_tool_calls = 1
    events = claude.run(run_request, ANTHROPIC, tmp_path)
    async with aclosing(events):
        await anext(events)
        while stage == "last request" and len(opened) < 2:
            await anext(events)
        assert len(closed) < len(opened)
    assert closed == list(range(len(opened)))
