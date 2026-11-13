"""MCP authorization discovery shared by chat and the host command."""

import asyncio
import base64
import hashlib
import re
from urllib.parse import parse_qsl, quote_plus, urlencode, urlsplit, urlunsplit

import httpx

from illo.connectors.addresses import public_address
from illo.connectors.http import HTTPFailure


async def metadata(http, config, *, probe=False) -> dict:
    async with asyncio.timeout(config.timeout_s):
        return await _metadata(http, config, probe=probe)


async def _metadata(http, config, *, probe):
    auth = config.auth
    if auth and auth.authorization_url and auth.token_url:
        for value in (auth.authorization_url, auth.token_url):
            public_address(value)
        return {
            "authorization_endpoint": auth.authorization_url,
            "token_endpoint": auth.token_url,
            "resource": config.url,
            "scopes_supported": auth.scopes,
            "token_endpoint_auth_methods_supported": ["client_secret_post"],
        }
    parts = urlsplit(config.url)
    public_address(config.url)
    origin = f"{parts.scheme}://{parts.netloc}"
    candidates = []
    if probe:
        try:
            await http.request(
                "POST",
                config.url,
                max_bytes=config.max_bytes,
                headers={"Accept": "application/json, text/event-stream"},
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {},
                        "clientInfo": {"name": "Illo", "version": "1"},
                    },
                },
            )
        except HTTPFailure as exc:
            if exc.status == 401:
                match = re.search(
                    r'\bresource_metadata\s*=\s*"([^"]+)"',
                    exc.headers.get("www-authenticate", ""),
                    re.I,
                )
                if match:
                    candidates.append(match[1])
        # An unavailable server is not evidence that it needs no sign-in.
    candidates += [
        origin + "/.well-known/oauth-protected-resource" + parts.path.rstrip("/"),
        origin + "/.well-known/oauth-protected-resource",
    ]
    resource = None
    for url in dict.fromkeys(candidates):
        public_address(url)
        try:
            resource = await http.request("GET", url, max_bytes=config.max_bytes)
            break
        except HTTPFailure as exc:
            if exc.status not in {401, 403, 404, 405}:
                raise
    if not isinstance(resource, dict) or not resource.get("authorization_servers"):
        if probe:
            return {}
        raise ValueError("The server has no OAuth metadata")
    issuer = resource["authorization_servers"][0]
    public_address(issuer)
    issuer_parts = urlsplit(issuer)
    if issuer_parts.query:
        raise ValueError("The issuer must not have a query")
    root = f"{issuer_parts.scheme}://{issuer_parts.netloc}"
    path = issuer_parts.path.rstrip("/")
    candidates = [
        root + "/.well-known/oauth-authorization-server" + path,
        issuer.rstrip("/") + "/.well-known/oauth-authorization-server",
        issuer.rstrip("/") + "/.well-known/openid-configuration",
    ]
    result = None
    for url in dict.fromkeys(candidates):
        try:
            result = await http.request("GET", url, max_bytes=config.max_bytes)
            break
        except HTTPFailure as exc:
            if exc.status != 404:
                raise
    if not isinstance(result, dict) or result.get("issuer") != issuer:
        raise ValueError("The OAuth issuer does not match its metadata")
    for key in (
        "authorization_endpoint",
        "token_endpoint",
        "registration_endpoint",
        "revocation_endpoint",
    ):
        if result.get(key):
            public_address(result[key])
    if not result.get("authorization_endpoint") or not result.get("token_endpoint"):
        raise ValueError("The OAuth metadata is incomplete")
    resource_id = resource.get("resource", config.url)
    public_address(resource_id)
    # A metadata response cannot redirect the grant to a different resource.
    if resource_id.rstrip("/") != config.url.rstrip("/"):
        raise ValueError("The OAuth resource does not match the connector")
    scopes = resource.get("scopes_supported", [])
    if not isinstance(scopes, list) or any(
        not isinstance(s, str) or not s or any(c.isspace() for c in s) for s in scopes
    ):
        raise ValueError("The OAuth scopes are invalid")
    return {**result, "resource": resource_id, "scopes_supported": scopes}


async def register(http, info, redirect_uri):
    public_address(info["registration_endpoint"])
    registered = await http.request(
        "POST",
        info["registration_endpoint"],
        json={
            "client_name": "Illo",
            "redirect_uris": [redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        },
    )
    if (
        not isinstance(registered, dict)
        or not isinstance(registered.get("client_id"), str)
        or not registered["client_id"]
    ):
        raise ValueError("The server did not provide a client ID")
    if registered.get("client_secret") is not None and not isinstance(
        registered["client_secret"], str
    ):
        raise ValueError("The server returned an invalid client secret")
    return registered


def authorization_url(info, config, state, verifier):
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )
    endpoint = urlsplit(info["authorization_endpoint"])
    parameters = dict(parse_qsl(endpoint.query))
    parameters.update(
        {
            "response_type": "code",
            "client_id": config.auth.client_id,
            "redirect_uri": config.auth.redirect_uri,
            "scope": " ".join(config.auth.scopes),
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "resource": info.get("resource", config.url),
        }
    )
    return urlunsplit(endpoint._replace(query=urlencode(parameters)))


def token_auth_method(info, client_secret):
    method = info.get("token_endpoint_auth_method")
    if not method:
        supported = info.get("token_endpoint_auth_methods_supported", ["client_secret_basic"])
        method = (
            ("client_secret_post" if "client_secret_post" in supported else "client_secret_basic")
            if client_secret
            else "none"
        )
    if method not in {"none", "client_secret_post", "client_secret_basic"}:
        raise ValueError("The client sign-in method is not supported")
    if method != "none" and not client_secret:
        raise ValueError("This OAuth client needs a secret")
    return method


async def token_request(http, info, data, client_secret=None):
    public_address(info["token_endpoint"])
    method = token_auth_method(info, client_secret)
    data = dict(data)
    options = {}
    if method == "client_secret_post":
        data["client_secret"] = client_secret
    elif method == "client_secret_basic":
        options["auth"] = httpx.BasicAuth(quote_plus(data["client_id"]), quote_plus(client_secret))
    return await http.request("POST", info["token_endpoint"], data=data, **options)
