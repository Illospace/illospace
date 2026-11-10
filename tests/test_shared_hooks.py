"""The shared hooks that keep areas independent: tool profiles, claims, card actions."""

import pytest

from illo.slackapi import ActionRouter, dm_person
from illo.tools.checks import scope
from illo.tools.registry import Arguments, Registry, Tool


async def _noop(ctx, args):
    return None


def _tool(name="demo_read", profiles=None, personal=False):
    return Tool(
        name=name,
        family="demo",
        risk="read",
        destination=lambda args: "ledger",
        personal=personal,
        handler=_noop,
        arguments=Arguments,
        profiles=profiles,
    )


def _run(profile="person_dm", tainted=False, principal="person:ada"):
    return {
        "principal": principal,
        "person_id": 1,
        "profile": profile,
        "tainted": tainted,
        "personal_read": False,
    }


def test_tool_profiles_set_the_grant():
    tool = _tool(profiles=frozenset({"person_dm"}))
    assert scope(_run("person_dm"), tool).outcome == "allow"
    assert scope(_run("team_channel", principal="team"), tool).outcome == "deny"


def test_taint_and_outsider_narrow_a_tool_profile():
    tool = _tool(profiles=frozenset({"person_dm", "team_channel"}))
    assert scope(_run("person_dm", tainted=True), tool).outcome == "deny"
    assert scope(_run("person_dm", principal="outsider"), tool).outcome == "deny"
    open_tool = _tool(profiles=frozenset({"tainted", "outsider"}))
    assert scope(_run("team_channel", tainted=True, principal="team"), open_tool).outcome == "deny"
    assert scope(_run("tainted", tainted=True, principal="outsider"), open_tool).outcome == "allow"


def test_registry_rejects_bad_profiles():
    registry = Registry()
    with pytest.raises(ValueError):
        registry.add(_tool(profiles=frozenset({"everyone"})))
    with pytest.raises(ValueError):
        registry.add(_tool(personal=True, profiles=frozenset({"person_dm", "tainted"})))
    registry.add(_tool(personal=True, profiles=frozenset({"person_dm"})))


async def test_claim_takes_an_event_before_the_brain(app, replay, channel, fake_brain):
    seen = []

    async def claim(ctx, event, authority):
        seen.append(authority.principal)
        return "Saved." if event.text == "my answer" else None

    app.state.ctx.gateway.register_claim(claim)
    taken = await replay(text="my answer")
    assert taken["decision"] == "claimed"
    assert channel.final[-1][1] == "Saved."
    assert not channel.working and not fake_brain.requests
    passed = await replay(text="another question")
    assert passed["decision"] == "run"
    assert seen == ["person:ada", "person:ada"]


async def test_claim_never_sees_an_unknown_sender(app, replay):
    seen = []

    async def claim(ctx, event, authority):
        seen.append(event)
        return "taken"

    app.state.ctx.gateway.register_claim(claim)
    result = await replay(sender="U_UNKNOWN")
    assert result["decision"] == "ignore" and not seen


async def test_action_router_uses_the_longest_prefix_and_needs_a_person():
    router, calls = ActionRouter(), []

    async def asks(ctx, person, action):
        calls.append(("ask", action["action_id"]))

    async def always(ctx, person, action):
        calls.append(("always", action["action_id"]))

    router.register("ask:", asks)
    router.register("ask:always:", always)
    with pytest.raises(ValueError):
        router.register("ask:", asks)
    assert await router.dispatch(None, {"key": "ada"}, {"action_id": "ask:always:3"})
    assert await router.dispatch(None, {"key": "ada"}, {"action_id": "ask:7:opt:a"})
    assert not await router.dispatch(None, None, {"action_id": "ask:7:opt:a"})
    assert not await router.dispatch(None, {"key": "ada"}, {"action_id": "note:1:keep"})
    assert calls == [("always", "ask:always:3"), ("ask", "ask:7:opt:a")]


async def test_dm_person_opens_a_dm_and_posts(app, slack_client):
    ctx = app.state.ctx
    sent = await dm_person(ctx, "ada", "Hello")
    assert sent and sent["channel"] == "D_EXAMPLE"
    methods = [call["method"] for call in slack_client.calls]
    assert methods == ["conversations_open", "chat_postMessage"]
    assert await dm_person(ctx, "nobody", "Hello") is None
    ctx.slack_client = None
    assert await dm_person(ctx, "ada", "Hello") is None


async def test_preparer_runs_before_claims_and_brain(app, replay, fake_brain):
    seen = []

    async def claim(ctx, event, authority):
        seen.append(event.text)
        return None

    async def prepare(ctx, event, authority):
        assert authority.principal == "person:ada"
        return event.model_copy(update={"text": "Prepared request"})

    app.state.ctx.gateway.register_claim(claim)
    app.state.ctx.gateway.register_preparer(prepare)
    await replay(text="Raw request")
    assert seen == ["Prepared request"]
    assert fake_brain.requests[0].user_message == "Prepared request"
