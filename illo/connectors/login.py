"""Host commands for connector status and MCP OAuth sign-in."""

import getpass
import secrets
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

from illo.connectors import store
from illo.connectors.discovery import (
    authorization_url,
    metadata,
    register,
    token_auth_method,
    token_request,
)
from illo.connectors.http import BoundedHTTP
from illo.workspace import load_workspace


async def login(conn, ctx, config, *, http=None, prompt=None) -> None:
    http = http or BoundedHTTP(public=True)
    prompt = prompt or getpass.getpass
    auth = config.auth
    if config.kind != "mcp" or not auth or auth.type != "oauth":
        raise ValueError("This connector does not use MCP OAuth")
    info = await metadata(http, config, probe=True)
    client_id = auth.client_id
    client_secret = (
        await store.vault(ctx).get(conn, auth.client_secret) if auth.client_secret else None
    )
    if not client_id:
        endpoint = info.get("registration_endpoint")
        if not endpoint:
            raise ValueError("Set an OAuth client ID in the connector file")
        registered = await register(http, info, auth.redirect_uri)
        client_id = registered["client_id"]
        client_secret = registered.get("client_secret", client_secret)
        info["token_endpoint_auth_method"] = registered.get("token_endpoint_auth_method", "none")
    config = config.model_copy(deep=True)
    config.auth.client_id = client_id
    if not config.auth.scopes:
        config.auth.scopes = info.get("scopes_supported", [])
    state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
    url = authorization_url(info, config, state, verifier)
    print("Open this sign-in address:")
    print(url)
    pasted = prompt("Paste the final redirect address (hidden): ")
    parsed, expected = urlsplit(pasted), urlsplit(auth.redirect_uri)
    if (parsed.scheme, parsed.netloc, parsed.path) != (
        expected.scheme,
        expected.netloc,
        expected.path,
    ):
        raise ValueError("The redirect address does not match")
    query = parse_qs(parsed.query)
    if len(query.get("state", [])) != 1 or not secrets.compare_digest(query["state"][0], state):
        raise ValueError("The OAuth state does not match")
    if len(query.get("code", [])) != 1:
        raise ValueError("The OAuth code is missing")
    data = {
        "grant_type": "authorization_code",
        "client_id": client_id,
        "redirect_uri": auth.redirect_uri,
        "code": query["code"][0],
        "code_verifier": verifier,
        "resource": info.get("resource", config.url),
    }
    token = await token_request(http, info, data, client_secret)
    if not token.get("access_token"):
        raise ValueError("The OAuth server did not return a token")
    token.update(
        token_url=info["token_endpoint"],
        client_id=client_id,
        resource=info.get("resource", config.url),
        token_auth_method=token_auth_method(info, client_secret),
    )
    if client_secret and not auth.client_secret:
        token["client_secret"] = client_secret
    await store.save(
        conn, ctx, config.key, None, token, token.get("scope", " ".join(config.auth.scopes)).split()
    )
    print("The connector is connected.")


async def execute(conn, args, config) -> None:
    ctx = SimpleNamespace(config=config, workspace=load_workspace(config.workspace_dir))
    if args.command == "connectors":
        for row in await store.status(conn):
            owner = row["person"] or "team"
            state = "revoked" if row["revoked_at"] else "ended" if row["broken_at"] else "connected"
            print(f"{row['connector_key']}  {owner}  {state}  expiry {row['expires_at'] or 'none'}")
        return
    connector = next((c for c in ctx.workspace.connectors if c.key == args.name), None)
    if not connector:
        raise ValueError("The connector is not configured")
    if (
        connector.kind == "google"
        and connector.scope == "team"
        and connector.team_grant
        and args.person
    ):
        await ctx.workspace.sync_people(conn)
        person = await one_person(conn, args.person)
        personal = next(
            (c for c in ctx.workspace.connectors if c.kind == "google" and c.scope == "personal"),
            None,
        )
        if not person or not personal:
            raise ValueError("Connect this person's Google account first")
        row, token = await store.load(conn, ctx, personal.key, person["id"])
        await store.save(conn, ctx, connector.key, None, token, row["scopes"])
        print("The team grant is connected. Reads stay inside the configured drive or folders.")
        return
    await login(conn, ctx, connector)


async def one_person(conn, key: str) -> dict | None:
    from illo.ledger.people import get

    return await get(conn, key)
