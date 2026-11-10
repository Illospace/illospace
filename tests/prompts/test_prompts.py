"""The prompt sets the voice. These tests keep it short, complete, and deterministic.

Update the snapshots after a deliberate text change:
ILLO_UPDATE_SNAPSHOTS=1 .venv/bin/pytest tests/prompts
"""

import os
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from illo.brain.prompts import BASE_PROMPT, PROFILE_TEXTS, build, build_for_run
from illo.gateway import InboundEvent
from illo.gateway.principal import Authority
from illo.ledger import asks
from illo.tools.checks import PROFILE_NAMES
from illo.workspace import load_workspace

ROOT = Path(__file__).resolve().parents[2]
SNAPSHOTS = Path(__file__).with_name("snapshots")
NOW = datetime(2026, 11, 10, 14, 4, tzinfo=UTC)

# One case for each profile: (principal, profile, channel id, event channel, open asks).
CASES = {
    "person_dm": ("person:ada", "person_dm", None, "slack", 3),
    "team_channel": ("team", "team_channel", "C_ACME_TEAM", "slack", 0),
    "tainted": ("team", "tainted", "C_ACME_ALERTS", "slack", 0),
    "routine": ("person:ada", "routine", None, "routine", 0),
    "agent_request": ("person:ben", "agent_request", None, "mcp", 0),
    "outsider": ("outsider", "tainted", None, "email", 0),
}


@pytest.fixture(scope="module")
def example():
    return load_workspace(ROOT / "workspace.example")


def words(text: str) -> int:
    return len(re.findall(r"\S+", text))


@pytest.mark.parametrize("name", CASES)
def test_assembled_prompt_matches_snapshot(example, name):
    principal, profile, channel_id, event_channel, open_asks = CASES[name]
    text = build(
        example,
        principal,
        profile,
        channel_id,
        now=NOW,
        event_channel=event_channel,
        open_asks=open_asks,
    )
    path = SNAPSHOTS / f"{name}.txt"
    if os.environ.get("ILLO_UPDATE_SNAPSHOTS") == "1":
        path.write_text(text + "\n")
    assert text + "\n" == path.read_text(), f"Update {path.name} if the change is deliberate"


def test_base_prompt_stays_short():
    assert words(BASE_PROMPT) <= 900


def test_every_profile_has_a_short_text():
    assert PROFILE_NAMES <= PROFILE_TEXTS.keys()
    for name, text in PROFILE_TEXTS.items():
        assert words(text) <= 150, name


def test_base_prompt_has_examples_and_no_tables():
    assert BASE_PROMPT.count("\nIllo:\n") >= 6
    assert not re.search(r"^\|.*\|$", BASE_PROMPT, re.M)


def test_open_asks_line_only_for_a_dm_with_open_asks(example):
    def facts(profile, count):
        return build(example, "person:ada", profile, None, now=NOW, open_asks=count)

    assert "Open asks of Ada: 2." in facts("person_dm", 2)
    assert "Open asks" not in facts("person_dm", 0)
    assert "Open asks" not in facts("routine", 2)


def test_local_time_uses_the_person_time_zone(example):
    assert "15:04, Europe/Paris" in build(example, "person:ada", "person_dm", now=NOW)
    assert "09:04, America/New_York" in build(example, "person:ben", "person_dm", now=NOW)
    assert "15:04, Europe/Paris" in build(example, "team", "team_channel", now=NOW)


def test_channel_rules_follow_the_channel(example):
    alerts = build(example, "team", "tainted", "C_ACME_ALERTS", now=NOW)
    team = build(example, "team", "team_channel", "C_ACME_TEAM", now=NOW)
    assert "### alerts-coordinator" in alerts and "### team" in alerts
    assert "### alerts-coordinator" not in team and "### team" in team


async def test_build_for_run_counts_the_open_asks_of_the_person(conn, workspace, ada, ben):
    await asks.create(conn, owner_person_id=ada["id"], question="Pick a log retention")
    await asks.create(conn, owner_person_id=ada["id"], question="Review the runner change")
    await asks.create(conn, owner_person_id=ben["id"], question="Not for Ada")
    event = InboundEvent(channel="slack", external_id="x", kind="dm", text="what needs me?")
    authority = Authority("person:ada", "person_dm", ada["id"])
    text = await build_for_run(conn, workspace, authority, event, now=NOW)
    assert "Open asks of Ada: 2." in text
    assert "• Channel: a Slack DM" in text


async def test_the_run_gets_the_assembled_prompt(app, replay, fake_brain):
    await replay(text="what needs me?")
    prompt = fake_brain.requests[0].system_prompt
    assert prompt.startswith(BASE_PROMPT)
    assert PROFILE_TEXTS["person_dm"] in prompt
    assert "• Principal: person:ada" in prompt
