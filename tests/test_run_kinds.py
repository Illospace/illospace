"""The run kind comes from the verified event channel and the authority, not the profile alone."""

import pytest

from illo.gateway import InboundEvent
from illo.gateway.principal import resolve
from illo.ledger import tokens
from illo.workspace import Routine, TeamDigestConfig

EVENTS = {
    "person_dm": ("slack", "dm", "U_ACME_ADA", {}),
    "team_mention": ("slack", "message", "U_ACME_ADA",
                     {"channel_id": "C_ACME_TEAM", "mentioned": True}),
    "tainted_channel": ("slack", "message", "U_ACME_ADA",
                        {"channel_id": "C_ACME_TEAM", "mentioned": True, "bot": True}),
    "person_email": ("email", "message", "ada@example.com", {}),
    "outside_email": ("email", "message", "someone@outside.example", {}),
    "team_routine": ("routine", "scheduled", "", {"routine": "team-check", "principal": "team"}),
    "personal_routine": ("routine", "scheduled", "",
                         {"routine": "ada-check", "principal": "person:ada"}),
    "personal_digest": ("digest", "scheduled", "", {"principal": "person:ada"}),
    "team_digest": ("team_digest", "scheduled", "", {}),
    "agent_request": ("mcp", "submit", "<token>", {}),
    "signal": ("signals", "signal", "", {}),
}

# The authority that `illo/gateway/principal.py` gives today: principal, profile, tainted.
AUTHORITY = {
    "person_dm": ("person:ada", "person_dm", False),
    "team_mention": ("team", "team_channel", False),
    "tainted_channel": ("team", "tainted", True),
    "person_email": ("person:ada", "person_dm", False),
    "outside_email": ("outsider", "tainted", True),
    "team_routine": ("team", "routine", False),
    # A personal routine and a personal digest keep the person's authority.
    "personal_routine": ("person:ada", "person_dm", False),
    "personal_digest": ("person:ada", "person_dm", False),
    "team_digest": ("team", "routine", False),
    "agent_request": ("person:ada", "agent_request", False),
    "signal": ("team", "tainted", True),
}


@pytest.fixture
def routed(workspace):
    routines = [
        Routine(name="team-check", schedule="0 9 * * 1", destination="none", principal="team"),
        Routine(name="ada-check", schedule="0 9 * * 1", destination="none", principal="person:ada"),
    ]
    digest = workspace.config.digest.model_copy(update={"team": TeamDigestConfig(channel="team")})
    config = workspace.config.model_copy(update={"digest": digest})
    return workspace.model_copy(update={"routines": routines, "config": config})


async def authority_for(conn, routed, ada, name):
    channel, kind, sender, payload = EVENTS[name]
    if sender == "<token>":
        sender = await tokens.create(conn, ada["id"], "run-kind", ["agent"])
    event = InboundEvent(channel=channel, external_id=name, kind=kind, sender=sender,
                         payload=payload)
    return event, await resolve(conn, event, routed)


@pytest.mark.parametrize("name", list(EVENTS))
async def test_principal_gives_the_profile_that_run_kinds_rely_on(conn, routed, ada, name):
    _, authority = await authority_for(conn, routed, ada, name)
    assert (authority.principal, authority.profile, authority.tainted) == AUTHORITY[name]
