"""Browser MCP sign-in uses the shared state ledger and private forms."""

import hashlib
import json
import secrets

from illo.asks import cards
from illo.connectors import discovery, files, oauth, store
from illo.connectors.addresses import public_address
from illo.connectors.config import Auth, Connector
from illo.connectors.http import private_http_logs
from illo.db import all_rows, one
from illo.ledger import kv, sensors

FAILURE = "The sign-in did not finish. Ask Illo for a new link."
COOKIE = "__Host-illo-mcp"


def callback_url(ctx):
    return oauth.public_url(ctx) + "/oauth/mcp/callback"


def browser_ready(ctx):
    try:
        url = public_address(oauth.public_url(ctx))
        return "." in url.host or ":" in url.host
    except ValueError:
        return False


def fallback(key):
    return f"Browser sign-in needs a public HTTPS address. On the host, run `illo connector login {key}`."


def pending_key(draft_id):
    return f"connect:mcp:proposal:{draft_id}"


async def save_proposal(ctx, conn, draft_id, info):
    await kv.set(conn, pending_key(draft_id), store.vault(ctx).encrypt(json.dumps(info)).decode())


async def load_proposal(ctx, conn, draft_id):
    raw = await kv.get(conn, pending_key(draft_id))
    if not raw:
        raise ValueError("The connection expired. Ask Illo to add it again.")
    return json.loads(store.vault(ctx).decrypt(raw.encode()))


def link_blocks(text, link):
    return [
        cards.section(text),
        {
            "type": "actions",
            "elements": [
                {"type": "button", "text": {"type": "plain_text", "text": "Sign in"}, "url": link}
            ],
        },
    ]


async def issue_link(ctx, conn, person, config, info, *, client_secret=None, create=False):
    expected = config.model_dump()
    config = config.model_copy(deep=True)
    config.auth.redirect_uri = callback_url(ctx)
    # The configured scopes can narrow the resource's list. Metadata grants no tool.
    supported = info.get("scopes_supported", [])
    if config.auth.scopes and supported and not set(config.auth.scopes) <= set(supported):
        raise ValueError("The connector asks for an unsupported scope")
    if not config.auth.scopes:
        config.auth.scopes = supported
    if client_secret is None and config.auth.client_secret:
        client_secret = await store.vault(ctx).get(conn, config.auth.client_secret)
    if client_secret:
        ctx.mcp_client.check_private(config.model_dump(), [client_secret])
        ctx.mcp_client.check_private(info, [client_secret])
    return await oauth.issue_state(
        ctx,
        conn,
        config.key,
        person["id"],
        {
            "verifier": secrets.token_urlsafe(48),
            "config": config.model_dump(),
            "info": info,
            "client_secret": client_secret,
            "create": create,
            "expected_config": expected,
        },
        provider="mcp",
    )


async def issue(ctx, conn, person, config, info, *, client_secret=None, create=False):
    from illo.connectors import chat

    await chat.active_person(conn, person["id"], owner=True)
    if not browser_ready(ctx):
        return fallback(config.key)
    link = await issue_link(
        ctx, conn, person, config, info, client_secret=client_secret, create=create
    )
    text = f"Open this link to sign in to {config.key}. The link works one time, for 10 minutes."
    await chat.dm(ctx, person, text, link_blocks(text, link))
    return "The sign-in link is in the DM."


async def after_add(ctx, conn, person, draft):
    from illo.connectors import chat

    args = draft["content"]["arguments"]
    if not browser_ready(ctx):
        return fallback(args["key"])
    info = await load_proposal(ctx, conn, draft["id"])
    if not info.get("registration_endpoint"):
        text = f"Enter the Client ID for {args['key']}. Use only the private form."
        await chat.dm(
            ctx,
            person,
            text,
            [
                cards.section(text),
                {
                    "type": "actions",
                    "elements": [
                        cards.button("Enter the client", f"connect:mcp:client:{draft['id']}")
                    ],
                },
            ],
        )
        return "The client form button is in the DM."
    registered = await discovery.register(ctx.mcp_client.http, info, callback_url(ctx))
    return await with_client(
        ctx,
        conn,
        person,
        draft,
        registered["client_id"],
        registered.get("client_secret"),
        auth_method=registered.get("token_endpoint_auth_method", "none"),
    )


async def with_client(ctx, conn, person, draft, client_id, client_secret, *, auth_method=None):
    from illo.connectors.chat import mcp_config

    config = mcp_config(draft["content"]["arguments"])
    info = await load_proposal(ctx, conn, draft["id"])
    if auth_method is not None:
        info["token_endpoint_auth_method"] = auth_method
    discovery.token_auth_method(info, client_secret)
    config.auth = Auth(
        type="oauth",
        client_id=client_id,
        authorization_url=info["authorization_endpoint"],
        token_url=info["token_endpoint"],
        scopes=info.get("scopes_supported", []),
        redirect_uri=callback_url(ctx),
    )
    return await issue(ctx, conn, person, config, info, client_secret=client_secret, create=True)


async def start(ctx, state, *, browser_nonce=None):
    from illo.connectors.chat import active_person

    async with ctx.pool.connection() as conn:
        row, details = await oauth.open_state(ctx, conn, state, browser_nonce, provider="mcp")
        await active_person(conn, row["person_id"], owner=True)
    config = Connector.model_validate(details["config"])
    if config.key != row["connector_key"] or config.auth.redirect_uri != callback_url(ctx):
        raise ValueError("The connector changed. Ask Illo for a new link.")
    return discovery.authorization_url(details["info"], config, state, details["verifier"])


async def complete(ctx, state, code, *, browser_nonce=None):
    from illo.connectors import chat

    if not code:
        raise ValueError("The sign-in code is missing")
    # Commit consumption before any network call, including a failed exchange.
    async with ctx.pool.connection() as conn:
        row, details = await oauth.consume_state(
            ctx, conn, state, None, browser_nonce, provider="mcp"
        )
        await chat.active_person(conn, row["person_id"], owner=True)
    config = Connector.model_validate(details["config"])
    if config.key != row["connector_key"] or config.auth.redirect_uri != callback_url(ctx):
        raise ValueError("The connector changed. Ask Illo for a new link.")
    info = details["info"]
    data = {
        "grant_type": "authorization_code",
        "client_id": config.auth.client_id,
        "redirect_uri": config.auth.redirect_uri,
        "code": code,
        "code_verifier": details["verifier"],
        "resource": info.get("resource", config.url),
    }
    token = await discovery.token_request(
        ctx.mcp_client.http, info, data, details.get("client_secret")
    )
    if (
        not isinstance(token, dict)
        or not isinstance(token.get("access_token"), str)
        or not token["access_token"]
    ):
        raise ValueError("The server did not provide an access token")
    scopes = token.get("scope", " ".join(config.auth.scopes)).split()
    if not set(scopes) <= set(config.auth.scopes):
        raise ValueError("The server returned a scope that Illo does not allow")
    async with ctx.pool.connection() as conn:
        person = await chat.active_person(conn, row["person_id"], owner=True)
        existing = next((c for c in ctx.workspace.connectors if c.key == config.key), None)
        if details["create"] and existing:
            raise ValueError("This connector already exists")
        if not details["create"] and (
            not existing or existing.model_dump() != details["expected_config"]
        ):
            raise ValueError("The connector changed. Ask Illo for a new link.")
        with private_http_logs():
            found = await ctx.mcp_client.discover(ctx, conn, config, bearer=token["access_token"])
        # A hostile catalog must not echo credentials into the workspace or DM.
        private = [
            state,
            code,
            details["verifier"],
            details.get("client_secret"),
            token.get("access_token"),
            token.get("refresh_token"),
        ]
        ctx.mcp_client.check_private(
            found, [value for value in private if isinstance(value, str) and value]
        )
        person = await chat.active_person(conn, row["person_id"], owner=True, lock=True)
        token.update(
            token_url=info["token_endpoint"],
            client_id=config.auth.client_id,
            resource=info.get("resource", config.url),
            token_auth_method=discovery.token_auth_method(info, details.get("client_secret")),
            signed_in_by=person["id"],
        )
        if details.get("client_secret"):
            config.auth.client_secret = "connector-" + config.key + "-client-secret"
            await store.vault(ctx).set(conn, config.auth.client_secret, details["client_secret"])
        if token.get("expires_in") is not None:
            if (
                isinstance(token["expires_in"], bool)
                or not 0 < int(token["expires_in"]) <= 315360000
            ):
                raise ValueError("The token expiry is invalid")
        config.discovered_tools = found
        ctx.mcp_client.check_private(
            config.model_dump(), [value for value in private if isinstance(value, str) and value]
        )
        # Re-sign-in keeps the current permissions. Only the owner can change them.
        await files.save(
            ctx,
            conn,
            config,
            create=details["create"],
            expected=None
            if details["create"]
            else Connector.model_validate(details["expected_config"]),
        )
        await store.save(conn, ctx, config.key, None, token, scopes)
        await sensors.ok(conn, "connector:" + config.key)
        await chat.audit_change(
            conn, person, "connect_add_mcp" if details["create"] else "connect_sign_in"
        )
    text = f"{config.key} is connected for the team."
    if details["create"]:
        text += " Name the tools to allow.\n" + chat.tools_text(found)
    await chat.dm(ctx, person, text)
    return f"{config.key} is connected. Go back to Slack."


async def reconnect_info(ctx, conn, config):
    info = await discovery.metadata(ctx.mcp_client.http, config)
    try:
        _, token = await store.load(conn, ctx, config.key, None)
    except ValueError:
        return info
    info["resource"] = token.get("resource", config.url)
    if token.get("token_auth_method"):
        info["token_endpoint_auth_method"] = token["token_auth_method"]
    return info


async def reconnect(ctx, conn, person, config):
    from illo.connectors.chat import active_person

    await active_person(conn, person["id"], owner=True)
    if not browser_ready(ctx):
        return fallback(config.key)
    info = await reconnect_info(ctx, conn, config)
    return await issue(ctx, conn, person, config, info)


async def claim(ctx, event, authority):
    if not oauth.person_dm(event, authority):
        return None
    import re

    text = event.text.strip().lower().rstrip(".!?")
    match = re.fullmatch(
        r"(?:connect ([a-z][a-z0-9_-]*) again|sign in to ([a-z][a-z0-9_-]*))", text
    )
    if not match:
        return None
    from illo.connectors import chat

    try:
        async with ctx.pool.connection() as conn:
            person = await chat.active_person(conn, authority.person_id, owner=True)
            config = chat.find_config(ctx, match[1] or match[2])
            if not config.auth or config.auth.type != "oauth":
                return "This connector does not use browser sign-in."
            return await reconnect(ctx, conn, person, config)
    except Exception:
        return FAILURE


async def notify_ended(ctx, conn, key=None):
    from illo.connectors import chat

    rows = await all_rows(
        conn,
        """SELECT * FROM connector_grants WHERE person_id IS NULL
        AND broken_at IS NOT NULL AND notified_at IS NULL
        AND (%s::text IS NULL OR connector_key=%s) ORDER BY id LIMIT 30 FOR UPDATE SKIP LOCKED""",
        (key, key),
    )
    for row in rows:
        config = next(
            (
                c
                for c in ctx.workspace.connectors
                if c.key == row["connector_key"]
                and c.kind == "mcp"
                and c.auth
                and c.auth.type == "oauth"
            ),
            None,
        )
        if not config:
            continue
        token = json.loads(store.vault(ctx).decrypt(row["secret"]))
        person = await one(
            conn,
            "SELECT * FROM people WHERE id=%s AND active AND role='owner'",
            (token.get("signed_in_by"),),
        )
        if not person:
            continue
        # Claim before delivery. A repeated call cannot send a second DM for this grant.
        await conn.execute(
            "UPDATE connector_grants SET notified_at=clock_timestamp() WHERE id=%s", (row["id"],)
        )
        message_key = f"mcp-ended:{row['id']}:{hashlib.sha256(bytes(row['secret'])).hexdigest()}"
        if browser_ready(ctx):
            info = await reconnect_info(ctx, conn, config)
            link = await issue_link(ctx, conn, person, config, info)
            text = f"Your sign-in to {config.key} ended. Sign in again to continue."
            await chat.dm(ctx, person, text, link_blocks(text, link), message_key=message_key)
        else:
            await chat.dm(ctx, person, fallback(config.key), message_key=message_key)


async def setup(ctx):
    oauth.add_browser_routes(ctx, "mcp", start, complete, COOKIE, failure=FAILURE)
    ctx.gateway.register_claim(claim)

    # An OAuth URL pasted into chat must not reach the brain or the event ledger.
    def redact(payload):
        for route in ("start", "callback"):
            payload = oauth.redact_payload(
                payload,
                oauth.public_url(ctx) + "/oauth/mcp/" + route,
                "[MCP sign-in address removed]",
            )
        return payload

    async def prepare(ctx, event, authority):
        return event.model_copy(
            update={"text": redact(event.text), "payload": redact(event.payload)}
        )

    ctx.gateway.register_preparer(prepare)
    ctx.gateway.register_payload_filter(redact)

    async def notices():
        async with ctx.pool.connection() as conn:
            await notify_ended(ctx, conn)

    ctx.every(60, notices)
