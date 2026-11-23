"""OAuth discovery and token requests cannot reach a private address."""

import asyncio
import socket
from unittest.mock import AsyncMock

import httpx
import pytest

from illo.connectors.addresses import PublicTransport
from illo.connectors.config import Connector
from illo.connectors.discovery import metadata
from illo.connectors.http import BoundedHTTP


def config():
    return Connector(key="acme-docs", kind="mcp", scope="team", url="https://mcp.example.com/mcp")


@pytest.mark.parametrize(
    "address",
    [
        "http://auth.example.com/token",
        "https://user:pass@auth.example.com/token",
        "https://127.0.0.1/token",
        "https://169.254.169.254/token",
        "https://10.0.0.1/token",
        "https://[::1]/token",
        "https://localhost/token",
        "https://[::ffff:127.0.0.1]/token",
        "https://auth.example.com/\\token",
        "https://auth.example.com/\ntoken",
    ],
)
async def test_each_fetched_address_must_be_public_https(address):
    seen = []
    http = BoundedHTTP(
        httpx.MockTransport(lambda r: seen.append(r) or httpx.Response(200)), public=True
    )
    with pytest.raises(ValueError):
        await http.request("POST", address, data={"code": "test-code"})
    assert not seen


@pytest.mark.parametrize(
    "addresses",
    [
        ["127.0.0.1"],
        ["93.184.216.34", "10.0.0.1"],
        ["169.254.169.254"],
        ["::1"],
        ["64:ff9b::7f00:1"],
        ["ff02::1"],
        ["100.100.100.200"],
    ],
)
async def test_public_dns_check_rejects_all_private_and_mixed_answers(monkeypatch, addresses):
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(
        loop,
        "getaddrinfo",
        AsyncMock(
            return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443)) for ip in addresses
            ]
        ),
    )
    seen = []
    http = BoundedHTTP(
        httpx.MockTransport(lambda r: seen.append(r) or httpx.Response(200)), public=True
    )
    with pytest.raises(ValueError, match="not public"):
        await http.request("GET", "https://mcp.example.com/metadata")
    assert not seen


async def test_requests_pin_the_checked_ip_and_keep_host_and_tls_name(monkeypatch):
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(
        loop,
        "getaddrinfo",
        AsyncMock(
            return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
        ),
    )
    seen = []
    async with httpx.AsyncClient(
        transport=PublicTransport(
            httpx.MockTransport(lambda r: seen.append(r) or httpx.Response(200))
        )
    ) as client:
        await client.post("https://auth.example.com/token", data={"code": "test-code"})
    assert seen[0].url.host == "93.184.216.34"
    assert seen[0].headers["host"] == "auth.example.com"
    assert seen[0].extensions["sni_hostname"] == "auth.example.com"


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
async def test_redirects_never_forward_a_code_or_secret(monkeypatch, status):
    monkeypatch.setattr(
        "illo.connectors.addresses.resolve_public", AsyncMock(return_value=["93.184.216.34"])
    )
    seen = []
    http = BoundedHTTP(
        httpx.MockTransport(
            lambda r: (
                seen.append(r)
                or httpx.Response(status, headers={"location": "https://127.0.0.1/token"})
            )
        ),
        public=True,
    )
    with pytest.raises(ValueError, match="redirect"):
        await http.request("POST", "https://auth.example.com/token", data={"code": "test-code"})
    assert len(seen) == 1


@pytest.mark.parametrize(
    "fault", ["header", "issuer", "token", "register", "issuer_mismatch", "resource_mismatch"]
)
async def test_untrusted_metadata_cannot_change_address_boundaries_or_issuer(monkeypatch, fault):
    monkeypatch.setattr(
        "illo.connectors.addresses.resolve_public", AsyncMock(return_value=["93.184.216.34"])
    )
    seen = []

    def respond(request):
        seen.append(request)
        path = request.url.path
        if path == "/mcp":
            url = (
                "https://127.0.0.1/metadata"
                if fault == "header"
                else "https://mcp.example.com/resource"
            )
            return httpx.Response(
                401, headers={"WWW-Authenticate": f'Bearer resource_metadata="{url}"'}
            )
        if path == "/resource":
            return httpx.Response(
                200,
                json={
                    "resource": "https://other.example.com/mcp"
                    if fault == "resource_mismatch"
                    else config().url,
                    "authorization_servers": [
                        "http://unsafe.example.com"
                        if fault == "issuer"
                        else "https://auth.example.com/tenant"
                    ],
                },
            )
        assert path == "/.well-known/oauth-authorization-server/tenant"
        return httpx.Response(
            200,
            json={
                "issuer": "https://other.example.com"
                if fault == "issuer_mismatch"
                else "https://auth.example.com/tenant",
                "authorization_endpoint": "https://auth.example.com/authorize",
                "token_endpoint": "https://169.254.169.254/token"
                if fault == "token"
                else "https://auth.example.com/token",
                "registration_endpoint": "http://auth.example.com/register"
                if fault == "register"
                else "https://auth.example.com/register",
            },
        )

    with pytest.raises(ValueError):
        await metadata(BoundedHTTP(httpx.MockTransport(respond), public=True), config(), probe=True)
    assert not any(r.url.path in {"/token", "/register", "/metadata"} for r in seen)


@pytest.mark.parametrize("form", ["inserted", "appended", "openid"])
async def test_metadata_path_fallbacks_and_resource_root_fallback(form):
    seen = []
    paths = {
        "inserted": "/.well-known/oauth-authorization-server/tenant",
        "appended": "/tenant/.well-known/oauth-authorization-server",
        "openid": "/tenant/.well-known/openid-configuration",
    }

    def respond(request):
        path = request.url.path
        seen.append(path)
        if path in {"/mcp", "/.well-known/oauth-protected-resource/mcp"}:
            return httpx.Response(401 if path == "/mcp" else 404)
        if path == "/.well-known/oauth-protected-resource":
            return httpx.Response(
                200,
                json={
                    "resource": config().url,
                    "authorization_servers": ["https://auth.example.com/tenant"],
                },
            )
        if path == paths[form]:
            return httpx.Response(
                200,
                json={
                    "issuer": "https://auth.example.com/tenant",
                    "authorization_endpoint": "https://auth.example.com/authorize",
                    "token_endpoint": "https://auth.example.com/token",
                    "tools": {"delete_all": {"risk": "read"}},
                },
            )
        return httpx.Response(404)

    cfg = config()
    info = await metadata(BoundedHTTP(httpx.MockTransport(respond)), cfg, probe=True)
    assert info["resource"] == cfg.url
    assert seen[3] == "/.well-known/oauth-authorization-server/tenant"
    assert seen[-1] == paths[form]
    assert cfg.tools == [] and cfg.auth is None


async def test_oauth_responses_have_wire_and_time_limits(monkeypatch):
    http = BoundedHTTP(
        httpx.MockTransport(lambda r: httpx.Response(200, json={"text": "x" * 2048}))
    )
    with pytest.raises(ValueError, match="too large"):
        await http.request("GET", "https://auth.example.com/metadata", max_bytes=1024)

    class SlowHTTP:
        async def request(self, *args, **kwargs):
            await asyncio.sleep(1)

    cfg = config().model_copy(update={"timeout_s": 0.01})
    with pytest.raises(TimeoutError):
        await metadata(SlowHTTP(), cfg, probe=True)


async def test_endpoint_queries_cannot_replace_oauth_security_parameters():
    from urllib.parse import parse_qs, urlsplit

    from illo.connectors.config import Auth
    from illo.connectors.discovery import authorization_url

    cfg = config()
    cfg.auth = Auth(
        type="oauth",
        client_id="acme-illo",
        redirect_uri="https://illo.example.com/oauth/mcp/callback",
    )
    url = authorization_url(
        {
            "authorization_endpoint": "https://auth.example.com/authorize?tenant=acme&state=wrong&redirect_uri=https://evil.example.com",
            "resource": cfg.url,
        },
        cfg,
        "one-time-state",
        "private-verifier",
    )
    params = parse_qs(urlsplit(url).query)
    assert params["tenant"] == ["acme"]
    assert params["state"] == ["one-time-state"]
    assert params["redirect_uri"] == [cfg.auth.redirect_uri]
    assert params["resource"] == [cfg.url]
    assert "private-verifier" not in url
