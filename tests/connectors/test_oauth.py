"""Sign-in tests use fake OAuth endpoints and the real one-use state ledger."""

from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest

from illo.connectors import oauth, store
from illo.connectors.google import GoogleClient
from illo.connectors.http import BoundedHTTP
from illo.ledger import people


@pytest.fixture
async def login_app(google_app, monkeypatch):
    ctx = google_app.state.ctx
    monkeypatch.setenv("ILLO_GOOGLE_CLIENT_ID", "test-client")
    monkeypatch.setenv("ILLO_GOOGLE_CLIENT_SECRET", "test-secret")
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "access_token": "test-access",
                "refresh_token": "test-refresh",
                "expires_in": 3600,
            },
        )

    ctx.google_client = GoogleClient(BoundedHTTP(httpx.MockTransport(respond)))
    return google_app, requests


async def issue_start(app):
    ctx = app.state.ctx
    async with ctx.pool.connection() as conn:
        ada = await people.get(conn, "ada")
    link = await oauth.issue(ctx, ada["id"])
    state = parse_qs(urlsplit(link).query)["state"][0]
    url = await oauth.start(ctx, state)
    return state, ada, url


async def test_state_one_use_owner_bound_and_tokens_encrypted(login_app):
    app, requests = login_app
    ctx = app.state.ctx
    state, ada, url = await issue_start(app)
    query = parse_qs(urlsplit(url).query)
    assert query["code_challenge_method"] == ["S256"]
    assert query["access_type"] == ["offline"]
    assert all(scope.endswith(".readonly") for scope in query["scope"][0].split())
    with pytest.raises(ValueError):
        await oauth.start(ctx, state)
    async with ctx.pool.connection() as conn:
        ben = await people.get(conn, "ben")
    with pytest.raises(ValueError):
        await oauth.complete(ctx, state, "test-code", person_id=ben["id"])
    assert not requests
    assert "connected" in await oauth.complete(ctx, state, "test-code", person_id=ada["id"])
    with pytest.raises(ValueError):
        await oauth.complete(ctx, state, "test-code")
    async with ctx.pool.connection() as conn:
        row, token = await store.load(conn, ctx, "google", ada["id"])
        assert token["refresh_token"] == "test-refresh"
        assert b"test-refresh" not in row["secret"]
    assert len(requests) == 1


async def test_state_expires_and_rejects_tamper(login_app):
    app, _ = login_app
    ctx = app.state.ctx
    state, _, _ = await issue_start(app)
    async with ctx.pool.connection() as conn:
        await conn.execute(
            "UPDATE connector_oauth_states SET expires_at=clock_timestamp()-interval '1 second'"
        )
    with pytest.raises(ValueError):
        await oauth.complete(ctx, state, "test-code")
    with pytest.raises(ValueError):
        await oauth.start(ctx, state + "x")


async def test_pasted_redirect_never_reaches_model_ledger_or_logs(
    login_app, replay, fake_brain, caplog
):
    app, _ = login_app
    state, _, _ = await issue_start(app)
    callback = (
        oauth.callback_url(app.state.ctx)
        + "?"
        + urlencode({"state": state, "code": "secret-code-test"})
    )
    result = await replay(text=callback, payload={"event": {"text": callback}})
    assert result["decision"] == "claimed"
    assert not fake_brain.requests
    async with app.state.ctx.pool.connection() as conn:
        event = await (await conn.execute("SELECT payload FROM events")).fetchone()
        assert "secret-code-test" not in str(event)
        assert state not in str(event)
    assert "secret-code-test" not in caplog.text


async def test_wrong_owner_paste_is_claimed_without_burning_state(login_app, replay, fake_brain):
    app, _ = login_app
    state, _, _ = await issue_start(app)
    callback = (
        oauth.callback_url(app.state.ctx) + "?" + urlencode({"state": state, "code": "test-code"})
    )
    result = await replay(sender="U_ACME_BEN", text=callback)
    assert result["decision"] == "claimed"
    assert not fake_brain.requests
    result = await replay(text=callback)
    assert "connected" in result["text"]


async def test_callback_routes_and_disconnect(login_app, http, replay):
    app, requests = login_app
    ctx = app.state.ctx
    async with ctx.pool.connection() as conn:
        ada = await people.get(conn, "ada")
    link = await oauth.issue(ctx, ada["id"])
    response = await http.get(link, follow_redirects=False)
    assert response.status_code == 307
    state = parse_qs(urlsplit(link).query)["state"][0]
    response = await http.get(
        "/oauth/google/callback", params={"state": state, "code": "test-code"}
    )
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    result = await replay(text="disconnect my Google")
    assert result["decision"] == "claimed"
    assert result["text"] == "Google is disconnected."
    assert requests[-1].url.path == "/revoke"
    async with ctx.pool.connection() as conn:
        assert not await (await conn.execute("SELECT id FROM connector_grants")).fetchall()


async def test_connect_command_bypasses_brain(login_app, replay, fake_brain):
    result = await replay(text="connect my Google")
    assert result["decision"] == "claimed"
    assert "/oauth/google/start?state=" in result["text"]
    assert not fake_brain.requests


async def test_loopback_install_uses_direct_signin_then_paste(login_app, replay):
    app, requests = login_app
    ctx = app.state.ctx
    ctx.config.public_url = "http://127.0.0.1:8000"
    result = await replay(text="connect my Google")
    assert "https://accounts.google.com/" in result["text"]
    async with ctx.pool.connection() as conn:
        row = await (await conn.execute("SELECT * FROM connector_oauth_states")).fetchone()
        assert row["started_at"]
    link = result["text"].split("Google: ")[1].split("\n")[0]
    state = parse_qs(urlsplit(link).query)["state"][0]
    result = await replay(
        text=oauth.callback_url(ctx) + "?" + urlencode({"state": state, "code": "test-code"})
    )
    assert "connected" in result["text"]
    assert len(requests) == 1
