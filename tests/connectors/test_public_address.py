"""A separate public address keeps local service calls private."""

from urllib.parse import parse_qs, urlsplit

import pytest

from illo.connectors import oauth
from illo.workspace import ConnectConfig


@pytest.mark.parametrize(
    "value",
    [
        "http://illo.example.com",
        "https://ada:private@illo.example.com",
        "https://illo.example.com?q=1",
        "https://illo.example.com?",
        "https://illo.example.com#part",
        "illo.example.com",
    ],
)
def test_invalid_public_address(value):
    with pytest.raises(ValueError):
        ConnectConfig(public_url=value)


@pytest.mark.parametrize("public", [None, "https://illo.example.com"])
async def test_callback_and_start_use_the_same_address(google_app, public, monkeypatch):
    app = google_app
    monkeypatch.setenv("ILLO_GOOGLE_CLIENT_ID", "test-client")
    monkeypatch.setenv("ILLO_GOOGLE_CLIENT_SECRET", "test-secret")
    ctx = app.state.ctx
    ctx.config.public_url = "http://127.0.0.1:8000"
    ctx.workspace.config.connect.public_url = public
    async with ctx.pool.connection() as conn:
        person = await (await conn.execute("SELECT id FROM people WHERE key='ada'")).fetchone()
    link = await oauth.issue(ctx, person["id"])
    base = public or "http://127.0.0.1:8000"
    assert link.startswith(base + "/oauth/google/start?")
    state = parse_qs(urlsplit(link).query)["state"][0]
    address = await oauth.start(ctx, state)
    assert parse_qs(urlsplit(address).query)["redirect_uri"] == [base + "/oauth/google/callback"]
    assert ctx.config.public_url == "http://127.0.0.1:8000"


async def test_public_override_completes_in_the_browser(google_app, http, monkeypatch):
    import httpx

    from illo.connectors.google import GoogleClient
    from illo.connectors.http import BoundedHTTP

    ctx = google_app.state.ctx
    monkeypatch.setenv("ILLO_GOOGLE_CLIENT_ID", "test-client")
    monkeypatch.setenv("ILLO_GOOGLE_CLIENT_SECRET", "test-secret")
    ctx.config.public_url = "http://127.0.0.1:8000"
    ctx.workspace.config.connect.public_url = "https://illo.example.com"

    def respond(request):
        if request.url.path.endswith("/profile"):
            return httpx.Response(200, json={"emailAddress": "ada@example.com"})
        return httpx.Response(
            200, json={"access_token": "fake-access", "refresh_token": "fake-refresh"}
        )

    ctx.google_client = GoogleClient(BoundedHTTP(httpx.MockTransport(respond)))
    async with ctx.pool.connection() as conn:
        person = await (await conn.execute("SELECT id FROM people WHERE key='ada'")).fetchone()
    link = await oauth.issue(ctx, person["id"])
    response = await http.get(link)
    assert response.status_code == 307
    assert response.headers["set-cookie"].startswith(oauth.BROWSER_COOKIE + "=")
    state = parse_qs(urlsplit(link).query)["state"][0]
    response = await http.get(oauth.callback_url(ctx), params={"state": state, "code": "fake-code"})
    assert response.status_code == 200 and "Google is connected." in response.text
