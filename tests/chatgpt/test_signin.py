"""The DM sign-in uses one state, one owner, and one thread."""

import json
import logging
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest

from illo.channels.slack.adapter import SlackAdapter
from illo.connectors import chatgpt, chatgpt_oauth, oauth
from illo.connectors.store import vault
from illo.db import all_rows, one
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
    state = await chatgpt_oauth.issue(ctx, ada["id"], event)
    mode, response = await chatgpt_oauth.start(ctx, state, paste=paste)
    return state, ada, mode, response


def address(state):
    return (
        chatgpt.REDIRECT_URI + "?" + urlencode({"state": state, "code": "marker-code-never-store"})
    )


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost"])
async def test_link_command_and_paste_exchange(chatgpt_app, replay, fake_brain, host):
    app, backend = chatgpt_app
    backend.device = True
    result = await replay(text="CONNECT MY CHATGPT WITH A LINK")
    assert result["decision"] == "claimed" and not fake_brain.requests
    link = result["text"].splitlines()[1]
    state = parse_qs(urlsplit(link).query)["state"][0]
    assert link.startswith(chatgpt.ISSUER + "/oauth/authorize?")
    assert not backend.requests
    query = parse_qs(urlsplit(link).query)
    assert query["redirect_uri"] == [chatgpt.REDIRECT_URI]
    assert query["scope"] == [chatgpt.SCOPES]
    assert query["code_challenge_method"] == ["S256"]
    assert "verifier" not in query and "offline_access" in query["scope"][0]
    result = await replay(text=address(state).replace("127.0.0.1", host))
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
    result = await replay(
        text="connect my ChatGPT with a link",
        payload={"channel_id": "D_ADA"}, reply_to="thread-a",
    )
    state = parse_qs(urlsplit(result["text"].splitlines()[1]).query)["state"][0]
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


async def test_start_is_one_use(chatgpt_app):
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
    chatgpt_app, replay, slack_client, fake_brain
):
    app, backend = chatgpt_app
    backend.device = True
    app.state.ctx.config.public_url = "http://unreachable.invalid:8080"
    result = await replay(text="connect my ChatGPT")
    assert result["decision"] == "claimed"
    assert result["text"] == (
        "1. Open https://auth.openai.com/codex/device and sign in to your own ChatGPT account.\n"
        "2. Enter this one-time code: ABCD-EFGH\n"
        "The code expires 10 minutes after your request. Illo sends you a DM after sign-in.\n"
        "Use this code only if you asked Illo to connect your ChatGPT.\n"
        "If ChatGPT refuses the code, turn on the device-code sign-in in the ChatGPT "
        'security settings, or write "connect my ChatGPT with a link".'
    )
    assert "state=" not in result["text"] and "unreachable.invalid" not in result["text"]
    assert len(backend.requests) == 1
    assert str(backend.requests[0].url) == chatgpt_oauth.DEVICE_BASE + "/usercode"
    assert "ABCD-EFGH" not in str(fake_brain.requests)
    await chatgpt_oauth.poll_devices(app.state.ctx)
    async with app.state.ctx.pool.connection() as conn:
        assert not await grants.load(conn, 1)
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


async def test_link_command_cancels_device_attempt(chatgpt_app, replay):
    app, backend = chatgpt_app
    backend.device = True
    state, _, mode, _ = await issue_start(app.state.ctx, paste=False)
    assert mode == "device"
    result = await replay(text="connect my ChatGPT with a link")
    url = result["text"].splitlines()[1]
    assert url.startswith(chatgpt.ISSUER + "/oauth/authorize?")
    assert parse_qs(urlsplit(url).query)["state"] != [state]
    before = len(backend.requests)
    await chatgpt_oauth.poll_device(app.state.ctx, oauth.digest(state))
    assert len(backend.requests) == before
    assert (await replay(text=address(state)))["text"] == chatgpt_oauth.FAILED
    new_state = parse_qs(urlsplit(url).query)["state"][0]
    assert (await replay(text=address(new_state)))["text"] == chatgpt.CONNECTED.format(email="ada@example.com")


@pytest.mark.parametrize("failure", ["http", "network", "bad_answer", "bad_interval"])
async def test_device_failure_falls_back_to_paste(chatgpt_app, replay, failure):
    app, backend = chatgpt_app

    async def respond(request):
        if request.url.path.endswith("/deviceauth/usercode"):
            if failure == "network":
                backend.requests.append(request)
                raise httpx.ConnectError("The test endpoint is unavailable", request=request)
            if failure in {"bad_answer", "bad_interval"}:
                backend.requests.append(request)
                return httpx.Response(200, json={
                    "device_auth_id": "fake-device",
                    "user_code": "ABCD-EFGH" if failure == "bad_interval" else None,
                    "interval": "invalid" if failure == "bad_interval" else "5",
                })
        return await backend.respond(request)

    app.state.ctx.chatgpt_client = chatgpt.ChatGPTClient(httpx.MockTransport(respond))
    result = await replay(text="connect my ChatGPT")
    assert result["decision"] == "claimed"
    url = result["text"].splitlines()[1]
    assert url.startswith(chatgpt.ISSUER + "/oauth/authorize?")
    assert "Paste that full address here, in this same DM and thread." in result["text"]
    assert len(backend.requests) == 1
    state = parse_qs(urlsplit(url).query)["state"][0]
    assert (await replay(text=address(state)))["text"] == chatgpt.CONNECTED.format(email="ada@example.com")


async def test_new_device_attempt_cancels_previous(chatgpt_app, replay, slack_client):
    app, backend = chatgpt_app
    ctx = app.state.ctx
    backend.device = True
    await replay(text="connect my ChatGPT")
    async with ctx.pool.connection() as conn:
        first = await one(conn, "SELECT * FROM connector_oauth_states")
    await replay(text="connect my ChatGPT")
    async with ctx.pool.connection() as conn:
        old = await one(conn, "SELECT * FROM connector_oauth_states WHERE state_hash=%s",
                        (first["state_hash"],))
        assert old["cancelled_at"] and old["consumed_at"]
        assert not await grants.state(conn, first["state_hash"])
    backend.pending = False
    before = len(backend.requests)
    await chatgpt_oauth.poll_device(ctx, first["state_hash"])
    assert len(backend.requests) == before
    async with ctx.pool.connection() as conn:
        assert not await grants.load(conn, first["person_id"])
    assert not slack_client.calls
    await chatgpt_oauth.poll_devices(ctx)
    assert len([c for c in slack_client.calls if c.get("text") == chatgpt.CONNECTED.format(email="ada@example.com")]) == 1


async def test_device_expires_after_ten_minutes_once(chatgpt_app, replay, slack_client):
    app, backend = chatgpt_app
    ctx = app.state.ctx
    backend.device = True
    await replay(text="connect my ChatGPT")
    async with ctx.pool.connection() as conn:
        row = await one(conn, "SELECT * FROM connector_oauth_states")
        seconds = (row["expires_at"] - row["started_at"]).total_seconds()
        assert 599 < seconds <= 600
        await conn.execute(
            "UPDATE connector_oauth_states SET started_at=clock_timestamp()-interval '10 minutes', "
            "expires_at=clock_timestamp()-interval '1 second'"
        )
    backend.pending = False
    before = len(backend.requests)
    await chatgpt_oauth.poll_devices(ctx)
    await chatgpt_oauth.poll_devices(ctx)
    assert len(backend.requests) == before
    assert len([c for c in slack_client.calls if c.get("text") == chatgpt_oauth.FAILED]) == 1
    async with ctx.pool.connection() as conn:
        assert not await grants.load(conn, row["person_id"])
        assert not await grants.state(conn, row["state_hash"])
        assert (await one(conn, "SELECT consumed_at FROM connector_oauth_states"))["consumed_at"]


@pytest.mark.parametrize("route", ["start", "paste"])
async def test_chatgpt_web_routes_are_absent(http, route):
    assert (await http.get("/oauth/chatgpt/" + route, params={"state": "example"})).status_code == 404


@pytest.mark.parametrize("flow", ["device", "link", "fallback"])
async def test_claim_secrets_stay_out_of_ledger_and_logs(
    chatgpt_app, caplog, slack_client, fake_brain, flow
):
    app, backend = chatgpt_app
    ctx = app.state.ctx
    backend.device = flow != "fallback"
    command = "connect my ChatGPT" + (" with a link" if flow == "link" else "")
    caplog.set_level(logging.DEBUG)
    logging.getLogger(__name__).info("The sign-in test captures logs")
    event = dm(text=command, payload={"channel_id": "D_ADA"}, reply_to="thread-a")
    result = await ctx.gateway.receive(event, SlackAdapter(ctx))
    assert result["decision"] == "claimed"
    assert any(c.get("text") == result["text"] for c in slack_client.calls)
    secrets = ["ABCD-EFGH", "fake-device", "marker-code-never-store"]
    if flow == "device":
        assert "ABCD-EFGH" in result["text"]
        backend.pending = False
        await chatgpt_oauth.poll_devices(ctx)
    else:
        url = result["text"].splitlines()[1]
        query = parse_qs(urlsplit(url).query)
        secrets.extend([url, query["state"][0], query["code_challenge"][0], "state=", "code_challenge="])
        pasted = address(query["state"][0])
        callback = event.model_copy(update={
            "external_id": "callback", "text": pasted,
            "payload": {"channel_id": "D_ADA", "nested": [{"text": pasted}]},
        })
        assert (await ctx.gateway.receive(callback, SlackAdapter(ctx)))["text"] == chatgpt.CONNECTED.format(email="ada@example.com")
    async with ctx.pool.connection() as conn:
        events = await all_rows(conn, "SELECT * FROM events")
        audit = await all_rows(conn, "SELECT * FROM audit")
        stored = await one(conn, "SELECT payload FROM events WHERE id=%s", (result["event_id"],))
        assert stored["payload"]["text"] == command
        assert "final_answer" not in stored["payload"]
        assert await grants.load(conn, 1)
    assert "The sign-in test captures logs" in caplog.text
    for secret in secrets:
        assert secret not in str(events)
        assert secret not in str(audit)
        assert secret not in caplog.text
    assert not fake_brain.requests


@pytest.mark.parametrize("command", ["connect my ChatGPT", "connect my ChatGPT with a link"])
@pytest.mark.parametrize("changes", [
    {"kind": "channel", "payload": {"channel_id": "C_ACME_TEAM", "mentioned": True}},
    {"payload": {"bot": True}},
    {"payload": {"bot_id": "B_EXAMPLE"}},
    {"payload": {"webhook": True}},
    {"payload": {"form": True}},
    {"payload": {"contains_outside_text": True}},
    {"suffix": " please"},
    {"prefix": "please "},
])
async def test_commands_require_exact_person_dm(chatgpt_app, replay, command, changes):
    app, backend = chatgpt_app
    changes = dict(changes)
    text = changes.pop("prefix", "") + command + changes.pop("suffix", "")
    assert (await replay(text=text, **changes))["decision"] != "claimed"
    assert not backend.requests
    async with app.state.ctx.pool.connection() as conn:
        assert not await one(conn, "SELECT 1 FROM connector_oauth_states")


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


async def test_n2_unverified_email_cannot_connect(chatgpt_app, replay):
    import httpx

    from tests.chatgpt.conftest import jwt, tokens

    app, _ = chatgpt_app
    state, person, _, _ = await issue_start(app.state.ctx)
    data = tokens()
    data["id_token"] = jwt(
        email="ada@example.com", email_verified=False,
        **{"https://api.openai.com/auth": {"chatgpt_account_id": "acme-account"}},
    )
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json=data)

    app.state.ctx.chatgpt_client = chatgpt.ChatGPTClient(httpx.MockTransport(respond))
    assert (await replay(text=address(state)))["text"] == chatgpt_oauth.FAILED
    async with app.state.ctx.pool.connection() as conn:
        assert not await grants.load(conn, person["id"])
    assert any(str(r.url) == chatgpt.REVOCATION_URL for r in requests)
