"""Check SDK message translation with the SDK's real message types."""

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, SystemMessage, TextBlock

from brain.models import RunError
from brain.runtimes.claude import translate


class ScriptedClient:
    def __init__(self, messages):
        self.messages = messages

    async def receive_response(self):
        for message in self.messages:
            yield message


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
