"""The DM sign-in uses one state, one owner, and one thread."""

import json
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest

from illo.connectors import chatgpt, chatgpt_oauth, oauth
from illo.connectors.store import vault
from illo.db import one
from illo.gateway import InboundEvent
from illo.ledger import chatgpt as grants
from illo.ledger import people


def dm(**kwargs):
    return InboundEvent(
        channel="slack", external_id="signin", kind="dm", sender="U_ACME_ADA", **kwargs
    )


async def issue_start(ctx, event=None, *, paste=True):
    event = event or dm()
    async with ctx.pool.connection() as conn:
        ada = await people.get(conn, "ada")
    link = await chatgpt_oauth.issue(ctx, ada["id"], event)
    state = parse_qs(urlsplit(link).query)["state"][0]
    mode, response = await chatgpt_oauth.start(ctx, state, paste=paste)
    return state, ada, mode, response


def address(state):
    return (
        chatgpt.REDIRECT_URI + "?" + urlencode({"state": state, "code": "marker-code-never-store"})
    )


async def test_connect_without_model_and_paste_exchange(chatgpt_app, replay, fake_brain):
    app, backend = chatgpt_app
    result = await replay(text="connect my ChatGPT")
    assert result["decision"] == "claimed" and not fake_brain.requests
    link = result["text"].splitlines()[1]
    state = parse_qs(urlsplit(link).query)["state"][0]
    mode, url = await chatgpt_oauth.start(app.state.ctx, state)
    assert mode == "paste"
    query = parse_qs(urlsplit(url).query)
    assert query["redirect_uri"] == [chatgpt.REDIRECT_URI]
    assert query["scope"] == [chatgpt.SCOPES]
    assert query["code_challenge_method"] == ["S256"]
    assert "verifier" not in query and "offline_access" in query["scope"][0]
    result = await replay(text=address(state))
    assert result["text"] == chatgpt.CONNECTED.format(email="ada@example.com")
    assert backend.requests[-1].headers["content-type"] == "application/x-www-form-urlencoded"
    body = parse_qs(backend.requests[-1].content.decode())
    assert body["code"] == ["marker-code-never-store"]
    assert body["redirect_uri"] == [chatgpt.REDIRECT_URI]
    assert len(body["code_verifier"][0]) >= 43
    assert not fake_brain.requests
    async with app.state.ctx.pool.connection() as conn:
        row = await grants.load(conn, 1)
        secret = json.loads(vault(app.state.ctx).decrypt(row["secret"]))
        assert secret["account_id"] == "acme-account" and secret["plan"] == "plus"


async def test_wrong_owner_and_thread_do_not_consume(chatgpt_app, replay, fake_brain):
    app, _ = chatgpt_app
    state, _, _, _ = await issue_start(
        app.state.ctx, dm(payload={"channel_id": "D_ADA"}, reply_to="thread-a")
    )
    for changes in (
        {"sender": "U_ACME_BEN"},
        {"reply_to": "thread-b"},
        {"payload": {"channel_id": "D_OTHER"}},
    ):
        args = {"payload": {"channel_id": "D_ADA"}, "reply_to": "thread-a", **changes}
        assert (await replay(text=address(state), **args))["text"] == chatgpt_oauth.FAILED
    assert not fake_brain.requests
    assert (
        await replay(text=address(state), payload={"channel_id": "D_ADA"}, reply_to="thread-a")
    )["text"] == chatgpt.CONNECTED.format(email="ada@example.com")


@pytest.mark.parametrize("failure", ["expired", "reused", "tampered", "unstarted"])
async def test_invalid_state(chatgpt_app, replay, failure):
    app, _ = chatgpt_app
    ctx = app.state.ctx
    state, _, _, _ = await issue_start(ctx)
    if failure == "expired":
        async with ctx.pool.connection() as conn:
            await conn.execute(
                "UPDATE connector_oauth_states SET expires_at=clock_timestamp()-interval '1 second'"
            )
    elif failure == "reused":
        assert (await replay(text=address(state)))["text"] == chatgpt.CONNECTED.format(email="ada@example.com")
    elif failure == "tampered":
        state += "x"
    else:
        async with ctx.pool.connection() as conn:
            await conn.execute("UPDATE connector_oauth_states SET started_at=NULL")
    assert (await replay(text=address(state)))["text"] == chatgpt_oauth.FAILED


async def test_start_link_is_one_use(chatgpt_app):
    app, _ = chatgpt_app
    state, _, _, _ = await issue_start(app.state.ctx)
    with pytest.raises(ValueError):
        await chatgpt_oauth.start(app.state.ctx, state)


@pytest.mark.parametrize(
    "text",
    [
        "{url} https://example.com/",
        "Explain {url}",
        "<{url}> <https://example.com/>",
    ],
)
async def test_two_urls_or_extra_text_are_filtered(chatgpt_app, replay, fake_brain, text):
    app, _ = chatgpt_app
    from tests.chatgpt.conftest import save_grant

    await save_grant(app.state.ctx)
    state, _, _, _ = await issue_start(app.state.ctx)
    pasted = text.format(url=address(state))
    result = await replay(text=pasted, payload={"nested": [{"text": pasted}]})
    assert result["decision"] == "run" and fake_brain.requests
    assert "marker-code-never-store" not in fake_brain.requests[-1].user_message
    async with app.state.ctx.pool.connection() as conn:
        assert await grants.state(conn, oauth.digest(state))


async def test_device_preferred_and_completed_by_timer(
    chatgpt_app, replay, http, slack_client, fake_brain
):
    app, backend = chatgpt_app
    backend.device = True
    result = await replay(text="connect my ChatGPT")
    link = result["text"].splitlines()[1]
    page = await http.get(link)
    assert page.status_code == 200
    assert page.headers["cache-control"] == "no-store"
    assert "ABCD-EFGH" in page.text and "https://auth.openai.com/codex/device" in page.text
    assert "ABCD-EFGH" not in str(fake_brain.requests)
    await chatgpt_oauth.poll_devices(app.state.ctx)
    async with app.state.ctx.pool.connection() as conn:
        assert not await grants.load(conn, 1)
        await conn.execute("UPDATE connector_oauth_states SET secret=secret")
        row = await one(conn, "SELECT * FROM connector_oauth_states")
        details = json.loads(vault(app.state.ctx).decrypt(row["secret"]))
        details["next_poll"] = 0
        await conn.execute(
            "UPDATE connector_oauth_states SET secret=%s",
            (vault(app.state.ctx).encrypt(json.dumps(details)),),
        )
    backend.pending = False
    await chatgpt_oauth.poll_devices(app.state.ctx)
    await chatgpt_oauth.poll_devices(app.state.ctx)
    assert len([c for c in slack_client.calls if c.get("text") == chatgpt.CONNECTED.format(email="ada@example.com")]) == 1
    body = parse_qs(backend.requests[-1].content.decode())
    assert body["redirect_uri"] == [chatgpt.ISSUER + "/deviceauth/callback"]
    assert body["code_verifier"] == ["fake-device-verifier"]


async def test_device_can_switch_to_paste_once(chatgpt_app, replay):
    app, backend = chatgpt_app
    backend.device = True
    state, _, mode, _ = await issue_start(app.state.ctx, paste=False)
    assert mode == "device"
    url = await chatgpt_oauth.switch_to_paste(app.state.ctx, state)
    assert url.startswith(chatgpt.ISSUER + "/oauth/authorize?")
    with pytest.raises(ValueError):
        await chatgpt_oauth.switch_to_paste(app.state.ctx, state)
    assert (await replay(text=address(state)))["text"] == chatgpt.CONNECTED.format(email="ada@example.com")


async def test_disconnect_cancels_inflight_exchange(chatgpt_app, replay):
    app, backend = chatgpt_app
    ctx = app.state.ctx
    state, ada, _, _ = await issue_start(ctx)

    async def disconnect():
        async with ctx.pool.connection() as conn:
            await grants.disconnect(conn, ada["id"])

    backend.on_exchange = disconnect
    assert (await replay(text=address(state)))["text"] == chatgpt_oauth.FAILED
    async with ctx.pool.connection() as conn:
        assert not await grants.load(conn, ada["id"])


async def test_status_and_disconnect(chatgpt_app, replay):
    app, _ = chatgpt_app
    from tests.chatgpt.conftest import save_grant

    await save_grant(app.state.ctx)
    assert "connected" in (await replay(text="ChatGPT status"))["text"]
    assert (await replay(text="disconnect my ChatGPT"))[
        "text"
    ] == "ChatGPT is disconnected from Illo."
    assert (await replay(text="ChatGPT status"))["text"] == "ChatGPT is not connected."


async def test_s3_channel_text_keeps_literal_slack_entities(app, replay, fake_brain):
    text = "please quote: &lt;!channel&gt; and &lt;@U999&gt; &amp; more"
    result = await replay(
        kind="channel", text=text, payload={"channel_id": "C_ACME_TEAM", "mentioned": True}
    )
    assert result["decision"] == "run"
    assert fake_brain.requests[-1].user_message == text
    async with app.state.ctx.pool.connection() as conn:
        row = await one(conn, "SELECT payload FROM events WHERE id=%s", (result["event_id"],))
    assert row["payload"]["text"] == text
