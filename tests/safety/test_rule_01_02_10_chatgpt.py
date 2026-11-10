"""A person's model grant stays separate from other people and from all recorded text."""

import json

import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr

from illo.brain.client import BrainEvent
from illo.brain.run_manager import provider_auth
from illo.connectors import chatgpt, chatgpt_oauth
from illo.db import all_rows, one
from illo.gateway.principal import Authority
from illo.ledger import chatgpt as grants
from tests.chatgpt.conftest import save_grant


@pytest.fixture
async def subscription_app(app):
    ctx = app.state.ctx
    ctx.config.vault_key = SecretStr(Fernet.generate_key().decode())
    ctx.workspace.config.brain.auth = "chatgpt"
    ctx.workspace.config.brain.runtime = "openai_responses"
    return app


async def test_rule_01_02_person_b_cannot_use_person_a_grant(subscription_app, replay, fake_brain):
    app = subscription_app
    await save_grant(app.state.ctx, "ada", access="ada-only-token")
    result = await replay(sender="U_ACME_BEN")
    assert result["text"] == chatgpt.MISSING and not fake_brain.requests
    ctx = app.state.ctx
    async with ctx.pool.connection() as conn:
        ben = await one(conn, "SELECT id FROM people WHERE key='ben'")
    # Even an invalid authority tuple cannot select a different person's token.
    assert (
        await provider_auth(
            ctx, Authority("person:ada", "person_dm", ben["id"]), "openai_responses"
        )
        is None
    )
    await save_grant(ctx, "ben", access="ben-only-token")
    await replay(sender="U_ACME_BEN")
    assert fake_brain.requests[-1].provider_auth.access_token.get_secret_value() == "ben-only-token"


async def test_rule_01_02_team_only_uses_configured_person(subscription_app):
    ctx = subscription_app.state.ctx
    await save_grant(ctx, "ada", access="ada-only-token")
    await save_grant(ctx, "ben", access="ben-only-token")
    auth = await provider_auth(ctx, Authority("team", "routine"), "openai_responses")
    assert auth is None
    ctx.workspace.config.brain.team_credential = "ben"
    ctx.workspace.config.brain.tainted_credential = "team"
    auth = await provider_auth(ctx, Authority("team", "tainted", tainted=True), "openai_responses")
    assert auth.access_token.get_secret_value() == "ben-only-token"


async def test_rule_02_tokens_never_enter_database_or_logs(subscription_app, replay, caplog):
    ctx = subscription_app.state.ctx
    marker = "fake-token-marker-do-not-record"
    await save_grant(ctx, access=marker)

    class FailingBrain:
        async def run(self, request):
            assert request.provider_auth.access_token.get_secret_value() == marker
            yield BrainEvent(type="text", text=marker)
            yield BrainEvent(
                type="error", category="runtime", code=marker, message=marker, usage={marker: 1}
            )

    ctx.brain_client = FailingBrain()
    await replay()
    async with ctx.pool.connection() as conn:
        for table in ("runs", "events", "audit", "sensors", "connector_grants", "kv"):
            rows = await all_rows(conn, "SELECT row_to_json(t) AS row FROM " + table + " t")
            assert marker not in json.dumps(rows, default=str), table
    assert marker not in caplog.text


async def test_rule_02_signin_codes_filtered_from_every_event_field(
    subscription_app, replay, fake_brain, caplog
):
    ctx = subscription_app.state.ctx
    marker = "fake-callback-code-marker"
    url = chatgpt.REDIRECT_URI + "?state=fake-state&code=" + marker
    result = await replay(text=url, payload={"nested": [{"text": url}]})
    assert result["decision"] == "claimed" and not fake_brain.requests
    async with ctx.pool.connection() as conn:
        assert marker not in str(await all_rows(conn, "SELECT * FROM events"))
    assert marker not in caplog.text


async def test_rule_02_signin_not_claimed_in_team_or_tainted_dm(
    subscription_app, replay, fake_brain
):
    await save_grant(subscription_app.state.ctx)
    url = chatgpt.REDIRECT_URI + "?state=fake-state&code=fake-secret-code"
    for changes in (
        {"kind": "message", "payload": {"channel_id": "C_ACME_TEAM", "mentioned": True}},
        {"payload": {"contains_outside_text": True}},
    ):
        result = await replay(text=url, **changes)
        assert result["decision"] != "claimed"
    assert not any("fake-secret-code" in r.user_message for r in fake_brain.requests)


@pytest.mark.parametrize(
    "profile", ["team_channel", "tainted", "routine", "agent_request", "outsider"]
)
async def test_rule_02_10_signin_tools_only_in_person_dm(app, profile):
    registry = app.state.ctx.registry
    run = {
        "profile": profile,
        "principal": "team",
        "tainted": profile == "tainted",
        "personal_read": False,
    }
    names = {t["name"] for t in registry.list_tools(run)}
    assert not names & {"chatgpt_connect", "chatgpt_status", "chatgpt_disconnect"}


async def test_rule_02_disconnect_invalidates_pending_signin(subscription_app):
    ctx = subscription_app.state.ctx
    from illo.connectors.oauth import digest
    from illo.gateway import InboundEvent

    event = InboundEvent(channel="slack", external_id="a", kind="dm")
    async with ctx.pool.connection() as conn:
        person = await one(conn, "SELECT id FROM people WHERE key='ada'")
    state = await chatgpt_oauth.issue(ctx, person["id"], event)
    async with ctx.pool.connection() as conn:
        await grants.disconnect(conn, person["id"])
    async with ctx.pool.connection() as conn:
        assert not await grants.state(conn, digest(state))
