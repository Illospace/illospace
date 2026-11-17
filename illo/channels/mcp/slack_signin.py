"""Slack proves identity. A separate POST asks for consent before any app receives it."""

import asyncio
import html
import secrets
from urllib.parse import urlencode

import jwt
from fastapi.responses import RedirectResponse

from illo.connectors import store
from illo.connectors.http import BoundedHTTP
from illo.db import one
from illo.ledger import agent_oauth as ledger
from illo.ledger import kv

# Slack publishes these addresses in its OpenID discovery document.
AUTHORIZE = "https://slack.com/openid/connect/authorize"
TOKEN = "https://slack.com/api/openid.connect.token"
JWKS = "https://slack.com/openid/connect/keys"
FAILED = "The sign-in did not finish. Start again in the app."
NOT_MEMBER = "This Slack account is not an active member of this Illo team."


async def credentials(ctx, conn):
    if not ctx.config.vault_key:
        return None
    vault = store.vault(ctx)
    client_id = await vault.get(conn, "slack-client-id")
    secret = await vault.get(conn, "slack-client-secret")
    return (client_id, secret) if client_id and secret else None


def failure(text=FAILED):
    from illo.channels.mcp.oauth import page

    return page("<h1>" + text + "</h1>", status=400)


async def start(request):
    from illo.channels.mcp.oauth import COOKIE, HEADERS, issuer

    ctx = request.app.state.ctx
    async with ctx.pool.connection() as conn:
        client = await credentials(ctx, conn)
        team = await kv.get(conn, "slack:team")
        if not client or not isinstance(team, str) or not team:
            await ledger.signin_failure(conn, "configuration")
            return failure()
        pair = await ledger.signin_start(conn, request.cookies.get(COOKIE, ""))
    if not pair:
        return failure()
    state, nonce = pair
    return RedirectResponse(AUTHORIZE + "?" + urlencode({
        "response_type": "code", "scope": "openid", "client_id": client[0],
        "redirect_uri": issuer(ctx) + "/oauth/agent/slack/callback", "state": state,
        "nonce": nonce, "team": team,
    }), headers=HEADERS, status_code=302)


def consent_page(row):
    from illo.channels.mcp.oauth import app_details, page

    return page(
        '<h1>Connect to Illo</h1><p>Signed in as ' + html.escape(row["person"]) + '.</p>'
        + app_details(row["name"], row["redirect_uri"])
        + '<p>It can read and write Illo memory, tasks and lanes as you.</p>'
        + '<p>Allow only if you started this yourself a moment ago.</p>'
        + '<form method="post" action="/oauth/agent/consent">'
        + '<input type="hidden" name="csrf" value="' + row["csrf"] + '">'
        + '<button name="choice" value="allow">Allow</button> '
        + '<button class="quiet" name="choice" value="cancel">Cancel</button></form>',
        forms=True,
    )


async def callback(request):
    from illo.channels.mcp.oauth import COOKIE, issuer

    ctx, q = request.app.state.ctx, request.query_params
    handle = request.cookies.get(COOKIE, "")
    async with ctx.pool.connection() as conn:
        attempt = await ledger.signin_state(conn, handle, q.get("state", ""))
    if not attempt:
        return failure()

    async def fail(reason, text=FAILED):
        async with ctx.pool.connection() as conn:
            await ledger.signin_failure(conn, reason, attempt)
        return failure(text)

    if len(q.multi_items()) != len(q) or len(str(request.url)) > 8192:
        return await fail("parameters")
    if "error" in q:
        return await fail("slack_error")
    if not q.get("code"):
        return await fail("code")
    async with ctx.pool.connection() as conn:
        client = await credentials(ctx, conn)
        team = await kv.get(conn, "slack:team")
    if not client or not isinstance(team, str) or not team:
        return await fail("configuration")
    try:
        # The MCP dependency includes PyJWT with RSA support.
        # This deadline includes DNS, retries, both responses, and body reads.
        async with asyncio.timeout(3):
            http = BoundedHTTP(public=True)
            data = await http.request("POST", TOKEN, data={
                "grant_type": "authorization_code", "code": q["code"],
                "client_id": client[0], "client_secret": client[1],
                "redirect_uri": issuer(ctx) + "/oauth/agent/slack/callback",
            }, timeout=1, max_bytes=32768)
            if (not isinstance(data, dict) or data.get("ok") is not True
                    or not isinstance(data.get("id_token"), str)):
                return await fail("exchange")
            encoded = data["id_token"]
            del data
            keys = await http.request("GET", JWKS, timeout=1, max_bytes=32768)
        header = jwt.get_unverified_header(encoded)
        if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str):
            return await fail("signature")
        matches = [k for k in keys["keys"] if k.get("kid") == header["kid"]
                   and k.get("kty") == "RSA" and k.get("use", "sig") == "sig"
                   and k.get("alg", "RS256") == "RS256"]
        if len(matches) != 1:
            return await fail("signature")
        identity = jwt.decode(encoded, jwt.PyJWK.from_dict(matches[0]).key,
                              algorithms=["RS256"], audience=client[0], issuer="https://slack.com",
                              options={"require": ["exp", "iss", "aud", "nonce", "sub", "iat"],
                                       "verify_exp": False, "verify_iat": False, "verify_nbf": False})
        del encoded
        if (identity.get("azp", client[0]) != client[0]
                or isinstance(identity["aud"], list) and len(identity["aud"]) > 1
                and identity.get("azp") != client[0]):
            return await fail("audience")
        nonce = identity.get("nonce")
        if (not isinstance(nonce, str)
                or not secrets.compare_digest(ledger.digest(nonce), attempt["slack_nonce_hash"])):
            return await fail("nonce")
    except Exception:
        return await fail("provider")
    async with ctx.pool.connection() as conn:
        now = (await one(conn, "SELECT extract(epoch FROM clock_timestamp()) AS now"))["now"]
    for key in ("exp", "iat", "nbf"):
        value = identity.get(key)
        if key not in identity and key != "exp":
            continue
        if (type(value) not in (int, float) or not 0 <= value <= 253402300799
                or (value <= now if key == "exp" else value > now)):
            return await fail("expiry")
    user = identity.get("https://slack.com/user_id")
    if identity.get("https://slack.com/team_id") != team:
        return await fail("team", NOT_MEMBER)
    if not isinstance(user, str) or not user or identity["sub"] != user:
        return await fail("member", NOT_MEMBER)
    async with ctx.pool.connection() as conn:
        # Check the workspace again after the outside calls.
        if await kv.get(conn, "slack:team") != team:
            await ledger.signin_failure(conn, "team", attempt)
            return failure(NOT_MEMBER)
        row = await ledger.signin_person(conn, handle, attempt, user)
    return consent_page(row) if row else failure(NOT_MEMBER)


async def consent(request):
    from illo.channels.mcp.oauth import COOKIE, HEADERS, body
    from illo.channels.slack.agent_oauth import notify

    try:
        form = await body(request, "application/x-www-form-urlencoded")
    except (ValueError, UnicodeError):
        return failure()
    if form.get("choice") not in {"allow", "cancel"}:
        return failure()
    ctx = request.app.state.ctx
    async with ctx.pool.connection() as conn:
        request_id = await ledger.consent(conn, request.cookies.get(COOKIE, ""),
                                          form.get("csrf", ""), form["choice"] == "allow")
    if not request_id:
        return failure()
    await notify(ctx, request_id)
    return RedirectResponse("/oauth/agent/continue", status_code=303, headers=HEADERS)
