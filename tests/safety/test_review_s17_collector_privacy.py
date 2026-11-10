import json
from types import SimpleNamespace

import pytest

from collectors.common import MCPClient, clean
from collectors.hooks import report
from collectors.t3.collector import Collector, Config, Thread


def slack_token(kind):
    return "xox" + kind + "-" + "1234567890-" * 2 + "a" * 26


@pytest.mark.parametrize("scheme", ["postgresql", "redis", "https", "ssh", "custom+db"])
def test_rule_09_redacts_credentials_from_every_url_scheme(scheme):
    value = clean(f"Read {scheme}://ada:private-password@db.example/path?code=secret#fragment")
    assert "ada:" not in value and "private-password" not in value
    assert "secret" not in value
    assert f"{scheme}://db.example/path" in value


@pytest.mark.parametrize(
    "secret",
    [
        slack_token("b"),
        slack_token("p"),
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJhY21lIn0.abcdefghijklmnopqrstuvwx",
        "AIza" + "a" * 35,
    ],
)
def test_rule_09_redacts_common_bare_tokens(secret):
    assert clean("Before " + secret + " after") == "Before [redacted] after"


@pytest.mark.parametrize(
    "share,expected", [(None, "private"), ("private", "private"), ("team", "team")]
)
def test_rule_09_t3_lanes_are_private_unless_shared(tmp_path, share, expected):
    config = Config(
        "https://illo.example/mcp", tmp_path / "token", state_file=tmp_path / "state.json"
    )
    if share is not None:
        config.share = share
    collector = Collector(config, SimpleNamespace(token="local"), SimpleNamespace(token="illo"))
    thread = Thread(
        "thread-1",
        "A personal task",
        "working",
        "Private prose",
        "https://t3.example/thread-1",
        "project",
    )
    assert collector.lane(thread)["visibility"] == expected


def test_rule_09_t3_share_config_and_environment(tmp_path, monkeypatch):
    path = tmp_path / "collector.toml"
    path.write_text(
        'mcp_url = "https://illo.example/mcp"\ntoken_file = "/tmp/acme-token"\nshare = "team"\n'
    )
    assert Config.load(path).share == "team"
    monkeypatch.setenv("ILLO_SHARE", "private")
    assert Config.load(path).share == "private"


@pytest.mark.parametrize(
    "share,expected", [(None, "private"), ("team", "team"), ("invalid", "private")]
)
@pytest.mark.parametrize("source", ["claude-code", "codex"])
def test_rule_09_hook_report_applies_share_config(monkeypatch, tmp_path, share, expected, source):
    token = tmp_path / "token"
    token.write_text("acme-token")
    token.chmod(0o600)
    config = tmp_path / "hooks.json"
    values = {"mcp_url": "https://illo.example/mcp", "token_file": str(token)}
    if share is not None:
        values["share"] = share
    config.write_text(json.dumps(values))
    calls = []
    monkeypatch.setattr(report, "repository", lambda _: "acme/app")
    monkeypatch.setattr(MCPClient, "call", lambda self, name, args: calls.append(args))
    report.worker(
        source,
        {
            "session_id": "session-1",
            "hook_event_name": "UserPromptSubmit",
            "prompt": "My private task",
            "type": "agent-turn-complete",
            "thread-id": "thread-1",
            "input-messages": ["My private task"],
        },
        config,
    )
    assert calls[0]["visibility"] == expected


def test_rule_09_hook_redacts_cached_title(monkeypatch, tmp_path):
    token = tmp_path / "token"
    token.write_text("acme-token")
    token.chmod(0o600)
    config = tmp_path / "hooks.json"
    config.write_text(json.dumps({"mcp_url": "https://illo.example/mcp", "token_file": str(token)}))
    (tmp_path / "hook-state.json").write_text(
        json.dumps(
            {
                "claude-code:session-1": {
                    "title": slack_token("b"),
                    "at": 0,
                }
            }
        )
    )
    calls = []
    monkeypatch.setattr(report, "repository", lambda _: "acme/app")
    monkeypatch.setattr(MCPClient, "call", lambda self, name, args: calls.append(args))
    report.worker("claude-code", {"session_id": "session-1", "hook_event_name": "Stop"}, config)
    assert calls[0]["title"] == "[redacted]"
