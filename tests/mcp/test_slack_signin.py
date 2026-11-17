"""Slack OpenID tests use locally signed tokens, fake HTTP, and the test database."""

import asyncio
import json
import re
import time
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import SecretStr

from illo.channels.mcp import slack_signin
from illo.channels.mcp.oauth import COOKIE
from illo.channels.slack.agent_oauth import notify
from illo.connectors import store
from illo.db import all_rows, one
from illo.ledger import kv, tokens
from tests.mcp.test_oauth import BASE, Flow


@pytest.fixture
async def flow(app, slack_client):
    app.state.ctx.workspace.config.connect.public_url = BASE
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE) as client:
        yield Flow(app, client, slack_client)


@pytest.fixture(scope="module")
def signing_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
async def signin(flow, monkeypatch, signing_key):
    ctx = flow.app.state.ctx
    ctx.config.vault_key = SecretStr(Fernet.generate_key().decode())
    async with ctx.pool.connection() as conn:
        vault = store.vault(ctx)
        await vault.set(conn, "slack-client-id", "fake-slack-client")
        await vault.set(conn, "slack-client-secret", "fake-slack-secret")
        await kv.set(conn, "slack:team", "T_ACME")
    fake = FakeOpenID(flow, signing_key)
    monkeypatch.setattr(slack_signin, "BoundedHTTP", lambda **kw: fake.http(**kw))
    return fake


class FakeOpenID:
    def __init__(self, flow, key):
        self.flow, self.key = flow, key
        self.calls, self.params = [], None
        self.claims = {}
        self.failure = None
        self.jwks_changes = {}
        self.raw_id_token = None
        self.other_key = None
        self.delay = 0
        self.after_exchange = None

    def http(self, **kw):
        assert kw == {"public": True}
        return self

    async def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        assert kwargs["timeout"] == 1 and kwargs["max_bytes"] == 32768
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.failure == url:
            raise ValueError("Never show a provider secret")
        if url == slack_signin.TOKEN:
            assert method == "POST"
            assert kwargs["data"] == {
                "grant_type": "authorization_code", "code": "fake-slack-code",
                "client_id": "fake-slack-client", "client_secret": "fake-slack-secret",
                "redirect_uri": BASE + "/oauth/agent/slack/callback",
            }
            if self.after_exchange:
                await self.after_exchange()
            async with self.flow.app.state.ctx.pool.connection() as conn:
                now = float((await one(conn, "SELECT extract(epoch FROM clock_timestamp()) AS now"))["now"])
            claims = {
                "iss": "https://slack.com", "aud": "fake-slack-client", "sub": "U_ACME_ADA",
                "nonce": self.params["nonce"][0], "exp": now + 300, "iat": now - 1,
                "https://slack.com/team_id": "T_ACME", "https://slack.com/user_id": "U_ACME_ADA",
                **self.claims,
            }
            encoded = jwt.encode(claims, self.other_key or self.key, algorithm="RS256", headers={"kid": "test-key"})
            self.raw_id_token = encoded
            return {"ok": True, "id_token": encoded, "access_token": "fake-private-access"}
        assert url == slack_signin.JWKS and method == "GET"
        key = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key()))
        return {"keys": [{**key, "kid": "test-key", "use": "sig", "alg": "RS256", **self.jwks_changes}]}

    async def start(self):
        await self.flow.start()
        response = await self.flow.http.get("/oauth/agent/slack/start")
        assert response.status_code == 302, response.text
        self.params = parse_qs(urlsplit(response.headers["location"]).query)
        return response

    async def callback(self, **changes):
        return await self.flow.http.get("/oauth/agent/slack/callback", params={
            "state": self.params["state"][0], "code": "fake-slack-code", **changes,
        })

    async def consent(self):
        await self.start()
        response = await self.callback()
        assert response.status_code == 200, response.text
        self.csrf = re.search(r'name="csrf" value="([^"]+)"', response.text)[1]
        return response

    async def allow(self, choice="allow", **changes):
        return await self.flow.http.post("/oauth/agent/consent", data={"csrf": self.csrf, "choice": choice, **changes})

    async def row(self):
        async with self.flow.app.state.ctx.pool.connection() as conn:
            return await one(conn, "SELECT * FROM agent_oauth_requests ORDER BY id DESC LIMIT 1")

    async def execute(self, sql):
        async with self.flow.app.state.ctx.pool.connection() as conn:
            await conn.execute(sql)

    def notices(self):
        return [c for c in self.flow.slack.calls if c["method"] == "chat_postMessage"
                and c.get("text", "").startswith("You connected ")]

    async def no_connection(self):
        async with self.flow.app.state.ctx.pool.connection() as conn:
            assert not await all_rows(conn, "SELECT * FROM tokens")
        row = await self.row()
        assert row["decision"] == "pending" and row["token_id"] is None


async def test_end_to_end_and_security(signin, fake_brain, caplog):
    response = await signin.consent()
    flow = signin.flow
    assert "Signed in as Ada." in response.text
    assert 'form-action \'self\'' in response.headers["content-security-policy"]
    for expected in ("default-src 'none'", "frame-ancestors 'none'", "base-uri 'none'"):
        assert expected in response.headers["content-security-policy"]
    assert "unsafe-inline" not in response.headers["content-security-policy"]
    assert "<form method=\"post\"" in response.text and "value=\"allow\"" in response.text
    assert "App name (not verified): Claude" in response.text and "claude.ai" in response.text
    assert not signin.notices()
    await signin.no_connection()
    row = await signin.row()
    assert row["consent_hash"] == tokens.digest(signin.csrf)
    assert row["slack_state_hash"] is None and row["slack_nonce_hash"] is None
    assert row["person_id"] and row["paired_at"]
    done = await signin.allow()
    # No redirect: a browser can apply `form-action` to a redirect after a form POST.
    assert done.status_code == 200 and "location" not in done.headers
    assert "form-action 'none'" in done.headers["content-security-policy"]
    assert "location.replace('/oauth/agent/continue')" in done.text and 'href="/oauth/agent/continue"' in done.text
    assert (await signin.allow()).status_code == 400
    assert (await signin.callback()).status_code == 400
    assert (await flow.http.get("/oauth/agent/slack/start")).status_code == 400
    continued = await flow.http.get("/oauth/agent/continue")
    assert continued.status_code == 302
    code = parse_qs(urlsplit(continued.headers["location"]).query)["code"][0]
    flow.form = {"grant_type": "authorization_code", "client_id": flow.client["client_id"],
                 "code": code, "code_verifier": flow.verifier}
    grant = (await flow.exchange()).json()
    assert (await flow.rpc(grant["access_token"])).status_code == 200
    async with flow.app.state.ctx.pool.connection() as conn:
        owner = await tokens.verify(conn, grant["access_token"], resource=BASE + "/mcp")
        ada = await one(conn, "SELECT * FROM people WHERE key='ada'")
        assert owner["person_id"] == ada["id"]
        rows = await all_rows(conn, "SELECT * FROM audit WHERE tool LIKE 'agent_oauth.%%'")
        assert {r["tool"] for r in rows} >= {"agent_oauth.signin_start", "agent_oauth.signin_success", "agent_oauth.consent_allow"}
        stored = repr(await all_rows(conn, "SELECT * FROM agent_oauth_requests")) + repr(rows)
    assert len(signin.notices()) == 1
    assert signin.notices()[0]["text"] == (
        "You connected Claude (claude.ai) to Illo. If this was not you, tell your operator at once.")
    assert not fake_brain.requests
    for secret in ("fake-slack-secret", "fake-private-access", signin.csrf, signin.params["nonce"][0], signin.raw_id_token):
        assert secret not in stored and secret not in caplog.text
    for secret in ("fake-slack-secret", "fake-private-access", signin.raw_id_token):
        assert secret not in response.text


async def test_start_parameters_and_hashes(signin):
    response = await signin.start()
    assert response.headers["location"].split("?")[0] == slack_signin.AUTHORIZE
    assert signin.params == {
        "response_type": ["code"], "scope": ["openid"], "client_id": ["fake-slack-client"],
        "redirect_uri": [BASE + "/oauth/agent/slack/callback"], "team": ["T_ACME"],
        "state": signin.params["state"], "nonce": signin.params["nonce"],
    }
    row = await signin.row()
    assert row["slack_state_hash"] == tokens.digest(signin.params["state"][0])
    assert row["slack_nonce_hash"] == tokens.digest(signin.params["nonce"][0])
    cookie = signin.flow.page.headers["set-cookie"]
    for flag in ("Secure", "HttpOnly", "SameSite=lax", "Path=/", "Max-Age=1200"):
        assert flag in cookie
    assert row["browser_hash"] == tokens.digest(signin.flow.http.cookies.get(COOKIE))
    assert row["pairing_hash"] not in signin.flow.page.text
    assert 'form-action \'none\'' in signin.flow.page.headers["content-security-policy"]
    assert '<details><summary>Use a code in Slack</summary>' in signin.flow.page.text
    assert 'href="/oauth/agent/slack/start"' in signin.flow.page.text


@pytest.mark.parametrize("missing", ["slack-client-id", "slack-client-secret", "both", "vault"])
async def test_page_only_offers_configured_signin(signin, missing):
    if missing == "vault":
        signin.flow.app.state.ctx.config.vault_key = None
    else:
        async with signin.flow.app.state.ctx.pool.connection() as conn:
            await conn.execute("DELETE FROM secrets WHERE name=%s OR %s='both'", (missing, missing))
    response = await signin.flow.start()
    assert "Sign in with Slack" not in response.text and "<details>" not in response.text
    await signin.flow.dm()
    await signin.flow.click()
    assert (await signin.flow.http.get("/oauth/agent/continue")).status_code == 302
    assert len(signin.notices()) == 1
    assert (await signin.flow.http.get("/oauth/agent/slack/start")).status_code == 400
    assert not signin.calls


@pytest.mark.parametrize("target", ["start", "callback", "consent"])
@pytest.mark.parametrize("cookie", [None, "wrong-browser"])
async def test_other_browser_cannot_act(signin, target, cookie):
    await signin.consent() if target == "consent" else await signin.start()
    signin.flow.http.cookies.clear()
    if cookie:
        signin.flow.http.cookies.set(COOKIE, cookie)
    if target == "consent":
        response = await signin.allow()
    elif target == "callback":
        response = await signin.callback()
    else:
        response = await signin.flow.http.get("/oauth/agent/slack/start")
    assert response.status_code == 400
    await signin.no_connection()


async def test_wrong_state_then_success_and_reuse(signin):
    await signin.start()
    assert (await signin.callback(state="wrong-state")).status_code == 400
    assert not signin.calls
    assert (await signin.callback()).status_code == 200
    calls = len(signin.calls)
    assert (await signin.callback()).status_code == 400
    assert len(signin.calls) == calls
    await signin.no_connection()


@pytest.mark.parametrize("error", ["access_denied", "a-private-provider-error"])
async def test_slack_error_is_plain_and_consumes_state(signin, error):
    await signin.start()
    response = await signin.callback(error=error)
    assert response.status_code == 400 and slack_signin.FAILED in response.text
    assert error not in response.text and not signin.calls
    assert (await signin.callback()).status_code == 400
    await signin.no_connection()


@pytest.mark.parametrize("endpoint", [slack_signin.TOKEN, slack_signin.JWKS])
async def test_provider_failure(signin, endpoint):
    signin.failure = endpoint
    await signin.start()
    response = await signin.callback()
    assert response.status_code == 400 and slack_signin.FAILED in response.text
    assert "Never show" not in response.text
    assert (await signin.callback()).status_code == 400
    await signin.no_connection()


@pytest.mark.parametrize("claims", [
    {"aud": "another-client"}, {"iss": "https://evil.example.com"}, {"nonce": "another-nonce"},
    {"exp": 1}, {"exp": None}, {"exp": "9999999999"}, {"exp": float("inf")},
    {"nbf": 9999999999}, {"iat": 9999999999}, {"nonce": None}, {"sub": "U_OTHER"},
    {"https://slack.com/team_id": "T_OTHER"}, {"https://slack.com/team_id": None},
    {"https://slack.com/user_id": "U_UNKNOWN", "sub": "U_UNKNOWN"},
    {"https://slack.com/user_id": None},
    {"exp": 10 ** 400}, {"azp": "another-client"}, {"aud": ["fake-slack-client", "another-client"]},
])
async def test_identity_refusals(signin, claims):
    signin.claims = claims
    await signin.start()
    response = await signin.callback()
    assert response.status_code == 400
    await signin.no_connection()
    row = await signin.row()
    assert row["person_id"] is None and row["paired_at"] is None and row["consent_hash"] is None


@pytest.mark.parametrize("key_change", [{"kid": "wrong-key"}, {"alg": "HS256"}, {"use": "enc"}])
async def test_jwks_refusals(signin, key_change):
    signin.jwks_changes = key_change
    await signin.start()
    assert (await signin.callback()).status_code == 400
    await signin.no_connection()


async def test_bad_signature(signin):
    signin.other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    await signin.start()
    assert (await signin.callback()).status_code == 400
    await signin.no_connection()


async def test_inactive_member_cannot_signin(signin):
    await signin.start()
    await signin.execute("UPDATE people SET active=false WHERE key='ada'")
    response = await signin.callback()
    assert response.status_code == 400 and slack_signin.NOT_MEMBER in response.text
    await signin.no_connection()


@pytest.mark.parametrize("stage", ["start", "callback", "after_exchange", "consent"])
async def test_expired_request(signin, stage):
    if stage == "start":
        await signin.flow.start()
    elif stage == "consent":
        await signin.consent()
    else:
        await signin.start()
    async def expire():
        await signin.execute("UPDATE agent_oauth_requests SET expires_at=clock_timestamp()-interval '1 second'")
    if stage == "after_exchange":
        signin.after_exchange = expire
    else:
        await expire()
    if stage == "start":
        response = await signin.flow.http.get("/oauth/agent/slack/start")
    elif stage == "consent":
        response = await signin.allow()
    else:
        response = await signin.callback()
    assert response.status_code == 400
    await signin.no_connection()


@pytest.mark.parametrize("csrf", [None, "wrong-value"])
async def test_consent_needs_csrf(signin, csrf):
    await signin.consent()
    form = {"choice": "allow"}
    if csrf:
        form["csrf"] = csrf
    assert (await signin.flow.http.post("/oauth/agent/consent", data=form)).status_code == 400
    await signin.no_connection()


async def test_get_never_approves_and_inactive_person_fails(signin):
    await signin.consent()
    response = await signin.flow.http.get("/oauth/agent/consent", params={"csrf": signin.csrf, "choice": "allow"})
    assert response.status_code == 405
    await signin.no_connection()
    await signin.execute("UPDATE people SET active=false WHERE key='ada'")
    assert (await signin.allow()).status_code == 400
    await signin.no_connection()


async def test_cancel(signin):
    await signin.consent()
    assert (await signin.allow("cancel")).status_code == 200
    assert (await signin.allow()).status_code == 400
    assert (await signin.flow.http.get("/oauth/agent/slack/start")).status_code == 400
    response = await signin.flow.http.get("/oauth/agent/continue")
    assert parse_qs(urlsplit(response.headers["location"]).query)["error"] == ["access_denied"]
    row = await signin.row()
    assert row["decision"] == "denied" and row["token_id"] is None
    assert not signin.notices()
    async with signin.flow.app.state.ctx.pool.connection() as conn:
        assert await one(conn, "SELECT id FROM audit WHERE tool='agent_oauth.consent_cancel'")


@pytest.mark.parametrize("pair_first", [True, False])
async def test_card_and_web_race_create_one_connection(signin, pair_first):
    await signin.start()
    if pair_first:
        await signin.flow.dm()
    response = await signin.callback()
    signin.csrf = re.search(r'name="csrf" value="([^"]+)"', response.text)[1]
    if not pair_first:
        await signin.flow.dm()
    await asyncio.gather(signin.allow(), signin.flow.click())
    async with signin.flow.app.state.ctx.pool.connection() as conn:
        assert len(await all_rows(conn, "SELECT * FROM tokens")) == 1
        assert len(await all_rows(conn, "SELECT * FROM audit WHERE tool='agent_oauth.approve'")) == 1
    assert len(signin.notices()) == 1
    assert (await signin.flow.http.get("/oauth/agent/continue")).status_code == 302


async def test_pairing_inside_details_still_works(signin):
    await signin.flow.start()
    assert "<details>" in signin.flow.page.text
    await signin.flow.dm()
    await signin.flow.click()
    await signin.flow.click()
    assert len(signin.notices()) == 1
    row = await signin.row()
    assert row["decision"] == "approved"
    assert (await signin.flow.http.get("/oauth/agent/slack/start")).status_code == 400


async def test_signin_cannot_replace_paired_person(signin):
    await signin.start()
    await signin.flow.dm()
    signin.claims = {"https://slack.com/user_id": "U_ACME_BEN", "sub": "U_ACME_BEN"}
    assert (await signin.callback()).status_code == 400
    await signin.no_connection()
    async with signin.flow.app.state.ctx.pool.connection() as conn:
        assert (await signin.row())["person_id"] == (await one(conn, "SELECT id FROM people WHERE key='ada'"))["id"]


async def test_notice_failure_keeps_connection_and_no_retry(signin, monkeypatch):
    await signin.consent()
    fail = AsyncMock(side_effect=RuntimeError("Private Slack failure"))
    monkeypatch.setattr("illo.channels.slack.agent_oauth.dm_person", fail)
    assert (await signin.allow()).status_code == 200
    row = await signin.row()
    assert row["decision"] == "approved" and row["notice_at"]
    await notify(signin.flow.app.state.ctx, row["id"])
    assert fail.await_count == 1


async def test_restart_invalidates_old_state_and_new_callback_wins(signin):
    await signin.start()
    old = signin.params
    response = await signin.flow.http.get("/oauth/agent/slack/start")
    signin.params = parse_qs(urlsplit(response.headers["location"]).query)
    assert (await signin.callback(state=old["state"][0])).status_code == 400
    assert (await signin.callback()).status_code == 200


async def test_callback_has_total_deadline(signin):
    await signin.start()
    signin.delay = 10
    async with asyncio.timeout(4):
        response = await signin.callback()
    assert response.status_code == 400
    await signin.no_connection()


@pytest.mark.parametrize("fault", ["missing-id", "slack-false", "bad-jwt", "none-alg", "duplicate-key", "bad-keys"])
async def test_invalid_provider_response(signin, monkeypatch, fault):
    original = signin.request

    async def request(method, url, **kwargs):
        data = await original(method, url, **kwargs)
        if url == slack_signin.TOKEN:
            if fault == "missing-id":
                return {"ok": True, "access_token": "fake-private-access"}
            if fault == "slack-false":
                return {"ok": False, "error": "private-provider-error"}
            if fault == "bad-jwt":
                data["id_token"] = "bad.jwt"
            if fault == "none-alg":
                claims = jwt.decode(data["id_token"], options={"verify_signature": False})
                data["id_token"] = jwt.encode(claims, None, algorithm="none", headers={"kid": "test-key"})
        elif fault == "duplicate-key":
            data["keys"].append(data["keys"][0])
        elif fault == "bad-keys":
            return {"keys": "private-provider-error"}
        return data

    monkeypatch.setattr(signin, "request", request)
    await signin.start()
    response = await signin.callback()
    assert response.status_code == 400 and "private-provider-error" not in response.text
    await signin.no_connection()


async def test_unknown_workspace_fails_without_outside_call(signin):
    await signin.flow.start()
    await signin.execute("DELETE FROM kv WHERE key='slack:team'")
    assert (await signin.flow.http.get("/oauth/agent/slack/start")).status_code == 400
    assert not signin.calls
    await signin.no_connection()


async def test_workspace_change_during_exchange_fails(signin):
    await signin.start()

    async def change_team():
        await signin.execute("UPDATE kv SET value='\"T_OTHER\"' WHERE key='slack:team'")

    signin.after_exchange = change_team
    assert (await signin.callback()).status_code == 400
    await signin.no_connection()


async def test_card_decision_during_exchange_cannot_bind_again(signin):
    await signin.start()
    await signin.flow.dm()
    signin.after_exchange = signin.flow.click
    assert (await signin.callback()).status_code == 400
    row = await signin.row()
    assert row["decision"] == "approved" and row["consent_hash"] is None
    assert len(signin.notices()) == 1
    async with signin.flow.app.state.ctx.pool.connection() as conn:
        assert len(await all_rows(conn, "SELECT * FROM tokens")) == 1


async def test_old_callback_cannot_bind_after_restart(signin):
    await signin.start()

    async def restart():
        assert (await signin.flow.http.get("/oauth/agent/slack/start")).status_code == 302

    signin.after_exchange = restart
    assert (await signin.callback()).status_code == 400
    await signin.no_connection()


async def test_consent_escapes_person_and_app_and_warns_for_loopback(signin):
    await signin.flow.register(uri="http://localhost:9876/callback", client_name='<script>app</script>')
    await signin.execute("UPDATE people SET name='<script>person</script>' WHERE key='ada'")
    response = await signin.consent()
    assert "<script>app" not in response.text and "<script>person" not in response.text
    assert "&lt;script&gt;app&lt;/script&gt;" in response.text
    assert "&lt;script&gt;person&lt;/script&gt;" in response.text
    assert "This address returns to a local app." in response.text


async def test_failure_audit_uses_fixed_reason(signin):
    await signin.start()
    assert (await signin.callback(state="private-state-input")).status_code == 400
    async with signin.flow.app.state.ctx.pool.connection() as conn:
        row = await one(conn, "SELECT * FROM audit WHERE tool='agent_oauth.signin_failure' ORDER BY id DESC LIMIT 1")
    assert row["args"] == {} and row["decision"] == "deny"
    assert row["reason"] == "The agent sign-in failed (state)"
    assert "private-state-input" not in repr(row)


@pytest.mark.parametrize("ahead,status", [(30, 200), (300, 400)])
async def test_small_clock_difference_is_allowed(signin, ahead, status):
    signin.claims = {"iat": time.time() + ahead, "nbf": time.time() + ahead}
    await signin.start()
    assert (await signin.callback()).status_code == status


@pytest.mark.parametrize("browser", ["allow", "cancel"])
@pytest.mark.parametrize("card,tokens", [("approve", 1), ("deny", 0)])
async def test_web_consent_after_the_card_keeps_the_card_decision(signin, card, tokens, browser):
    await signin.start()
    await signin.flow.dm()
    response = await signin.callback()
    signin.csrf = re.search(r'name="csrf" value="([^"]+)"', response.text)[1]
    await signin.flow.click(card)
    assert (await signin.allow(csrf="wrong")).status_code == 400
    response = await signin.allow(browser)
    # The page says what the card decided. It starts no automatic step.
    word = "approved" if tokens else "denied"
    assert response.status_code == 200 and f"You already {word} this connection in Slack." in response.text
    assert "<script" not in response.text and 'href="/oauth/agent/continue"' in response.text
    assert (await signin.allow(browser)).status_code == 400
    async with signin.flow.app.state.ctx.pool.connection() as conn:
        assert len(await all_rows(conn, "SELECT * FROM tokens")) == tokens
        assert not await all_rows(conn, "SELECT * FROM audit WHERE tool LIKE 'agent_oauth.consent_%'")
    assert len(signin.notices()) == tokens
    assert (await signin.row())["decision"] == word
    response = await signin.flow.http.get("/oauth/agent/continue")
    assert response.status_code == 302 and ("code=" in response.headers["location"]) == bool(tokens)
