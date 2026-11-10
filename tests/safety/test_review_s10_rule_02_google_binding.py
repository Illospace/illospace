"""Rule 2: a Google grant needs the owner's account and the same browser."""

# ruff: noqa: F811

from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from illo.connectors import oauth
from illo.ledger import people
from tests.connectors.conftest import google_app  # noqa: F401
from tests.connectors.test_oauth import login_app  # noqa: F401


async def begin(app, http, person="ada"):
    ctx = app.state.ctx
    ctx.config.public_url = "https://test"
    async with ctx.pool.connection() as conn:
        owner = await people.get(conn, person)
    link = await oauth.issue(ctx, owner["id"])
    response = await http.get(link, follow_redirects=False)
    assert response.status_code == 307
    state = parse_qs(urlsplit(response.headers["location"]).query)["state"][0]
    return state, response


async def no_grant(ctx):
    async with ctx.pool.connection() as conn:
        assert not await (await conn.execute("SELECT id FROM connector_grants")).fetchall()


async def test_rule_02_shared_start_link_cannot_connect_adas_account_to_ben(login_app, http):
    app, requests = login_app
    state, _ = await begin(app, http, "ben")
    result = await http.get(
        "https://test/oauth/google/callback", params={"state": state, "code": "ada-code"}
    )
    assert result.status_code == 400
    assert "people.yaml" in result.text
    await no_grant(app.state.ctx)
    assert requests[-1].url.path == "/revoke"


@pytest.mark.parametrize("cookie", [None, "wrong-nonce"])
async def test_rule_02_callback_needs_the_start_browser(login_app, http, cookie):
    app, requests = login_app
    state, _ = await begin(app, http)
    http.cookies.clear()
    if cookie:
        http.cookies.set("__Host-illo-google", cookie)
    result = await http.get(
        "https://test/oauth/google/callback", params={"state": state, "code": "ada-code"}
    )
    assert result.status_code == 400
    assert not requests
    await no_grant(app.state.ctx)


async def test_rule_02_browser_cookie_is_host_only_and_single_use(login_app, http):
    app, _ = login_app
    state, start = await begin(app, http)
    cookie = start.headers["set-cookie"]
    assert cookie.startswith("__Host-illo-google=")
    assert all(flag in cookie.lower() for flag in ("secure", "httponly", "samesite=lax", "path=/"))
    assert "domain=" not in cookie.lower()
    params = {"state": state, "code": "ada-code"}
    done = await http.get("https://test/oauth/google/callback", params=params)
    assert done.status_code == 200
    assert "Max-Age=0" in done.headers["set-cookie"]
    assert (await http.get("https://test/oauth/google/callback", params=params)).status_code == 400


async def test_rule_02_dm_state_cannot_complete_through_unbound_web_callback(login_app, http):
    app, requests = login_app
    ctx = app.state.ctx
    async with ctx.pool.connection() as conn:
        ada = await people.get(conn, "ada")
    state = parse_qs(urlsplit(await oauth.issue(ctx, ada["id"])).query)["state"][0]
    await oauth.start(ctx, state)
    result = await http.get("/oauth/google/callback", params={"state": state, "code": "ada-code"})
    assert result.status_code == 400
    assert not requests


async def test_rule_02_each_start_has_a_distinct_browser_nonce(login_app, http):
    app, _ = login_app
    state, _ = await begin(app, http)
    await begin(app, http)
    done = await http.get(
        "https://test/oauth/google/callback", params={"state": state, "code": "ada-code"}
    )
    assert done.status_code == 400
    await no_grant(app.state.ctx)


@pytest.mark.parametrize(
    "address", [None, "foreign@example.net", "ADA@EXAMPLE.COM", "alias@example.com"]
)
async def test_rule_02_account_must_match_one_of_the_owners_addresses(login_app, address):
    app, requests = login_app
    ctx = app.state.ctx

    def respond(request):
        requests.append(request)
        if request.url.path.endswith("/profile"):
            return httpx.Response(200, json={"emailAddress": address})
        return httpx.Response(
            200, json={"access_token": "test-access", "refresh_token": "test-refresh"}
        )

    from illo.connectors.http import BoundedHTTP

    ctx.google_client.http = BoundedHTTP(httpx.MockTransport(respond))
    async with ctx.pool.connection() as conn:
        ada = await people.get(conn, "ada")
        await conn.execute(
            "UPDATE people SET emails=array_append(emails, 'alias@example.com') WHERE id=%s",
            (ada["id"],),
        )
    state = parse_qs(urlsplit(await oauth.issue(ctx, ada["id"])).query)["state"][0]
    await oauth.start(ctx, state)
    if address in {None, "foreign@example.net"}:
        with pytest.raises(ValueError, match="people.yaml"):
            await oauth.complete(ctx, state, "code", person_id=ada["id"])
        await no_grant(ctx)
        assert requests[-1].url.path == "/revoke"
    else:
        assert "connected" in await oauth.complete(ctx, state, "code", person_id=ada["id"])
