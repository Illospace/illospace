"""Each registered tool explains its behavior before the model calls it."""

import os
from pathlib import Path

import pytest

from illo.channels.mcp import TOOLS
from illo.tools.registry import Registry, Tool

SNAPSHOTS = Path(__file__).with_name("snapshots")


def check_description(name, description):
    assert len(description.strip()) >= 40, name
    assert description.strip() not in {name, name.replace("_", " ")}, name


def test_all_registered_tools_have_descriptions():
    registry = Registry()
    registry.discover()
    for tool in [*registry.tools.values(), *TOOLS.values()]:
        check_description(tool.name, tool.description)


@pytest.mark.parametrize("description", ["", "sample_tool", "sample tool"])
def test_description_check_rejects_missing_text(description):
    async def handler(ctx, args):
        return None

    registry = Registry()
    registry.add(Tool("sample_tool", "test", "read", lambda _: "ledger", False, handler,
                      description=description))
    with pytest.raises(AssertionError):
        for tool in registry.tools.values():
            check_description(tool.name, tool.description)


@pytest.mark.parametrize(
    "profile",
    [
        "person_dm",
        "team_channel",
        "routine",
        "agent_request",
        "tainted",
        "outsider",
        "digest",
        "team_digest",
        "mcp",
    ],
)
def test_tool_description_snapshots(profile):
    registry = Registry()
    registry.discover()
    run = {
        "principal": "outsider"
        if profile == "outsider"
        else "team"
        if profile in {"team_channel", "routine", "team_digest", "tainted"}
        else "person:sample",
        "profile": "person_dm"
        if profile == "digest"
        else "team_channel"
        if profile == "team_digest"
        else profile,
        "person_id": None
        if profile in {"outsider", "team_channel", "routine", "team_digest", "tainted"}
        else 1,
        "event_channel": profile if profile in {"digest", "team_digest"} else "slack",
        "tainted": profile == "tainted",
        "personal_read": False,
    }
    tools = (
        [t.declaration() for t in TOOLS.values()] if profile == "mcp" else registry.list_tools(run)
    )
    text = "".join(
        f"{t['name']}: {t['description']}\n" for t in sorted(tools, key=lambda t: t["name"])
    )
    path = SNAPSHOTS / f"tools-{profile}.txt"
    if os.environ.get("ILLO_UPDATE_SNAPSHOTS") == "1":
        path.write_text(text)
    assert text == path.read_text()
    if profile != "mcp":
        assert not {"asks_digest", "lanes_attention"} & {t["name"] for t in tools}
