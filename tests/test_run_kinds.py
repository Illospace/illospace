"""The run kind comes from the verified event channel and the authority, not the profile alone."""

import dataclasses

import pytest

from illo.brain.routes import resolve_route, run_kind
from illo.gateway import InboundEvent
from illo.gateway.principal import Authority, resolve
from illo.ledger import tokens
from illo.workspace import RouteSettings, Routine, TeamDigestConfig

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


RUN_KIND = {
    "person_dm": "person_dm",
    "team_mention": "team_channel",
    "tainted_channel": "tainted",
    "person_email": "person_dm",
    "outside_email": "tainted",
    "team_routine": "routine",
    "personal_routine": "routine",
    "personal_digest": "digest",
    "team_digest": "digest",
    "agent_request": "agent_request",
    "signal": "tainted",
}


@pytest.mark.parametrize("name", list(EVENTS))
async def test_run_kind_of_each_verified_event(conn, routed, ada, name):
    event, authority = await authority_for(conn, routed, ada, name)
    assert run_kind(event, authority, deep=False) == RUN_KIND[name]
    # A deep run is a deep run, whatever its channel and profile.
    assert run_kind(event, authority, deep=True) == "deep"


@pytest.mark.parametrize("name", ["personal_routine", "personal_digest"])
async def test_personal_background_runs_never_get_the_dm_route(conn, routed, ada, name):
    event, authority = await authority_for(conn, routed, ada, name)
    assert authority.profile == "person_dm"
    assert run_kind(event, authority, deep=False) != "person_dm"


def test_a_tainted_authority_is_tainted_on_every_channel():
    for channel in ("slack", "email", "routine", "digest", "mcp"):
        event = InboundEvent(channel=channel, external_id="x", kind="message")
        assert run_kind(event, Authority("team", "person_dm", tainted=True), deep=False) == "tainted"


@pytest.mark.parametrize("channel,profile", [("slack", "routine"), ("chat", "person_dm"),
                                             ("email", "agent_request"), ("deep", "person_dm")])
def test_an_unknown_pair_raises(channel, profile):
    event = InboundEvent(channel=channel, external_id="x", kind="message")
    with pytest.raises(ValueError, match="No run kind"):
        run_kind(event, Authority("team", profile), deep=False)


def test_a_route_takes_each_field_that_it_omits_from_default(workspace):
    routes = {"default": RouteSettings(provider="chatgpt", model="model-a", effort="low",
                                       max_tool_calls=12, timeout_s=120),
              "deep": RouteSettings(model="model-b", max_tool_calls=80, max_output_tokens=900)}
    brain = workspace.config.brain.model_copy(update={"routes": routes})
    routed = workspace.model_copy(update={
        "config": workspace.config.model_copy(update={"brain": brain})})
    assert dataclasses.asdict(resolve_route(routed, "deep")) == {
        "kind": "deep", "provider": "chatgpt", "model": "model-b", "effort": "low",
        "max_tool_calls": 80, "timeout_s": 120, "max_output_tokens": 900}
    assert resolve_route(routed, "person_dm").model == "model-a"
    with pytest.raises(dataclasses.FrozenInstanceError):
        resolve_route(routed, "person_dm").model = "other"
