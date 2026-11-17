"""OAuth for the agent door. Slack proves identity and holds the consent buttons."""

import asyncio
import html
import json
import re
import secrets
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from psycopg import Error
from psycopg_pool import PoolTimeout

from illo.channels.agent_urls import valid_redirect
from illo.connectors.oauth import public_url
from illo.db import one
from illo.ledger import agent_oauth as ledger

COOKIE = "__Host-illo-agent"
MAX_BODY = 16384
CLAUDE_REDIRECTS = {
    "https://claude.ai/api/mcp/auth_callback",
    "https://claude.com/api/mcp/auth_callback",
}
HEADERS = {
    "Cache-Control": "no-store", "Pragma": "no-cache",
    "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}
PKCE = re.compile(r"[A-Za-z0-9_-]{43}")
VERIFIER = re.compile(r"[A-Za-z0-9._~-]{43,128}")


def issuer(ctx):
    try:
        base = public_url(ctx)
        parsed = urlsplit(base)
        if (not valid_redirect(base, https_only=True) or "?" in base
                or parsed.path not in {"", "/"}):
            return None
        return base
    except (ValueError, AttributeError):
        return None


def challenge_header(ctx, path):
    base = issuer(ctx)
    if not base:
        return "Bearer"
    return f'Bearer resource_metadata="{base}/.well-known/oauth-protected-resource{path.rstrip("/")}", scope="agent"'


def trusted_redirect(ctx, uri):
    return (uri in CLAUDE_REDIRECTS
            or valid_redirect(uri)
            or valid_redirect(uri, https_only=True) and uri in ctx.workspace.config.connect.agent_redirects)


def redirect_matches(registered, requested):
    if valid_redirect(registered) and valid_redirect(requested):
        def without_port(uri):
            parsed = urlsplit(uri)
            host = "[::1]" if parsed.hostname == "::1" else parsed.hostname
            return uri.replace("http://" + parsed.netloc, "http://" + host, 1)
        return without_port(registered) == without_port(requested)
    return registered == requested


def clean_name(name):
    return "".join(c for c in name if not unicodedata.category(c).startswith("C"))[:80].strip() or "Agent app"


def scope_allowed(scope):
    return isinstance(scope, str) and set(scope.split()) <= {"agent", "offline_access"}


def resource_value(base, value):
    return value.rstrip("/") if value in {base + p for p in ("/mcp", "/mcp/", "/api/mcp", "/api/mcp/")} else None


def json_response(value, status=200):
    return JSONResponse(value, status_code=status, headers=HEADERS)


def error_page():
    return page("<h1>The sign-in request is invalid.</h1><p>Start again in the app.</p>", status=400)


def page(body, *, script="", status=200):
    nonce = secrets.token_urlsafe(24)
    csp = ("default-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'; "
           f"script-src 'nonce-{nonce}'; connect-src 'self'")
    # The browser uses its default neutral page style. No outside assets are needed.
    content = ('<!doctype html><html lang="en"><meta charset="utf-8">'
               '<meta name="viewport" content="width=device-width, initial-scale=1">'
               '<title>Connect to Illo</title><main>' + body + '</main>'
               + (f'<script nonce="{nonce}">{script}</script>' if script else "") + '</html>')
    return HTMLResponse(content, status_code=status, headers={**HEADERS, "Content-Security-Policy": csp})


def redirect(uri, params, base):
    parsed = urlsplit(uri)
    values = parse_qsl(parsed.query, keep_blank_values=True) + list(params.items()) + [("iss", base)]
    return RedirectResponse(urlunsplit(parsed._replace(query=urlencode(values))), status_code=302, headers=HEADERS)


async def body(request, content_type):
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != content_type:
        raise ValueError("invalid_request")
    data = bytearray()
    async for part in request.stream():
        if len(data) + len(part) > MAX_BODY:
            raise ValueError("invalid_request")
        data.extend(part)
    if content_type == "application/json":
        value = json.loads(data)
        if not isinstance(value, dict):
            raise ValueError("invalid_client_metadata")
        return value
    pairs = parse_qsl(data.decode("utf-8"), keep_blank_values=True, max_num_fields=30)
    if len(dict(pairs)) != len(pairs):
        raise ValueError("invalid_request")
    return dict(pairs)


async def protected_resource(request):
    base = issuer(request.app.state.ctx)
    path = "/api/mcp" if request.url.path.endswith("/api/mcp") else "/mcp"
    return json_response({"resource": base + path, "authorization_servers": [base],
                          "scopes_supported": ["agent"], "bearer_methods_supported": ["header"]})


async def authorization_server(request):
    base = issuer(request.app.state.ctx)
    return json_response({
        "issuer": base, "authorization_endpoint": base + "/oauth/agent/authorize",
        "token_endpoint": base + "/oauth/agent/token", "registration_endpoint": base + "/oauth/agent/register",
        "response_types_supported": ["code"], "grant_types_supported": ["authorization_code", "refresh_token"],
        "token_endpoint_auth_methods_supported": ["none"], "code_challenge_methods_supported": ["S256"],
        "scopes_supported": ["agent"], "authorization_response_iss_parameter_supported": True,
    })


async def register(request):
    ctx = request.app.state.ctx
    try:
        value = await body(request, "application/json")
    except (ValueError, UnicodeError):
        return json_response({"error": "invalid_client_metadata"}, 400)
    redirects = value.get("redirect_uris")
    if (not isinstance(redirects, list) or not 1 <= len(redirects) <= 10
            or not all(isinstance(u, str) and trusted_redirect(ctx, u) for u in redirects)):
        return json_response({"error": "invalid_redirect_uri"}, 400)
    if (value.get("token_endpoint_auth_method", "none") != "none"
            or value.get("response_types", ["code"]) != ["code"]
            or not isinstance(value.get("grant_types", []), list)
            or not all(isinstance(g, str) for g in value.get("grant_types", []))
            or not value.get("grant_types", ["authorization_code"])
            or not set(value.get("grant_types", ["authorization_code"])) <= {"authorization_code", "refresh_token"}
            or not scope_allowed(value.get("scope", "")) or not isinstance(value.get("client_name", ""), str)):
        return json_response({"error": "invalid_client_metadata"}, 400)
    async with ctx.pool.connection() as conn:
        value = await ledger.register(conn, clean_name(value.get("client_name", "Agent app")), redirects)
    if "error" in value:
        return json_response(value, 503)
    return json_response({"client_id": value["id"], "client_name": value["name"], "redirect_uris": redirects,
                          "token_endpoint_auth_method": "none", "grant_types": ["authorization_code", "refresh_token"],
                          "response_types": ["code"], "scope": "agent"}, 201)


async def authorize(request):
    ctx = request.app.state.ctx
    base, q = issuer(ctx), request.query_params
    if len(str(request.url)) > 8192:
        return error_page()
    async with ctx.pool.connection() as conn:
        client = await one(conn, "SELECT * FROM agent_oauth_clients WHERE id=%s", (q.get("client_id", ""),))
    uri = q.get("redirect_uri", "")
    if (not client or not trusted_redirect(ctx, uri)
            or not any(redirect_matches(u, uri) for u in client["redirect_uris"])):
        return error_page()
    params = {"state": q["state"]} if "state" in q else {}
    error = None
    if len(q.multi_items()) != len(q):
        error = "invalid_request"
    elif "response_type" not in q:
        error = "invalid_request"
    elif q.get("response_type") != "code":
        error = "unsupported_response_type"
    elif q.get("code_challenge_method") != "S256" or not PKCE.fullmatch(q.get("code_challenge", "")):
        error = "invalid_request"
    elif not resource_value(base, q.get("resource", "")):
        error = "invalid_target"
    elif not scope_allowed(q.get("scope", "")):
        error = "invalid_scope"
    if error:
        return redirect(uri, {**params, "error": error}, base)
    async with ctx.pool.connection() as conn:
        value = await ledger.request(conn, client["id"], uri, resource_value(base, q["resource"]), q.get("state"), q["code_challenge"])
    if "error" in value:
        return redirect(uri, {**params, **value}, base)
    host = urlsplit(uri).hostname
    warning = "<p>This address returns to a local app. Check that you started this sign-in.</p>" if valid_redirect(uri) else ""
    response = page(
        '<h1>Connect to Illo</h1><p>App name (not verified): ' + html.escape(client["name"])
        + '</p><p>Return address: <strong>' + html.escape(host) + '</strong></p>' + warning
        + '<p>Send this message to Illo in a Slack direct message:</p><pre id="message">connect '
        + value["code"] + '</pre><button id="copy" type="button">Copy</button>'
        + '<p id="status">Wait for your approval in Slack.</p>',
        script="""
document.getElementById('copy').addEventListener('click', async () => {
  try { await navigator.clipboard.writeText(document.getElementById('message').textContent); }
  catch { document.getElementById('status').textContent = 'Select and copy the message above.'; }
});
async function poll() {
  try {
    const reply = await fetch('/oauth/agent/status', {cache: 'no-store'});
    if (!reply.ok) throw new Error();
    const value = await reply.json();
    if (value.status !== 'pending') { location.replace('/oauth/agent/continue'); return; }
  } catch { document.getElementById('status').textContent = 'Illo cannot check the sign-in yet. Please wait.'; }
  setTimeout(poll, 2000);
}
setTimeout(poll, 2000);
""")
    response.set_cookie(COOKIE, value["handle"], max_age=1200, secure=True, httponly=True, samesite="lax", path="/")
    return response


async def status(request):
    async with request.app.state.ctx.pool.connection() as conn:
        row = await ledger.browser_request(conn, request.cookies.get(COOKIE, ""))
    if not row or row["continued_at"]:
        return json_response({"error": "invalid_request"}, 400)
    return json_response({"status": "expired" if row["expired"] else row["decision"]})


async def continue_signin(request):
    ctx = request.app.state.ctx
    async with ctx.pool.connection() as conn:
        result = await ledger.continue_request(conn, request.cookies.get(COOKIE, ""))
    if not result:
        return error_page()
    response = redirect(*result, issuer(ctx))
    response.delete_cookie(COOKIE, secure=True, httponly=True, samesite="lax", path="/")
    return response


async def token(request):
    ctx = request.app.state.ctx
    try:
        form = await body(request, "application/x-www-form-urlencoded")
    except (ValueError, UnicodeError):
        async with ctx.pool.connection() as conn:
            await ledger.limit(conn, "token", "all", ledger.TOKEN_FAILURES, seconds=60)
        return json_response({"error": "invalid_request"}, 400)
    if "resource" in form:
        form["resource"] = resource_value(issuer(ctx), form["resource"]) or ""
    async with ctx.pool.connection() as conn:
        # All failures share one counter. Unknown client IDs cannot grow its keys.
        await conn.execute("SELECT pg_advisory_xact_lock(697637152)")
        failures = await one(conn, """SELECT attempts FROM agent_oauth_limits WHERE kind='token'
            AND key='all' AND started_at>clock_timestamp()-interval '1 minute'""")
        if failures and failures["attempts"] >= ledger.TOKEN_FAILURES:
            result = {"error": "invalid_grant"}
        elif not scope_allowed(form.get("scope", "")):
            result = {"error": "invalid_scope"}
        elif form.get("client_secret") or request.headers.get("authorization"):
            result = {"error": "invalid_client"}
        elif form.get("grant_type") == "authorization_code":
            if not VERIFIER.fullmatch(form.get("code_verifier", "")):
                result = {"error": "invalid_grant"}
            else:
                result = await ledger.exchange(conn, form)
        elif form.get("grant_type") == "refresh_token":
            result = await ledger.refresh(conn, form)
        else:
            result = {"error": "unsupported_grant_type"}
        if "error" in result:
            await ledger.limit(conn, "token", "all", ledger.TOKEN_FAILURES, seconds=60)
    return json_response(result, 400 if "error" in result else 200)


def guarded(handler):
    async def serve(request: Request):
        if not issuer(request.app.state.ctx):
            raise HTTPException(404, "Not found")
        try:
            async with asyncio.timeout(5):
                return await handler(request)
        except (TimeoutError, PoolTimeout, Error):
            return json_response({"error": "temporarily_unavailable"}, 503)
    serve.__name__ = handler.__name__
    return serve


async def setup(ctx):
    from illo.channels.slack.agent_oauth import action

    for path in ("", "/mcp", "/api/mcp"):
        ctx.app.add_api_route("/.well-known/oauth-protected-resource" + path, guarded(protected_resource), methods=["GET"])
    ctx.app.add_api_route("/.well-known/oauth-authorization-server", guarded(authorization_server), methods=["GET"])
    for name, handler, method in (("register", register, "POST"), ("authorize", authorize, "GET"),
                                  ("status", status, "GET"), ("continue", continue_signin, "GET"), ("token", token, "POST")):
        ctx.app.add_api_route("/oauth/agent/" + name, guarded(handler), methods=[method])
    ctx.slack_actions.register("agent-oauth:", action)

    async def clean():
        async with ctx.pool.connection() as conn:
            await ledger.cleanup(conn)
    ctx.every(3600, clean)
