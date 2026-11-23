"""Read local fake pages while production DNS policy remains closed."""

import asyncio
import socket

import httpx
import pytest

from illo.tools import web


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.1",
        "172.16.0.1",
        "192.168.0.1",
        "169.254.169.254",
        "::1",
        "fc00::1",
        "::ffff:8.8.8.8",
        "0.0.0.0",
    ],
)
async def test_private_and_metadata_addresses_refused(monkeypatch, address):
    async def resolve(*args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
    with pytest.raises(ValueError, match="not public"):
        await web.resolve_public("public.example", 443)


async def test_mixed_dns_refused(monkeypatch):
    async def resolve(*args, **kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443))
            for ip in ["8.8.8.8", "127.0.0.1"]
        ]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
    with pytest.raises(ValueError):
        await web.resolve_public("public.example", 443)


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://public.example",
        "https://user:password@public.example",
        "https://public.example\\@localhost/",
        "https://public.example/\nsecret",
        "https://@public.example/",
    ],
)
def test_url_restrictions(url):
    with pytest.raises((ValueError, httpx.InvalidURL)):
        web.normalize_url(url)


@pytest.fixture
async def pages(monkeypatch):
    connections = []
    tasks = set()

    async def serve(reader, writer):
        task = asyncio.current_task()
        tasks.add(task)
        try:
            request = await reader.readuntil(b"\r\n\r\n")
            connections.append(request)
            path = request.split(b" ")[1]
            headers = b"Content-Type: text/html\r\n"
            body = b"<title>A page</title><article>Hello<script>bad()</script><p>Main text</p></article>"
            status = b"200 OK"
            if path == b"/private":
                status, headers, body = b"302 Found", b"Location: http://127.0.0.1/secret\r\n", b""
            elif path == b"/redirect":
                status, headers, body = b"302 Found", b"Location: /page\r\n", b""
            elif path == b"/loop":
                status, headers, body = b"302 Found", b"Location: /loop\r\n", b""
            elif path == b"/binary":
                headers = b"Content-Type: image/png\r\n"
            elif path == b"/big":
                body = b"x" * (web.MAX_BYTES + 1)
            elif path == b"/big-stream":
                body = b"x" * (web.MAX_BYTES + 1)
                headers += b"Connection: close\r\n"
            elif path == b"/long":
                body = b"x" * 20000
                headers = b"Content-Type: text/plain\r\n"
            elif path == b"/slow":
                await asyncio.Event().wait()
            if path != b"/big-stream":
                headers += f"Content-Length: {len(body)}\r\n".encode()
            writer.write(b"HTTP/1.1 " + status + b"\r\n" + headers + b"\r\n" + body)
            await writer.drain()
        finally:
            writer.close()
            tasks.discard(task)

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    original = web.resolve_public

    async def test_resolver(host, port):
        if host == "public.example":
            return ["127.0.0.1"]  # Local test server only.
        return await original(host, port)

    monkeypatch.setattr(web, "resolve_public", test_resolver)
    try:
        yield f"http://public.example:{port}", connections
    finally:
        server.close()
        pending = list(tasks)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        await server.wait_closed()


async def test_title_text_outside_block_and_pinned_host(pages):
    url, connections = pages
    text = await web.fetch_page(web.normalize_url(url + "/page", True), allow_http=True)
    assert "A page" in text and "Main text" in text and "bad()" not in text
    assert text.startswith('<outside_data source="web">\n')
    assert b"Host: public.example:" in connections[0]
    assert web.USER_AGENT.encode() in connections[0]


@pytest.mark.parametrize(
    ("path", "message"),
    [
        ("/private", "not public"),
        ("/binary", "not text"),
        ("/big", "too large"),
        ("/big-stream", "too large"),
        ("/loop", "too many redirects"),
    ],
)
async def test_redirect_type_size_limits(pages, path, message):
    url, connections = pages
    with pytest.raises(ValueError, match=message):
        await web.fetch_page(web.normalize_url(url + path, True), allow_http=True)
    if path == "/private":
        assert len(connections) == 1
    if path == "/loop":
        assert len(connections) == 4


async def test_public_redirect_and_text_limit(pages):
    url, _ = pages
    assert "A page" in await web.fetch_page(
        web.normalize_url(url + "/redirect", True), allow_http=True
    )
    text = await web.fetch_page(web.normalize_url(url + "/long", True), allow_http=True)
    assert text.count("x") == web.MAX_TEXT


async def test_total_timeout(pages, monkeypatch):
    url, _ = pages
    timeout = asyncio.timeout
    monkeypatch.setattr(web.asyncio, "timeout", lambda _: timeout(0.01))
    with pytest.raises(TimeoutError):
        await web.fetch_page(web.normalize_url(url + "/slow", True), allow_http=True)


def test_url_is_exact_or_host_root():
    text = "Read https://EXAMPLE.com:443/article?query=one#section please."
    assert web.allowed_url(web.normalize_url("https://example.com/article?query=one"), text, False)
    assert web.allowed_url(web.normalize_url("https://example.com/"), text, False)
    assert not web.allowed_url(web.normalize_url("https://example.com/?secret=team"), text, False)
    assert not web.allowed_url(
        web.normalize_url("https://example.com/article?query=two"), text, False
    )
    assert not web.allowed_url(web.normalize_url("https://another.example/"), text, False)
