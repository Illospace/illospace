"""Host commands for connector status and MCP OAuth sign-in."""

import base64
import getpass
import hashlib
import secrets
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode, urlsplit

from illo.connectors import store
from illo.connectors.config import validate_url
from illo.connectors.http import BoundedHTTP
from illo.workspace import load_workspace


async def metadata(http, config) -> dict:
    auth = config.auth
    if auth.authorization_url and auth.token_url:
        return {"authorization_endpoint": auth.authorization_url, "token_endpoint": auth.token_url}
    parts = urlsplit(config.url)
    origin = f"{parts.scheme}://{parts.netloc}"
    resource_url = origin + "/.well-known/oauth-protected-resource" + parts.path.rstrip("/")
    try:
        resource = await http.request("GET", resource_url)
    except ValueError:
        resource = await http.request("GET", origin + "/.well-known/oauth-protected-resource")
    issuer = resource["authorization_servers"][0].rstrip("/")
    validate_url(issuer)
    issuer_parts = urlsplit(issuer)
    authorization_metadata = (
        f"{issuer_parts.scheme}://{issuer_parts.netloc}"
        + "/.well-known/oauth-authorization-server"
        + issuer_parts.path
    )
    result = await http.request("GET", authorization_metadata)
    if result.get("issuer", issuer).rstrip("/") != issuer:
        raise ValueError("The OAuth issuer does not match its metadata")
    for key in ("authorization_endpoint", "token_endpoint", "registration_endpoint"):
        if result.get(key):
            validate_url(result[key])
    return result


async def login(conn, ctx, config, *, http=None, prompt=None) -> None:
    http = http or BoundedHTTP()
    prompt = prompt or getpass.getpass
    auth = config.auth
    if config.kind != "mcp" or not auth or auth.type != "oauth":
        raise ValueError("This connector does not use MCP OAuth")
    info = await metadata(http, config)
    client_id = auth.client_id
    client_secret = (
        await store.vault(ctx).get(conn, auth.client_secret) if auth.client_secret else None
    )
    if not client_id:
        endpoint = info.get("registration_endpoint")
        if not endpoint:
            raise ValueError("Set an OAuth client ID in the connector file")
        registered = await http.request(
            "POST",
            endpoint,
            json={
                "client_name": "Illo",
                "redirect_uris": [auth.redirect_uri],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
            },
        )
        client_id = registered["client_id"]
    state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )
    url = (
        info["authorization_endpoint"]
        + "?"
        + urlencode(
            {
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": auth.redirect_uri,
                "scope": " ".join(auth.scopes),
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "resource": config.url,
            }
        )
    )
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
        "resource": config.url,
    }
    if client_secret:
        data["client_secret"] = client_secret
    token = await http.request("POST", info["token_endpoint"], data=data)
    if not token.get("access_token"):
        raise ValueError("The OAuth server did not return a token")
    token.update(token_url=info["token_endpoint"], client_id=client_id)
    await store.save(
        conn, ctx, config.key, None, token, token.get("scope", " ".join(auth.scopes)).split()
    )
    print("The connector is connected.")


async def execute(conn, args, config) -> None:
    ctx = SimpleNamespace(config=config, workspace=load_workspace(config.workspace_dir))
    if args.command == "connectors":
        for row in await store.status(conn):
            owner = row["person"] or "team"
            state = "revoked" if row["revoked_at"] else "connected"
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
