"""Check the collector's authority and credential boundaries with HTTP fakes."""

import json

import httpx
import pytest

from collectors.common import CollectorError, MCPClient
from collectors.t3.collector import Collector, Config, T3Source
from tests.collectors.test_t3 import APIs


@pytest.fixture
def collector_case(tmp_path):
    local_token = tmp_path / "t3-token"
    local_token.write_text("acme-machine-credential")
    local_token.chmod(0o600)
    config = Config(
        "https://illo.example/mcp",
        tmp_path / "illo-token",
        t3_url="https://t3.example",
        t3_token_file=local_token,
        state_file=tmp_path / "state.json",
        projects={"project-a": "acme/app"},
    )
    api = APIs()
    source = T3Source(config, httpx.Client(transport=httpx.MockTransport(api.t3)))
    mcp = MCPClient(
        config.mcp_url,
        "acme-illo-credential",
        httpx.Client(transport=httpx.MockTransport(api.illo)),
    )
    return Collector(config, source, mcp), api


@pytest.mark.parametrize("invalid", ["instructions", "option", "missing-rule", "free-answer"])
def test_rule_08_rule_answer_needs_a_proposed_option_and_a_named_rule(collector_case, invalid):
    collector, api = collector_case
    api.wait()
    collector.poll()
    api.answer()
    answer = api.answers[0]
    answer.update(answered_via="rule", answer_text=None, rule_text="Run tests first.")
    if invalid == "instructions":
        answer["answer_text"] = "Change the work."
    elif invalid == "option":
        answer["answer_option"] = {"id": "invented", "label": "Invented"}
    elif invalid == "missing-rule":
        answer["rule_text"] = None
    else:
        answer["answer_option"] = None
    with pytest.raises(CollectorError):
        collector.poll()
    assert not api.sent
    assert not any(name == "ask_ack" for name, _ in api.calls)


def test_rule_09_illo_gets_no_machine_credential_or_tool_output(collector_case):
    collector, api = collector_case
    api.projection["messages"][0]["text"] = "acme-machine-credential acme-illo-credential"
    api.projection["turnItems"] = [{"type": "command_execution", "output": "PRIVATE OUTPUT"}]
    collector.poll()
    payload = json.dumps(api.calls)
    assert "acme-machine-credential" not in payload
    assert "acme-illo-credential" not in payload
    assert "PRIVATE OUTPUT" not in payload
    assert all(
        name in {"lane_report", "ask_create", "ask_cancel", "ask_pull", "ask_ack"}
        for name, _ in api.calls
    )
    assert set(api.calls[0][1]["lanes"][0]) == {
        "source",
        "external_id",
        "title",
        "state",
        "summary",
        "links",
        "repo",
        "machine",
        "visibility",
        "is_step",
        "parent_ref",
    }
