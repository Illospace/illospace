"""Public fetches pin DNS, cap bytes, and keep page text inside data markers."""

import asyncio
import socket

import httpx
import pytest

from illo.deep import web
from illo.tools.web import resolve_public


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "::1",
        "10.1.2.3",
        "192.168.1.2",
        "169.254.1.1",
        "169.254.169.254",
        "100.64.0.1",
        "224.0.0.1",
        "fd00::1",
        "fe80::1",
        "::ffff:127.0.0.1",
        "64:ff9b::7f00:1",
    ],
)
async def test_non_public_addresses_fail(monkeypatch, address):
    async def dns(*args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", dns)
    with pytest.raises(ValueError, match="not public"):
        await resolve_public("example.com", 443)


async def test_mixed_dns_fails(monkeypatch):
    async def dns(*args, **kwargs):
        return [(2, 1, 6, "", (ip, 443)) for ip in ["93.184.216.34", "10.0.0.1"]]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", dns)
    with pytest.raises(ValueError, match="not public"):
        await resolve_public("example.com", 443)


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com/a",
        "https://name:pass@example.com",
        "http://169.254.169.254/a",
        "http://brain/a",
        "http://sandbox/a",
        "http://gateway/a",
        "http://metadata.google.internal/a",
        "https://example.com\\@127.0.0.1/a",
    ],
)
async def test_refused_urls(url):
    with pytest.raises((ValueError, httpx.InvalidURL)):
        await web.fetch(url)


async def public(*args):
    return ["93.184.216.34"]


async def test_pin_links_markers_and_no_cookies(monkeypatch):
    monkeypatch.setattr(web, "resolve_public", public)
    calls = []

    def respond(request):
        calls.append(request)
        assert request.url.host == "93.184.216.34"
        assert request.headers["host"] == "example.com"
        assert request.extensions["sni_hostname"] == "example.com"
        assert "cookie" not in request.headers
        if len(calls) == 1:
            return httpx.Response(302, headers={"location": "/page", "set-cookie": "secret=value"})
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text='<script>secret</script><style>hide</style><p>Read <a href="/proof">the proof</a>.</p>',
        )

    result = await web.fetch("https://example.com", transport=httpx.MockTransport(respond))
    assert 'source="web"' in result and result.endswith("</outside_data>")
    assert "the proof (https://example.com/proof)" in result
    assert "secret" not in result and "hide" not in result
    assert len(calls) == 2


async def test_redirect_is_checked_again(monkeypatch):
    hosts = []

    async def dns(host, port):
        hosts.append(host)
        if host == "127.0.0.1":
            raise ValueError("The host is not public")
        return ["93.184.216.34"]

    monkeypatch.setattr(web, "resolve_public", dns)
    transport = httpx.MockTransport(
        lambda _: httpx.Response(302, headers={"location": "http://127.0.0.1/private"})
    )
    with pytest.raises(ValueError):
        await web.fetch("https://example.com", transport=transport)
    assert hosts == ["example.com", "127.0.0.1"]


@pytest.mark.parametrize("announced", [True, False])
async def test_body_cap(monkeypatch, announced):
    monkeypatch.setattr(web, "resolve_public", public)
    headers = {"content-type": "text/plain"}
    if announced:
        headers["content-length"] = str(web.MAX_BYTES + 1)
    body = b"x" * (web.MAX_BYTES + 1)
    transport = httpx.MockTransport(lambda _: httpx.Response(200, headers=headers, content=body))
    with pytest.raises(ValueError, match="too large"):
        await web.fetch("https://example.com", transport=transport)


async def test_text_cap_and_redirect_limit(monkeypatch):
    monkeypatch.setattr(web, "resolve_public", public)
    transport = httpx.MockTransport(
        lambda _: httpx.Response(
            200, headers={"content-type": "application/json"}, text="x" * 20001
        )
    )
    text = await web.fetch("https://example.com", transport=transport)
    assert "Page text cut at 20,000 characters" in text
    transport = httpx.MockTransport(lambda _: httpx.Response(302, headers={"location": "/again"}))
    with pytest.raises(ValueError, match="too many redirects"):
        await web.fetch("https://example.com", transport=transport)
