"""MCP browser tests use fake servers and the real state and grant ledger."""

import base64
import hashlib
import json
import logging
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest

from illo.connectors import mcp_oauth, oauth, store
from illo.connectors.config import Connector
from illo.connectors.http import BoundedHTTP
from illo.connectors.mcp_client import SignInEnded
from illo.db import all_rows, one
from illo.ledger import runs
from illo.tools.registry import ToolContext
from tests.connectors.test_chat import (
    approve,
    connect_ctx,  # noqa: F401
    fake_mcp,
    finish,
    person,
    propose,
    submission,
)

RESOURCE = "https://mcp.example.com/mcp"
ISSUER = "https://auth.example.com/tenant"
ACCESS = "test-private-access-value"
REFRESH = "test-private-refresh-value"
CLIENT_SECRET = "test-private-client-value"
CODE = "test-private-code-value"


@pytest.fixture
async def server(connect_ctx, monkeypatch):  # noqa: F811
    ctx = connect_ctx
    ctx.config.public_url = "https://illo.example.com"
    fake_mcp(ctx)
    seen = []
    settings = {"registration": True, "token_error": None, "refresh_error": None}
    monkeypatch.setattr(
        "illo.connectors.addresses.resolve_public", AsyncMock(return_value=["93.184.216.34"])
    )

    def respond(request):
        seen.append(request)
        path = request.url.path
        if path == "/mcp":
            return httpx.Response(
                401,
                headers={
                    "WWW-Authenticate": 'Bearer resource_metadata="https://mcp.example.com/resource-info"'
                },
            )
        if path == "/resource-info" or "oauth-protected-resource" in path:
            return httpx.Response(
                200,
                json={
                    "resource": RESOURCE,
                    "authorization_servers": [ISSUER],
                    "scopes_supported": ["read", "write"],
                },
            )
        if "oauth-authorization-server" in path:
            info = {
                "issuer": ISSUER,
                "authorization_endpoint": ISSUER + "/authorize",
                "token_endpoint": ISSUER + "/token",
                "token_endpoint_auth_methods_supported": ["client_secret_post", "none"],
            }
            if settings["registration"]:
                info["registration_endpoint"] = ISSUER + "/register"
            return httpx.Response(200, json=info)
        if path.endswith("/register"):
            data = json.loads(request.content)
            assert data == {
                "client_name": "Illo",
                "redirect_uris": [mcp_oauth.callback_url(ctx)],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
            }
            return httpx.Response(
                201, json={"client_id": "acme-illo", "client_secret": CLIENT_SECRET}
            )
        assert path.endswith("/token")
        data = parse_qs(request.content.decode())
        assert data["resource"] == [RESOURCE]
        error = (
            settings["refresh_error"]
            if data["grant_type"] == ["refresh_token"]
            else settings["token_error"]
        )
        if error:
            return httpx.Response(400, json={"error": error, "error_description": CLIENT_SECRET})
        return httpx.Response(
            200,
            json={
                "access_token": ACCESS,
                "refresh_token": REFRESH,
                "expires_in": 3600,
                "scope": "read write",
            },
        )

    ctx.mcp_client.http = BoundedHTTP(httpx.MockTransport(respond), public=True)
    return ctx, seen, settings


def signin_link(ctx):
    for call in reversed(ctx.slack_client.calls):
        if call["method"] == "chat_postMessage" and call.get("blocks"):
            for block in call["blocks"]:
                for element in block.get("elements", []):
                    if element.get("url"):
                        return element["url"]
    raise AssertionError("No sign-in link")


def browser(ctx):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=ctx.app), base_url="https://illo.example.com"
    )


async def begin(ctx):
    draft = await propose(ctx, auth="bearer")
    assert draft["content"]["arguments"]["auth"] == "oauth"
    await approve(ctx, draft)
    return draft, signin_link(ctx)


async def finish_browser(ctx, link):
    async with browser(ctx) as client:
        start = await client.get(link)
        assert start.status_code == 307
        state = parse_qs(urlsplit(link).query)["state"][0]
        return await client.get(mcp_oauth.callback_url(ctx), params={"state": state, "code": CODE})


async def test_full_browser_flow_add_discovery_registration_pkce_allow(server, caplog, fake_brain):
    ctx, seen, _ = server
    caplog.set_level(logging.DEBUG)
    draft, link = await begin(ctx)
    assert "Sign-in: in your browser, at `auth.example.com`." in ctx.slack_client.calls[1]["text"]
    assert [r.url.path for r in seen[:3]] == [
        "/mcp",
        "/resource-info",
        "/.well-known/oauth-authorization-server/tenant",
    ]
    assert not (ctx.workspace.root / "connectors/acme-crm.yaml").exists()
    async with browser(ctx) as client:
        response = await client.get(link)
        assert response.status_code == 307
        assert (
            "Secure" in response.headers["set-cookie"]
            and "HttpOnly" in response.headers["set-cookie"]
        )
        assert "Domain=" not in response.headers["set-cookie"]
        params = parse_qs(urlsplit(response.headers["location"]).query)
        assert params["resource"] == [RESOURCE]
        assert params["redirect_uri"] == [mcp_oauth.callback_url(ctx)]
        assert params["scope"] == ["read write"]
        assert params["code_challenge_method"] == ["S256"]
        state = params["state"][0]
        response = await client.get(
            mcp_oauth.callback_url(ctx), params={"state": state, "code": CODE}
        )
        assert response.status_code == 200
        assert response.text == "acme-crm is connected. Go back to Slack."
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["referrer-policy"] == "no-referrer"
        repeated = await client.get(
            mcp_oauth.callback_url(ctx), params={"state": state, "code": CODE}
        )
        assert repeated.status_code == 400
    token_request = next(r for r in seen if r.url.path.endswith("/token"))
    form = parse_qs(token_request.content.decode())
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(form["code_verifier"][0].encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    assert params["code_challenge"] == [challenge]
    cfg = next(c for c in ctx.workspace.connectors if c.key == "acme-crm")
    assert cfg.tools == []
    assert set(cfg.discovered_tools) == {"search", "edit"}
    assert cfg.auth.client_id == "acme-illo" and cfg.auth.scopes == ["read", "write"]
    async with ctx.pool.connection() as conn:
        row, token = await store.load(conn, ctx, cfg.key, None)
        assert token["access_token"] == ACCESS and token["refresh_token"] == REFRESH
        assert ACCESS.encode() not in row["secret"]
        assert await store.vault(ctx).get(conn, cfg.auth.client_secret) == CLIENT_SECRET
        values = []
        for table in (
            "events",
            "audit",
            "drafts",
            "kv",
            "sensors",
            "secrets",
            "connector_oauth_states",
        ):
            values.extend(await all_rows(conn, f"SELECT * FROM {table}"))
    for private in (ACCESS, REFRESH, CLIENT_SECRET, CODE, state, form["code_verifier"][0]):
        assert private not in str(values) + caplog.text + str(fake_brain.requests)
        assert private not in (ctx.workspace.root / "connectors/acme-crm.yaml").read_text()
    allowed = await propose(
        ctx, kind="connect_allow_tools", tools=[{"name": "search", "risk": "read"}]
    )
    await approve(ctx, allowed)
    assert next(c for c in ctx.workspace.connectors if c.key == cfg.key).tools[0].name == "search"
    assert draft["content"]["arguments"].keys() == {
        "key",
        "url",
        "auth",
        "auth_host",
        "description",
    }


@pytest.mark.parametrize("secret", ["", CLIENT_SECRET])
async def test_manual_client_form_and_optional_secret(server, secret):
    ctx, seen, settings = server
    settings["registration"] = False
    draft = await propose(ctx)
    await approve(ctx, draft)
    from illo.channels.slack import actions

    await actions.dispatch(
        ctx,
        {
            "user": {"id": "U_ACME_ADA"},
            "channel": {"id": "D_EXAMPLE"},
            "trigger_id": "trigger",
            "actions": [{"action_id": f"connect:mcp:client:{draft['id']}"}],
        },
    )
    view = next(c["view"] for c in reversed(ctx.slack_client.calls) if c["method"] == "views_open")
    assert next(b for b in view["blocks"] if b["block_id"] == "secret")["optional"]
    assert (await finish(ctx, submission(view, client_id="acme-illo", secret=secret)))[
        "response_action"
    ] == "clear"
    response = await finish_browser(ctx, signin_link(ctx))
    assert response.status_code == 200
    data = parse_qs(seen[-1].content.decode())
    assert data.get("client_secret", [""]) == [secret]
    assert not any(r.url.path.endswith("/register") for r in seen)


@pytest.mark.parametrize("failure", ["token", "catalog", "catalog_secret", "scope", "expiry"])
async def test_failed_signin_saves_no_connector_grant_or_key(server, failure):
    ctx, _, settings = server
    _, link = await begin(ctx)
    if failure == "token":
        settings["token_error"] = "invalid_grant"
    elif failure.startswith("catalog"):
        http = ctx.mcp_client.http
        fake_mcp(
            ctx, fail=failure == "catalog", echo=ACCESS if failure == "catalog_secret" else None
        )
        ctx.mcp_client.http = http
    else:
        old = ctx.mcp_client.http.request

        async def changed(method, url, **kwargs):
            token = await old(method, url, **kwargs)
            if url.endswith("/token"):
                token.update({"scope": "admin"} if failure == "scope" else {"expires_in": "bad"})
            return token

        ctx.mcp_client.http.request = changed
    response = await finish_browser(ctx, link)
    assert response.status_code == 400 and response.text == mcp_oauth.FAILURE
    assert response.headers["cache-control"] == "no-store"
    assert not (ctx.workspace.root / "connectors/acme-crm.yaml").exists()
    async with ctx.pool.connection() as conn:
        assert not await all_rows(conn, "SELECT * FROM connector_grants")
        assert not await all_rows(conn, "SELECT * FROM secrets")


async def test_state_cannot_cross_provider_browser_or_connector(server):
    ctx, _, _ = server
    _, link = await begin(ctx)
    state = parse_qs(urlsplit(link).query)["state"][0]
    with pytest.raises(ValueError):
        await oauth.start(ctx, state)
    async with browser(ctx) as client:
        assert (await client.get(link)).status_code == 307
        assert (await client.get(link)).status_code == 400
    async with browser(ctx) as stranger:
        response = await stranger.get(
            mcp_oauth.callback_url(ctx), params={"state": state, "code": CODE}
        )
        assert response.status_code == 400
    async with ctx.pool.connection() as conn:
        await conn.execute("UPDATE connector_oauth_states SET connector_key='another'")
    with pytest.raises(ValueError):
        await mcp_oauth.complete(ctx, state, CODE, browser_nonce="wrong")
    assert not (ctx.workspace.root / "connectors/acme-crm.yaml").exists()


@pytest.mark.parametrize("change", ["expire", "inactive", "not_owner", "tamper", "before_start"])
async def test_state_rejects_expiry_inactive_person_role_change_tamper_and_missing_start(
    server, change
):
    ctx, seen, _ = server
    _, link = await begin(ctx)
    state = parse_qs(urlsplit(link).query)["state"][0]
    async with ctx.pool.connection() as conn:
        if change == "expire":
            await conn.execute(
                "UPDATE connector_oauth_states SET expires_at=clock_timestamp()-interval '1 second'"
            )
        elif change == "inactive":
            await conn.execute("UPDATE people SET active=false WHERE key='ada'")
        elif change == "not_owner":
            await conn.execute("UPDATE people SET role='member' WHERE key='ada'")
    if change == "tamper":
        state += "x"
    count = len(seen)
    async with browser(ctx) as client:
        if change != "before_start":
            response = await client.get("/oauth/mcp/start", params={"state": state})
            assert response.status_code == 400
        response = await client.get(
            mcp_oauth.callback_url(ctx), params={"state": state, "code": CODE}
        )
        assert response.status_code == 400
    assert len(seen) == count


@pytest.mark.parametrize("kind", ["invalid_grant", "no_refresh"])
async def test_ended_grant_refusal_sensor_one_dm_and_reconnect_preserves_allow(
    server, replay, fake_brain, kind
):
    ctx, seen, settings = server
    _, link = await begin(ctx)
    assert (await finish_browser(ctx, link)).status_code == 200
    allowed = await propose(
        ctx, kind="connect_allow_tools", tools=[{"name": "search", "risk": "read"}]
    )
    await approve(ctx, allowed)
    cfg = next(c for c in ctx.workspace.connectors if c.key == "acme-crm")
    owner = await person(ctx)
    async with ctx.pool.connection() as conn:
        if kind == "no_refresh":
            row, token = await store.load(conn, ctx, cfg.key, None)
            token.pop("refresh_token")
            await store.save(conn, ctx, cfg.key, None, token, row["scopes"])
        await conn.execute(
            "UPDATE connector_grants SET expires_at=clock_timestamp()-interval '1 second'"
        )
        run = await runs.create(
            conn,
            principal="person:ada",
            person_id=owner["id"],
            profile="person_dm",
            runtime="fake",
            model="fake",
        )
    settings["refresh_error"] = "invalid_grant"
    before = len(ctx.slack_client.calls)
    args = {"connector": cfg.key, "tool": "search", "arguments": {}}
    for _ in range(2):
        async with ctx.pool.connection() as conn:
            result = await ctx.registry.call(ToolContext(conn, run, ctx), "connectors_call", args)
        assert result["decision"] == "deny"
        assert "The sign-in to acme-crm ended. Ada must sign in again." in result["reason"]
    notices = [c for c in ctx.slack_client.calls[before:] if c["method"] == "chat_postMessage"]
    assert len(notices) == 1
    assert notices[0]["text"] == "Your sign-in to acme-crm ended. Sign in again to continue."
    assert signin_link(ctx)
    async with ctx.pool.connection() as conn:
        grant = await one(conn, "SELECT * FROM connector_grants")
        assert grant["broken_at"] and grant["notified_at"]
        sensor = await one(conn, "SELECT * FROM sensors WHERE key=%s", ("connector:" + cfg.key,))
        assert sensor["last_error_at"] > sensor["last_ok_at"]
    if kind == "no_refresh":
        assert not any(
            parse_qs(r.content.decode()).get("grant_type") == ["refresh_token"]
            for r in seen
            if r.url.path.endswith("/token")
        )
    for phrase in ("connect acme-crm again", "sign in to acme-crm"):
        result = await replay(text=phrase)
        assert result["decision"] == "claimed" and not fake_brain.requests
        assert (await finish_browser(ctx, signin_link(ctx))).status_code == 200
        cfg = next(c for c in ctx.workspace.connectors if c.key == "acme-crm")
        assert [t.name for t in cfg.tools] == ["search"]
    async with ctx.pool.connection() as conn:
        grant = await one(conn, "SELECT * FROM connector_grants")
        assert not grant["broken_at"] and not grant["notified_at"]


async def test_transient_refresh_error_does_not_end_grant(server):
    ctx, _, settings = server
    _, link = await begin(ctx)
    await finish_browser(ctx, link)
    cfg = next(c for c in ctx.workspace.connectors if c.key == "acme-crm")
    settings["refresh_error"] = "temporarily_unavailable"
    async with ctx.pool.connection() as conn:
        await conn.execute(
            "UPDATE connector_grants SET expires_at=clock_timestamp()-interval '1 second'"
        )
        with pytest.raises(ValueError) as error:
            await ctx.mcp_client.bearer(ctx, conn, cfg)
        assert not isinstance(error.value, SignInEnded)
        row = await one(conn, "SELECT * FROM connector_grants")
        assert not row["broken_at"] and not row["notified_at"]


@pytest.mark.parametrize(
    "url", ["http://localhost:8000", "https://127.0.0.1", "http://illo.example.com"]
)
async def test_no_public_address_gives_host_fallback(server, url):
    ctx, seen, _ = server
    ctx.config.public_url = url
    draft = await propose(ctx)
    await approve(ctx, draft)
    assert any(c.get("text") == mcp_oauth.fallback("acme-crm") for c in ctx.slack_client.calls)
    assert not any(r.url.path.endswith("/register") for r in seen)
    async with ctx.pool.connection() as conn:
        assert not await all_rows(conn, "SELECT * FROM connector_oauth_states")


async def test_non_owner_cannot_add_or_sign_in_team(server, replay):
    ctx, seen, _ = server
    assert (await propose(ctx, who="ben"))["decision"] == "deny"
    assert not seen
    _, link = await begin(ctx)
    await finish_browser(ctx, link)
    before = len(ctx.slack_client.calls)
    result = await replay(sender="U_ACME_BEN", text="sign in to acme-crm")
    assert result["decision"] == "claimed" and result["text"] == mcp_oauth.FAILURE
    assert len(ctx.slack_client.calls) == before


async def test_pasted_urls_and_oauth_logs_hide_query_strings(server, replay, fake_brain, caplog):
    ctx, _, _ = server
    _, link = await begin(ctx)
    state = parse_qs(urlsplit(link).query)["state"][0]
    url = mcp_oauth.callback_url(ctx) + "?" + urlencode({"state": state, "code": CODE})
    await replay(text="Help with " + url, payload={"nested": [link, url]})
    async with ctx.pool.connection() as conn:
        ledger = str(await all_rows(conn, "SELECT * FROM events"))
    assert state not in ledger + str(fake_brain.requests)
    assert CODE not in ledger + str(fake_brain.requests)
    caplog.set_level(logging.INFO)
    logging.getLogger("uvicorn.access").info("GET /oauth/mcp/start?state=%s", state)
    logging.getLogger("httpx").info("GET /oauth/mcp/callback?code=%s", CODE)
    assert state not in caplog.text and CODE not in caplog.text


async def test_resignin_rejects_changed_permissions(server):
    ctx, _, _ = server
    _, link = await begin(ctx)
    await finish_browser(ctx, link)
    cfg = next(c for c in ctx.workspace.connectors if c.key == "acme-crm")
    async with ctx.pool.connection() as conn:
        await mcp_oauth.reconnect(ctx, conn, await person(ctx), cfg)
    link = signin_link(ctx)
    allowed = await propose(
        ctx, kind="connect_allow_tools", tools=[{"name": "search", "risk": "read"}]
    )
    await approve(ctx, allowed)
    assert (await finish_browser(ctx, link)).status_code == 400
    assert [
        t.name for t in next(c for c in ctx.workspace.connectors if c.key == cfg.key).tools
    ] == ["search"]


async def test_concurrent_callbacks_exchange_one_code(server):
    import asyncio

    ctx, seen, _ = server
    _, link = await begin(ctx)
    async with browser(ctx) as client:
        await client.get(link)
        state = parse_qs(urlsplit(link).query)["state"][0]
        replies = await asyncio.gather(
            *[
                client.get(mcp_oauth.callback_url(ctx), params={"state": state, "code": CODE})
                for _ in range(2)
            ]
        )
    assert sorted(r.status_code for r in replies) == [200, 400]
    assert len([r for r in seen if r.url.path.endswith("/token")]) == 1


async def test_failed_code_exchange_consumes_state_before_retry(server):
    ctx, seen, settings = server
    settings["token_error"] = "invalid_grant"
    _, link = await begin(ctx)
    async with browser(ctx) as client:
        await client.get(link)
        state = parse_qs(urlsplit(link).query)["state"][0]
        for _ in range(2):
            response = await client.get(
                mcp_oauth.callback_url(ctx), params={"state": state, "code": CODE}
            )
            assert response.status_code == 400
    assert len([r for r in seen if r.url.path.endswith("/token")]) == 1
    async with ctx.pool.connection() as conn:
        row = await one(conn, "SELECT * FROM connector_oauth_states")
        assert row["consumed_at"]


@pytest.mark.parametrize(
    "query",
    [
        {"state": "one", "error": "access_denied"},
        [("state", "one"), ("state", "two"), ("code", CODE)],
        {"state": "one"},
        {"state": "one", "code": ""},
    ],
)
async def test_bad_callback_returns_private_failure_page(server, query):
    ctx, seen, _ = server
    async with browser(ctx) as client:
        response = await client.get(mcp_oauth.callback_url(ctx), params=query)
    assert response.status_code == 400 and response.text == mcp_oauth.FAILURE
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert not seen


async def test_state_rejects_connector_change_with_the_right_browser(server):
    ctx, seen, _ = server
    _, link = await begin(ctx)
    async with browser(ctx) as client:
        await client.get(link)
        async with ctx.pool.connection() as conn:
            await conn.execute("UPDATE connector_oauth_states SET connector_key='acme-other'")
        state = parse_qs(urlsplit(link).query)["state"][0]
        response = await client.get(
            mcp_oauth.callback_url(ctx), params={"state": state, "code": CODE}
        )
    assert response.status_code == 400
    assert not any(r.url.path.endswith("/token") for r in seen)


async def test_callback_checks_owner_again_after_the_network_call(server):
    ctx, _, _ = server
    _, link = await begin(ctx)
    old = ctx.mcp_client.discover

    async def demote(*args, **kwargs):
        tools = await old(*args, **kwargs)
        async with ctx.pool.connection() as conn:
            await conn.execute("UPDATE people SET role='member' WHERE key='ada'")
        return tools

    ctx.mcp_client.discover = demote
    assert (await finish_browser(ctx, link)).status_code == 400
    async with ctx.pool.connection() as conn:
        assert not await all_rows(conn, "SELECT * FROM connector_grants")
        assert not await all_rows(conn, "SELECT * FROM secrets")
    assert not (ctx.workspace.root / "connectors/acme-crm.yaml").exists()


async def test_configured_scopes_narrow_the_resource_scopes(server):
    ctx, _, _ = server
    config = Connector(
        key="acme-docs",
        kind="mcp",
        scope="team",
        url=RESOURCE,
        auth={"type": "oauth", "client_id": "acme-illo", "scopes": ["read"]},
    )
    info = {
        "authorization_endpoint": ISSUER + "/authorize",
        "token_endpoint": ISSUER + "/token",
        "resource": RESOURCE,
        "scopes_supported": ["read", "write"],
    }
    async with ctx.pool.connection() as conn:
        await mcp_oauth.issue(ctx, conn, await person(ctx), config, info, create=True)
    async with browser(ctx) as client:
        response = await client.get(signin_link(ctx))
    assert parse_qs(urlsplit(response.headers["location"]).query)["scope"] == ["read"]
    assert config.auth.scopes == ["read"]


async def test_metadata_and_registration_cannot_set_permissions_or_redirect(server):
    ctx, _, _ = server
    old = ctx.mcp_client.http.request

    async def changed(method, url, **kwargs):
        result = await old(method, url, **kwargs)
        if isinstance(result, dict):
            result.update(
                redirect_uri="https://evil.example.com/callback",
                redirect_uris=["https://evil.example.com/callback"],
                tools={"edit": {"risk": "read"}},
                allowed_tools=["edit"],
            )
        return result

    ctx.mcp_client.http.request = changed
    _, link = await begin(ctx)
    assert (await finish_browser(ctx, link)).status_code == 200
    config = next(c for c in ctx.workspace.connectors if c.key == "acme-crm")
    assert config.tools == [] and config.auth.redirect_uri == mcp_oauth.callback_url(ctx)


async def test_cached_catalog_cannot_hide_an_ended_signin(server):
    ctx, _, settings = server
    _, link = await begin(ctx)
    await finish_browser(ctx, link)
    config = next(c for c in ctx.workspace.connectors if c.key == "acme-crm")
    ctx.mcp_client.schemas[config.model_dump_json()] = {"search": {}}
    settings["refresh_error"] = "invalid_grant"
    async with ctx.pool.connection() as conn:
        await conn.execute(
            "UPDATE connector_grants SET expires_at=clock_timestamp()-interval '1 second'"
        )
        with pytest.raises(SignInEnded):
            await ctx.mcp_client.catalog(ctx, conn, config)
        row = await one(conn, "SELECT * FROM connector_grants")
        assert row["broken_at"]


async def test_notice_retries_after_slack_failure_and_then_stops(server):
    ctx, _, _ = server
    _, link = await begin(ctx)
    await finish_browser(ctx, link)
    async with ctx.pool.connection() as conn:
        await conn.execute("UPDATE connector_grants SET broken_at=clock_timestamp()")
    old = ctx.slack_client.chat_postMessage
    ctx.slack_client.chat_postMessage = AsyncMock(side_effect=RuntimeError("Slack is unavailable"))
    with pytest.raises(RuntimeError):
        async with ctx.pool.connection() as conn:
            await mcp_oauth.notify_ended(ctx, conn)
    async with ctx.pool.connection() as conn:
        row = await one(conn, "SELECT * FROM connector_grants")
        assert not row["notified_at"]
    first_attempt = ctx.slack_client.chat_postMessage.call_args.kwargs
    ctx.slack_client.chat_postMessage = old
    before = len(ctx.slack_client.calls)
    for _ in range(2):
        async with ctx.pool.connection() as conn:
            await mcp_oauth.notify_ended(ctx, conn)
    notices = [c for c in ctx.slack_client.calls[before:] if c["method"] == "chat_postMessage"]
    assert len(notices) == 1
    assert notices[0]["client_msg_id"] == first_attempt["client_msg_id"]


@pytest.mark.parametrize("key", ["google", "chatgpt"])
async def test_builtin_connector_names_cannot_start_an_mcp_flow(server, key):
    ctx, seen, _ = server
    result = await propose(ctx, key=key)
    assert result["decision"] == "deny" and not seen


@pytest.mark.parametrize("private", [ACCESS, REFRESH, CLIENT_SECRET])
@pytest.mark.parametrize("surface", ["schema", "result"])
async def test_remote_tools_cannot_return_credentials_to_the_brain(server, private, surface):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    from illo.connectors.config import ConnectorTool
    from tests.fakes import FakeMCPSession

    ctx, _, _ = server
    _, link = await begin(ctx)
    await finish_browser(ctx, link)
    config = next(c for c in ctx.workspace.connectors if c.key == "acme-crm")
    config = config.model_copy(update={"tools": [ConnectorTool(name="search", risk="read")]})
    session = FakeMCPSession(tools=["search"], text=private)
    if surface == "schema":
        session.list_tools = AsyncMock(
            return_value=SimpleNamespace(
                tools=[
                    SimpleNamespace(
                        name="search", inputSchema={"type": "object", "description": private}
                    )
                ],
                nextCursor=None,
            )
        )

    @asynccontextmanager
    async def factory(config, bearer):
        yield session

    ctx.mcp_client.session_factory = factory
    async with ctx.pool.connection() as conn:
        with pytest.raises(ValueError):
            if surface == "schema":
                await ctx.mcp_client.catalog(ctx, conn, config)
            else:
                await ctx.mcp_client.call(ctx, conn, config, "search", {})


async def test_dynamic_registration_supports_a_public_client_without_a_secret(server):
    ctx, seen, _ = server
    old = ctx.mcp_client.http.request

    async def public_client(method, url, **kwargs):
        result = await old(method, url, **kwargs)
        if url.endswith("/register"):
            result.pop("client_secret")
        return result

    ctx.mcp_client.http.request = public_client
    _, link = await begin(ctx)
    assert (await finish_browser(ctx, link)).status_code == 200
    config = next(c for c in ctx.workspace.connectors if c.key == "acme-crm")
    assert not config.auth.client_secret
    assert "client_secret" not in parse_qs(seen[-1].content.decode())
    async with ctx.pool.connection() as conn:
        assert not await all_rows(conn, "SELECT * FROM secrets")


async def test_private_client_uses_basic_auth_when_the_server_requires_it(server):
    import base64

    ctx, seen, settings = server
    settings["registration"] = False
    old = ctx.mcp_client.http.request

    async def basic_client(method, url, **kwargs):
        result = await old(method, url, **kwargs)
        if "oauth-authorization-server" in url:
            result["token_endpoint_auth_methods_supported"] = ["client_secret_basic"]
        return result

    ctx.mcp_client.http.request = basic_client
    draft = await propose(ctx)
    await approve(ctx, draft)
    async with ctx.pool.connection() as conn:
        await mcp_oauth.with_client(ctx, conn, await person(ctx), draft, "acme-illo", CLIENT_SECRET)
    assert (await finish_browser(ctx, signin_link(ctx))).status_code == 200
    request = seen[-1]
    assert (
        request.headers["authorization"]
        == "Basic " + base64.b64encode(("acme-illo:" + CLIENT_SECRET).encode()).decode()
    )
    assert "client_secret" not in parse_qs(request.content.decode())
    config = next(c for c in ctx.workspace.connectors if c.key == "acme-crm")
    async with ctx.pool.connection() as conn:
        await conn.execute(
            "UPDATE connector_grants SET expires_at=clock_timestamp()-interval '1 second'"
        )
        assert await ctx.mcp_client.bearer(ctx, conn, config) == ACCESS
    assert seen[-1].headers["authorization"] == request.headers["authorization"]


async def test_private_client_values_cannot_enter_the_browser_address(server):
    ctx, _, _ = server
    old = ctx.mcp_client.http.request

    async def echo_secret(method, url, **kwargs):
        result = await old(method, url, **kwargs)
        if url.endswith("/register"):
            result["client_id"] = CLIENT_SECRET
        return result

    ctx.mcp_client.http.request = echo_secret
    draft = await propose(ctx)
    await approve(ctx, draft)
    async with ctx.pool.connection() as conn:
        assert not await all_rows(conn, "SELECT * FROM connector_oauth_states")
        assert not await all_rows(conn, "SELECT * FROM connector_grants")
    assert CLIENT_SECRET not in str(ctx.slack_client.calls)


async def test_oauth_tokens_cannot_enter_the_connector_config(server):
    ctx, _, _ = server
    old = ctx.mcp_client.http.request

    async def echo_access(method, url, **kwargs):
        result = await old(method, url, **kwargs)
        if url.endswith("/register"):
            result["client_id"] = ACCESS
        return result

    ctx.mcp_client.http.request = echo_access
    _, link = await begin(ctx)
    assert (await finish_browser(ctx, link)).status_code == 400
    assert not (ctx.workspace.root / "connectors/acme-crm.yaml").exists()
    async with ctx.pool.connection() as conn:
        assert not await all_rows(conn, "SELECT * FROM connector_grants")
        assert not await all_rows(conn, "SELECT * FROM secrets")


async def test_host_status_shows_an_ended_grant(server, capsys):
    from types import SimpleNamespace

    from illo.connectors.login import execute

    ctx, _, _ = server
    _, link = await begin(ctx)
    await finish_browser(ctx, link)
    async with ctx.pool.connection() as conn:
        await conn.execute("UPDATE connector_grants SET broken_at=clock_timestamp()")
        await execute(conn, SimpleNamespace(command="connectors"), ctx.config)
    assert "acme-crm  team  ended" in capsys.readouterr().out
