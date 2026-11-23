"""Exercise agent sign-in with real HTTP and Postgres, and recorded Slack calls."""

import asyncio
import re
import secrets
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from pydantic import ValidationError

from illo.channels.mcp import oauth
from illo.channels.slack.agent_oauth import pairing_code, sent_by_app
from illo.channels.slack.blocks import message_text
from illo.channels.slack.intake import SlackIntake
from illo.cli import execute, parser
from illo.db import all_rows, one
from illo.ledger import agent_oauth as ledger
from illo.ledger import tokens
from illo.workspace import ConnectConfig
from tests.mcp.client import AgentClient

BASE = "https://illo.example.com"
CALLBACK = "https://claude.ai/api/mcp/auth_callback"


@pytest.fixture
async def flow(app, slack_client):
    app.state.ctx.workspace.config.connect.public_url = BASE
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE) as client:
        yield Flow(app, client, slack_client)


class Flow:
    def __init__(self, app, http, slack):
        self.app, self.http, self.slack = app, http, slack
        self.intake = SlackIntake(app.state.ctx, "U_ACME_ILLO")
        self.verifier = secrets.token_urlsafe(48)
        self.client = None
        self.form = None
        self.seq = 0

    async def register(self, uri=CALLBACK, **changes):
        response = await self.http.post("/oauth/agent/register", json={
            "client_name": "Claude", "redirect_uris": [uri], "token_endpoint_auth_method": "none", **changes,
        })
        assert response.status_code == 201, response.text
        self.client = response.json()
        return self.client

    async def start(self, **changes):
        if not self.client:
            await self.register()
        params = {"client_id": self.client["client_id"], "redirect_uri": self.client["redirect_uris"][0],
                  "response_type": "code", "code_challenge_method": "S256",
                  "code_challenge": ledger.challenge(self.verifier), "resource": BASE + "/mcp",
                  "scope": "agent offline_access", "state": "state + /?=é", **changes}
        self.params = {k: v for k, v in params.items() if v is not None}
        self.page = await self.http.get("/oauth/agent/authorize", params=self.params)
        return self.page

    async def dm(self, text=None, **changes):
        if text is None:
            text = re.search(r"connect ([A-Z2-9]{4}-[A-Z2-9]{4})", self.page.text)[0]
        self.seq += 1
        return await self.intake.receive({"type": "event_callback", "event": {
            "type": "message", "channel": "D_ACME_ADA", "channel_type": "im", "user": "U_ACME_ADA",
            "ts": f"1700000000.{self.seq:06}", "text": text, **changes,
        }})

    def last_card(self):
        return next(c for c in reversed(self.slack.calls) if c["method"] == "chat_postMessage" and c.get("blocks"))

    async def click(self, choice="approve", user="U_ACME_ADA", **changes):
        card = self.last_card()
        message_ts = next(ts for ts, m in self.slack.messages.items() if m.get("blocks") == card["blocks"])
        button = card["blocks"][-1]["elements"][0 if choice == "approve" else 1]
        await self.intake.receive({"type": "block_actions", "user": {"id": user},
                                   "channel": {"id": card["channel"]}, "message": {"ts": message_ts},
                                   "actions": [{"action_id": button["action_id"], "action_ts": str(self.seq), **changes}]})

    async def code(self, **start):
        response = await self.start(**start)
        assert response.status_code == 200, response.text
        await self.dm()
        await self.click()
        response = await self.http.get("/oauth/agent/continue")
        assert response.status_code == 302, response.text
        values = parse_qs(urlsplit(response.headers["location"]).query)
        self.form = {"grant_type": "authorization_code", "client_id": self.client["client_id"],
                     "code": values["code"][0], "code_verifier": self.verifier,
                     "redirect_uri": self.params["redirect_uri"], "resource": self.params.get("resource")}
        return values

    async def exchange(self, **changes):
        return await self.http.post("/oauth/agent/token", data={k: v for k, v in {**self.form, **changes}.items() if v is not None})

    async def connect(self, **start):
        await self.code(**start)
        response = await self.exchange()
        assert response.status_code == 200, response.text
        return response.json()

    async def refresh(self, value, **changes):
        form = {"grant_type": "refresh_token", "client_id": self.client["client_id"],
                "resource": BASE + "/mcp", "refresh_token": value, **changes}
        return await self.http.post("/oauth/agent/token", data={k: v for k, v in form.items() if v is not None})

    async def rpc(self, bearer, method="tools/list", params=None, path="/mcp"):
        return await self.http.post(path, headers={"Authorization": f"Bearer {bearer}"},
                                    json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})


async def test_discovery_and_complete_flow(flow, fake_brain):
    for path in ("/mcp", "/api/mcp"):
        response = await flow.http.post(path, json={})
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == (
            f'Bearer resource_metadata="{BASE}/.well-known/oauth-protected-resource{path}", scope="agent"')
    for suffix, resource in (("", "/mcp"), ("/mcp", "/mcp"), ("/api/mcp", "/api/mcp")):
        response = await flow.http.get("/.well-known/oauth-protected-resource" + suffix)
        assert response.json() == {"resource": BASE + resource, "authorization_servers": [BASE],
                                   "scopes_supported": ["agent"], "bearer_methods_supported": ["header"]}
    metadata = await flow.http.get("/.well-known/oauth-authorization-server")
    assert metadata.json() == {
        "issuer": BASE, "authorization_endpoint": BASE + "/oauth/agent/authorize",
        "token_endpoint": BASE + "/oauth/agent/token", "registration_endpoint": BASE + "/oauth/agent/register",
        "response_types_supported": ["code"], "grant_types_supported": ["authorization_code", "refresh_token"],
        "token_endpoint_auth_methods_supported": ["none"], "code_challenge_methods_supported": ["S256"],
        "scopes_supported": ["agent"], "authorization_response_iss_parameter_supported": True,
    }
    values = await flow.code()
    assert values["state"] == ["state + /?=é"]
    assert values["iss"] == [BASE]
    assert not fake_brain.requests
    card = flow.last_card()
    assert "read and write Illo memory, tasks and lanes as you" in card["text"]
    assert "Approve only if you started this yourself a moment ago." in card["text"]
    assert "claude.ai" in card["text"]
    assert [b["text"]["text"] for b in card["blocks"][-1]["elements"]] == ["Approve", "Deny"]
    assert flow.slack.calls[-1]["method"] == "chat_update"
    assert all(b["type"] != "actions" for b in flow.slack.calls[-1]["blocks"])
    assert (await flow.http.get("/oauth/agent/continue")).status_code == 400
    response = await flow.exchange()
    assert response.headers["cache-control"] == "no-store"
    grant = response.json()
    assert set(grant) == {"access_token", "refresh_token", "expires_in", "token_type", "scope"}
    assert grant["expires_in"] == 3600 and grant["scope"] == "agent"
    static = await AgentClient.create(flow.app, flow.http)
    assert (await flow.rpc(grant["access_token"])).json() == (await static.rpc("tools/list")).json()
    params = {"name": "illo_read", "arguments": {"capability": "knowledge.search", "arguments": {"query": "sample"}}}
    assert (await flow.rpc(grant["access_token"], "tools/call", params)).json() == (await static.rpc("tools/call", params)).json()
    async with flow.app.state.ctx.pool.connection() as conn:
        owner = await tokens.verify(conn, grant["access_token"], resource=BASE + "/mcp")
        other = await tokens.verify(conn, static.token)
        assert owner["person_id"] == other["person_id"] and owner.keys() == other.keys()
        assert owner["label"] == "Claude (claude.ai)" and owner["agent_key"] is None
        stored = await one(conn, "SELECT * FROM tokens WHERE id=%s", (owner["id"],))
        assert stored["last_used_at"] and stored["token_hash"].startswith("!agent-oauth:")
        assert await tokens.verify(conn, stored["token_hash"]) is None
        assert not await all_rows(conn, "SELECT * FROM events")
        assert not await all_rows(conn, "SELECT * FROM runs")
        assert not await all_rows(conn, "SELECT * FROM tasks")
        assert not await all_rows(conn, "SELECT * FROM items WHERE kind<>'note'")
    rotated = (await flow.refresh(grant["refresh_token"])).json()
    assert rotated["refresh_token"] != grant["refresh_token"]
    assert (await flow.rpc(rotated["access_token"])).status_code == 200
    async with flow.app.state.ctx.pool.connection() as conn:
        assert await tokens.verify(conn, rotated["access_token"], resource=BASE + "/mcp") == owner
    async with flow.app.state.ctx.pool.connection() as conn:
        await conn.execute("UPDATE agent_oauth_refresh SET used_at=used_at-interval '61 seconds' WHERE used_at IS NOT NULL")
    replay = await flow.refresh(grant["refresh_token"])
    assert replay.status_code == 400 and replay.json() == {"error": "invalid_grant"}
    assert (await flow.rpc(rotated["access_token"])).status_code == 401
    assert (await flow.rpc(grant["access_token"])).status_code == 401
    assert (await flow.refresh(rotated["refresh_token"])).json() == {"error": "invalid_grant"}
    async with flow.app.state.ctx.pool.connection() as conn:
        rows = await all_rows(conn, "SELECT * FROM audit WHERE tool LIKE 'agent_oauth.%%' ORDER BY id")
        assert [r["tool"] for r in rows] == ["agent_oauth." + s for s in (
            "registration", "request", "approve", "exchange", "refresh", "reuse")]
        assert all(r["args"] == {} for r in rows)
        for secret in (*grant.values(), *rotated.values(), flow.form["code"], flow.verifier):
            if isinstance(secret, str) and len(secret) > 10:
                assert secret not in str(rows)


@pytest.mark.parametrize("uri", ["https://evil.example/callback", "http://evil.example/callback", "https://claude.ai/api/mcp/auth_callback/", "http://localhost.evil.example/callback", "http://localhost:0/cb", "http://user@localhost/cb", "http://127.0.0.1/cb#frag", "http://[::1]:65536/cb"])
async def test_registration_refuses_redirects(flow, uri):
    response = await flow.http.post("/oauth/agent/register", json={"redirect_uris": [uri]})
    assert response.status_code == 400 and response.json() == {"error": "invalid_redirect_uri"}


@pytest.mark.parametrize("uri", [CALLBACK, "https://claude.com/api/mcp/auth_callback", "http://localhost/callback", "http://127.0.0.1:12345/callback", "http://[::1]:12345/callback"])
async def test_trusted_redirects(flow, uri):
    await flow.register(uri)
    response = await flow.start()
    assert response.status_code == 200
    assert urlsplit(uri).hostname in response.text
    assert ("local app" in response.text) == uri.startswith("http:")


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "[::1]"])
async def test_loopback_matches_only_port(flow, host):
    await flow.register(f"http://{host}:1234/callback?q=x")
    assert (await flow.start(redirect_uri=f"http://{host}:54321/callback?q=x")).status_code == 200
    for uri in (f"http://{host}:54321/other?q=x", f"http://{host}:54321/callback?q=y", "http://other.example:54321/callback?q=x"):
        response = await flow.start(redirect_uri=uri)
        assert response.status_code == 400 and "location" not in response.headers
    other = "localhost" if host != "localhost" else "127.0.0.1"
    assert (await flow.start(redirect_uri=f"http://{other}:54321/callback?q=x")).status_code == 400


@pytest.mark.parametrize("change", [{"client_id": "unknown"}, {"redirect_uri": "https://evil.example/"}, {"redirect_uri": "https://claude.com/api/mcp/auth_callback"}])
async def test_bad_client_or_redirect_never_redirects(flow, change):
    response = await flow.start(**change)
    assert response.status_code == 400 and "location" not in response.headers
    assert "connect " not in response.text


@pytest.mark.parametrize("changes,error", [
    ({"code_challenge": None}, "invalid_request"), ({"code_challenge_method": "plain"}, "invalid_request"),
    ({"code_challenge_method": None}, "invalid_request"),
    ({"resource": "https://evil.example/mcp"}, "invalid_target"), ({"resource": BASE + "/other"}, "invalid_target"),
    ({"response_type": "token"}, "unsupported_response_type"),
    ({"response_type": None}, "invalid_request"),
])
async def test_authorize_errors_redirect_with_state(flow, changes, error):
    response = await flow.start(**changes)
    assert response.status_code == 302
    params = parse_qs(urlsplit(response.headers["location"]).query)
    assert params == {"error": [error], "state": ["state + /?=é"], "iss": [BASE]}


@pytest.mark.parametrize("changes", [{"code_verifier": "z" * 64}, {"code_verifier": None}, {"resource": BASE + "/api/mcp"}, {"client_id": "wrong"}, {"redirect_uri": "http://localhost/callback"}])
async def test_code_bindings(flow, changes):
    await flow.code()
    response = await flow.exchange(**changes)
    assert response.status_code == 400 and response.json() == {"error": "invalid_grant"}
    assert (await flow.exchange()).status_code == 200


async def test_code_reuse_revokes_first_grant(flow):
    grant = await flow.connect()
    assert (await flow.exchange()).json() == {"error": "invalid_grant"}
    assert (await flow.rpc(grant["access_token"])).status_code == 401
    assert (await flow.refresh(grant["refresh_token"])).json() == {"error": "invalid_grant"}


async def test_code_expiry(flow):
    await flow.code()
    async with flow.app.state.ctx.pool.connection() as conn:
        await conn.execute("UPDATE agent_oauth_requests SET code_expires_at=clock_timestamp()-interval '1 second'")
    assert (await flow.exchange()).json() == {"error": "invalid_grant"}


async def test_request_expiry(flow):
    await flow.start()
    async with flow.app.state.ctx.pool.connection() as conn:
        await conn.execute("UPDATE agent_oauth_requests SET expires_at=clock_timestamp()-interval '1 second'")
    await flow.dm()
    assert "invalid, used, or expired" in flow.slack.calls[-1]["text"]
    assert (await flow.http.get("/oauth/agent/status")).json() == {"status": "expired"}
    response = await flow.http.get("/oauth/agent/continue")
    assert parse_qs(urlsplit(response.headers["location"]).query)["error"] == ["access_denied"]


async def test_deny(flow):
    await flow.start()
    assert (await flow.http.get("/oauth/agent/status")).json() == {"status": "pending"}
    await flow.dm()
    await flow.click("deny")
    assert (await flow.http.get("/oauth/agent/status")).json() == {"status": "denied"}
    response = await flow.http.get("/oauth/agent/continue")
    assert parse_qs(urlsplit(response.headers["location"]).query)["error"] == ["access_denied"]
    async with flow.app.state.ctx.pool.connection() as conn:
        assert not await all_rows(conn, "SELECT * FROM tokens")
        assert await one(conn, "SELECT id FROM audit WHERE tool='agent_oauth.deny'")


@pytest.mark.parametrize("user,fields", [("U_UNKNOWN", {}), ("U_ACME_ADA", {"user_team": "T_OTHER"}), ("U_ACME_ADA", {"bot_id": "B_OTHER"})])
async def test_pair_requires_active_human(flow, user, fields, fake_brain):
    await flow.start()
    await flow.dm(user=user, **fields)
    assert not fake_brain.requests
    async with flow.app.state.ctx.pool.connection() as conn:
        assert (await one(conn, "SELECT person_id FROM agent_oauth_requests"))["person_id"] is None
        assert not await all_rows(conn, "SELECT * FROM events")


async def test_inactive_person_cannot_pair(flow):
    await flow.start()
    async with flow.app.state.ctx.pool.connection() as conn:
        await conn.execute("UPDATE people SET active=false WHERE key='ada'")
    await flow.dm()
    assert "invalid, used, or expired" in flow.slack.calls[-1]["text"]


async def test_only_pairing_person_can_click(flow):
    await flow.start()
    await flow.dm()
    before = len(flow.slack.calls)
    await flow.click(user="U_ACME_BEN")
    assert len(flow.slack.calls) == before
    assert (await flow.http.get("/oauth/agent/status")).json() == {"status": "pending"}
    await flow.click()
    assert (await flow.http.get("/oauth/agent/status")).json() == {"status": "approved"}


async def test_wrong_code_and_person_limit(flow, monkeypatch, fake_brain):
    monkeypatch.setattr(ledger, "PAIR_ATTEMPTS", 2)
    await flow.start()
    for _ in range(2):
        assert (await flow.dm("`connect ZZZZ-ZZZZ`"))["decision"] == "agent_oauth"
    await flow.dm()
    assert "invalid, used, or expired" in flow.slack.calls[-1]["text"]
    assert not fake_brain.requests
    async with flow.app.state.ctx.pool.connection() as conn:
        assert (await one(conn, "SELECT attempts FROM agent_oauth_limits WHERE kind='pair'"))["attempts"] == 3
        assert not await all_rows(conn, "SELECT * FROM events")


@pytest.mark.parametrize("text,decision", [("connect my google", "claimed"), ("connect AWS", "claimed"),
                                         ("Please connect ABCD-EFGH", "run"), ("connect ABCD-EFGH next", "run")])
async def test_other_dm_text_reaches_normal_gateway(flow, text, decision, fake_brain):
    result = await flow.dm(text)
    assert result["decision"] == decision
    assert len(fake_brain.requests) == (decision == "run")


@pytest.mark.parametrize("text", [
    "connect ABCD-EFGH", "  `CoNnEcT abcd-efgh`  ", "```connect ABCD-EFGH```", "`````connect ABCD-EFGH`````",
    # Some apps post for the person and add this signature. The words follow the person's language.
    "connect ABCD-EFGH *Sent using* <@U0APP123>", "connect ABCD-EFGH\n\n*Envoyé avec* <@U0APP123>",
    "`connect ABCD-EFGH` _Sent using_ <@U0APP123>", "connect ABCD-EFGH *x* <@U0APP123>",
    "connect ABCD-EFGH *" + "x" * 40 + "*<@U0APP123>",
])
def test_pairing_text(text):
    assert pairing_code(text) == "ABCDEFGH"
    assert sent_by_app(text) == ("<@" in text)


@pytest.mark.parametrize("text", [
    "connect ABCD-EFGH next", "connect ABCD-EFGH <@U0APP123>", "connect ABCD-EFGH *Sent using* Claude",
    "connect ABCD-EFGH *Sent using* <@U0APP123> and more", "Please connect ABCD-EFGH *Sent using* <@U0APP123>",
    "connect ABCD-EFGH *" + "x" * 41 + "* <@U0APP123>", "connect ABCD-EFGH *a*\n*b* <@U0APP123>",
    "connect ABCD-EFGH *Sent using_ <@U0APP123>", "connect ABCD-EFGH *Sent\rusing* <@U0APP123>",
    "connect ABCD-EFGH *Sent\u2028using* <@U0APP123>", "connect ABCD-EFGH*Sent using* <@U0APP123>",
    "connect ABCD-EFGH ** <@U0APP123>", "connect ABCD-EFGH *a* <@U1> *b* <@U2>",
    "*Sent using* <@U0APP123>",
])
def test_other_text_is_not_a_pairing_message(text):
    assert pairing_code(text) is None
    assert not sent_by_app(text)


def test_long_hostile_text_costs_little_work():
    texts = ("connect ABCD-EFGH" + " " * 200_000 + "!", "connect ABCD-EFGH " + "*a" * 100_000,
             "connect ABCD-EFGH" + " " * 300 + "*Sent using* <@U0APP123>")
    # Processor time, so that a pause of the host does not fail the test.
    start = time.process_time()
    for text in texts:
        assert pairing_code(text) is None
    assert time.process_time() - start < 0.5


async def test_code_that_an_app_sends_for_the_person_gets_the_card(flow, fake_brain):
    await flow.start()
    code = re.search(r"connect ([A-Z2-9]{4}-[A-Z2-9]{4})", flow.page.text)[0]
    result = await flow.dm(code + "\n\n*Envoyé avec* <@U0APP123>")
    assert result == {"decision": "agent_oauth"}
    assert not fake_brain.requests
    text = flow.slack.calls[-1]["text"]
    assert "An app sent this code in your name.\nApprove only if you started this yourself" in text
    async with flow.app.state.ctx.pool.connection() as conn:
        row = await one(conn, "SELECT decision, person_id FROM agent_oauth_requests")
        assert row["decision"] == "pending" and row["person_id"]
        assert not await all_rows(conn, "SELECT * FROM events")
    # Only the click of the same person approves.
    before = len(flow.slack.calls)
    await flow.click(user="U_ACME_BEN")
    assert len(flow.slack.calls) == before
    assert (await flow.http.get("/oauth/agent/status")).json() == {"status": "pending"}
    await flow.click()
    assert (await flow.http.get("/oauth/agent/status")).json() == {"status": "approved"}


async def test_typed_code_has_no_app_line_on_the_card(flow):
    await flow.start()
    await flow.dm()
    assert "An app sent this code" not in flow.slack.calls[-1]["text"]


@pytest.mark.parametrize("change", ["expiry", "revoke", "inactive"])
async def test_access_refusals(flow, change):
    grant = await flow.connect()
    async with flow.app.state.ctx.pool.connection() as conn:
        if change == "expiry":
            await conn.execute("UPDATE agent_oauth_access SET expires_at=clock_timestamp()-interval '1 second'")
        elif change == "revoke":
            await conn.execute("UPDATE tokens SET revoked_at=clock_timestamp()")
        else:
            await conn.execute("UPDATE people SET active=false WHERE key='ada'")
    response = await flow.rpc(grant["access_token"])
    assert response.status_code == 401 and "resource_metadata" in response.headers["www-authenticate"]


async def test_resource_binding_and_alias(flow):
    grant = await flow.connect(resource=BASE + "/api/mcp/")
    assert (await flow.rpc(grant["access_token"], path="/api/mcp")).status_code == 200
    assert (await flow.rpc(grant["access_token"], path="/mcp")).status_code == 401


@pytest.mark.parametrize("scope", [None, "agent", "offline_access", "agent offline_access", "claudeai", "collector"])
async def test_supported_scopes(flow, scope):
    grant = await flow.connect(scope=scope)
    assert grant["scope"] == "agent" and grant["refresh_token"]


async def test_token_endpoint_requires_form(flow):
    await flow.code()
    response = await flow.http.post("/oauth/agent/token", json=flow.form)
    assert response.status_code == 400 and response.json() == {"error": "invalid_request"}
    assert (await flow.exchange()).status_code == 200


@pytest.mark.parametrize("changes", [{"refresh_token": "bad"}, {"resource": "https://evil.example/mcp"}, {"client_id": "bad"}])
async def test_refresh_errors_are_invalid_grant(flow, changes):
    grant = await flow.connect()
    changes = dict(changes)
    value = changes.pop("refresh_token", grant["refresh_token"])
    response = await flow.refresh(value, **changes)
    assert response.status_code == 400 and response.json() == {"error": "invalid_grant"}


async def test_refresh_idle_expiry(flow):
    grant = await flow.connect()
    async with flow.app.state.ctx.pool.connection() as conn:
        await conn.execute("UPDATE agent_oauth_refresh SET expires_at=clock_timestamp()-interval '1 second'")
    assert (await flow.refresh(grant["refresh_token"])).json() == {"error": "invalid_grant"}


async def test_no_public_url_keeps_old_door(app, http):
    assert oauth.issuer(app.state.ctx) is None
    assert (await http.post("/mcp", json={})).headers["www-authenticate"] == "Bearer"
    for path, method in (("/.well-known/oauth-protected-resource", "GET"), ("/.well-known/oauth-protected-resource/mcp", "GET"),
                         ("/.well-known/oauth-protected-resource/api/mcp", "GET"), ("/.well-known/oauth-authorization-server", "GET"),
                         ("/oauth/agent/authorize", "GET"), ("/oauth/agent/status", "GET"), ("/oauth/agent/continue", "GET"),
                         ("/oauth/agent/register", "POST"), ("/oauth/agent/token", "POST")):
        assert (await http.request(method, path)).status_code == 404
    static = await AgentClient.create(app, http)
    assert (await static.rpc("tools/list")).status_code == 200


async def test_cli_lists_and_revokes_connection(flow, capsys):
    grant = await flow.connect()
    ctx = flow.app.state.ctx
    ctx.config.database_url = ctx.pool.conninfo
    assert await execute(parser().parse_args(["token", "list"]), ctx.config) == 0
    assert "Claude (claude.ai)" in capsys.readouterr().out
    async with ctx.pool.connection() as conn:
        token_id = (await one(conn, "SELECT id FROM tokens"))["id"]
    assert await execute(parser().parse_args(["token", "revoke", str(token_id)]), ctx.config) == 0
    assert (await flow.rpc(grant["access_token"])).status_code == 401
    assert (await flow.refresh(grant["refresh_token"])).json() == {"error": "invalid_grant"}


async def test_page_security_and_hash_storage(flow):
    name = '<script>alert("hello")</script>'
    await flow.register(client_name=name)
    response = await flow.start()
    assert name not in response.text and "&lt;script&gt;" in response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    csp = response.headers["content-security-policy"]
    script = re.search(r'<script nonce="([^"]+)"', response.text)[1]
    style = re.search(r'<style nonce="([^"]+)"', response.text)[1]
    assert f"script-src 'nonce-{script}'" in csp and f"style-src 'nonce-{style}'" in csp
    assert "unsafe-inline" not in csp and "default-src 'none'" in csp
    assert "Secure" in response.headers["set-cookie"] and "HttpOnly" in response.headers["set-cookie"]
    code = re.search(r"connect ([A-Z2-9]{4}-[A-Z2-9]{4})", response.text)[1].replace("-", "")
    handle = flow.http.cookies.get(oauth.COOKIE)
    async with flow.app.state.ctx.pool.connection() as conn:
        row = await one(conn, "SELECT * FROM agent_oauth_requests")
        assert row["pairing_hash"] == tokens.digest(code) and row["browser_hash"] == tokens.digest(handle)
        assert code not in str(row) and handle not in str(row)
        assert len(code) == 8 and all(c in ledger.PAIR_ALPHABET for c in code)
    flow.http.cookies.clear()
    assert (await flow.http.get("/oauth/agent/status")).status_code == 400
    assert (await flow.http.get("/oauth/agent/continue")).status_code == 400


async def test_registration_and_pending_caps(flow, monkeypatch):
    monkeypatch.setattr(ledger, "REGISTRATIONS_PER_HOUR", 1)
    await flow.register()
    response = await flow.http.post("/oauth/agent/register", json={"redirect_uris": [CALLBACK]})
    assert response.status_code == 503
    monkeypatch.setattr(ledger, "MAX_PENDING_PER_CLIENT", 1)
    assert (await flow.start()).status_code == 200
    response = await flow.start()
    assert parse_qs(urlsplit(response.headers["location"]).query)["error"] == ["temporarily_unavailable"]


async def test_cleanup_drops_only_unapproved_clients(flow):
    await flow.connect()
    approved = flow.client["client_id"]
    await flow.register()
    async with flow.app.state.ctx.pool.connection() as conn:
        await conn.execute("UPDATE agent_oauth_clients SET created_at=clock_timestamp()-interval '25 hours'")
        await ledger.cleanup(conn)
        assert [r["id"] for r in await all_rows(conn, "SELECT id FROM agent_oauth_clients")] == [approved]


async def test_body_limit_and_registration_metadata(flow):
    for value in ({"redirect_uris": [CALLBACK], "token_endpoint_auth_method": "client_secret_post"},
                  {"redirect_uris": [CALLBACK], "grant_types": [{}]}, {"redirect_uris": [CALLBACK], "client_name": "x" * 17000}):
        assert (await flow.http.post("/oauth/agent/register", json=value)).status_code == 400
    assert (await flow.http.post("/oauth/agent/token", content="x" * 17000, headers={"Content-Type": "application/x-www-form-urlencoded"})).status_code == 400


async def test_configured_redirect_and_validation(flow):
    uri = "https://app.example.com/callback?one=two"
    flow.app.state.ctx.workspace.config.connect.agent_redirects = [uri]
    await flow.register(uri)
    values = await flow.code()
    assert values["one"] == ["two"]
    for bad in ("http://app.example.com/callback", "https://user@app.example.com/callback", "https://app.example.com/callback#fragment"):
        with pytest.raises(ValidationError):
            ConnectConfig(agent_redirects=[bad])


async def test_concurrent_code_exchange_revokes_connection(flow):
    await flow.code()
    replies = await asyncio.gather(flow.exchange(), flow.exchange())
    assert sorted(r.status_code for r in replies) == [200, 400]
    grant = next(r.json() for r in replies if r.status_code == 200)
    assert (await flow.rpc(grant["access_token"])).status_code == 401


async def test_concurrent_refresh_keeps_one_chain(flow):
    grant = await flow.connect()
    replies = await asyncio.gather(flow.refresh(grant["refresh_token"]), flow.refresh(grant["refresh_token"]))
    assert [r.status_code for r in replies] == [200, 200]
    pairs = [r.json() for r in replies]
    # Both sessions hold the same pair. No second chain exists.
    assert pairs[0] == pairs[1] and pairs[0]["refresh_token"] != grant["refresh_token"]
    assert (await flow.rpc(pairs[0]["access_token"])).status_code == 200
    async with flow.app.state.ctx.pool.connection() as conn:
        live = await all_rows(conn, "SELECT 1 FROM agent_oauth_refresh WHERE used_at IS NULL")
        assert len(live) == 1
        await conn.execute("UPDATE agent_oauth_refresh SET used_at=used_at-interval '61 seconds' WHERE used_at IS NOT NULL")
    # After the retry window, the old refresh token is a replay.
    assert (await flow.refresh(grant["refresh_token"])).json() == {"error": "invalid_grant"}
    assert (await flow.rpc(pairs[0]["access_token"])).status_code == 401


async def test_refresh_waits_for_the_connection_lock(flow):
    grant = await flow.connect()
    async with flow.app.state.ctx.pool.connection() as holder:
        await holder.execute("SELECT 1 FROM tokens WHERE label='Claude (claude.ai)' FOR UPDATE")
        tasks = [asyncio.create_task(flow.refresh(grant["refresh_token"])) for _ in range(2)]
        done, _ = await asyncio.wait(tasks, timeout=0.5)
        assert not done
    replies = await asyncio.gather(*tasks)
    assert [r.status_code for r in replies] == [200, 200]
    assert replies[0].json() == replies[1].json()


async def test_stolen_refresh_token_is_found_at_the_next_rotation(flow):
    grant = await flow.connect()
    owner = (await flow.refresh(grant["refresh_token"])).json()
    thief = (await flow.refresh(grant["refresh_token"])).json()
    assert thief == owner
    assert (await flow.refresh(owner["refresh_token"])).status_code == 200
    async with flow.app.state.ctx.pool.connection() as conn:
        await conn.execute("UPDATE agent_oauth_refresh SET used_at=used_at-interval '61 seconds' WHERE used_at IS NOT NULL")
    # The second holder of the same token starts no chain of its own. Its use ends the connection.
    assert (await flow.refresh(thief["refresh_token"])).json() == {"error": "invalid_grant"}
    assert (await flow.rpc(owner["access_token"])).status_code == 401


@pytest.mark.parametrize("stop", ["UPDATE tokens SET revoked_at=clock_timestamp()", "UPDATE people SET active=false"])
async def test_retry_window_gives_nothing_after_a_stop(flow, stop):
    grant = await flow.connect()
    assert (await flow.refresh(grant["refresh_token"])).status_code == 200
    async with flow.app.state.ctx.pool.connection() as conn:
        await conn.execute(stop)
    assert (await flow.refresh(grant["refresh_token"])).json() == {"error": "invalid_grant"}


async def test_next_secrets_need_the_ledger_seed(flow):
    grant = await flow.connect()
    first = (await flow.refresh(grant["refresh_token"])).json()
    async with flow.app.state.ctx.pool.connection() as conn:
        seeds = [r["seed"] for r in await all_rows(conn, "SELECT seed FROM agent_oauth_refresh")]
    assert len(set(seeds)) == 2 and all(len(seed) == 64 for seed in seeds)
    assert first["refresh_token"] in {ledger.successor(grant["refresh_token"], seed, "refresh") for seed in seeds}
    assert first["refresh_token"] != ledger.successor(grant["refresh_token"], "", "refresh")


async def test_app_can_omit_resource_and_redirect(flow):
    await flow.code(resource=None, scope="claudeai")
    flow.form.pop("resource", None)
    grant = (await flow.exchange(redirect_uri=None)).json()
    assert grant["scope"] == "agent"
    assert (await flow.rpc(grant["access_token"])).status_code == 200
    assert (await flow.rpc(grant["access_token"], path="/api/mcp")).status_code == 401
    assert (await flow.refresh(grant["refresh_token"], resource=None)).status_code == 200


async def test_access_verify_holds_no_usage_lock(flow):
    grant = await flow.connect()
    async with flow.app.state.ctx.pool.connection() as first:
        row = await tokens.verify(first, grant["access_token"], resource=BASE + "/mcp")
        async with asyncio.timeout(2):
            async with flow.app.state.ctx.pool.connection() as second:
                assert await tokens.verify(second, grant["access_token"], resource=BASE + "/mcp")
                assert await tokens.revoke(second, row["id"])
        assert await tokens.verify(first, grant["access_token"], resource=BASE + "/mcp") is None


async def test_pairing_message_changes_no_memory(flow, fake_brain):
    async with flow.app.state.ctx.pool.connection() as conn:
        before = await all_rows(conn, "SELECT * FROM items ORDER BY id")
    await flow.start()
    await flow.dm()
    assert not fake_brain.requests
    async with flow.app.state.ctx.pool.connection() as conn:
        assert await all_rows(conn, "SELECT * FROM items ORDER BY id") == before
        for table in ("events", "runs", "tasks", "submissions", "memory_changes", "note_proposals"):
            assert not await all_rows(conn, f"SELECT * FROM {table}")


@pytest.mark.parametrize("thread", [False, True])
async def test_history_does_not_send_old_pairing_code_to_model(flow, fake_brain, thread):
    flow.slack.messages = {"old": {"ts": "1699999999.000001", "user": "U_ACME_ADA", "text": "`connect ABCD-EFGH`"}}
    fields = {"thread_ts": "1699999999.000001"} if thread else {}
    assert (await flow.dm("What is the next step?", **fields))["decision"] == "run"
    text = fake_brain.requests[-1].user_message
    assert "ABCD-EFGH" not in text and "Agent sign-in code removed" in text


def test_shared_slack_reader_removes_pairing_code_from_tools():
    assert message_text({"text": "`connect ABCD-EFGH`"}) == "[Agent sign-in code removed]"
    assert message_text({"blocks": [{"type": "section", "text": {"type": "plain_text", "text": "connect ABCD-EFGH"}}]}) == "[Agent sign-in code removed]"
    assert message_text({"text": "connect ABCD-EFGH\n\n*Sent using* <@U0APP123>"}) == "[Agent sign-in code removed]"
    assert message_text({"text": "connect AWS"}) == "connect AWS"


async def test_lifetimes_use_database_clock(flow):
    await flow.connect()
    async with flow.app.state.ctx.pool.connection() as conn:
        request = await one(conn, """SELECT extract(epoch FROM expires_at-created_at) AS life,
            extract(epoch FROM code_expires_at-continued_at) AS code_life FROM agent_oauth_requests""")
        assert 599 < request["life"] < 601 and 119 < request["code_life"] < 121
        access = await one(conn, "SELECT extract(epoch FROM expires_at-created_at) AS life FROM agent_oauth_access")
        refresh = await one(conn, "SELECT extract(epoch FROM expires_at-created_at) AS life FROM agent_oauth_refresh")
        assert 3599 < access["life"] < 3601
        assert 90 * 86400 - 1 < refresh["life"] < 90 * 86400 + 1


async def test_expired_or_inactive_consent_creates_no_token(flow):
    await flow.start()
    await flow.dm()
    async with flow.app.state.ctx.pool.connection() as conn:
        await conn.execute("UPDATE people SET active=false WHERE key='ada'")
    await flow.click()
    async with flow.app.state.ctx.pool.connection() as conn:
        assert not await all_rows(conn, "SELECT * FROM tokens")
        await conn.execute("UPDATE people SET active=true WHERE key='ada'")
        await conn.execute("UPDATE agent_oauth_requests SET expires_at=clock_timestamp()-interval '1 second'")
    await flow.click()
    assert "expired" in flow.slack.calls[-1]["text"]
    async with flow.app.state.ctx.pool.connection() as conn:
        assert not await all_rows(conn, "SELECT * FROM tokens")


async def test_pairing_code_is_single_use(flow):
    await flow.start()
    await flow.dm()
    await flow.dm()
    assert "invalid, used, or expired" in flow.slack.calls[-1]["text"]
    assert len([c for c in flow.slack.calls if c["method"] == "chat_postMessage" and c.get("blocks")]) == 1


async def test_retry_after_failed_slack_send(flow, monkeypatch):
    await flow.start()
    original = flow.slack.chat_postMessage

    async def fail(**kwargs):
        raise RuntimeError("Slack is unavailable")

    monkeypatch.setattr(flow.slack, "chat_postMessage", fail)
    with pytest.raises(RuntimeError):
        await flow.dm()
    monkeypatch.setattr(flow.slack, "chat_postMessage", original)
    flow.seq = 0
    await flow.dm()
    assert flow.last_card()["blocks"][-1]["type"] == "actions"


async def test_repeat_click_keeps_one_connection(flow):
    await flow.start()
    await flow.dm()
    await flow.click()
    flow.seq += 1
    await flow.click()
    async with flow.app.state.ctx.pool.connection() as conn:
        assert len(await all_rows(conn, "SELECT * FROM tokens")) == 1
        assert len(await all_rows(conn, "SELECT * FROM audit WHERE tool='agent_oauth.approve'")) == 1


async def test_client_store_and_global_pending_caps(flow, monkeypatch):
    await flow.register()
    monkeypatch.setattr(ledger, "MAX_CLIENTS", 1)
    assert (await flow.http.post("/oauth/agent/register", json={"redirect_uris": [CALLBACK]})).status_code == 503
    monkeypatch.setattr(ledger, "MAX_PENDING", 1)
    assert (await flow.start()).status_code == 200
    assert parse_qs(urlsplit((await flow.start()).headers["location"]).query)["error"] == ["temporarily_unavailable"]


async def test_static_tokens_work_on_both_addresses_with_oauth(flow):
    client = await AgentClient.create(flow.app, flow.http)
    for path in ("/mcp", "/api/mcp"):
        assert (await client.rpc("tools/list", path=path)).status_code == 200


async def test_boot_public_url_can_enable_oauth(flow):
    flow.app.state.ctx.workspace.config.connect.public_url = None
    flow.app.state.ctx.config.public_url = BASE
    assert (await flow.http.get("/.well-known/oauth-authorization-server")).json()["issuer"] == BASE


@pytest.mark.parametrize("value", ["", "http://illo.example.com", BASE + "/path", BASE + "?", "https://@illo.example.com", "https://illo.example.com\n.evil.example"])
async def test_invalid_public_origin_disables_oauth(flow, value):
    flow.app.state.ctx.workspace.config.connect.public_url = None
    flow.app.state.ctx.config.public_url = value
    assert (await flow.http.get("/.well-known/oauth-authorization-server")).status_code == 404
    assert (await flow.http.post("/mcp", json={})).headers["www-authenticate"] == "Bearer"


async def test_token_scope_and_repeated_parameters(flow):
    grant = await flow.connect()
    first = (await flow.refresh(grant["refresh_token"], scope="collector")).json()
    assert first["scope"] == "agent"
    assert (await flow.refresh(first["refresh_token"], scope="agent offline_access")).status_code == 200
    response = await flow.http.post("/oauth/agent/token", content="grant_type=refresh_token&grant_type=authorization_code",
                                    headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert response.json() == {"error": "invalid_request"}
