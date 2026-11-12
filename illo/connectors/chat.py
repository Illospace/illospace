"""Deterministic connection flows. Only private forms accept keys."""

import re
from urllib.parse import parse_qs, urlsplit

from illo.asks import cards
from illo.channels.slack.modals import open_form
from illo.connectors import files, google, oauth, store
from illo.connectors.config import Connector, ConnectorTool
from illo.connectors.http import HTTPFailure, private_http_logs
from illo.db import one
from illo.ledger import audit, drafts, kv
from illo.vault import delete

GOOGLE_READY = "Google is ready. Each person writes: connect my Google."
GOOGLE_TEAM_READY = "Google is ready for the team. Each person writes: connect my Google."
PERSONAL_PHRASES = {
    "connect my google",
    "connect my google account",
    "connect my calendar",
    "connect my mail",
    "connect my gmail",
    "connect my drive",
}


def setup_text(ctx):
    return (
        "Set up Google one time for the whole team. Google has no other way to make this client.\n"
        "1. Open https://console.cloud.google.com/projectcreate. Enter a project name and click Create. "
        "Select that project at the top of the next pages.\n"
        "2. Open https://console.cloud.google.com/auth/overview and click Get started. "
        "App name: Illo. Audience: Internal. Select your support email and enter a contact email. "
        "Accept the terms and click Create.\n"
        "3. Turn on these three services. Open each link and click Enable:\n"
        "https://console.cloud.google.com/apis/library/gmail.googleapis.com\n"
        "https://console.cloud.google.com/apis/library/calendar-json.googleapis.com\n"
        "https://console.cloud.google.com/apis/library/drive.googleapis.com\n"
        "4. Open https://console.cloud.google.com/auth/clients/create. "
        "Type: Web application. Name: Illo. Under Authorized redirect URIs "
        "(the address where Google sends you after sign-in), click Add URI and paste "
        f"`{oauth.callback_url(ctx)}`. Click Create. Keep the Client ID and Client secret from that page.\n"
        "5. Tap the button below and paste the two values. Use only the form, never a chat message."
    )


async def dm(ctx, person, text, blocks=None):
    if not ctx.slack_client or not person.get("slack_id"):
        raise ValueError("Slack is not connected for this person")
    opened = await ctx.slack_client.conversations_open(users=person["slack_id"])
    return await ctx.slack_client.chat_postMessage(
        channel=opened["channel"]["id"],
        text=text,
        blocks=blocks,
        unfurl_links=False,
        unfurl_media=False,
    )


async def active_person(conn, person_id, *, owner=False):
    person = await one(conn, "SELECT * FROM people WHERE id=%s AND active", (person_id,))
    if not person or (owner and person["role"] != "owner"):
        raise ValueError("Only an active owner can change team connections.")
    return person


async def owner_notice(conn):
    owner = await one(
        conn, "SELECT name FROM people WHERE role='owner' AND active ORDER BY key LIMIT 1"
    )
    return f"{owner['name'] if owner else 'The owner'} must set up Google one time. Then write: connect my Google."


async def client_ready(ctx, conn):
    try:
        await google.credentials(ctx, conn)
        return True
    except ValueError:
        return False


async def google_setup(ctx, conn, person, *, send=True):
    if person["role"] != "owner":
        text = await owner_notice(conn)
        if send:
            await dm(ctx, person, text)
        return text
    ready = await client_ready(ctx, conn)
    if ready and not any(c.key == "google" for c in ctx.workspace.connectors):
        await files.save(
            ctx, conn, Connector(key="google", kind="google", scope="personal"), create=True
        )
    text = GOOGLE_TEAM_READY if ready else setup_text(ctx)
    if ready:
        previous = await kv.get(conn, "connect:google:callback")
        if previous != oauth.callback_url(ctx):
            text += (
                "\nIn https://console.cloud.google.com/auth/clients, open the Illo client. "
                "Add this callback address (where Google sends you after sign-in): "
                f"`{oauth.callback_url(ctx)}`."
            )
    blocks = [
        cards.section(text),
        {
            "type": "actions",
            "elements": [
                cards.button(
                    "Replace the Google client" if ready else "Enter the Google client",
                    "connect:google:form",
                )
            ],
        },
    ]
    await dm(ctx, person, text, blocks)
    if not send:
        return ""
    return (
        "The Google setup steps are in the DM." if not ready else "The Google state is in the DM."
    )


async def start(ctx, conn, person_id, kind, scope, *, send=True):
    person = await active_person(conn, person_id)
    if kind == "google":
        if scope == "team" or not await client_ready(ctx, conn):
            return await google_setup(ctx, conn, person, send=send)
        if not google.google_config(ctx):
            if person["role"] != "owner":
                text = await owner_notice(conn)
                if send:
                    await dm(ctx, person, text)
                    return "The owner must set up Google first."
                return text
            await files.save(
                ctx, conn, Connector(key="google", kind="google", scope="personal"), create=True
            )
        link = await oauth.issue(ctx, person_id, conn=conn)
        if urlsplit(oauth.public_url(ctx)).hostname in {"localhost", "127.0.0.1", "::1"}:
            state = parse_qs(urlsplit(link).query)["state"][0]
            link = await oauth.start(ctx, state, conn=conn)
            text = (
                "Open this link to connect Google: "
                + link
                + "\nThe final local address can fail to open. Paste that full address here."
            )
        else:
            text = "Open this link to connect your Google account: " + link
        if send:
            await dm(ctx, person, text)
            return "The sign-in link is in the DM."
        return text
    config = next((c for c in ctx.workspace.connectors if c.key == kind and c.scope == scope), None)
    text = (
        "This team connector is configured. Ask the owner to check its state."
        if config
        else "This connector is not configured. Ask the owner to add it."
    )
    await dm(ctx, person, text)
    return text


async def google_button(ctx, person, action):
    if not (
        person["active"]
        and person["role"] == "owner"
        and (action.get("channel") or "").startswith("D")
    ):
        return
    async with ctx.pool.connection() as conn:
        await active_person(conn, person["id"], owner=True)
    await open_form(
        ctx,
        person,
        action["trigger_id"],
        "google",
        [("client_id", "Client ID"), ("secret", "Client secret")],
    )


def form_errors(kind, values):
    errors = {}
    if kind == "google" and not re.fullmatch(
        r"[0-9]+-[A-Za-z0-9_-]+\.apps\.googleusercontent\.com", values.get("client_id", "")
    ):
        errors["client_id"] = "Use the full Client ID from the Google page."
    if not values.get("secret") or any(c.isspace() for c in values["secret"]):
        errors["secret"] = "Enter a value without spaces."
    return errors


async def submit_form(ctx, person, form, values):
    async with ctx.pool.connection() as conn:
        person = await active_person(conn, person["id"], owner=True)
        if form["kind"] == "google":
            try:
                with private_http_logs():
                    await ctx.google_client.http.request(
                        "POST",
                        google.TOKEN_URL,
                        data={
                            "grant_type": "authorization_code",
                            "code": "illo-invalid-check-code",
                            "client_id": values["client_id"],
                            "client_secret": values["secret"],
                            "redirect_uri": oauth.callback_url(ctx),
                        },
                    )
            except HTTPFailure as exc:
                if exc.error != "invalid_grant":
                    await audit_change(conn, person, "connect_google_client", decision="deny")
                    await dm(
                        ctx,
                        person,
                        "Google did not accept this client. Check both values and open the form again.",
                    )
                    return
            else:
                await dm(
                    ctx, person, "Google did not confirm this client. Open the form to try again."
                )
                return
            await store.vault(ctx).set(conn, "google-client-id", values["client_id"])
            await store.vault(ctx).set(conn, "google-client-secret", values["secret"])
            await kv.set(conn, "connect:google:callback", oauth.callback_url(ctx))
            await audit_change(conn, person, "connect_google_client")
            if not any(c.key == "google" for c in ctx.workspace.connectors):
                await files.save(
                    ctx, conn, Connector(key="google", kind="google", scope="personal"), create=True
                )
            text = GOOGLE_READY
        else:
            draft = await one(
                conn, "SELECT * FROM drafts WHERE id=%s FOR UPDATE", (form["draft_id"],)
            )
            if (
                not draft
                or draft["state"] != "approved"
                or draft["kind"] != "connect_propose_mcp"
                or draft["approver_person_id"] != person["id"]
            ):
                return
            text = await add_mcp(
                ctx, conn, person, draft["content"]["arguments"], bearer=values["secret"]
            )
            await drafts.mark_sent(conn, draft["id"])
    await dm(ctx, person, text)


async def audit_change(conn, person, tool, *, decision="allow"):
    await audit.write(
        conn,
        principal="person:" + person["key"],
        tool=tool,
        args={},
        decision=decision,
        reason="The owner used the private form or approval card.",
    )


def mcp_config(args):
    return Connector(
        key=args["key"], kind="mcp", scope="team", url=args["url"], description=args["description"]
    )


def tools_text(found):
    lines = [
        cards.visible_escape(name)
        + ": "
        + cards.visible_escape(re.split(r"(?<=[.!?])\s|\n", text)[0][:300])
        for name, text in list(found.items())[:30]
    ]
    if len(found) > 30:
        lines.append(f"and {len(found) - 30} more")
    return "\n".join(lines)


async def add_mcp(ctx, conn, person, args, *, bearer=None):
    config = mcp_config(args)
    if any(c.key == config.key for c in ctx.workspace.connectors):
        return "This connector already exists. Remove it before you add a replacement."
    try:
        with private_http_logs():
            found = await ctx.mcp_client.discover(ctx, conn, config, bearer=bearer)
    except Exception:
        return "Illo could not read the tool list. Check the address and sign-in. No connector was added."
    if bearer and any(bearer in str(value) for pair in found.items() for value in pair):
        return "The server returned a key in its tool list. No connector was added."
    config.discovered_tools = found
    # Save a key only after the tool list succeeds. It never enters the draft.
    if args["auth"] == "bearer":
        from illo.connectors.config import Auth

        config.auth = Auth(type="bearer", secret="connector-" + config.key)
        await store.vault(ctx).set(conn, config.auth.secret, bearer)
    await files.save(ctx, conn, config, create=True)
    await audit_change(conn, person, "connect_add_mcp")
    return f"The connector `{config.key}` is added. Tools found:\n{tools_text(found)}\nTell me which tools to allow."


def preview(kind, args):
    key = cards.visible_escape(args["key"])
    if kind == "connect_propose_mcp":
        auth = "none" if args["auth"] == "none" else "a key that you enter in a form"
        return (
            f"Add the connector `{key}`? Address: {cards.visible_escape(args['url'])}. Sign-in: {auth}.",
            "Add",
            "Do not add",
        )
    if kind == "connect_allow_tools":
        read = (
            ", ".join(cards.visible_escape(t["name"]) for t in args["tools"] if t["risk"] == "read")
            or "none"
        )
        write = (
            ", ".join(
                cards.visible_escape(t["name"]) for t in args["tools"] if t["risk"] == "write"
            )
            or "none"
        )
        return (
            f"Allow these tools of `{key}`? Read: {read}. "
            f"Write (each call needs your approval card): {write}.",
            "Allow",
            "Do not allow",
        )
    return f"Remove the connector `{key}` and its saved key?", "Remove", "Do not remove"


async def propose(tc, args, kind):
    person = await active_person(tc.conn, tc.run.get("person_id"), owner=True)
    if kind != "connect_propose_mcp":
        config = find_config(tc.app, args["key"])
        if kind == "connect_allow_tools":
            if any(t["name"] not in config.discovered_tools for t in args["tools"]):
                raise ValueError("The server did not list this tool.")
    elif any(c.key == args["key"] for c in tc.app.workspace.connectors):
        raise ValueError("This connector already exists.")
    row = await drafts.create(
        tc.conn,
        run_id=tc.run["id"],
        approver_person_id=person["id"],
        kind=kind,
        destination="ledger",
        content={"arguments": args},
    )
    from illo.asks.drafts import sync_card

    await sync_card(tc.app, tc.conn, row)
    return {"draft_id": row["id"], "state": "pending", "message": "The approval card is in the DM."}


def find_config(ctx, key):
    config = next(
        (
            c
            for c in ctx.workspace.connectors
            if c.key == key and c.kind == "mcp" and c.scope == "team"
        ),
        None,
    )
    if not config:
        raise ValueError("The team MCP connector was not found.")
    return config


async def open_mcp_form(ctx, person, draft, trigger_id):
    await open_form(
        ctx, person, trigger_id, "mcp", [("secret", "Server key")], draft_id=draft["id"]
    )


async def perform(ctx, draft):
    async with ctx.pool.connection() as conn:
        person = await active_person(conn, draft["approver_person_id"], owner=True)
        stored = await one(conn, "SELECT * FROM drafts WHERE id=%s FOR UPDATE", (draft["id"],))
        if not stored or stored["state"] != "approved":
            return
        args = stored["content"]["arguments"]
        if stored["kind"] == "connect_propose_mcp":
            if args["auth"] == "bearer":
                raise ValueError("Use the private form for the server key")
            text = await add_mcp(ctx, conn, person, args)
        else:
            config = find_config(ctx, args["key"])
            if stored["kind"] == "connect_allow_tools":
                if any(t["name"] not in config.discovered_tools for t in args["tools"]):
                    raise ValueError("The server did not list this tool")
                allowed = {t.name: t for t in config.tools}
                for tool in args["tools"]:
                    allowed[tool["name"]] = ConnectorTool(
                        name=tool["name"], risk="read" if tool["risk"] == "read" else "send_outside"
                    )
                config = config.model_copy(update={"tools": list(allowed.values())})
                await files.save(ctx, conn, config)
                text = "The tool permissions are saved. Each write needs your approval card."
            else:
                await files.remove(ctx, conn, config)
                if config.auth and config.auth.secret:
                    await delete(conn, config.auth.secret)
                await conn.execute(
                    "DELETE FROM connector_grants WHERE connector_key=%s", (config.key,)
                )
                text = "The connector and its saved key are removed."
            await audit_change(conn, person, stored["kind"])
    await dm(ctx, person, text)


async def setup(ctx):
    ctx.slack_actions.register("connect:google:", google_button)
    for kind in ("connect_propose_mcp", "connect_allow_tools", "connect_remove"):
        ctx.draft_performers[kind] = perform

    async def expire_forms():
        async with ctx.pool.connection() as conn:
            await conn.execute(
                "DELETE FROM kv WHERE key LIKE 'connect:form:%' AND (value->>'expires')::timestamptz<clock_timestamp()"
            )

    ctx.every(300, expire_forms)
